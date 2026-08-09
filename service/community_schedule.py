"""야간 커뮤니티 배치 스케줄러 (A1).

**왜 이 모듈이 있나** — 예전에는 문서가 `ready` 될 때마다 kb 워커가 커뮤니티 빌드를
enqueue 했다. 빌드 1건은 Louvain + 커뮤니티마다 LLM 리포트라 수십 분이 걸리는데,
배치 적재 중에는 앞선 빌드가 끝나기 무섭게 다음 빌드가 시작되고 **그 결과는 곧 낡는다**
— 마지막 빌드만 전체를 커버한다. 주간 LLM 부하를 그렇게 태울 이유가 없다.

그래서 **진입점을 하나로 모으고**(야간 1회 + 수동 `/communities/build`) 적재 경로에서는
트리거하지 않는다. 대가는 "커뮤니티가 최대 하루 뒤 반영"이고, 사용자가 인지하고 택했다.

**후보의 근거는 `kbp.graph_touch` 다** — 잡 테이블을 스캔하지 않는다:
  * `kind IN ('insert','ingest')` 만 보면 그래프 추출을 끈 vector-only KB 가
    현행 0회에서 매일 1회 LLM 빌드로 **나빠진다**(kb 트리거는 그 경우 enqueue 하지 않음).
    payload 로도 못 거른다 — insert payload 는 chunks 전량이라 거의 항상 오프로드되어
    `jobs.payload` 컬럼이 NULL 이다. **러너 시점에만** 볼 수 있어 거기서 기록한다.
  * GC 가 TTL(기본 72h) 경과 잡을 지우므로, 야간이 3일 넘게 멈추면 적재 **증거째**
    사라져 영구 미빌드가 된다. `graph_touch` 는 GC 대상이 아니다.

시각은 **로컬존**으로 다룬다. 이 레포의 다른 코드는 `datetime.now(timezone.utc)` 관례인데
그걸 그대로 쓰면 `BUILD_AT=03:00` 창이 KST 12:00 에 열려 목적이 정반대가 된다.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger("kb_pipeline.service.community_schedule")

RUN_NAME = "community-nightly"
BATCH_PREFIX = "community-nightly:"


# ── 설정 ───────────────────────────────────────────────────────────────────

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        log.warning("%s 파싱 실패 — 기본값 %s 사용", name, default)
        return default


def _enabled() -> bool:
    return (os.environ.get("KBP_COMMUNITY_BUILD_ENABLED", "true").strip().lower()
            not in {"0", "false", "no", "off"})


def zone() -> Any:
    """스케줄 판정용 타임존.

    2단 폴백을 둔다 — TZ 오타(`KST-9` 같은 POSIX 표기 포함)에서 한 번, **tzdata 자체가
    없는 이미지**에서 또 한 번. 폴백이 같은 예외를 던지면 스레드 기동이 막히고, 그게
    워커 본체 기동 지점이라 live worker 0 → facade 의 모든 제출이 503 이 된다.
    """
    name = os.environ.get("TZ") or "Asia/Seoul"
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        log.warning("TZ=%r 사용 불가 — Asia/Seoul 폴백", name)
    try:
        return ZoneInfo("Asia/Seoul")
    except Exception:  # noqa: BLE001 - tzdata 미포함 이미지
        log.warning("tzdata 없음 — 고정 UTC+9 폴백")
        return timezone(timedelta(hours=9))


def build_at() -> dtime:
    raw = (os.environ.get("KBP_COMMUNITY_BUILD_AT") or "03:00").strip()
    try:
        hh, mm = raw.split(":")
        return dtime(int(hh), int(mm))
    except Exception:  # noqa: BLE001
        log.warning("KBP_COMMUNITY_BUILD_AT=%r 파싱 실패 — 03:00 사용", raw)
        return dtime(3, 0)


def current_run_date(now: datetime, at: dtime) -> date:
    """지금이 어느 '밤'에 속하는지.

    ``BUILD_AT`` 이전이면 **전날 밤이 아직 안 끝난 것**으로 본다. 이 값을 한 번 고정해야
    자정을 넘는 설정(`BUILD_AT=23:30`, `WINDOW=120`)에서 창의 후반이 죽지 않는다 —
    매 틱 `now.date()` 로 창을 재계산하면 00:30 틱이 다른 밤으로 판정된다.

    ⚠️ ``now`` 는 **tz-aware 로컬존**이어야 한다(UTC 를 넘기면 창이 통째로 어긋난다).
    """
    return now.date() if now.time() >= at else now.date() - timedelta(days=1)


# ── 한 틱 ──────────────────────────────────────────────────────────────────

def tick(repo, blobs, runner, *, now: datetime | None = None) -> dict[str, Any]:
    """스케줄 한 틱. 반환값은 테스트·로깅용 요약이다.

    호출 순서가 중요하다:
      ① 지난 밤 잔여 취소 — **창 판정과 무관하게 매 틱 먼저**
      ② 마감 취소 — 창 판정 **앞**
      ③ 창(또는 굳은 run 회수) 판정 → claim → 제출
    """
    tz = zone()
    now = now or datetime.now(tz)
    at = build_at()
    run_date = current_run_date(now, at)
    start = datetime.combine(run_date, at, tz)
    window = _env_int("KBP_COMMUNITY_WINDOW_MINUTES", 120)
    deadline_min = _env_int("KBP_COMMUNITY_DEADLINE_MINUTES", 420)
    stale_min = _env_int("KBP_COMMUNITY_STALE_RUN_MINUTES", 30)
    end = start + timedelta(minutes=window)
    deadline = start + timedelta(minutes=deadline_min)
    today_key = f"{BATCH_PREFIX}{run_date}"

    # ① 지난 밤 잔여. 워커가 04:00 에 죽었다 낮 12:00 에 살아나면 run_date 는 '오늘'이라
    #    어제 밤 queued 가 취소 대상이 아니고, claim 경로에 시간 조건이 없어 뜨자마자
    #    **업무시간에** 캡만큼 실행한다.
    n = repo.cancel_nightly_queued(exclude_key=today_key)
    if n:
        log.warning("지난 밤 커뮤니티 잡 %d건 취소(업무시간 실행 방지)", n)

    # ② 마감. DEADLINE(420) 은 정의상 WINDOW(120) 밖이라 창 판정 뒤에 두면 도달 못 한다.
    if now >= deadline:
        n = repo.cancel_nightly_queued(key=today_key)
        if n:
            log.warning("마감 초과 — queued 커뮤니티 잡 %d건 취소", n)

    # ③ 창 안이거나, 굳은 run 을 회수할 수 있고 아직 마감 전이면 진행한다.
    #    창만 보면 `WINDOW`(120) 종료 `STALE`(30) 분 전 이후에 워커가 죽었을 때
    #    재claim 가능 시각이 창 밖이라 **그 밤이 통째로 사라진다**(이틀 지연).
    in_window = start <= now < end
    if not in_window:
        if now >= deadline or not repo.has_stale_started(RUN_NAME, run_date, stale_min):
            return {"skipped": "outside-window"}
    if not repo.claim_run(RUN_NAME, run_date, stale_min):
        return {"skipped": "already-claimed"}

    submitted = deduped = failed = backlog = 0
    err: str | None = None
    try:
        cap = _env_int("KBP_COMMUNITY_MAX_PER_NIGHT", 8)
        candidates, total = repo.workspaces_needing_community(cap)
        backlog = max(0, total - len(candidates))
        budget = _env_int("KBP_COMMUNITY_SUBMIT_BUDGET_SECONDS", 600)
        loop_deadline = time.monotonic() + budget

        for i, kb_id in enumerate(candidates):
            # ensure_workspace 는 동기 HTTP 다. edgequake 가 hang 이면 캡만큼 곱해져
            # 틱 하나가 오래 막히고 ①②도 못 돈다.
            if time.monotonic() > loop_deadline:
                log.warning("제출 루프 예산 초과 — 나머지 %d건은 다음 밤",
                            len(candidates) - i)
                break
            try:
                eq_ws = runner.eq_client.ensure_workspace(kb_id, name=kb_id)
            except Exception:  # noqa: BLE001
                log.exception("ensure_workspace 실패 — 건너뜀 kb=%s", kb_id)
                # 시도는 기록한다. 안 하면 깨진 workspace 가 last_attempt_at=NULL 로
                # 남아 정렬 상단을 **영구 점유**한다.
                _safe(repo.record_attempt, kb_id, None)
                failed += 1
                continue

            if repo.has_live_community_job(eq_ws):
                _safe(repo.record_attempt, kb_id, eq_ws)
                deduped += 1
                continue

            from service.jobs.api import submit_job_ex

            _job_id, created = submit_job_ex(
                repo, blobs, kind="community",
                payload={"workspace_id": kb_id},
                workspace_key=eq_ws,
                # 야간 키를 **수동 키(`community:{eq_ws}`)와 분리**한다. 같은 키면
                # 야간 잡이 queued 인 동안 수동 호출이 ON CONFLICT 로 그 job_id 를
                # 돌려받아, 운영자가 202 를 받고도 아무 일이 안 일어난다.
                idem_key=f"{BATCH_PREFIX}{eq_ws}:{run_date}",
                batch_key=today_key)
            _safe(repo.record_attempt, kb_id, eq_ws)
            if created:
                submitted += 1
            else:
                deduped += 1

        if backlog:
            log.warning("야간 커뮤니티 후보 %d건 중 %d건만 제출 — 잔여 %d건은 다음 밤",
                        total, len(candidates), backlog)
        status = "ok"
    except Exception as exc:  # noqa: BLE001 - 스레드를 잃지 않는다
        status, err = "failed", f"{type(exc).__name__}: {exc}"
        log.exception("야간 커뮤니티 배치 실패 run_date=%s", run_date)
    finally:
        # 이게 안 불리면 batch_runs 행이 'started' 로 굳어 그 밤 남은 틱이 전부
        # claim 실패한다. finish_run 자체가 던져도 스레드는 살아야 한다.
        _safe(repo.finish_run, RUN_NAME, run_date, submitted=submitted,
              deduped=deduped, failed=failed, backlog=backlog,
              status=status, error=err)

    log.info("야간 커뮤니티 배치 run_date=%s submitted=%d deduped=%d failed=%d backlog=%d",
             run_date, submitted, deduped, failed, backlog)
    return {"run_date": run_date, "submitted": submitted, "deduped": deduped,
            "failed": failed, "backlog": backlog, "status": status}


def _safe(fn, *args, **kwargs) -> None:
    """기록용 호출 — 실패해도 스케줄을 멈추지 않는다."""
    try:
        fn(*args, **kwargs)
    except Exception:  # noqa: BLE001
        log.error("스케줄 기록 실패: %s", getattr(fn, "__name__", fn), exc_info=True)


# ── 기동 로그 ──────────────────────────────────────────────────────────────

def log_previous_run(repo, *, now: datetime | None = None) -> str:
    """**기대하는 밤**의 결과를 로그로 남긴다(가장 최근 행이 아니다).

    '가장 최근'을 쓰면 워커가 3일간 창을 놓쳐 3일 전 ``ok`` 행만 남아 있어도 정상으로
    보여 멈춤을 못 잡는다. 야간이 유일한 빌드 경로가 되므로 이게 유일한 탐지 수단이다.
    """
    tz = zone()
    now = now or datetime.now(tz)
    at = build_at()
    # 지금 속한 밤의 **직전** 밤
    prev = current_run_date(now, at) - timedelta(days=1)
    try:
        row = repo.last_batch_run(RUN_NAME, prev)
    except Exception:  # noqa: BLE001
        log.warning("직전 밤(%s) 배치 기록 조회 실패", prev, exc_info=True)
        return "unknown"
    if row is None:
        log.warning("직전 밤(%s) 야간 커뮤니티 배치 기록이 없다 — 워커가 내려가 있었을 수 있다",
                    prev)
        return "missing"
    status = row.get("status")
    if status == "ok":
        log.info("직전 밤(%s) 배치 ok — submitted=%s deduped=%s failed=%s backlog=%s",
                 prev, row.get("submitted"), row.get("deduped"), row.get("failed"),
                 row.get("backlog"))
    elif status == "failed":
        log.error("직전 밤(%s) 배치 실패 — %s", prev, row.get("error"))
    else:
        # 'started' 로 굳었다 = 마커만 서고 끝나지 않은 밤(SIGKILL·OOM·재기동).
        log.error("직전 밤(%s) 배치가 '%s' 로 남아 있다 — 비정상 종료로 보인다",
                  prev, status)
    return status or "unknown"


# ── 루프 ───────────────────────────────────────────────────────────────────

def run_forever(repo, blobs, runner, *, stop: threading.Event | None = None) -> None:
    """스케줄 루프. 예외로 스레드를 잃지 않는다.

    최상위 가드가 없으면 `finish_run` 이 PG 장애로 던질 때 스레드가 죽고, 워커 본체는
    살아있어 재기동이 없으므로 '기동 시 직전 밤 로그' 가 영원히 다시 안 찍힌다.
    """
    stop = stop or threading.Event()
    tz = zone()
    at = build_at()
    poll = _env_int("KBP_COMMUNITY_POLL_SECONDS", 60)

    now = datetime.now(tz)
    nxt = datetime.combine(current_run_date(now, at) + timedelta(days=1), at, tz)
    log.info("야간 커뮤니티 배치 스레드 시작 — 다음 실행 %s (TZ=%s, poll=%ds)",
             nxt.isoformat(), tz, poll)
    _safe(log_previous_run, repo)

    ttl = _ttl_seconds()
    if ttl is not None and ttl < 48 * 3600:
        log.warning("KBP_JOB_TTL 이 %ds(<48h) — 캡에 밀린 후보가 TTL 로 탈락할 수 있다", ttl)

    while not stop.is_set():
        try:
            tick(repo, blobs, runner)
        except Exception:  # noqa: BLE001 - 어떤 이유로도 스레드를 잃지 않는다
            log.exception("야간 커뮤니티 스케줄 틱 실패")
        stop.wait(poll)


def _ttl_seconds() -> int | None:
    """`gc.ttl_seconds()` 는 **파싱 실패 시 None** 을 돌려준다 — `None < x` 는 TypeError."""
    try:
        from service.jobs import gc

        return gc.ttl_seconds()
    except Exception:  # noqa: BLE001
        return None
