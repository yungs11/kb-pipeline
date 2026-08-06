"""JobRepo — 실 Postgres 라운드트립. `KBP_PG_DSN` 없으면 skip.

claim 의 상한·펜싱·취소는 SQL 이 하는 일이라 fake 로 검증할 수 없다. 여기서만 잡힌다.
실제로 첫 라이브 실행에서 `coalesce(workspace_key, '\\x00anon')` 이 파이썬 소스에서 진짜
NUL 바이트가 되어 SQL 문법 오류를 냈다 — 순수 단위테스트로는 안 잡히는 종류다.

실행:  KBP_PG_DSN=postgres://edgequake:edgequake_secret@localhost:5433/edgequake pytest
"""
from __future__ import annotations

import datetime as dt
import os
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")

from service.jobs.repo import JobRepo, LeaseLost  # noqa: E402
from service.jobs.schema import ensure_schema  # noqa: E402

pytestmark = pytest.mark.requires_pg

DSN = os.environ.get("KBP_PG_DSN")

if not DSN:
    pytest.skip("KBP_PG_DSN unset", allow_module_level=True)


def _live_worker_present() -> bool:
    """살아있는 facade-worker 가 이 큐를 물고 있는가."""
    try:
        with psycopg.connect(DSN, connect_timeout=3) as conn:
            row = conn.execute(
                "SELECT count(*) FROM kbp.job_workers"
                " WHERE heartbeat_at >= now() - interval '60 seconds'"
            ).fetchone()
        return bool(row and row[0])
    except psycopg.Error:
        return False   # 스키마 없음 = 아직 아무도 안 씀


if _live_worker_present():
    # 이 테스트들은 큐를 **단독으로** 써야 한다. 실제 worker 가 떠 있으면 2초마다
    # claim 이 돌아 queued 잡을 채가므로, "제출 직후 queued 상태" 를 전제하는 단언들이
    # 무작위로 깨진다(실제로 test_cancel_queued_is_immediate 가 그렇게 깨졌다).
    # 조용한 flaky 로 두는 것보다 이유를 밝히고 건너뛰는 게 낫다.
    pytest.skip(
        "live facade-worker is consuming this queue — stop it to run these tests "
        "(pgrep -f -- '-m service\\.worker' | xargs kill)",
        allow_module_level=True,
    )


def _truncate():
    with psycopg.connect(DSN) as conn:
        conn.execute("DELETE FROM kbp.jobs")
        conn.execute("DELETE FROM kbp.job_workers")
        conn.commit()


@pytest.fixture()
def repo():
    """앞뒤로 비운다.

    뒤도 비우는 이유: 이 DSN 은 dev 스택이 공유하는 실 DB 다. 테스트가 남긴
    `pytest:*` worker 행과 running 잡이 그대로 남으면 `GET /jobs/workers` 가 없는
    worker 를 online 으로 보고하고 슬롯을 점유한 것처럼 보인다(실제로 그랬다).
    """
    ensure_schema(DSN)
    _truncate()
    yield JobRepo(DSN)
    _truncate()


@pytest.fixture()
def worker(repo):
    wid = f"pytest:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    repo.mark_worker(worker_id=wid, capacity=4, active_count=0,
                     started_at=dt.datetime.now(dt.timezone.utc))
    return wid


def test_ensure_schema_is_idempotent():
    ensure_schema(DSN)
    ensure_schema(DSN)  # 두 번째 호출이 예외 없이 no-op


def test_claim_respects_bucket_limit(repo, worker):
    """parse 상한 4 — 6건을 넣어도 4건만 running 이 된다."""
    for i in range(6):
        repo.submit(kind="parse", payload={"n": i})
    assert len(repo.claim(worker_id=worker, local_free=10)) == 4
    assert repo.claim(worker_id=worker, local_free=10) == []  # 포화


def test_completing_a_job_frees_its_slot(repo, worker):
    for i in range(6):
        repo.submit(kind="parse", payload={"n": i})
    claimed = repo.claim(worker_id=worker, local_free=10)
    repo.complete(claimed[0].id, worker_id=worker, attempt=claimed[0].attempt,
                  status="succeeded", result={"ok": True})
    assert repo.get(claimed[0].id)["status"] == "succeeded"
    assert len(repo.claim(worker_id=worker, local_free=10)) == 1


def test_local_free_caps_claim(repo, worker):
    for i in range(6):
        repo.submit(kind="parse")
    assert len(repo.claim(worker_id=worker, local_free=2)) == 2


def test_null_workspace_is_not_capped_per_tenant(repo, worker):
    """workspace 없는 잡(현행 /parse·/chunk)이 per-workspace 2 에 묶이면 안 된다.

    묶이면 처리량이 현행보다 나빠진다 — 유량제어가 아니라 처리량 파괴다.
    """
    for i in range(4):
        repo.submit(kind="parse")
    assert len(repo.claim(worker_id=worker, local_free=10)) == 4


def test_named_workspace_is_capped(repo, worker):
    for i in range(4):
        repo.submit(kind="parse", workspace_key="ws-a")
    assert len(repo.claim(worker_id=worker, local_free=10)) == 2


def test_foreign_worker_write_is_fenced(repo, worker):
    """lease 를 잃은 좀비의 쓰기는 거부된다 — 결과 덮어쓰기 방지(§3.3)."""
    repo.submit(kind="parse")
    claimed = repo.claim(worker_id=worker, local_free=1)
    with pytest.raises(LeaseLost):
        repo.complete(claimed[0].id, worker_id="zombie:9:dead",
                      attempt=claimed[0].attempt, status="succeeded", result={"x": 1})
    assert repo.get(claimed[0].id)["status"] == "running"  # 덮어써지지 않았다


def test_stage_write_is_fenced(repo, worker):
    repo.submit(kind="insert", workspace_key="ws-a")
    claimed = repo.claim(worker_id=worker, local_free=1)
    repo.set_stage(claimed[0].id, worker_id=worker, attempt=claimed[0].attempt,
                   stage="inserting")
    assert repo.get(claimed[0].id)["stage"] == "inserting"
    with pytest.raises(LeaseLost):
        repo.set_stage(claimed[0].id, worker_id="zombie:9:dead",
                       attempt=claimed[0].attempt, stage="parsing")


def test_cancel_queued_is_immediate(repo):
    job_id = repo.submit(kind="parse")
    assert repo.cancel(job_id)["status"] == "canceled"   # D6: dict 계약
    assert repo.get(job_id)["status"] == "canceled"


def test_cancel_running_only_flags(repo, worker):
    repo.submit(kind="parse")
    claimed = repo.claim(worker_id=worker, local_free=1)
    assert repo.cancel(claimed[0].id)["status"] == "running"
    assert repo.is_cancel_requested(claimed[0].id)
    assert repo.get(claimed[0].id)["status"] == "running"  # 아직 실행 중


def test_cancel_unknown_returns_none(repo):
    assert repo.cancel(uuid.uuid4()) is None


def test_cancel_terminal_returns_none(repo, worker):
    repo.submit(kind="parse")
    claimed = repo.claim(worker_id=worker, local_free=1)
    repo.complete(claimed[0].id, worker_id=worker, attempt=claimed[0].attempt,
                  status="succeeded", result={})
    assert repo.cancel(claimed[0].id) is None  # terminal 에는 플래그를 찍지 않는다


def test_cancelled_queued_job_is_finished_by_maintenance(repo, worker):
    """running 중 취소된 잡이 회수로 queued 가 되어도 좀비로 남지 않는다."""
    job_id = repo.submit(kind="parse")
    claimed = repo.claim(worker_id=worker, local_free=1)
    repo.cancel(claimed[0].id)
    repo.requeue(claimed[0].id, worker_id=worker, attempt=claimed[0].attempt,
                 error="transient")
    repo.claim(worker_id=worker, local_free=1)  # 유지보수 (1b) 가 돈다
    assert repo.get(job_id)["status"] == "canceled"


def test_admission_does_not_resurrect_cancelled(repo, worker):
    """후보 조회 이후 취소된 잡을 승인 UPDATE 가 되살리지 않는다."""
    job_id = repo.submit(kind="parse")
    repo.cancel(job_id)
    assert repo.claim(worker_id=worker, local_free=10) == []
    assert repo.get(job_id)["status"] == "canceled"


def test_exhausted_attempts_are_not_reclaimed(repo, worker):
    """insert 는 max_attempts=1 — 한 번 requeue 되면 다시 집히지 않는다.

    가드가 없으면 재적재 루프가 된다(edgequake 에 멱등키가 없으므로).

    D13 이후로는 ``queued`` 로 남지도 않는다. 되돌리면 후보 조회가 그 행을 영구 배제해
    좀비가 되므로 requeue 가 곧장 ``failed`` 로 끝낸다 — "다시 안 집힌다" 는 원래 의도는
    더 강하게 지켜진다.
    """
    repo.submit(kind="insert", workspace_key="ws-a")
    claimed = repo.claim(worker_id=worker, local_free=1)
    assert repo.get(claimed[0].id)["attempt_count"] == 1
    repo.requeue(claimed[0].id, worker_id=worker, attempt=claimed[0].attempt,
                 error="boom")
    assert repo.get(claimed[0].id)["status"] == "failed"
    assert repo.claim(worker_id=worker, local_free=10) == []


def test_worker_stats_uses_kb_compatible_keys(repo, worker):
    """kb 의 worker_capacity() 와 동일 키 — 마지막 키는 running 이 아니라 processing."""
    repo.submit(kind="parse")
    stats = repo.worker_stats()
    assert set(stats) >= {"online", "capacity", "active", "available",
                          "queued", "processing"}
    assert stats["online"] is True
    assert stats["queued"] == 1
    assert stats["oldest_queued_age_seconds"] >= 0


def test_live_worker_count_is_zero_without_heartbeat(repo):
    """worker 가 없으면 접수를 거절해야 한다(§4.4 fail-fast)."""
    assert repo.live_worker_count() == 0


def test_ingest_occupies_all_three_buckets(repo, worker):
    """running ingest 2건이면 chunk 잡이 승인되지 않는다."""
    for i in range(3):
        repo.submit(kind="ingest", workspace_key=f"ws-{i}")
    assert len(repo.claim(worker_id=worker, local_free=10)) == 2  # min(4,2,2)
    repo.submit(kind="chunk", workspace_key="ws-z")
    assert repo.claim(worker_id=worker, local_free=10) == []


def test_kind_head_of_line_is_avoided(repo, worker):
    """앞선 후보가 전부 chunk 로 막혀도 같은 틱에 parse 가 승인된다."""
    for i in range(4):
        repo.submit(kind="chunk", workspace_key=f"cw-{i}")
    parse_id = repo.submit(kind="parse")
    claimed = repo.claim(worker_id=worker, local_free=10)
    assert parse_id in {c.id for c in claimed}


def test_ahead_in_partition(repo):
    first = repo.submit(kind="parse", workspace_key="ws-a")
    second = repo.submit(kind="parse", workspace_key="ws-a")
    assert repo.ahead_in_partition(repo.get(first)) == 0
    assert repo.ahead_in_partition(repo.get(second)) == 1


def test_legacy_refs_to_purge_covers_payload_ref(repo):
    """무인증 legacy /chunk 는 input_ref 가 없고 payload_ref 로 MinIO 에 올라간다."""
    job_id = repo.submit(kind="chunk", legacy=True, payload_ref="kbp-jobs/x/payload.json")
    assert repo.legacy_refs_to_purge(job_id) == (None, "kbp-jobs/x/payload.json")
    non_legacy = repo.submit(kind="chunk", payload_ref="kbp-jobs/y/payload.json")
    assert repo.legacy_refs_to_purge(non_legacy) == (None, None)


# ── 세대 토큰(attempt_count) 펜싱 ──────────────────────────────────────────
#
# claimed_by 만으로는 부족하다. worker 는 배포상 1개이고 worker_id 는 프로세스 수명
# 동안 고정이라, 회수된 잡을 **같은 worker** 가 다시 집으면 옛 스레드의 쓰기가
# `claimed_by=$me AND status='running'` 을 그대로 통과한다.


def test_same_worker_regeneration_fences_stale_complete(repo, worker):
    """attempt 1 스레드의 complete 가 attempt 2 를 종결시키지 못한다.

    막지 못하면: attempt 1 결과로 잡이 succeeded 가 되고, attempt 2 스레드는 계속
    달려 edgequake 에 문서를 한 번 더 제출한다(중복 적재).
    """
    repo.submit(kind="parse")
    gen1 = repo.claim(worker_id=worker, local_free=1)[0]
    assert gen1.attempt == 1

    # 회수 → 같은 worker 가 재claim (worker_id 동일, attempt 만 증가)
    repo.requeue(gen1.id, worker_id=worker, attempt=gen1.attempt, error="transient")
    gen2 = repo.claim(worker_id=worker, local_free=1)[0]
    assert gen2.id == gen1.id and gen2.attempt == 2

    with pytest.raises(LeaseLost):
        repo.complete(gen1.id, worker_id=worker, attempt=gen1.attempt,
                      status="succeeded", result={"stale": True})
    row = repo.get(gen1.id)
    assert row["status"] == "running"          # 좀비가 종결시키지 못했다
    assert row["attempt_count"] == 2

    # 현 세대의 쓰기는 통과한다
    repo.complete(gen2.id, worker_id=worker, attempt=gen2.attempt,
                  status="succeeded", result={"fresh": True})
    assert repo.get(gen1.id)["result"] == {"fresh": True}


def test_same_worker_regeneration_fences_stale_stage(repo, worker):
    """부작용 **직전** 쓰기가 막힌다 — runner 는 여기서 다운스트림 호출을 포기해야 한다."""
    repo.submit(kind="parse")
    gen1 = repo.claim(worker_id=worker, local_free=1)[0]
    repo.requeue(gen1.id, worker_id=worker, attempt=gen1.attempt, error="transient")
    repo.claim(worker_id=worker, local_free=1)
    with pytest.raises(LeaseLost):
        repo.set_stage(gen1.id, worker_id=worker, attempt=gen1.attempt, stage="inserting")


def test_heartbeat_ignores_stale_generation(repo, worker):
    """옛 세대의 heartbeat 가 현 세대 lease 를 연장하면 안 된다."""
    repo.submit(kind="parse")
    gen1 = repo.claim(worker_id=worker, local_free=1)[0]
    repo.requeue(gen1.id, worker_id=worker, attempt=gen1.attempt, error="transient")
    gen2 = repo.claim(worker_id=worker, local_free=1)[0]

    before = repo.get(gen2.id)["heartbeat_at"]
    repo.heartbeat(worker_id=worker, leases=[(gen1.id, gen1.attempt)])  # 옛 세대
    assert repo.get(gen2.id)["heartbeat_at"] == before

    repo.heartbeat(worker_id=worker, leases=[(gen2.id, gen2.attempt)])  # 현 세대
    assert repo.get(gen2.id)["heartbeat_at"] > before


# ── 제출 멱등키 (D1) ───────────────────────────────────────────────────────
#
# 소비자(kb)는 429/5xx 를 최대 3회 재시도한다. 제출 경로에서 그건 잡 중복 생성이고,
# insert/ingest 에서는 곧 edgequake 중복 적재다.


def test_same_idem_key_returns_existing_job(repo):
    a = repo.submit(kind="parse", idem_key="k1")
    b = repo.submit(kind="parse", idem_key="k1")
    assert a == b
    assert len(repo.list_jobs()) == 1


def test_different_idem_keys_create_separate_jobs(repo):
    a = repo.submit(kind="parse", idem_key="k1")
    b = repo.submit(kind="parse", idem_key="k2")
    assert a != b


def test_no_idem_key_never_collides(repo):
    """키가 없으면 멱등 보장도 없다 — 매번 새 잡(현행 동작)."""
    a = repo.submit(kind="parse")
    b = repo.submit(kind="parse")
    assert a != b


def test_failed_job_releases_idem_key(repo, worker):
    """설정을 고치고 같은 파일을 다시 올리면 새 잡이 만들어져야 한다.

    실패를 캐시하면 옛 failed job_id 가 계속 반환되어 영구 실패로 굳는다.
    """
    first = repo.submit(kind="parse", idem_key="k1")
    claimed = repo.claim(worker_id=worker, local_free=1)[0]
    repo.complete(claimed.id, worker_id=worker, attempt=claimed.attempt,
                  status="failed", error="boom")
    assert repo.get(first)["idem_key"] is None
    second = repo.submit(kind="parse", idem_key="k1")
    assert second != first


def test_succeeded_job_keeps_idem_key(repo, worker):
    """성공은 캐시한다 — 재시도가 같은 결과를 받는다."""
    first = repo.submit(kind="parse", idem_key="k1")
    claimed = repo.claim(worker_id=worker, local_free=1)[0]
    repo.complete(claimed.id, worker_id=worker, attempt=claimed.attempt,
                  status="succeeded", result={"ok": True})
    assert repo.submit(kind="parse", idem_key="k1") == first


def test_succeeded_domain_failure_releases_idem_key(repo, worker):
    """job 은 succeeded 지만 본문이 도메인 실패(parse-svc {"status":"failed"})면
    clear_idem=True 로 idem_key 를 비운다(2026-08-06).

    실관측: html 문서 하나가 이 상태로 굳어(job succeeded, enriched_content 없음)
    명시적 idem_key(시간창 없음)가 영구 캐싱돼, 근본 원인(변환/파싱 실패)을 고친
    뒤에도 재시도가 계속 같은 빈 결과를 받았다.
    """
    first = repo.submit(kind="parse", idem_key="k1")
    claimed = repo.claim(worker_id=worker, local_free=1)[0]
    repo.complete(claimed.id, worker_id=worker, attempt=claimed.attempt,
                  status="succeeded", result={"status": "failed", "detail": "parse_failed"},
                  clear_idem=True)
    row = repo.get(first)
    assert row["status"] == "succeeded"  # 계약 불변 — 도메인 실패는 잡 실패가 아니다
    assert row["idem_key"] is None
    second = repo.submit(kind="parse", idem_key="k1")
    assert second != first


def test_cancelled_job_releases_idem_key(repo):
    first = repo.submit(kind="parse", idem_key="k1")
    repo.cancel(first)
    assert repo.submit(kind="parse", idem_key="k1") != first


def test_recovered_exhausted_job_releases_idem_key(repo, worker):
    """회수가 attempt 소진으로 failed 종결할 때도 키를 놓아야 한다."""
    first = repo.submit(kind="insert", workspace_key="ws-a", idem_key="k1")
    claimed = repo.claim(worker_id=worker, local_free=1)[0]
    # heartbeat 를 과거로 돌려 stale 로 만든다 (insert 는 max_attempts=1)
    with psycopg.connect(DSN) as conn:
        conn.execute("UPDATE kbp.jobs SET heartbeat_at = now() - interval '1 hour' "
                     "WHERE id = %s", (claimed.id,))
        conn.commit()
    repo.claim(worker_id=worker, local_free=1)   # 회수 틱
    row = repo.get(first)
    assert row["status"] == "failed"
    assert row["idem_key"] is None


# ── advisory lock 격리 (edgequake 와 같은 DB 를 쓰므로) ────────────────────
#
# advisory lock 은 **DB 단위**다 — 스키마로 안 갈린다(pg_locks 에 schema 컬럼이 없다).
# 이 DB 는 edgequake 본체와 공유하고, edgequake 의 sqlx 마이그레이션은 1-인자 bigint
# 형식을 쓴다. Postgres 는 1-인자와 2-인자 lock 공간을 분리해 관리하므로, 우리가
# 2-인자 (classid, objid) 를 쓰면 값이 겹쳐도 구조적으로 충돌하지 않는다.


def test_claim_uses_two_arg_advisory_lock(repo):
    """claim 이 실제로 (LOCK_CLASSID, LOCK_OBJ_CLAIM) 을 잡는다."""
    import threading

    from service.jobs import schema as sch

    holder = psycopg.connect(DSN, autocommit=True)
    holder.execute("SELECT pg_advisory_lock(%s, %s)",
                   (sch.LOCK_CLASSID, sch.LOCK_OBJ_CLAIM))
    done = threading.Event()
    threading.Thread(
        target=lambda: (repo.claim(worker_id="locktest:1:a", local_free=1), done.set()),
        daemon=True,
    ).start()
    try:
        assert not done.wait(2.0), "claim 이 우리 advisory lock 을 안 잡는다"
    finally:
        holder.execute("SELECT pg_advisory_unlock_all()")
        done.wait(5)
        holder.close()


def test_claim_is_isolated_from_bigint_lock_space(repo):
    """동일 비트의 1-인자 bigint lock 을 누가 쥐어도 claim 은 막히지 않는다.

    edgequake(sqlx)가 bigint 형식을 쓰므로, 이 격리가 깨지면 edgequake 기동이
    facade-worker 의 claim 을 막거나 그 반대가 된다.
    """
    import threading

    from service.jobs import schema as sch

    combined = (sch.LOCK_CLASSID << 32) | sch.LOCK_OBJ_CLAIM
    holder = psycopg.connect(DSN, autocommit=True)
    holder.execute("SELECT pg_advisory_lock(%s::bigint)", (combined,))
    done = threading.Event()
    threading.Thread(
        target=lambda: (repo.claim(worker_id="locktest:2:b", local_free=1), done.set()),
        daemon=True,
    ).start()
    try:
        assert done.wait(3.0), "1-인자 bigint lock 이 claim 을 막았다 — 공간 분리 실패"
    finally:
        holder.execute("SELECT pg_advisory_unlock_all()")
        holder.close()


def test_schema_and_claim_locks_do_not_block_each_other(repo):
    """기동 DDL lock 이 운영 중 claim 을 막으면 안 된다(objid 로 갈린다)."""
    from service.jobs import schema as sch

    holder = psycopg.connect(DSN, autocommit=True)
    holder.execute("SELECT pg_advisory_lock(%s, %s)",
                   (sch.LOCK_CLASSID, sch.LOCK_OBJ_SCHEMA))
    try:
        repo.claim(worker_id="locktest:3:c", local_free=1)   # 막히지 않아야 한다
    finally:
        holder.execute("SELECT pg_advisory_unlock_all()")
        holder.close()


def test_idem_reinsert_keeps_job_id_aligned_with_object_keys(repo, worker):
    """멱등 재삽입이 행 id 와 객체 키를 어긋나게 하면 안 된다.

    submit 은 호출자가 만든 job_id 로 `{prefix}/{job_id}/...` 객체를 이미 올려둔 상태로
    불린다(api.submit_job). 멱등 충돌 뒤 키가 비워져 재삽입할 때 새 uuid 를 만들면,
    행의 input_ref/payload_ref 는 여전히 **옛 uuid** 경로를 가리킨다.

    그러면 고아 스윕이 "이 job_id 로 된 행이 없다" 고 판단해 **살아있는 잡의 입력을
    지운다**. GC 이전부터 있던 버그다.
    """
    job_id = uuid.uuid4()
    refs = {"input_ref": f"kbp-jobs/{job_id}/input.bin",
            "payload_ref": f"kbp-jobs/{job_id}/payload.json"}

    # 1) 같은 키로 먼저 하나 만들고 실패로 종결시켜 키를 비운다
    first = repo.submit(kind="parse", idem_key="k-align")
    claimed = repo.claim(worker_id=worker, local_free=1)[0]
    repo.complete(claimed.id, worker_id=worker, attempt=claimed.attempt,
                  status="failed", error="boom")
    assert repo.get(first)["idem_key"] is None

    # 2) 같은 idem_key 로 재제출 — INSERT 는 충돌하지만 기존 행이 키를 비운 상태라
    #    재삽입 경로를 탄다
    created = repo.submit(kind="parse", job_id=job_id, idem_key="k-align", **refs)

    row = repo.get(created)
    assert created == job_id, "재삽입이 호출자의 job_id 를 버렸다"
    assert str(job_id) in row["input_ref"], "행 id 와 객체 키가 어긋난다"
    assert str(job_id) in row["payload_ref"]


def test_idem_reinsert_race_path_preserves_job_id():
    """재귀 재삽입 경로가 호출자의 job_id 를 보존하는지 소스로 확인한다.

    이 경로는 "INSERT 는 충돌했는데 SELECT 시점엔 키가 없다"는 **경합에서만** 도달해서
    결정적으로 재현할 수 없다(키가 비워지면 INSERT 가 애초에 충돌하지 않는다). 그래서
    재귀 호출이 job_id 를 넘기는지를 직접 단언한다 — 이게 빠지면 행 id 와 객체 키가
    어긋나 고아 스윕이 살아있는 입력을 지운다.
    """
    import inspect

    from service.jobs.repo import JobRepo as _R

    src = inspect.getsource(_R.submit)
    body = src[src.index("if existing is None"):]
    recursive = body[body.index("return self.submit("):]
    assert "job_id=job_id" in recursive[:recursive.index(")")], (
        "재귀 재삽입이 job_id 를 안 넘긴다 — 행 id 와 객체 키가 어긋난다"
    )


# ── TTL GC (D2) ────────────────────────────────────────────────────────────

def _age(job_id, seconds):
    """completed_at·created_at 을 과거로 민다."""
    with psycopg.connect(DSN) as conn:
        conn.execute(
            "UPDATE kbp.jobs SET completed_at = now() - make_interval(secs => %s),"
            "                   created_at   = now() - make_interval(secs => %s)"
            " WHERE id = %s", (seconds, seconds, job_id))
        conn.commit()


def _terminal(repo, worker, *, kind="parse", status="succeeded", **kw):
    job_id = repo.submit(kind=kind, **kw)
    claimed = [c for c in repo.claim(worker_id=worker, local_free=10) if c.id == job_id]
    if claimed:
        repo.complete(job_id, worker_id=worker, attempt=claimed[0].attempt,
                      status=status, result={})
    return job_id


def test_purge_removes_expired_and_returns_refs(repo, worker):
    job_id = _terminal(repo, worker, input_ref="kbp-jobs/x/input.bin")
    _age(job_id, 10_000)
    rows = repo.purge_expired(ttl_seconds=3600, batch=10)
    assert [r["id"] for r in rows] == [job_id]
    assert rows[0]["input_ref"] == "kbp-jobs/x/input.bin"
    assert repo.get(job_id) is None


def test_purge_keeps_fresh_jobs(repo, worker):
    job_id = _terminal(repo, worker)
    assert repo.purge_expired(ttl_seconds=3600, batch=10) == []
    assert repo.get(job_id) is not None


def test_purge_disabled_returns_none(repo, worker):
    """TTL<=0 은 비상 정지다 — 전량 삭제 레버가 되면 안 된다."""
    job_id = _terminal(repo, worker)
    _age(job_id, 10_000)
    assert repo.purge_expired(ttl_seconds=0, batch=10) is None
    assert repo.purge_expired(ttl_seconds=-1, batch=10) is None
    assert repo.get(job_id) is not None


def test_purge_recovers_terminal_rows_with_null_completed_at(repo, worker):
    """completed_at 이 NULL 인 terminal 행도 created_at 기준으로 회수된다."""
    job_id = _terminal(repo, worker)
    with psycopg.connect(DSN) as conn:
        conn.execute("UPDATE kbp.jobs SET completed_at = NULL,"
                     " created_at = now() - interval '10 hours' WHERE id = %s", (job_id,))
        conn.commit()
    rows = repo.purge_expired(ttl_seconds=3600, batch=10)
    assert [r["id"] for r in rows] == [job_id]
    assert rows[0]["completed_at"] is None      # WARN 재료가 실려 온다


def test_purge_protects_parent_of_non_terminal_child(repo, worker):
    parent = _terminal(repo, worker)
    _age(parent, 10_000)
    repo.submit(kind="chunk", parent_job_id=parent)      # queued 자식
    assert repo.purge_expired(ttl_seconds=3600, batch=10) == []
    assert repo.get(parent) is not None


def test_purge_deletes_parent_when_child_is_terminal(repo, worker):
    parent = _terminal(repo, worker)
    child = _terminal(repo, worker, kind="chunk", parent_job_id=parent)
    _age(parent, 10_000); _age(child, 10_000)
    purged = {r["id"] for r in repo.purge_expired(ttl_seconds=3600, batch=10)}
    assert parent in purged


def test_purge_protected_rows_do_not_stall_gc(repo, worker):
    """head-of-line 방지 — 보호 대상이 배치만큼 앞에 쌓여도 뒤가 지워진다.

    NOT EXISTS 를 LIMIT 밖에 두면 보호 행(정의상 가장 오래된 축)이 정렬 앞머리를
    채워 매 사이클 0건 삭제로 GC 가 영구 정체한다.
    """
    protected = []
    for _ in range(3):
        p = _terminal(repo, worker)
        _age(p, 20_000)                      # 가장 오래됨 = 정렬 앞머리
        repo.submit(kind="chunk", parent_job_id=p)   # queued 자식으로 보호
        protected.append(p)
    victim = _terminal(repo, worker)
    _age(victim, 10_000)

    rows = repo.purge_expired(ttl_seconds=3600, batch=3)   # batch < 보호 수 + 1
    assert [r["id"] for r in rows] == [victim], "보호 행이 GC 를 정체시켰다"
    for p in protected:
        assert repo.get(p) is not None


def test_purge_respects_batch_limit(repo, worker):
    ids = []
    for _ in range(5):
        j = _terminal(repo, worker)
        _age(j, 10_000); ids.append(j)
    first = repo.purge_expired(ttl_seconds=3600, batch=2)
    assert len(first) == 2
    rest = repo.purge_expired(ttl_seconds=3600, batch=10)
    assert len(rest) == 3


def test_job_ids_present_distinguishes_empty_from_failure(repo, worker):
    job_id = _terminal(repo, worker)
    assert repo.job_ids_present([job_id]) == {job_id}
    assert repo.job_ids_present([uuid.uuid4()]) == set()      # 빈 결과
    broken = JobRepo("postgres://nobody:nobody@127.0.0.1:1/none", connect_timeout=1)
    assert broken.job_ids_present([uuid.uuid4()]) is None      # 조회 실패
    assert broken.refs_in_use(["k"]) is None


def test_refs_in_use_matches_any_ref_column(repo):
    repo.submit(kind="parse", input_ref="a", payload_ref="b")
    repo.submit(kind="chunk", payload_ref="c")
    used = repo.refs_in_use(["a", "b", "c", "zzz"])
    assert used == {"a", "b", "c"}


# ── D5: 재시도 경로도 중복 적재를 막는다 ────────────────────────────────────
#
# `_recover`(회수 경로)는 `stage='inserting'` 을 최우선 분기로 막고 있었는데, `requeue`
# (runner 예외 경로)는 stage 를 무조건 지우고 queued 로 되돌렸다. edgequake 제출 후
# 폴링 중 5xx·타임아웃이면 `classify` 가 재시도 가능으로 분류하므로, ingest(max 3)가
# parse 부터 다시 돌며 **같은 문서를 또 제출**했다.

def _claim_and_insert_stage(repo, worker, kind):
    repo.submit(kind=kind, workspace_key="ws-dup")
    claimed = repo.claim(worker_id=worker, local_free=1)[0]
    repo.set_stage(claimed.id, worker_id=worker, attempt=claimed.attempt,
                   stage="inserting")
    return claimed


def test_requeue_after_insert_stage_fails_instead_of_retrying(repo, worker):
    """제출까지 갔으면 되돌리지 않는다 — 되돌리면 중복 적재다."""
    claimed = _claim_and_insert_stage(repo, worker, "ingest")
    repo.requeue(claimed.id, worker_id=worker, attempt=claimed.attempt,
                 error="downstream 503")
    row = repo.get(claimed.id)
    assert row["status"] == "failed"
    assert "not retried" in (row["error"] or "")
    assert row["completed_at"] is not None


def test_requeued_insert_stage_job_is_never_claimed_again(repo, worker):
    """terminal 이므로 다른 워커가 집어갈 수 없다."""
    claimed = _claim_and_insert_stage(repo, worker, "ingest")
    repo.requeue(claimed.id, worker_id=worker, attempt=claimed.attempt, error="boom")
    assert repo.claim(worker_id=worker, local_free=10) == []


def test_requeue_before_insert_stage_still_retries(repo, worker):
    """parse·chunk 단계의 일시 실패는 여전히 재시도돼야 한다(회귀 방지)."""
    repo.submit(kind="ingest", workspace_key="ws-dup2")
    claimed = repo.claim(worker_id=worker, local_free=1)[0]
    repo.set_stage(claimed.id, worker_id=worker, attempt=claimed.attempt,
                   stage="parsing")
    repo.requeue(claimed.id, worker_id=worker, attempt=claimed.attempt, error="503")
    row = repo.get(claimed.id)
    assert row["status"] == "queued"
    assert row["error"] == "503"          # 원래 사유가 보존된다
    assert row["stage"] is None
    assert repo.claim(worker_id=worker, local_free=10) != []


def test_insert_stage_requeue_clears_the_idem_key(repo, worker):
    """실패로 끝나면 멱등키를 비운다 — 안 비우면 같은 문서 재제출이 영구히 막힌다."""
    repo.submit(kind="ingest", workspace_key="ws-dup3", idem_key="dup-key-1")
    claimed = repo.claim(worker_id=worker, local_free=1)[0]
    repo.set_stage(claimed.id, worker_id=worker, attempt=claimed.attempt,
                   stage="inserting")
    repo.requeue(claimed.id, worker_id=worker, attempt=claimed.attempt, error="503")
    assert repo.get(claimed.id)["idem_key"] is None
    # 같은 키로 다시 제출하면 새 잡이 만들어진다(옛 failed id 가 반환되지 않는다).
    new_id = repo.submit(kind="ingest", workspace_key="ws-dup3", idem_key="dup-key-1")
    assert new_id != claimed.id


def test_insert_stage_guard_holds_on_both_paths(repo, worker):
    """회수(_recover)와 재시도(requeue) 둘 다 같은 판정이어야 한다.

    한쪽만 막으면 나머지 경로로 중복 적재가 샌다 — 이 테스트가 두 경로의 대칭을 고정한다.
    """
    outcomes = {}
    for path in ("recover", "requeue"):
        repo.submit(kind="ingest", workspace_key=f"ws-sym-{path}")
        c = repo.claim(worker_id=worker, local_free=1)[0]
        repo.set_stage(c.id, worker_id=worker, attempt=c.attempt, stage="inserting")
        if path == "requeue":
            repo.requeue(c.id, worker_id=worker, attempt=c.attempt, error="503")
        else:
            # lease 를 만료시켜 회수 경로를 태운다.
            with psycopg.connect(DSN) as conn:
                conn.execute(
                    "UPDATE kbp.jobs SET heartbeat_at = now() - interval '1 day'"
                    " WHERE id = %s", (c.id,))
                conn.commit()
            repo.claim(worker_id=worker, local_free=1)   # 유지보수 (1) 이 돈다
        row = repo.get(c.id)
        outcomes[path] = (row["status"], "not retried" in (row["error"] or ""))
    assert outcomes["recover"] == outcomes["requeue"] == ("failed", True), outcomes


# ── D13: 소진된 queued 좀비 ─────────────────────────────────────────────────
#
# requeue 가 attempt_count >= max 를 안 보면 queued 로 되돌아가는데, _candidates 는
# attempt_count < max 로 그 행을 영구 배제한다. terminal 도 아니고 GC 대상도 아닌 채
# 큐에 남고, 자식이면 부모의 TTL 삭제까지 영구 차단한다.

def test_requeue_on_last_attempt_finishes_instead_of_stranding(repo, worker):
    """insert 는 max_attempts=1 — 첫 시도의 requeue 가 곧 소진이다."""
    repo.submit(kind="insert", workspace_key="ws-z1")
    c = repo.claim(worker_id=worker, local_free=1)[0]
    assert repo.get(c.id)["attempt_count"] == 1
    repo.requeue(c.id, worker_id=worker, attempt=c.attempt, error="boom")
    row = repo.get(c.id)
    assert row["status"] == "failed"
    assert "attempts exhausted" in (row["error"] or "")
    assert "boom" in (row["error"] or "")        # 원래 사유도 남는다
    assert row["completed_at"] is not None
    assert row["idem_key"] is None


def test_requeue_with_attempts_left_still_returns_to_queue(repo, worker):
    """parse 는 max 3 — 1회차 실패는 여전히 재시도돼야 한다(회귀 방지)."""
    repo.submit(kind="parse")
    c = repo.claim(worker_id=worker, local_free=1)[0]
    repo.requeue(c.id, worker_id=worker, attempt=c.attempt, error="503")
    row = repo.get(c.id)
    assert row["status"] == "queued" and row["error"] == "503"
    assert repo.claim(worker_id=worker, local_free=10) != []


def test_maintenance_finishes_already_stranded_rows(repo, worker):
    """이 수정 **전에** 갇힌 행(또는 상한을 낮춰 소급 소진된 행)을 걷어낸다."""
    repo.submit(kind="insert", workspace_key="ws-z2")
    c = repo.claim(worker_id=worker, local_free=1)[0]
    # 옛 동작을 손으로 재현 — 소진인데 queued 로 되돌린 상태.
    with psycopg.connect(DSN) as conn:
        conn.execute(
            "UPDATE kbp.jobs SET status='queued', claimed_by=NULL, claimed_at=NULL,"
            " heartbeat_at=NULL, stage=NULL WHERE id=%s", (c.id,))
        conn.commit()
    assert repo.get(c.id)["status"] == "queued"

    repo.claim(worker_id=worker, local_free=10)          # 유지보수 (1d) 가 돈다
    row = repo.get(c.id)
    assert row["status"] == "failed"
    assert "attempts exhausted while queued" in (row["error"] or "")
    assert row["completed_at"] is not None


def test_maintenance_leaves_retryable_queued_rows_alone(repo, worker):
    """아직 시도가 남은 queued 행을 죽이면 큐가 통째로 멎는다."""
    job_id = repo.submit(kind="parse")
    assert repo.get(job_id)["status"] == "queued"
    repo.claim(worker_id=worker, local_free=0)           # 유지보수만 돈다
    assert repo.get(job_id)["status"] == "queued"


def test_stranded_child_no_longer_blocks_parent_ttl_gc(repo, worker):
    """좀비 자식이 살아있으면 부모가 TTL 로도 안 지워진다 — 종결돼야 풀린다."""
    parent = repo.submit(kind="chunk")
    pc = repo.claim(worker_id=worker, local_free=1)[0]
    repo.complete(pc.id, worker_id=worker, attempt=pc.attempt,
                  status="succeeded", result={})
    child = repo.submit(kind="insert", workspace_key="ws-z3", parent_job_id=parent)
    cc = [c for c in repo.claim(worker_id=worker, local_free=5) if c.id == child][0]
    repo.requeue(cc.id, worker_id=worker, attempt=cc.attempt, error="boom")
    assert repo.get(child)["status"] == "failed"         # 좀비가 아니라 terminal


# ── D6: inserting 이후 취소 거부 + refs 반환 ────────────────────────────────

def test_cancel_returns_refs_for_prompt_cleanup(repo, worker):
    """즉시 취소된 잡의 객체를 곧장 지우려면 키를 함께 받아야 한다."""
    job_id = repo.submit(kind="parse", input_ref="kbp-jobs/x/input.bin")
    out = repo.cancel(job_id)
    assert out["status"] == "canceled"
    assert out["input_ref"] == "kbp-jobs/x/input.bin"
    assert repo.get(job_id)["status"] == "canceled"


def test_cancel_is_refused_while_inserting(repo, worker):
    """edgequake 제출 뒤라 멈추면 부분 적재가 남는다 — 상태를 건드리지 않는다."""
    repo.submit(kind="insert", workspace_key="ws-c1")
    c = repo.claim(worker_id=worker, local_free=1)[0]
    repo.set_stage(c.id, worker_id=worker, attempt=c.attempt, stage="inserting")

    out = repo.cancel(c.id)
    assert out["status"] == "inserting"
    row = repo.get(c.id)
    assert row["status"] == "running"            # 그대로 달린다
    assert row["cancel_requested"] is False      # 플래그도 안 세운다


def test_cancel_before_inserting_flags_the_running_job(repo, worker):
    repo.submit(kind="parse", workspace_key="ws-c2")
    c = repo.claim(worker_id=worker, local_free=1)[0]
    repo.set_stage(c.id, worker_id=worker, attempt=c.attempt, stage="parsing")

    out = repo.cancel(c.id)
    assert out["status"] == "running"
    assert repo.get(c.id)["cancel_requested"] is True


def test_cancel_on_terminal_job_returns_none(repo, worker):
    repo.submit(kind="parse", workspace_key="ws-c3")
    c = repo.claim(worker_id=worker, local_free=1)[0]
    repo.complete(c.id, worker_id=worker, attempt=c.attempt, status="succeeded", result={})
    assert repo.cancel(c.id) is None


def test_cancel_on_missing_job_returns_none(repo, worker):
    assert repo.cancel(uuid.uuid4()) is None


# ── D10: community 는 claim 시점에 멱등키를 비운다 ──────────────────────────
#
# 이 두 테스트는 `_admit` SQL 동작이라 **실 Postgres 에서만** 관측된다. 인메모리 더블은
# _admit 을 아예 안 탄다(같은 계약을 memory.start 로 따로 검증한다).

def test_community_idem_key_is_cleared_on_claim(repo, worker):
    """빌드가 시작된 뒤 들어온 트리거는 **새 잡**이어야 한다.

    성공까지 키를 들고 있으면 running 잡이 후속 트리거를 흡수하는데, 그 잡은 시작 시점의
    그래프만 본다 → 배치 뒤쪽 문서의 엔티티가 커뮤니티에 영영 안 들어간다.
    """
    key = "community:ws-claimclear"
    first = repo.submit(kind="community", workspace_key="ws-cc", idem_key=key)
    c = [x for x in repo.claim(worker_id=worker, local_free=5) if x.id == first][0]
    assert repo.get(c.id)["idem_key"] is None          # claim 이 비웠다

    second = repo.submit(kind="community", workspace_key="ws-cc", idem_key=key)
    assert second != first                              # 새 잡이 만들어진다


def test_community_bursts_still_merge_while_queued(repo, worker):
    """claim 전(queued)에 들어온 트리거는 여전히 합쳐진다(실측 한 배치 3회)."""
    key = "community:ws-burst"
    ids = {repo.submit(kind="community", workspace_key="ws-b", idem_key=key)
           for _ in range(3)}
    assert len(ids) == 1


def test_insert_idem_key_survives_claim(repo, worker):
    """kind 분기 회귀 — insert 키를 claim 에서 비우면 재실행 중복 적재가 되살아난다.

    kb 의 재실행(arq max_tries·recover_stale·retry_failed_item)이 옛 잡을 맞아야 edgequake
    에 문서가 두 번 들어가지 않는다.
    """
    key = "kb-insert:9f1c2d3e-4a5b-6c7d-8e9f-0a1b2c3d4e5f"
    jid = repo.submit(kind="insert", workspace_key="ws-keep", idem_key=key)
    c = [x for x in repo.claim(worker_id=worker, local_free=5) if x.id == jid][0]
    assert repo.get(c.id)["idem_key"] == key            # 그대로 남아 있다

    again = repo.submit(kind="insert", workspace_key="ws-keep", idem_key=key)
    assert again == jid                                 # 재실행이 같은 잡을 맞는다
