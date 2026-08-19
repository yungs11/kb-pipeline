"""Worker 틱 루프 — heartbeat 불변식과 결과 기록. fake repo 로 돈다(DB 불요).

설계 §6.1 의 불변식을 고정한다:
  * heartbeat 는 **전용 스레드**가 친다 — executor 가 포화돼도, 틱 루프가 막혀도 돈다
  * 드레인 중에도 heartbeat 가 유지된다(SIGTERM 이 heartbeat 를 죽이면 안 된다)
  * heartbeat 는 **in-flight 잡만** 갱신한다(일괄 갱신 금지)
  * 어떤 예외로도 heartbeat 스레드가 죽지 않는다
"""
from __future__ import annotations

import threading
import time
import uuid

import pytest

from service.jobs.repo import ClaimedJob, LeaseLost
from service.jobs.runner import JobAborted, JobFailed, JobRetryable
from service.worker import Worker


class FakeRepo:
    def __init__(self, *, to_claim=None, heartbeat_raises=0):
        self.to_claim = list(to_claim or [])
        self.heartbeats: list[list] = []
        self.marks: list[int] = []
        self.completed: list[tuple] = []
        self.clear_idem_calls: list[tuple] = []
        self.requeued: list[tuple] = []
        self.jobs: dict = {}
        self._hb_raises_left = heartbeat_raises
        self.lock = threading.Lock()

    def claim(self, *, worker_id, local_free):
        with self.lock:
            if not self.to_claim or local_free <= 0:
                return []
            batch, self.to_claim = self.to_claim[:local_free], self.to_claim[local_free:]
            return batch

    def heartbeat(self, *, worker_id, leases):
        with self.lock:
            if self._hb_raises_left > 0:
                self._hb_raises_left -= 1
                raise RuntimeError("transient DB error")
            self.heartbeats.append(list(leases))

    def mark_worker(self, *, worker_id, capacity, active_count, started_at):
        with self.lock:
            self.marks.append(active_count)

    def get(self, job_id):
        return self.jobs.get(job_id, {"id": job_id, "kind": "parse", "payload": {},
                                      "payload_ref": None, "input_ref": None,
                                      "parent_job_id": None})

    def complete(self, job_id, *, worker_id, attempt, status, result=None,
                 result_ref=None, error=None, clear_idem=False,
                 page_count=None, lanes=None, domain_error=None):
        with self.lock:
            self.completed.append((job_id, status, result, error))
            self.clear_idem_calls.append((job_id, clear_idem))
            self.summaries = getattr(self, "summaries", [])
            self.summaries.append((job_id, page_count, lanes))

    def requeue(self, job_id, *, worker_id, attempt, error):
        with self.lock:
            self.requeued.append((job_id, error))

    def legacy_refs_to_purge(self, job_id):
        return (None, None)

    def set_stage(self, *a, **kw):
        pass


class FakeBlobs:
    def __init__(self):
        self.deleted = []

    def check_bucket(self):
        return True

    def store_json(self, job_id, name, obj):
        return (obj, None)

    def load_json(self, inline, ref):
        return inline

    def delete(self, key):
        self.deleted.append(key)


class FakeRunner:
    def __init__(self, *, result=None, raises=None, block: threading.Event | None = None):
        self.result = result if result is not None else {"ok": True}
        self.raises = raises
        self.block = block
        self.calls = 0

    def run(self, job, *, worker_id, attempt):
        self.calls += 1
        if self.block is not None:
            self.block.wait(timeout=10)
        if self.raises:
            raise self.raises
        return self.result


def _worker(repo, runner, blobs=None, **env):
    w = Worker(repo=repo, blobs=blobs or FakeBlobs(), runner=runner, dsn="postgres://x")
    w.poll_interval = 0.02
    w.capacity = env.get("capacity", 2)
    return w


def _claim(n=1):
    return [ClaimedJob(id=uuid.uuid4(), kind="parse", attempt=1) for _ in range(n)]


# ── 실행 결과 기록 ─────────────────────────────────────────────────────────

def test_successful_job_is_completed_with_result():
    jobs = _claim(1)
    repo = FakeRepo(to_claim=jobs)
    w = _worker(repo, FakeRunner(result={"enriched_content": "x"}))
    w._tick()
    _wait(lambda: repo.completed)
    job_id, status, result, error = repo.completed[0]
    assert job_id == jobs[0].id and status == "succeeded"
    assert result == {"enriched_content": "x"}


def test_domain_failure_result_clears_idem_key():
    """job 은 succeeded 로 끝나지만(파싱 실패는 잡 실패가 아니다) 본문이 parse-svc
    {"status":"failed"} 면 idem_key 를 비운다 — 안 그러면 명시적 idem_key(시간창 없음)가
    영구 캐싱돼 근본 원인을 고쳐도 재현이 안 된다(2026-08-06 실관측: html 문서 1건이
    이 상태로 굳어 이후 모든 재시도가 같은 빈 enriched_content 를 영원히 돌려받음)."""
    jobs = _claim(1)
    repo = FakeRepo(to_claim=jobs)
    w = _worker(repo, FakeRunner(result={"status": "failed", "detail": "parse_failed"}))
    w._tick()
    _wait(lambda: repo.completed)
    job_id, status, result, error = repo.completed[0]
    assert status == "succeeded"  # 계약 불변 — 도메인 실패는 잡 실패가 아니다
    assert repo.clear_idem_calls[0] == (job_id, True)


def test_normal_success_result_keeps_idem_key():
    jobs = _claim(1)
    repo = FakeRepo(to_claim=jobs)
    w = _worker(repo, FakeRunner(result={"enriched_content": "x"}))
    w._tick()
    _wait(lambda: repo.completed)
    job_id = repo.completed[0][0]
    assert repo.clear_idem_calls[0] == (job_id, False)


def test_retryable_failure_requeues():
    jobs = _claim(1)
    repo = FakeRepo(to_claim=jobs)
    w = _worker(repo, FakeRunner(raises=JobRetryable("502")))
    w._tick()
    _wait(lambda: repo.requeued)
    assert repo.requeued[0][0] == jobs[0].id
    assert repo.completed == []


def test_permanent_failure_completes_as_failed():
    repo = FakeRepo(to_claim=_claim(1))
    w = _worker(repo, FakeRunner(raises=JobFailed("400")))
    w._tick()
    _wait(lambda: repo.completed)
    assert repo.completed[0][1] == "failed"


def test_aborted_job_touches_nothing():
    """lease 를 잃어 부작용 전에 중단 — 이미 다른 세대가 갖고 있으므로 건드리면 안 된다."""
    repo = FakeRepo(to_claim=_claim(1))
    w = _worker(repo, FakeRunner(raises=JobAborted("lost")))
    w._tick()
    time.sleep(0.15)
    assert repo.completed == [] and repo.requeued == []


def test_lease_lost_on_complete_is_discarded_not_raised():
    """부작용 이후 쓰기가 거부되면 결과만 버린다(프로세스는 계속 돈다)."""
    repo = FakeRepo(to_claim=_claim(1))

    def boom(*a, **kw):
        raise LeaseLost("gone")

    repo.complete = boom
    w = _worker(repo, FakeRunner())
    w._tick()
    time.sleep(0.15)  # 예외가 밖으로 새지 않는다


# ── heartbeat 불변식 ───────────────────────────────────────────────────────

def test_heartbeat_runs_while_executor_is_saturated():
    """슬롯이 꽉 차 있어도 heartbeat 가 돈다.

    heartbeat 를 executor 에 제출하면 여기서 굶어, 300s 뒤 전 잡이 stale 로 오판된다.
    """
    block = threading.Event()
    jobs = _claim(2)
    repo = FakeRepo(to_claim=jobs)
    w = _worker(repo, FakeRunner(block=block), capacity=2)
    w._tick()                       # 슬롯 2/2 점유, 두 잡 모두 block 에 매달림
    hb = threading.Thread(target=w._heartbeat_loop, daemon=True)
    hb.start()
    try:
        _wait(lambda: len(repo.heartbeats) >= 3, timeout=3)
        assert repo.heartbeats[-1]              # in-flight lease 가 실려 있다
        assert repo.marks[-1] == 2              # active_count 가 갱신된다
    finally:
        block.set()
        w._shutdown.set()
        hb.join(timeout=2)


def test_heartbeat_only_covers_inflight_jobs():
    """일괄(claimed_by=$me) 갱신 금지 — 죽은 잡까지 살려두면 슬롯을 오래 점유한다."""
    jobs = _claim(1)
    repo = FakeRepo(to_claim=jobs)
    w = _worker(repo, FakeRunner(block=threading.Event()))
    w._tick()
    w._heartbeat_once()
    assert repo.heartbeats[-1] == [(jobs[0].id, 1)]


def test_heartbeat_reports_zero_when_idle():
    repo = FakeRepo()
    w = _worker(repo, FakeRunner())
    w._heartbeat_once()
    assert repo.heartbeats[-1] == []
    assert repo.marks[-1] == 0


def test_heartbeat_thread_survives_db_errors():
    """transient DB 오류 한 번으로 스레드를 잃으면 큐 전체가 멈춘다."""
    repo = FakeRepo(heartbeat_raises=2)
    w = _worker(repo, FakeRunner())
    hb = threading.Thread(target=w._heartbeat_loop, daemon=True)
    hb.start()
    try:
        _wait(lambda: len(repo.heartbeats) >= 2, timeout=3)
        assert hb.is_alive()
    finally:
        w._shutdown.set()
        hb.join(timeout=2)


def test_drain_keeps_heartbeat_alive_until_jobs_finish():
    """SIGTERM 이 heartbeat 를 죽이면 안 된다.

    이관 원본은 shutdown(wait=True) 로 블로킹한 뒤에야 heartbeat 를 1회 부른다. 그대로
    포팅하면 stop_grace_period(1800s) 동안 heartbeat 가 STALE_LEASE(300s)를 넘겨
    드레인 중인 잡 전량이 회수·중복 실행된다.
    """
    block = threading.Event()
    repo = FakeRepo(to_claim=_claim(1))
    w = _worker(repo, FakeRunner(block=block))
    w._tick()
    hb = threading.Thread(target=w._heartbeat_loop, daemon=True)
    hb.start()
    w._hb_thread = hb

    drained = threading.Thread(target=w._drain, daemon=True)
    w._stop.set()          # SIGTERM 상당 — 새 claim 중단
    drained.start()

    time.sleep(0.2)
    before = len(repo.heartbeats)
    time.sleep(0.2)
    assert len(repo.heartbeats) > before, "드레인 중 heartbeat 가 멈췄다"
    assert not w._shutdown.is_set(), "드레인 전에 heartbeat 종료 신호가 켜졌다"

    block.set()
    drained.join(timeout=3)
    assert w._shutdown.is_set()  # 드레인 완료 후에야 heartbeat 종료


def test_tick_claims_only_up_to_free_slots():
    repo = FakeRepo(to_claim=_claim(5))
    w = _worker(repo, FakeRunner(block=threading.Event()), capacity=2)
    w._tick()
    assert len(w._inflight) == 2


def _wait(pred, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met in time")


# ── GC 스레드 (D2) ─────────────────────────────────────────────────────────

def test_gc_runs_in_its_own_thread_and_does_not_block_claims(monkeypatch):
    """GC 가 오래 걸려도 claim 이 계속 돈다.

    틱 루프에 GC 를 넣으면 그동안 claim·reap 이 멎어 큐가 정지하고 `_inflight` 가
    안 비워져 free 가 실제보다 작게 계산된다.
    """
    monkeypatch.setenv("KBP_JOB_GC_INTERVAL_SECONDS", "1")
    monkeypatch.setenv("KBP_JOB_ORPHAN_SWEEP_BOOT_DELAY_SECONDS", "99999")
    slow = threading.Event()
    from service.jobs import gc as gc_mod
    monkeypatch.setattr(gc_mod, "run_ttl_gc", lambda *a, **k: slow.wait(timeout=10))

    repo = FakeRepo(to_claim=_claim(3))
    w = _worker(repo, FakeRunner(), capacity=3)
    gc = threading.Thread(target=w._gc_loop, daemon=True); gc.start()
    try:
        time.sleep(0.2)                 # GC 가 slow 안에서 붙잡혀 있다
        w._tick()                       # 틱은 영향받지 않아야 한다
        assert len(w._inflight) == 3, "GC 가 claim 을 막았다"
    finally:
        slow.set(); w._shutdown.set(); gc.join(timeout=2)


def test_gc_thread_survives_exceptions(monkeypatch):
    """GC 가 매번 던져도 스레드가 살아 다음 사이클을 돈다."""
    monkeypatch.setenv("KBP_JOB_GC_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("KBP_JOB_ORPHAN_SWEEP_BOOT_DELAY_SECONDS", "99999")
    calls = []
    from service.jobs import gc as gc_mod

    def boom(*a, **k):
        calls.append(1)
        raise RuntimeError("minio down")

    monkeypatch.setattr(gc_mod, "run_ttl_gc", boom)
    w = _worker(FakeRepo(), FakeRunner())
    t = threading.Thread(target=w._gc_loop, daemon=True); t.start()
    try:
        _wait(lambda: len(calls) >= 2, timeout=3)
        assert t.is_alive()
    finally:
        w._shutdown.set(); t.join(timeout=2)


def test_orphan_sweep_waits_for_boot_delay(monkeypatch):
    """기동 직후엔 스윕이 돌지 않는다(TTL 은 돈다)."""
    monkeypatch.setenv("KBP_JOB_GC_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("KBP_JOB_ORPHAN_SWEEP_BOOT_DELAY_SECONDS", "99999")
    ttl, sweep = [], []
    from service.jobs import gc as gc_mod
    monkeypatch.setattr(gc_mod, "run_ttl_gc", lambda *a, **k: ttl.append(1))
    monkeypatch.setattr(gc_mod, "run_orphan_sweep", lambda *a, **k: sweep.append(1))
    w = _worker(FakeRepo(), FakeRunner())
    t = threading.Thread(target=w._gc_loop, daemon=True); t.start()
    try:
        _wait(lambda: ttl, timeout=3)
        assert sweep == [], "부팅 직후 스윕이 돌았다"
    finally:
        w._shutdown.set(); t.join(timeout=2)
