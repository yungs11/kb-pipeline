"""잡 큐 GC — TTL 삭제 + 고아 객체 스윕.

설계: ``docs/superpowers/specs/2026-08-04-job-queue-gc-design.md``

Phase 1 은 GC 를 의도적으로 뺐다. 전제가 바뀌어서 넣는다 — `legacy=false`(신규
``/jobs/*``) 잡은 staging 을 남기는데, kb 가 플래그를 켜면 쓰는 게 정확히 그 경로라
업로드마다 원본이 MinIO 에 영구 적재된다.

**두 경로의 위험도가 다르다.**

* TTL 삭제 — 행이 근거다. 안전하다.
* 고아 스윕 — **증거의 부재**로 삭제를 결정한다. 여기가 위험하다. 그래서 판정 입력이
  하나라도 실패하면 사이클 전체를 중단한다(fail-closed).
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

log = logging.getLogger("kb_pipeline.service.jobs.gc")


# ── 설정 ───────────────────────────────────────────────────────────────────


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def ttl_seconds() -> int | None:
    """TTL(초). **파싱 실패는 ``None``** — GC 전체를 정지시킨다.

    ``repo._env_int`` 를 쓰지 않는 이유: 그 헬퍼는 ``ValueError`` 를 삼키고 기본값을
    돌려주므로 ``KBP_JOB_TTL_HOURS=off`` 같은 오타가 **정지가 아니라 기본 72h 동작**이
    된다. 안전 규칙("파싱 실패면 멈춘다")이 성립하려면 실패를 구분해야 한다.

    ``KBP_JOB_TTL_SECONDS`` 가 있으면 우선한다(테스트·미세조정용).
    """
    raw = os.environ.get("KBP_JOB_TTL_SECONDS")
    if raw is not None and raw != "":
        try:
            return int(raw)
        except ValueError:
            log.error("KBP_JOB_TTL_SECONDS=%r is not an int — GC disabled", raw)
            return None
    raw = os.environ.get("KBP_JOB_TTL_HOURS")
    if raw is None or raw == "":
        return 72 * 3600
    try:
        return int(raw) * 3600
    except ValueError:
        log.error("KBP_JOB_TTL_HOURS=%r is not an int — GC disabled", raw)
        return None


@dataclass
class GcConfig:
    ttl_seconds: int | None = field(default_factory=ttl_seconds)
    batch: int = field(default_factory=lambda: _int("KBP_JOB_GC_BATCH", 500))
    query_chunk: int = field(default_factory=lambda: _int("KBP_JOB_GC_QUERY_CHUNK", 1000))
    orphan_grace: int = field(
        default_factory=lambda: _int("KBP_JOB_ORPHAN_GRACE_SECONDS", 21600))
    orphan_max_ratio: float = field(
        default_factory=lambda: float(os.environ.get("KBP_JOB_ORPHAN_MAX_RATIO") or 0.9))
    orphan_min_for_ratio: int = field(
        default_factory=lambda: _int("KBP_JOB_ORPHAN_MIN_FOR_RATIO", 100))


# ── TTL 삭제 ───────────────────────────────────────────────────────────────


def run_ttl_gc(repo, blobs, *, cfg: GcConfig | None = None) -> int:
    """TTL 경과 terminal 잡을 지운다. 반환값은 삭제된 행 수.

    행을 먼저(트랜잭션 안에서) 지우고 **커밋이 끝난 뒤** 객체를 지운다. 반대면 커밋
    실패 시 행은 살아남고 객체만 사라져 ``/jobs/{id}/result`` 가 500 이 된다.
    """
    cfg = cfg or GcConfig()
    rows = repo.purge_expired(ttl_seconds=cfg.ttl_seconds, batch=cfg.batch)
    if rows is None:
        log.debug("TTL GC disabled (ttl_seconds=%r)", cfg.ttl_seconds)
        return 0
    if not rows:
        return 0

    null_completed = [r for r in rows if r.get("completed_at") is None]
    if null_completed:
        # 모든 terminal 전이는 completed_at 을 채운다는 불변식이 깨진 행들이다.
        # created_at 폴백으로 회수는 했지만 원인을 남긴다.
        log.warning(
            "purged %d terminal job(s) with NULL completed_at (ids=%s)",
            len(null_completed), [str(r["id"]) for r in null_completed[:5]],
        )

    removed = 0
    for row in rows:
        for key in (row.get("input_ref"), row.get("payload_ref"), row.get("result_ref")):
            if key:
                blobs.delete(key)
                removed += 1
    log.info("TTL GC: purged %d job(s), %d object(s)", len(rows), removed)
    return len(rows)


# ── 고아 스윕 ──────────────────────────────────────────────────────────────


@dataclass
class SweepResult:
    listed: int = 0        # 파싱 성공한 전체 나열 수(비율 가드의 분모)
    candidates: int = 0    # grace 를 넘긴 것
    orphans: int = 0       # 최종 삭제 대상
    deleted: int = 0
    aborted: str | None = None   # fail-closed / 보류 사유


def run_orphan_sweep(repo, blobs, *, cfg: GcConfig | None = None,
                     now: dt.datetime | None = None) -> SweepResult:
    """행이 없는 `{prefix}/{uuid}/` 객체를 지운다.

    **삭제 조건 4개를 모두 만족할 때만** 지운다:

    1. 키가 우리 형식(`job_id` 파싱 성공)
    2. `last_modified` 가 tz-aware 이고 grace 를 넘김 (None·naive 면 건너뛴다)
    3. 그 `job_id` 로 된 행이 없음
    4. 그 **키**가 어떤 행의 `*_ref` 와도 일치하지 않음

    4번은 행 id 와 객체 키가 어긋난 행이 과거에 만들어졌을 수 있어서 두는 방어다.
    """
    cfg = cfg or GcConfig()
    res = SweepResult()
    if cfg.ttl_seconds is None or cfg.ttl_seconds <= 0:
        res.aborted = "gc disabled"
        return res

    now = now or dt.datetime.now(dt.timezone.utc)
    grace = dt.timedelta(seconds=cfg.orphan_grace)

    # 1) 나열 — 실패하면 사이클 중단(fail-closed)
    try:
        listed: list[tuple[uuid.UUID, str]] = []
        for job_id, key, last_modified in blobs.iter_job_objects():
            if job_id is None:
                continue                      # 우리 것이 아니다 — 건드리지 않는다
            res.listed += 1
            if last_modified is None or last_modified.tzinfo is None:
                continue                      # 판정 불가 → 보존
            if now - last_modified <= grace:
                continue                      # 제출 창 보호
            listed.append((job_id, key))
    except Exception:  # noqa: BLE001 - 나열 실패도 fail-closed
        log.exception("orphan sweep: listing failed — aborting cycle")
        res.aborted = "list failed"
        return res
    res.candidates = len(listed)
    if not listed:
        return res

    # 2) 판정 — 청크로 나눠 질의한다. 대형 배열 3중 OR 는 seq scan 이라
    #    statement_timeout 에 걸리면 사이클 전체가 죽는다.
    orphan_keys: list[str] = []
    for chunk in _chunks(listed, cfg.query_chunk):
        ids = {j for j, _ in chunk}
        present = repo.job_ids_present(sorted(ids))
        if present is None:
            res.aborted = "job_ids_present failed"
            log.warning("orphan sweep aborted (fail-closed): %s", res.aborted)
            return res
        maybe = [(j, k) for j, k in chunk if j not in present]
        if not maybe:
            continue
        used = repo.refs_in_use([k for _, k in maybe])
        if used is None:
            res.aborted = "refs_in_use failed"
            log.warning("orphan sweep aborted (fail-closed): %s", res.aborted)
            return res
        orphan_keys.extend(k for _, k in maybe if k not in used)
    res.orphans = len(orphan_keys)
    if not orphan_keys:
        return res

    # 3) sanity — 분모는 **grace 필터 이전의 전체 나열 수**다.
    #    후보(grace 통과분)를 분모로 쓰면 비율이 구조적으로 1.0 에 붙어(TTL GC 가 행과
    #    객체를 함께 지우므로 남는 건 사실상 고아뿐이다) 스윕이 필요한 상태에서 항상
    #    보류된다. 고아 수가 적을 땐 비율 자체가 무의미하므로 하한을 둔다.
    if (res.orphans >= cfg.orphan_min_for_ratio
            and res.listed
            and res.orphans / res.listed > cfg.orphan_max_ratio):
        res.aborted = (f"orphan ratio {res.orphans}/{res.listed} "
                       f"> {cfg.orphan_max_ratio}")
        log.warning("orphan sweep held back: %s", res.aborted)
        return res

    for key in orphan_keys[:cfg.batch]:
        blobs.delete(key)
        res.deleted += 1
    log.info("orphan sweep: listed=%d candidates=%d orphans=%d deleted=%d",
             res.listed, res.candidates, res.orphans, res.deleted)
    return res


def _chunks(seq: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    size = max(1, size)
    for i in range(0, len(seq), size):
        yield seq[i:i + size]
