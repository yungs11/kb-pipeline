"""야간 커뮤니티 배치 — 시각 판정·제출 계약. **PG 불요.**

이 파일이 `requires_pg` 가 아닌 것이 중요하다. 창 판정·자정 랩·TZ 폴백·제출 계약은
전부 순수 파이썬 로직인데, `requires_pg` 파일에 몰아넣으면 `KBP_PG_DSN` 미설정·활성
워커 감지로 **모듈 전체가 skip** 되어 이 프로젝트의 기본 dev 상태에서 한 줄도 실행되지
않는다. SQL 의미론만 `test_community_nightly.py`(requires_pg)로 보낸다.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from service import community_schedule as cs
from service.jobs.memory import InMemoryBlobStore, InMemoryJobRepo

KST = ZoneInfo("Asia/Seoul")


class SpyEq:
    def __init__(self, *, raises_for=()):
        self.raises_for = set(raises_for)
        self.calls = []

    def ensure_workspace(self, workspace_id, name=None):
        self.calls.append(workspace_id)
        if workspace_id in self.raises_for:
            raise RuntimeError("edgequake 5xx")
        return f"eq-{workspace_id}"


class SpyRunner:
    def __init__(self, eq=None):
        self.eq_client = eq or SpyEq()


def _env(monkeypatch, **kw):
    monkeypatch.setenv("KBP_COMMUNITY_TZ", "Asia/Seoul")
    monkeypatch.setenv("KBP_COMMUNITY_BUILD_AT", "03:00")
    monkeypatch.setenv("KBP_COMMUNITY_WINDOW_MINUTES", "120")
    monkeypatch.setenv("KBP_COMMUNITY_DEADLINE_MINUTES", "420")
    monkeypatch.setenv("KBP_COMMUNITY_MAX_PER_NIGHT", "8")
    monkeypatch.setenv("KBP_COMMUNITY_STALE_RUN_MINUTES", "30")
    for k, v in kw.items():
        monkeypatch.setenv(k, str(v))


def _touched(repo, *kb_ids):
    for kb in kb_ids:
        repo.touch_graph(kb)


# ── 실행 창 ────────────────────────────────────────────────────────────────

def test_outside_window_submits_nothing(monkeypatch):
    _env(monkeypatch)
    repo, blobs = InMemoryJobRepo(), InMemoryBlobStore()
    _touched(repo, "kb-1")
    out = cs.tick(repo, blobs, SpyRunner(), now=datetime(2026, 8, 9, 14, 0, tzinfo=KST))
    assert out == {"skipped": "outside-window"}
    assert repo.list_jobs(kind="community") == []


def test_inside_window_submits(monkeypatch):
    _env(monkeypatch)
    repo, blobs = InMemoryJobRepo(), InMemoryBlobStore()
    _touched(repo, "kb-1")
    out = cs.tick(repo, blobs, SpyRunner(), now=datetime(2026, 8, 9, 3, 5, tzinfo=KST))
    assert out["submitted"] == 1
    jobs = repo.list_jobs(kind="community")
    assert len(jobs) == 1


def test_second_tick_same_night_is_skipped(monkeypatch):
    """대상 0건이어도 claim_run 마커가 남아 그 밤 남은 틱이 다시 돌지 않는다."""
    _env(monkeypatch)
    repo, blobs = InMemoryJobRepo(), InMemoryBlobStore()
    now = datetime(2026, 8, 9, 3, 5, tzinfo=KST)
    assert cs.tick(repo, blobs, SpyRunner(), now=now)["status"] == "ok"
    assert cs.tick(repo, blobs, SpyRunner(), now=now) == {"skipped": "already-claimed"}


def test_midnight_wrap_keeps_one_run_date(monkeypatch):
    """`BUILD_AT=23:30, WINDOW=120` — 23:40 과 00:30 이 **같은 밤**이어야 한다.

    매 틱 `now.date()` 로 창을 재계산하면 00:30 이 다른 밤으로 판정돼 창 후반이 죽는다.
    """
    _env(monkeypatch, KBP_COMMUNITY_BUILD_AT="23:30")
    at = cs.build_at()
    d1 = cs.current_run_date(datetime(2026, 8, 9, 23, 40, tzinfo=KST), at)
    d2 = cs.current_run_date(datetime(2026, 8, 10, 0, 30, tzinfo=KST), at)
    assert d1 == d2 == date(2026, 8, 9)


def test_utc_now_would_shift_the_window(monkeypatch):
    """`now` 를 UTC 로 넘기면 창 판정이 어긋난다 — 로컬존 계약을 고정한다.

    레포의 다른 코드는 `datetime.now(timezone.utc)` 관례라 그대로 따라 쓰기 쉬운데,
    그러면 KST 03:00 창이 12:00 에 열린다.
    """
    _env(monkeypatch)
    at = cs.build_at()
    local = datetime(2026, 8, 9, 3, 5, tzinfo=KST)
    assert cs.current_run_date(local, at) == date(2026, 8, 9)
    # 같은 순간을 UTC 로 표현하면 18:05(전날) → 03:00 이전이라 **전날 밤**으로 판정된다
    utc = local.astimezone(timezone.utc)
    assert cs.current_run_date(utc, at) == date(2026, 8, 8)


# ── 취소 (①②) ─────────────────────────────────────────────────────────────

def test_previous_night_leftovers_are_canceled_outside_window(monkeypatch):
    """워커가 낮 12:00 에 살아나도 어제 밤 queued 는 취소된다.

    claim 경로에 시간 조건이 없어, 안 자르면 뜨자마자 **업무시간에** 캡만큼 실행한다.
    """
    _env(monkeypatch)
    repo, blobs = InMemoryJobRepo(), InMemoryBlobStore()
    repo.submit(kind="community", workspace_key="eq-kb-1",
                batch_key="community-nightly:2026-08-08")
    out = cs.tick(repo, blobs, SpyRunner(), now=datetime(2026, 8, 9, 12, 0, tzinfo=KST))
    assert out == {"skipped": "outside-window"}          # 창 밖인데도
    assert repo.list_jobs(kind="community")[0]["status"] == "canceled"   # ①이 돌았다


def test_deadline_cancels_todays_queued(monkeypatch):
    """DEADLINE(420) 은 WINDOW(120) 밖이라, 창 판정 뒤에 두면 영원히 도달 못 한다."""
    _env(monkeypatch)
    repo, blobs = InMemoryJobRepo(), InMemoryBlobStore()
    repo.submit(kind="community", workspace_key="eq-kb-1",
                batch_key="community-nightly:2026-08-09")
    cs.tick(repo, blobs, SpyRunner(), now=datetime(2026, 8, 9, 10, 30, tzinfo=KST))
    assert repo.list_jobs(kind="community")[0]["status"] == "canceled"


def test_running_jobs_are_not_canceled(monkeypatch):
    _env(monkeypatch)
    repo, blobs = InMemoryJobRepo(), InMemoryBlobStore()
    jid = repo.submit(kind="community", workspace_key="eq-kb-1",
                      batch_key="community-nightly:2026-08-08")
    repo.start(jid, worker_id="w1")
    cs.tick(repo, blobs, SpyRunner(), now=datetime(2026, 8, 9, 12, 0, tzinfo=KST))
    assert repo.get(jid)["status"] == "running"


# ── 제출 계약 ──────────────────────────────────────────────────────────────

def test_submit_contract(monkeypatch):
    _env(monkeypatch)
    repo, blobs = InMemoryJobRepo(), InMemoryBlobStore()
    _touched(repo, "kb-1")
    cs.tick(repo, blobs, SpyRunner(), now=datetime(2026, 8, 9, 3, 5, tzinfo=KST))
    job = repo.list_jobs(kind="community")[0]
    assert job["payload"] == {"workspace_id": "kb-1"}      # skip_if_unchanged 없음(A2)
    assert job["workspace_key"] == "eq-kb-1"               # eq UUID (kb id 아님)
    # ★ 야간 키는 **수동 키(`community:{eq_ws}`)와 달라야** 한다. 같으면 야간 잡이
    #   queued 인 동안 수동 재빌드가 그 job_id 를 돌려받아 아무 일도 안 일어난다.
    assert job["idem_key"] == "community-nightly:eq-kb-1:2026-08-09"
    assert job["idem_key"] != "community:eq-kb-1"
    assert job["batch_key"] == "community-nightly:2026-08-09"


def test_live_community_job_is_deduped_and_still_records_attempt(monkeypatch):
    """dedupe 경로도 `record_attempt` 를 해야 한다 — 안 하면 정렬 상단을 영구 점유한다."""
    _env(monkeypatch)
    repo, blobs = InMemoryJobRepo(), InMemoryBlobStore()
    _touched(repo, "kb-1")
    repo.submit(kind="community", workspace_key="eq-kb-1")   # 이미 queued
    out = cs.tick(repo, blobs, SpyRunner(), now=datetime(2026, 8, 9, 3, 5, tzinfo=KST))
    assert out["deduped"] == 1 and out["submitted"] == 0
    assert repo.community_builds["kb-1"].get("last_attempt_at") is not None


def test_ensure_workspace_failure_skips_item_but_records_attempt(monkeypatch):
    """깨진 workspace 가 `last_attempt_at=NULL` 로 남으면 **매 밤 영구 1순위**가 된다."""
    _env(monkeypatch)
    repo, blobs = InMemoryJobRepo(), InMemoryBlobStore()
    _touched(repo, "kb-bad", "kb-ok")
    runner = SpyRunner(SpyEq(raises_for={"kb-bad"}))
    out = cs.tick(repo, blobs, runner, now=datetime(2026, 8, 9, 3, 5, tzinfo=KST))
    assert out["failed"] == 1 and out["submitted"] == 1     # 루프가 멈추지 않았다
    assert repo.community_builds["kb-bad"].get("last_attempt_at") is not None


def test_idem_conflict_counts_as_deduped(monkeypatch):
    """`created=False` 가 어떤 카운터에도 안 잡히면 batch_runs 가 후보 수와 대사되지 않는다."""
    _env(monkeypatch)
    repo, blobs = InMemoryJobRepo(), InMemoryBlobStore()
    _touched(repo, "kb-1")
    # 같은 밤 키를 미리 점유 — 단 kind/workspace_key 를 달리해 has_live 검사는 피한다
    repo.submit(kind="community", workspace_key="other",
                idem_key="community-nightly:eq-kb-1:2026-08-09")
    out = cs.tick(repo, blobs, SpyRunner(), now=datetime(2026, 8, 9, 3, 5, tzinfo=KST))
    assert out["submitted"] == 0 and out["deduped"] == 1


def test_backlog_is_recorded_when_candidates_exceed_cap(monkeypatch):
    _env(monkeypatch, KBP_COMMUNITY_MAX_PER_NIGHT=2)
    repo, blobs = InMemoryJobRepo(), InMemoryBlobStore()
    _touched(repo, "kb-1", "kb-2", "kb-3", "kb-4", "kb-5")
    out = cs.tick(repo, blobs, SpyRunner(), now=datetime(2026, 8, 9, 3, 5, tzinfo=KST))
    assert out["submitted"] == 2 and out["backlog"] == 3
    assert repo.batch_runs[("community-nightly", date(2026, 8, 9))]["backlog"] == 3


# ── 굳은 run 회수 ──────────────────────────────────────────────────────────

def test_stale_started_is_reclaimed_after_window(monkeypatch):
    """창 종료 STALE 분 전 이후에 죽으면, 창만 보면 그 밤이 통째로 사라진다(이틀 지연)."""
    _env(monkeypatch)
    repo, blobs = InMemoryJobRepo(), InMemoryBlobStore()
    _touched(repo, "kb-1")
    # 04:45 에 claim 했다가 죽은 상태를 만든다
    repo.batch_runs[("community-nightly", date(2026, 8, 9))] = {
        "name": "community-nightly", "run_date": date(2026, 8, 9),
        "run_at": datetime(2026, 8, 9, 4, 45, tzinfo=KST).astimezone(timezone.utc),
        "submitted": 0, "deduped": 0, "failed": 0, "backlog": 0,
        "status": "started", "error": None}
    # 05:20 — 창(03:00~05:00) 밖이지만 마감(10:00) 전이고 굳은 started 가 있다
    out = cs.tick(repo, blobs, SpyRunner(), now=datetime(2026, 8, 9, 5, 20, tzinfo=KST))
    assert out["status"] == "ok" and out["submitted"] == 1


def test_stale_reclaim_not_allowed_after_deadline(monkeypatch):
    _env(monkeypatch)
    repo, blobs = InMemoryJobRepo(), InMemoryBlobStore()
    _touched(repo, "kb-1")
    repo.batch_runs[("community-nightly", date(2026, 8, 9))] = {
        "name": "community-nightly", "run_date": date(2026, 8, 9),
        "run_at": datetime(2026, 8, 9, 4, 45, tzinfo=KST).astimezone(timezone.utc),
        "submitted": 0, "deduped": 0, "failed": 0, "backlog": 0,
        "status": "started", "error": None}
    out = cs.tick(repo, blobs, SpyRunner(), now=datetime(2026, 8, 9, 11, 0, tzinfo=KST))
    assert out == {"skipped": "outside-window"}


# ── 견고성 ─────────────────────────────────────────────────────────────────

def test_exception_is_recorded_and_thread_survives(monkeypatch):
    _env(monkeypatch)
    repo, blobs = InMemoryJobRepo(), InMemoryBlobStore()
    _touched(repo, "kb-1")

    def boom(cap):
        raise RuntimeError("db down")

    monkeypatch.setattr(repo, "workspaces_needing_community", boom)
    out = cs.tick(repo, blobs, SpyRunner(), now=datetime(2026, 8, 9, 3, 5, tzinfo=KST))
    assert out["status"] == "failed"
    row = repo.batch_runs[("community-nightly", date(2026, 8, 9))]
    assert row["status"] == "failed" and "db down" in row["error"]


def test_finish_run_failure_does_not_propagate(monkeypatch):
    """`finish_run` 이 던져도 스레드가 죽으면 안 된다 — 죽으면 기동 로그가 영원히 안 찍힌다."""
    _env(monkeypatch)
    repo, blobs = InMemoryJobRepo(), InMemoryBlobStore()
    _touched(repo, "kb-1")

    def boom(*a, **kw):
        raise RuntimeError("pg down")

    monkeypatch.setattr(repo, "finish_run", boom)
    cs.tick(repo, blobs, SpyRunner(), now=datetime(2026, 8, 9, 3, 5, tzinfo=KST))


def test_bad_tz_falls_back_without_raising(monkeypatch):
    monkeypatch.setenv("KBP_COMMUNITY_TZ", "KST-9")   # POSIX 표기 — ZoneInfo 가 못 읽는다
    assert cs.zone() is not None


# ── D33: 스케줄 존과 컨테이너 TZ 의 분리 ───────────────────────────────────

def test_schedule_zone_defaults_to_kst_without_any_env(monkeypatch):
    """★ 아무 것도 안 줘도 KST 로 판정해야 한다.

    이게 깨지면 배포에 `KBP_COMMUNITY_TZ` 를 **반드시** 줘야 하는 셈이고, 안 준 배포는
    창이 조용히 UTC 03:00(=KST 정오)으로 이동한다 — 실패가 아니라 **잘못된 시각에 성공**
    이라 로그로 드러나지 않는다.
    """
    monkeypatch.delenv("KBP_COMMUNITY_TZ", raising=False)
    monkeypatch.delenv("TZ", raising=False)
    assert cs.zone().utcoffset(datetime(2026, 8, 9)) == timedelta(hours=9)


def test_container_tz_does_not_move_the_window(monkeypatch):
    """★ D33 의 불변식 — 컨테이너 `TZ` 는 스케줄 판정에 영향이 없어야 한다.

    한 변수가 "창 판정"과 "로그 시각 표기"를 겸하면, 로그를 UTC 로 통일하려 `TZ=UTC` 로
    두는 순간 야간 창이 KST 정오로 이동한다. `zone()` 이 `TZ` 를 읽으면 이 테스트가
    빨강이 된다.
    """
    monkeypatch.delenv("KBP_COMMUNITY_TZ", raising=False)
    for tz in ("UTC", "America/New_York", "KST-9", ""):
        monkeypatch.setenv("TZ", tz)
        off = cs.zone().utcoffset(datetime(2026, 8, 9))
        assert off == timedelta(hours=9), f"TZ={tz!r} 가 스케줄 존을 {off} 로 바꿨다"


def test_schedule_zone_is_overridable_by_its_own_env(monkeypatch):
    monkeypatch.setenv("KBP_COMMUNITY_TZ", "UTC")
    assert cs.zone().utcoffset(datetime(2026, 8, 9)) == timedelta(0)


def test_bad_build_at_falls_back(monkeypatch):
    monkeypatch.setenv("KBP_COMMUNITY_BUILD_AT", "삼시")
    from datetime import time as dtime

    assert cs.build_at() == dtime(3, 0)


def test_enabled_flag(monkeypatch):
    monkeypatch.setenv("KBP_COMMUNITY_BUILD_ENABLED", "false")
    assert cs._enabled() is False
    monkeypatch.setenv("KBP_COMMUNITY_BUILD_ENABLED", "true")
    assert cs._enabled() is True


# ── 기동 로그 (조용히 멈춤 탐지) ───────────────────────────────────────────

def test_previous_run_missing_is_reported(monkeypatch):
    _env(monkeypatch)
    repo = InMemoryJobRepo()
    assert cs.log_previous_run(repo, now=datetime(2026, 8, 9, 3, 5, tzinfo=KST)) == "missing"


def test_previous_run_uses_expected_date_not_latest_row(monkeypatch):
    """'가장 최근 행' 을 쓰면 3일 전 ok 만 남아 있어도 정상으로 보여 멈춤을 못 잡는다."""
    _env(monkeypatch)
    repo = InMemoryJobRepo()
    repo.batch_runs[("community-nightly", date(2026, 8, 5))] = {
        "name": "community-nightly", "run_date": date(2026, 8, 5),
        "run_at": None, "submitted": 3, "deduped": 0, "failed": 0, "backlog": 0,
        "status": "ok", "error": None}
    # 직전 밤(08-08) 행이 없으므로 missing 이어야 한다
    assert cs.log_previous_run(repo, now=datetime(2026, 8, 9, 3, 5, tzinfo=KST)) == "missing"


@pytest.mark.parametrize("status", ["ok", "failed", "started"])
def test_previous_run_statuses(monkeypatch, status):
    """`started` 로 굳은 밤(비정상 종료)도 분기가 있어야 한다 — 가장 조용한 실패다."""
    _env(monkeypatch)
    repo = InMemoryJobRepo()
    repo.batch_runs[("community-nightly", date(2026, 8, 8))] = {
        "name": "community-nightly", "run_date": date(2026, 8, 8),
        "run_at": None, "submitted": 1, "deduped": 0, "failed": 0, "backlog": 0,
        "status": status, "error": "x"}
    assert cs.log_previous_run(repo, now=datetime(2026, 8, 9, 3, 5, tzinfo=KST)) == status
