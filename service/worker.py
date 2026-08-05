"""facade 잡 큐 worker — ``python -m service.worker``.

설계 §6.1.

facade API 와 **별도 프로세스**다. API 를 재기동해도 진행 중인 적재가 안 죽고, worker 만
따로 늘릴 수 있다. 다운스트림(parse-svc/adaptive_chunk/edgequake) 호출은 오직 여기
슬롯 안에서만 일어나므로 **유량제어 지점이 코드상 한 곳**이다.

스레드 구조 셋:

* **틱 루프**(메인) — 유지보수 → claim → 실행 제출. 매 틱 try/except.
* **heartbeat 스레드**(전용) — in-flight lease 와 ``job_workers`` 를 갱신. executor 에도
  틱 루프에도 붙이지 않는다. executor 에 붙이면 슬롯 포화 시 안 돌고, 틱 루프에 붙이면
  claim 이 advisory lock 에서 지연될 때 함께 멎는다 — 어느 쪽이든 그 worker 의 전 잡이
  동시에 stale 로 오판된다.
* **executor**(잡 실행) — ``KBP_JOB_WORKER_CONCURRENCY`` 개.
* **GC 스레드**(전용) — TTL 삭제 + 고아 객체 스윕. 틱 루프에 넣으면 GC 가 도는 동안
  ``claim``·``_reap`` 이 멎어 큐가 정지하고 ``_inflight`` 가 안 비워져 용량이 줄어든다.
  스윕은 MinIO 전체 나열이라 수 초~수 분이 걸릴 수 있다.
"""
from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone

from service.jobs import blobs as blobs_mod
from service.jobs import gc as gc_mod
from service.jobs.repo import JobRepo, LeaseLost
from service.jobs.runner import JobAborted, JobFailed, JobRetryable, JobRunner
from service.jobs.schema import ensure_schema

log = logging.getLogger("kb_pipeline.service.worker")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


class Worker:
    def __init__(self, *, repo=None, blobs=None, runner=None, dsn: str | None = None):
        self.dsn = dsn or os.environ["KBP_PG_DSN"]
        self.repo = repo or JobRepo(self.dsn)
        self.blobs = blobs if blobs is not None else blobs_mod.JobBlobStore.from_env()
        self.runner = runner or JobRunner(repo=self.repo, blobs=self.blobs)

        self.capacity = _env_int("KBP_JOB_WORKER_CONCURRENCY", 4)
        self.poll_interval = float(_env_int("KBP_JOB_POLL_INTERVAL_SECONDS", 2))
        # worker_id 는 프로세스마다 유일하지만 **프로세스 수명 동안 고정**이다. 그래서
        # 이것만으로는 같은 worker 의 재claim 을 구분하지 못한다 — 세대 토큰은
        # attempt_count 가 맡는다(repo 참조).
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.started_at = datetime.now(timezone.utc)

        #: 새 claim 을 멈추는 신호(SIGTERM). heartbeat 는 계속 돌아야 한다.
        self._stop = threading.Event()
        #: heartbeat 스레드까지 끝내는 신호. **드레인이 끝난 뒤에만** 세운다 —
        #: 하나의 이벤트로 둘을 겸하면 SIGTERM 순간 heartbeat 가 죽어, 드레인 중인
        #: 잡 전량이 STALE_LEASE 를 넘겨 다른 worker 에게 회수된다.
        self._shutdown = threading.Event()
        self._executor = ThreadPoolExecutor(max_workers=self.capacity,
                                            thread_name_prefix="kbp-job")
        #: future → (job_id, attempt). heartbeat 가 이 집합만 갱신한다.
        self._inflight: dict[Future, tuple[uuid.UUID, int]] = {}
        self._lock = threading.Lock()
        self._hb_thread: threading.Thread | None = None
        self._gc_thread: threading.Thread | None = None

    # ── heartbeat (전용 스레드) ────────────────────────────────────────────

    def _leases(self) -> list[tuple[uuid.UUID, int]]:
        with self._lock:
            return list(self._inflight.values())

    def _heartbeat_once(self) -> None:
        leases = self._leases()
        self.repo.heartbeat(worker_id=self.worker_id, leases=leases)
        self.repo.mark_worker(
            worker_id=self.worker_id, capacity=self.capacity,
            active_count=len(leases), started_at=self.started_at,
        )

    def _heartbeat_loop(self) -> None:
        """**어떤 예외로도 이 스레드는 죽지 않는다.**

        죽으면 두 가지가 같이 터진다 — 60s 뒤 live worker 0 판정으로 접수가 전부 503 이
        되고(worker 는 멀쩡히 잡을 돌리는데 큐가 멈춘다), 300s 뒤 in-flight 잡 전량이
        stale 로 회수되어 2중 실행된다.
        """
        while not self._shutdown.is_set():
            try:
                self._heartbeat_once()
            except Exception:  # noqa: BLE001 - transient DB 오류로 스레드를 잃지 않는다
                log.exception("heartbeat failed; continuing")
            self._shutdown.wait(self.poll_interval)
        # 종료 직전 마지막 갱신(드레인 완료 반영).
        try:
            self._heartbeat_once()
        except Exception:  # noqa: BLE001
            log.exception("final heartbeat failed")

    # ── GC (전용 스레드) ───────────────────────────────────────────────────

    def _gc_loop(self) -> None:
        """TTL 삭제 + 고아 스윕. **어떤 예외로도 이 스레드는 죽지 않는다.**

        주기 기준시각은 프로세스 메모리다.
          * TTL 삭제 — 기동 후 첫 사이클에 1회, 이후 ``GC_INTERVAL`` 마다.
          * 고아 스윕 — 기동 후 ``BOOT_DELAY``(기본 600s) 뒤 첫 실행, 이후
            ``SWEEP_INTERVAL`` 마다. 부팅 지연에 SWEEP_INTERVAL(6h)을 재사용하면
            재기동이 잦은 환경에서 **한 번도 안 돈다** — 고아가 쌓이는 게 정확히 그
            환경이다.
          * staging 스윕 — kb 의 ``parse-staging/`` 을 나이로만 수거한다(D20). 위 둘과
            **프리픽스도 판정도 다르다** — 참조가 kb DB 에 있어 facade 는 "누가 아직
            쓰는가" 를 알 수 없으므로 순수 TTL 이다. 고아 스윕과 같은 주기를 쓴다.
        """
        gc_interval = _env_int("KBP_JOB_GC_INTERVAL_SECONDS", 3600)
        sweep_interval = _env_int("KBP_JOB_ORPHAN_SWEEP_INTERVAL_SECONDS", 21600)
        boot_delay = _env_int("KBP_JOB_ORPHAN_SWEEP_BOOT_DELAY_SECONDS", 600)
        tick = min(60.0, max(1.0, self.poll_interval))

        next_gc = 0.0                                   # 첫 사이클에 바로
        next_sweep = time.monotonic() + boot_delay
        while not self._shutdown.is_set():
            now = time.monotonic()
            try:
                if now >= next_gc:
                    gc_mod.run_ttl_gc(self.repo, self.blobs)
                    next_gc = time.monotonic() + gc_interval
                if now >= next_sweep:
                    gc_mod.run_orphan_sweep(self.repo, self.blobs)
                    self._sweep_staging()
                    # lock 을 못 잡아 건너뛴 경우에도 타이머는 리셋한다
                    # (매 사이클 try-lock 을 두드리지 않게).
                    next_sweep = time.monotonic() + sweep_interval
            except Exception:  # noqa: BLE001 - GC 실패로 스레드를 잃지 않는다
                log.exception("gc cycle failed; continuing")
                next_gc = time.monotonic() + gc_interval
                next_sweep = time.monotonic() + sweep_interval
            self._shutdown.wait(tick)

    def _sweep_staging(self) -> None:
        """kb 의 ``parse-staging/`` TTL 스윕. 실패해도 잡 큐 GC 를 막지 않는다.

        MinIO 자격증명이 없거나(개발 기동) 프리픽스가 비어 있으면 조용히 넘어간다 —
        이건 부가 청소지 큐 동작의 전제가 아니다.
        """
        from service.jobs.staging_gc import StagingStore, run_staging_sweep

        try:
            run_staging_sweep(StagingStore.from_env())
        except Exception:  # noqa: BLE001 - staging 청소 실패가 GC 스레드를 죽이면 안 된다
            log.exception("staging sweep failed; continuing")

    # ── 틱 ─────────────────────────────────────────────────────────────────

    def _reap(self) -> None:
        with self._lock:
            done = [f for f in self._inflight if f.done()]
            for f in done:
                job_id, _ = self._inflight.pop(f)
                try:
                    f.result()
                except Exception:  # pragma: no cover - _execute 가 흡수한다
                    log.exception("job future crashed job=%s", job_id)

    def _tick(self) -> None:
        self._reap()
        with self._lock:
            free = self.capacity - len(self._inflight)
        # claim() 안에서 유지보수(회수·취소종결·만료 worker 삭제)가 local_free 와
        # 무관하게 먼저 돈다. free==0 이어도 호출해야 하는 이유다.
        claimed = self.repo.claim(worker_id=self.worker_id, local_free=free)
        for job in claimed:
            fut = self._executor.submit(self._execute, job)
            with self._lock:
                self._inflight[fut] = (job.id, job.attempt)

    def _execute(self, claimed) -> None:
        """잡 하나 실행 + 결과 기록. 예외를 밖으로 내보내지 않는다."""
        job_id, attempt = claimed.id, claimed.attempt
        try:
            job = self.repo.get(job_id)
            if job is None:
                log.warning("claimed job vanished: %s", job_id)
                return
            result = self.runner.run(job, worker_id=self.worker_id, attempt=attempt)
        except JobAborted:
            # lease 를 잃어 부작용 **전에** 중단했다. 상태를 건드리지 않는다 —
            # 이미 다른 세대가 이 잡을 갖고 있다.
            return
        except JobRetryable as exc:
            self._safe(self.repo.requeue, job_id, worker_id=self.worker_id,
                       attempt=attempt, error=str(exc))
            return
        except JobFailed as exc:
            self._finish(job_id, attempt, "failed", error=str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - 예상 못 한 것은 실패로 종결
            log.exception("job crashed job=%s", job_id)
            self._finish(job_id, attempt, "failed", error=str(exc))
            return
        self._finish(job_id, attempt, "succeeded", result=result)

    def _finish(self, job_id, attempt, status, *, result=None, error=None) -> None:
        inline, ref = (None, None)
        if result is not None:
            try:
                inline, ref = self.blobs.store_json(job_id, "result", result)
            except Exception as exc:  # noqa: BLE001 - 결과를 못 남기면 실패로 본다
                log.exception("failed to store result job=%s", job_id)
                status, error, inline, ref = "failed", f"result store failed: {exc}", None, None
        self._safe(self.repo.complete, job_id, worker_id=self.worker_id,
                   attempt=attempt, status=status, result=inline,
                   result_ref=ref, error=error)
        self._purge_legacy_inputs(job_id)

    def _purge_legacy_inputs(self, job_id) -> None:
        """레거시 잡의 staging·payload 객체를 terminal 즉시 지운다(§5.3).

        GC 가 없는 Phase 1 에서, 인증 없이 객체를 남길 수 있는 경로가 이것뿐이다.
        """
        try:
            input_ref, payload_ref = self.repo.legacy_refs_to_purge(job_id)
        except Exception:  # noqa: BLE001
            log.warning("could not read legacy refs job=%s", job_id, exc_info=True)
            return
        self.blobs.delete(input_ref)
        self.blobs.delete(payload_ref)

    @staticmethod
    def _safe(fn, *args, **kwargs) -> None:
        """lease 를 잃은 뒤의 쓰기는 버린다(부작용은 이미 끝났다)."""
        try:
            fn(*args, **kwargs)
        except LeaseLost as exc:
            log.warning("discarding write: %s", exc)
        except Exception:  # noqa: BLE001
            log.exception("job bookkeeping write failed")

    # ── 수명 ───────────────────────────────────────────────────────────────

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        ensure_schema(self.dsn)
        self.blobs.check_bucket()
        log.info("worker started id=%s capacity=%d", self.worker_id, self.capacity)

        self._hb_thread = threading.Thread(
            target=self._heartbeat_loop, name="kbp-heartbeat", daemon=True)
        self._hb_thread.start()

        # GC 는 전용 스레드다 — 틱에 넣으면 claim 이 멎는다.
        self._gc_thread = threading.Thread(
            target=self._gc_loop, name="kbp-gc", daemon=True)
        self._gc_thread.start()

        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:  # noqa: BLE001 - DB transient 후 다음 틱에서 재연결
                log.exception("worker tick failed; retrying after poll interval")
            # heartbeat 스레드가 죽으면 lease 를 유지할 수 없다. 잡을 드레인하고
            # 프로세스를 끝내 `restart: unless-stopped` 가 재생성하게 한다.
            if self._hb_thread is not None and not self._hb_thread.is_alive():
                log.error("heartbeat thread died; draining and exiting for restart")
                break
            self._stop.wait(self.poll_interval)

        self._drain()
        log.info("worker stopped id=%s", self.worker_id)

    def _drain(self) -> None:
        """SIGTERM 후 새 claim 을 멈추고 진행 중 잡을 완주시킨다.

        **드레인 중에도 heartbeat 를 유지해야 한다.** 이관 원본
        (``batch_worker.py:262-264``)은 ``shutdown(wait=True)`` 로 루프를 블로킹한 뒤에야
        heartbeat 를 1회 부르는데, 그대로 포팅하면 ``stop_grace_period``(1800s) 동안
        heartbeat 가 ``STALE_LEASE``(300s)를 넘겨 드레인 중인 잡 전량이 다른 worker 에게
        회수·중복 실행된다. **의도적으로 원본과 다른 지점이다.**
        """
        self._executor.shutdown(wait=False, cancel_futures=False)
        while True:
            self._reap()
            with self._lock:
                if not self._inflight:
                    break
            # heartbeat 스레드가 죽어 있으면 드레인 루프가 직접 친다 — lease 를 잃으면
            # 완주하던 잡이 회수돼 2중 실행된다.
            if self._hb_thread is None or not self._hb_thread.is_alive():
                try:
                    self._heartbeat_once()
                except Exception:  # noqa: BLE001
                    log.exception("drain heartbeat failed")
            self._shutdown.wait(self.poll_interval)
        # 드레인이 끝난 뒤에야 heartbeat 를 멈춘다.
        self._shutdown.set()
        for t in (self._hb_thread, self._gc_thread):
            if t is not None:
                t.join(timeout=self.poll_interval * 2)


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, os.environ.get("KBP_LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    worker = Worker()

    def _stop(_signum, _frame):  # type: ignore[no-untyped-def]
        log.info("signal received; draining")
        worker.stop()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    worker.run()


if __name__ == "__main__":
    main()
