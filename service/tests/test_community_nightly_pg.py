"""야간 커뮤니티 배치 — **SQL 의미론**. 실 Postgres 필요.

여기 있는 것만 실 DB 가 필요하다. 시각 판정·제출 계약 같은 순수 파이썬 로직은
`test_community_schedule.py`(PG 불요)에 있다 — 그쪽까지 이 파일에 넣으면
`KBP_PG_DSN` 미설정·활성 워커 감지로 **모듈 전체가 skip** 되어 기본 dev 상태에서
한 줄도 안 돈다.

특히 고정하는 것:
  * ``make_interval(mins => %s)`` — psycopg3 는 SQL **문자열 리터럴 안의 `%s` 도**
    치환하므로 ``interval '%s minutes'`` 로 쓰면 매 호출 예외가 된다.
  * ``LIKE 'community-nightly:%%'`` — 파라미터가 있는 쿼리에서 리터럴 `%` 이스케이프.
  * ``last_success_at`` 이 **빌드 시작 스냅샷**이라 빌드 도중 도착한 적재가 살아남는다.
"""
from __future__ import annotations

import datetime as dt
import os
import time

import psycopg
import pytest

from service.jobs.repo import JobRepo
from service.jobs.schema import ensure_schema

pytestmark = pytest.mark.requires_pg

DSN = os.environ.get("KBP_PG_DSN")

if not DSN:
    pytest.skip("KBP_PG_DSN unset", allow_module_level=True)


def _live_worker_present() -> bool:
    try:
        with psycopg.connect(DSN, connect_timeout=3) as conn:
            row = conn.execute(
                "SELECT count(*) FROM kbp.job_workers"
                " WHERE heartbeat_at >= now() - interval '60 seconds'"
            ).fetchone()
        return bool(row and row[0])
    except psycopg.Error:
        return False


if _live_worker_present():
    pytest.skip(
        "live facade-worker is consuming this queue — stop it to run these tests",
        allow_module_level=True,
    )


RUN = "community-nightly"
D = dt.date(2026, 8, 9)


def _truncate():
    with psycopg.connect(DSN) as conn:
        for t in ("jobs", "graph_touch", "community_builds", "batch_runs"):
            conn.execute(f"DELETE FROM kbp.{t}")
        conn.commit()


@pytest.fixture()
def repo():
    ensure_schema(DSN)
    _truncate()
    yield JobRepo(DSN)
    _truncate()


def _mk(repo, *, batch_key=None, status="queued", idem=None, ws="eq-1"):
    jid = repo.submit(kind="community", payload={"workspace_id": "kb-1"},
                      workspace_key=ws, batch_key=batch_key, idem_key=idem)
    if status != "queued":
        with psycopg.connect(DSN) as conn:
            conn.execute("UPDATE kbp.jobs SET status=%s WHERE id=%s", (status, jid))
            conn.commit()
    return jid


# ── 스키마 ─────────────────────────────────────────────────────────────────

def test_schema_creates_the_three_tables(repo):
    with psycopg.connect(DSN) as conn:
        names = {r[0] for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='kbp'").fetchall()}
    assert {"graph_touch", "community_builds", "batch_runs"} <= names


# ── claim_run: make_interval 관용구 ────────────────────────────────────────

def test_claim_run_is_atomic_and_marks_the_night(repo):
    assert repo.claim_run(RUN, D, 30) is True
    assert repo.claim_run(RUN, D, 30) is False      # 같은 밤 재실행 금지
    assert repo.last_batch_run(RUN, D)["status"] == "started"


def test_claim_run_reclaims_a_stuck_started_row(repo):
    """`finish_run` 이 안 불리는 종료(SIGKILL·OOM)에서 행이 'started' 로 굳는다.

    회수하지 않으면 그 밤 남은 틱이 전부 claim 실패해 제출 0건이 된다.
    """
    assert repo.claim_run(RUN, D, 30) is True
    assert repo.has_stale_started(RUN, D, 30) is False   # 아직 경과 전
    assert repo.claim_run(RUN, D, 0) is True             # 0분 → 즉시 경과로 본다
    assert repo.has_stale_started(RUN, D, 0) is True


def test_claim_run_reclaims_a_failed_row(repo):
    repo.claim_run(RUN, D, 30)
    repo.finish_run(RUN, D, submitted=0, deduped=0, failed=0, backlog=0,
                    status="failed", error="boom")
    assert repo.claim_run(RUN, D, 999) is True           # 실패한 밤은 즉시 재시도


def test_claim_run_does_not_reclaim_an_ok_row(repo):
    repo.claim_run(RUN, D, 30)
    repo.finish_run(RUN, D, submitted=1, deduped=0, failed=0, backlog=0, status="ok")
    assert repo.claim_run(RUN, D, 0) is False


def test_last_batch_run_is_keyed_by_expected_date(repo):
    """'가장 최근 행' 이면 3일 전 ok 만 남아도 정상으로 보여 멈춤을 못 잡는다."""
    repo.claim_run(RUN, dt.date(2026, 8, 5), 30)
    repo.finish_run(RUN, dt.date(2026, 8, 5), submitted=3, deduped=0, failed=0,
                    backlog=0, status="ok")
    assert repo.last_batch_run(RUN, D) is None           # 기대 밤에는 행이 없다


# ── cancel_nightly_queued: LIKE %% + batch_key 매칭 ────────────────────────

def test_cancel_targets_only_other_nights_queued(repo):
    y = _mk(repo, batch_key="community-nightly:2026-08-08")
    t = _mk(repo, batch_key="community-nightly:2026-08-09")
    run = _mk(repo, batch_key="community-nightly:2026-08-08", status="running")
    other = _mk(repo, batch_key="batch-other:2026-08-08")

    n = repo.cancel_nightly_queued(exclude_key="community-nightly:2026-08-09")
    assert n == 1
    with psycopg.connect(DSN) as conn:
        got = {str(i): s for i, s in
               conn.execute("SELECT id, status FROM kbp.jobs").fetchall()}
    assert got[str(y)] == "canceled"
    assert got[str(t)] == "queued"        # exclude 된 오늘 밤
    assert got[str(run)] == "running"     # running 은 보존
    assert got[str(other)] == "queued"    # 접두사 불일치


def test_cancel_clears_idem_key(repo):
    """안 비우면 다음 밤 제출이 `ON CONFLICT` 로 **취소된 job_id** 를 돌려받아,
    `deduped` 로만 세어지고 그 KB 가 조용히 멈춘다.
    """
    jid = _mk(repo, batch_key="community-nightly:2026-08-08",
              idem="community-nightly:eq-1:2026-08-08")
    repo.cancel_nightly_queued(key="community-nightly:2026-08-08")
    with psycopg.connect(DSN) as conn:
        row = conn.execute("SELECT status, idem_key FROM kbp.jobs WHERE id=%s",
                           (jid,)).fetchone()
    assert row == ("canceled", None)


def test_cancel_also_catches_jobs_whose_idem_key_is_already_null(repo):
    """claim·requeue 를 거친 잡은 `idem_key` 가 이미 NULL 이다 — `batch_key` 로 잡아야 한다."""
    _mk(repo, batch_key="community-nightly:2026-08-08", idem=None)
    assert repo.cancel_nightly_queued(key="community-nightly:2026-08-08") == 1


# ── 후보 선정: 두 축 분리 ──────────────────────────────────────────────────

def test_candidate_requires_touch_after_last_success(repo):
    repo.touch_graph("kb-1")
    repo.touch_graph("kb-2")
    repo.record_community_success("kb-1", "eq-1", repo.db_now())
    assert repo.workspaces_needing_community(10) == (["kb-2"], 1)


def test_insert_during_a_build_survives_to_the_next_night(repo):
    """★ 이 plan 의 핵심 불변식.

    빌드는 진입 시점 그래프만 보고 수십 분 걸린다. `last_success_at` 을 **완료 시각**으로
    기록하면 빌드 도중 성공한 적재가 `touched_at > last_success_at` 을 못 만족해
    **영구 탈락**한다 — 현행(적재마다 새 잡)보다 나빠진다.
    """
    snapshot = repo.db_now()          # ① 빌드 시작
    time.sleep(0.05)
    repo.touch_graph("kb-1")          # ② 빌드 도중 적재 성공
    time.sleep(0.05)
    repo.record_community_success("kb-1", "eq-1", snapshot)   # ③ 완료 — 값은 ①
    assert repo.workspaces_needing_community(10) == (["kb-1"], 1)


def test_recording_completion_time_would_drop_it(repo):
    """위 불변식이 깨졌을 때의 결과를 명시적으로 고정한다(회귀 방향 표시)."""
    _snapshot = repo.db_now()
    time.sleep(0.05)
    repo.touch_graph("kb-1")
    time.sleep(0.05)
    repo.record_community_success("kb-1", "eq-1", repo.db_now())   # ← 완료 시각
    assert repo.workspaces_needing_community(10) == ([], 0)


def test_failure_keeps_the_workspace_a_candidate(repo):
    repo.touch_graph("kb-1")
    repo.record_community_failure("kb-1", "eq-1")
    assert repo.workspaces_needing_community(10) == (["kb-1"], 1)
    with psycopg.connect(DSN) as conn:
        row = conn.execute("SELECT status, finished_at, last_success_at"
                           "  FROM kbp.community_builds WHERE workspace_key='kb-1'"
                           ).fetchone()
    assert row[0] == "failed" and row[1] is not None and row[2] is None


def test_ordering_prefers_least_recently_attempted(repo):
    """`fail_streak`(A2) 없이 굶김을 막는 축이다."""
    for k in ("kb-a", "kb-b", "kb-c"):
        repo.touch_graph(k)
    repo.record_attempt("kb-c", "eq-c")
    time.sleep(0.02)
    repo.record_attempt("kb-a", "eq-a")
    order, _ = repo.workspaces_needing_community(10)
    assert order[0] == "kb-b"          # 시도 이력이 없는 것이 가장 앞
    assert order.index("kb-c") < order.index("kb-a")


def test_returns_total_before_cap(repo):
    """`LIMIT` 만 두면 backlog 를 알 수 없어 경고도 기록도 못 한다."""
    for i in range(5):
        repo.touch_graph(f"kb-{i}")
    lst, total = repo.workspaces_needing_community(2)
    assert (len(lst), total) == (2, 5)


def test_record_attempt_does_not_touch_success_columns(repo):
    repo.touch_graph("kb-1")
    repo.record_attempt("kb-1", "eq-1")
    with psycopg.connect(DSN) as conn:
        row = conn.execute("SELECT last_attempt_at, last_success_at"
                           "  FROM kbp.community_builds WHERE workspace_key='kb-1'"
                           ).fetchone()
    assert row[0] is not None and row[1] is None
    assert repo.workspaces_needing_community(10) == (["kb-1"], 1)   # 후보 유지


# ── in-flight ──────────────────────────────────────────────────────────────

def test_has_live_community_job(repo):
    jid = _mk(repo, ws="eq-1")
    assert repo.has_live_community_job("eq-1") is True
    assert repo.has_live_community_job("eq-other") is False
    with psycopg.connect(DSN) as conn:
        conn.execute("UPDATE kbp.jobs SET status='succeeded' WHERE id=%s", (jid,))
        conn.commit()
    assert repo.has_live_community_job("eq-1") is False
