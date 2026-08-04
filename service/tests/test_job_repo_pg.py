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


@pytest.fixture()
def repo():
    ensure_schema(DSN)
    with psycopg.connect(DSN) as conn:
        conn.execute("DELETE FROM kbp.jobs")
        conn.execute("DELETE FROM kbp.job_workers")
        conn.commit()
    return JobRepo(DSN)


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
    assert repo.cancel(job_id) == "canceled"
    assert repo.get(job_id)["status"] == "canceled"


def test_cancel_running_only_flags(repo, worker):
    repo.submit(kind="parse")
    claimed = repo.claim(worker_id=worker, local_free=1)
    assert repo.cancel(claimed[0].id) == "running"
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
    """
    repo.submit(kind="insert", workspace_key="ws-a")
    claimed = repo.claim(worker_id=worker, local_free=1)
    assert repo.get(claimed[0].id)["attempt_count"] == 1
    repo.requeue(claimed[0].id, worker_id=worker, attempt=claimed[0].attempt,
                 error="boom")
    assert repo.get(claimed[0].id)["status"] == "queued"
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
