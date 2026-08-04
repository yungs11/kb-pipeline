"""잡 제출·조회 라우터 + 레거시 대기 헬퍼.

설계 §4.

여기서 하는 일은 **접수와 조회뿐**이다. 다운스트림 호출은 worker 프로세스의 슬롯 안에서만
일어난다(§1 불변 규칙). 그래서 이 핸들러들은 전부 밀리초 안에 끝나야 하고, 유일한 예외가
`?wait`/레거시 대기인데 그마저 **DB 만 폴링**한다.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from typing import Any

from fastapi import (APIRouter, Body, Depends, File, Form, Header, HTTPException,
                     Query, UploadFile)

from service.jobs import blobs as blobs_mod
from service.jobs.repo import JobRepo
from service.jobs.runner import JobAborted, JobFailed, JobRetryable, JobRunner

log = logging.getLogger("kb_pipeline.service.jobs.api")

TERMINAL = frozenset({"succeeded", "failed", "canceled"})


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


# ── 대기 permit ────────────────────────────────────────────────────────────
#
# 대기 핸들러를 동기 `def` 로 두는 것만으로는 부족하다. `/healthz` 도 동기 `def` 라
# **같은 AnyIO 스레드풀**을 공유하므로, 대기가 풀을 채우면 healthcheck 가 큐에 갇혀
# 컨테이너가 unhealthy 로 떨어진다. 프로세스당 동시 대기 수를 제한한다.
_WAITERS = threading.BoundedSemaphore(_env_int("KBP_JOB_MAX_WAITERS", 4))


class _Permit:
    """`with` 로 잡고, 못 잡으면 503. **잡 생성 전에** 획득해야 한다.

    제출-후-거절은 금지다 — 잡을 INSERT 한 뒤 503 을 내면 worker 는 그 잡을 정상
    실행하는데 kb 는 5xx 를 재시도해 **두 번째 잡**을 만든다(멱등키가 없다 — D1).
    레거시 /insert·/ingest 에서는 곧 edgequake 중복 적재다.
    """

    def __enter__(self):
        if not _WAITERS.acquire(blocking=False):
            raise HTTPException(status_code=503, detail="too many waiting requests",
                                headers={"Retry-After": "5"})
        return self

    def __exit__(self, *exc):
        _WAITERS.release()
        return False


# ── 의존성 ─────────────────────────────────────────────────────────────────
#
# 모듈 스코프에 인스턴스를 만들지 않는다(§6 불변식) — `importlib.reload(service.app)`
# 만으로 실 DB·MinIO 접속을 시도하게 된다.


def get_job_repo():
    return JobRepo()


def get_job_blobs():
    return blobs_mod.JobBlobStore.from_env()


def get_job_runner(repo=Depends(get_job_repo), blobs=Depends(get_job_blobs)):
    """인라인 실행용 runner. 다운스트림 팩토리는 app 의 `_resolve` 를 통과시킨다 —
    그래야 `dependency_overrides` 가 살아난다(그냥 함수를 넘기면 무시된다)."""
    from service import app as app_mod

    return JobRunner(
        repo=repo, blobs=blobs,
        parse_factory=lambda: app_mod._resolve(app_mod.get_parse_client),
        chunk_factory=lambda: app_mod._resolve(app_mod.get_adaptive_chunk),
        eq_factory=lambda: app_mod._resolve(app_mod.get_edgequake),
    )


router = APIRouter()


# ── 멱등키 ─────────────────────────────────────────────────────────────────


def derive_idem_key(
    *, kind: str, payload: dict[str, Any], file_bytes: bytes | None,
    explicit: str | None, workspace_key: str | None, parent_job_id: str | None = None,
) -> str:
    """제출 멱등키. 명시 헤더가 있으면 그것, 없으면 요청 내용에서 파생한다.

    소비자(kb)는 429/5xx 를 최대 3회 재시도한다. 제출 경로에서 그건 잡 중복 생성이고,
    ``/insert``·``/ingest`` 에서는 곧 edgequake 중복 적재다(멱등키가 없는 한).

    **자동 파생 키에는 시간 버킷이 들어간다.** 그러지 않으면 "설정을 고치고 같은 파일을
    다시 파싱" 같은 정상 재요청이 옛 잡 id 를 돌려받는 조용한 no-op 이 된다. 버킷 폭
    (``KBP_JOB_IDEM_WINDOW_SECONDS``, 기본 300s)은 재시도 버스트(수 초)보다 훨씬 넓고
    의도적 재요청(수 분 뒤)보다는 좁다. 명시 헤더에는 버킷을 넣지 않는다 — 소비자가
    수명을 스스로 정한 것이므로.
    """
    if explicit:
        return f"h:{explicit}"
    material = {
        "kind": kind,
        "workspace": workspace_key,
        # 같은 payload 라도 참조하는 선행 잡이 다르면 다른 요청이다.
        "parent": str(parent_job_id) if parent_job_id else None,
        # payload 는 키 순서에 무관해야 한다(소비자가 dict 순서를 보장하지 않는다).
        "payload": json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str),
        "file": hashlib.sha256(file_bytes).hexdigest() if file_bytes else None,
    }
    digest = hashlib.sha256(
        json.dumps(material, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    window = max(1, _env_int("KBP_JOB_IDEM_WINDOW_SECONDS", 300))
    bucket = int(time.time()) // window
    return f"a:{digest}:{bucket}"


# ── 제출 헬퍼 ──────────────────────────────────────────────────────────────


def submit_job(
    repo, blobs, *, kind: str, payload: dict[str, Any],
    file_bytes: bytes | None = None, workspace_key: str | None = None,
    batch_key: str | None = None, parent_job_id: str | None = None,
    legacy: bool = False, idem_key: str | None = None,
) -> uuid.UUID:
    """staging 업로드 → payload 오프로딩 → 행 INSERT. 밀리초.

    **살아있는 worker 가 없으면 잡을 만들지 않고 503 을 낸다**(§4.4). facade-worker 는
    이번에 새로 생기는 프로세스라 빠뜨리기 쉽고, 그러면 오늘은 facade 만 띄우면 되던
    `/parse` 가 상한까지 매달렸다가 실패한다.
    """
    if repo.live_worker_count() <= 0:
        raise HTTPException(status_code=503, detail="no live facade-worker",
                            headers={"Retry-After": "10"})

    if file_bytes is not None:
        cap = blobs_mod.max_upload_bytes()
        if len(file_bytes) > cap:
            raise HTTPException(status_code=413,
                                detail=f"upload exceeds {cap} bytes")

    parent = _validate_parent(repo, parent_job_id)

    # 객체 키가 {prefix}/{job_id}/... 라 INSERT 전에 id 를 정한다. 그래야 행을 만든 뒤
    # 참조를 UPDATE 로 덧칠하는 우회가 없다.
    job_id = uuid.uuid4()

    input_ref = None
    if file_bytes is not None:
        input_ref = blobs.key(job_id, "input.bin")
        blobs.put_bytes(input_ref, file_bytes, content_type="application/octet-stream")
    inline, payload_ref = blobs.store_json(job_id, "payload", payload)

    created = repo.submit(job_id=job_id, kind=kind, payload=inline,
                          payload_ref=payload_ref, input_ref=input_ref,
                          workspace_key=workspace_key, batch_key=batch_key,
                          parent_job_id=parent, legacy=legacy, idem_key=idem_key)
    if created != job_id:
        # 멱등 충돌 — 기존 잡을 재사용한다. 방금 올린 staging 객체는 어떤 행도
        # 참조하지 않으므로 즉시 지운다(GC 가 없어 그냥 두면 영구 고아다).
        blobs.delete(input_ref)
        if payload_ref:
            blobs.delete(payload_ref)
    return created


def _validate_parent(repo, parent_job_id):
    if not parent_job_id:
        return None
    parent = repo.get(parent_job_id)
    if parent is None:
        raise HTTPException(status_code=404, detail="referenced job not found")
    if parent["status"] != "succeeded":
        raise HTTPException(status_code=409,
                            detail=f"referenced job is {parent['status']}")
    return parent["id"]


# ── 대기 ───────────────────────────────────────────────────────────────────


def wait_for_job(repo, blobs, job_id, *, timeout: float, runner=None,
                 inline: bool = False) -> dict[str, Any]:
    """완료까지 **DB 만 폴링**한다. 인라인 모드면 같은 프로세스에서 실행한다.

    잡 행이 사라지면 즉시 5xx 로 종결한다 — dev 의 edgequake 런처가 postgres 를 볼륨
    없이 재생성하면 큐가 통째로 사라지는데, 무한 대기하면 소비자가 상한까지 매달린다.
    """
    if inline and runner is not None:
        _run_inline(repo, blobs, job_id, runner)

    interval = float(_env_int("KBP_JOB_WAIT_POLL_INTERVAL_SECONDS", 2))
    deadline = time.monotonic() + timeout
    while True:
        row = repo.get(job_id)
        if row is None:
            raise HTTPException(status_code=500, detail="job row vanished")
        if row["status"] in TERMINAL:
            return row
        if time.monotonic() >= deadline:
            # 4xx 다. 504 로 내면 kb 가 5xx 재시도로 같은 요청을 다시 보내고,
            # 멱등키가 없어 /insert·/ingest 에서 중복 적재가 된다.
            raise HTTPException(
                status_code=409,
                detail={"message": "job still running; poll /jobs/{id}",
                        "job_id": str(job_id)},
            )
        time.sleep(interval)


def _run_inline(repo, blobs, job_id, runner) -> None:
    """테스트·dev 전용 — 제출 즉시 같은 프로세스에서 실행한다.

    프로덕션 코드 경로에는 이걸 켜는 env 나 기본값이 없다. `app.state.job_inline` 을
    테스트 fixture 가 세울 때만 동작한다.
    """
    worker_id = "inline"
    attempt = repo.start(job_id, worker_id=worker_id)
    job = repo.get(job_id)
    try:
        result = runner.run(job, worker_id=worker_id, attempt=attempt)
    except JobAborted:
        return
    except (JobFailed, JobRetryable) as exc:
        repo.complete(job_id, worker_id=worker_id, attempt=attempt,
                      status="failed", error=str(exc))
        return
    except Exception as exc:  # noqa: BLE001
        repo.complete(job_id, worker_id=worker_id, attempt=attempt,
                      status="failed", error=str(exc))
        return
    inline_result, ref = blobs.store_json(job_id, "result", result)
    repo.complete(job_id, worker_id=worker_id, attempt=attempt,
                  status="succeeded", result=inline_result, result_ref=ref)


def result_body(blobs, row) -> Any:
    """결과 본문을 복원한다. ``result_ref`` 가 있으면 **반드시** MinIO 에서 읽는다.

    복원 실패를 빈 본문으로 갈음하면 kb 가 `or ""` 로 조용히 흡수해서, 202 전환을
    포기하며 막으려던 '빈 문서 적재' 가 그대로 재현된다(§2.2).
    """
    try:
        body = blobs.load_json(row.get("result"), row.get("result_ref"))
    except Exception as exc:  # noqa: BLE001
        log.exception("failed to restore job result %s", row["id"])
        raise HTTPException(status_code=500,
                            detail=f"job result unavailable: {exc}") from exc
    if body is None:
        raise HTTPException(status_code=500, detail="job produced no result")
    return body


# ── 신규 잡 엔드포인트 ─────────────────────────────────────────────────────


@router.post("/jobs/parse", status_code=202)
async def submit_parse(
    file: UploadFile = File(...),
    content_type: str | None = Form(None),
    docs_id: str | None = Form(None),
    workspace_id: str | None = Form(None),
    batch_key: str | None = Form(None),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    repo=Depends(get_job_repo), blobs=Depends(get_job_blobs),
):
    from service.app import _safe_basename

    data = await file.read()
    payload = {
        "filename": _safe_basename(file.filename or "upload"),
        # 폼 필드가 없으면 멀티파트 파트 헤더로 폴백한다 — worker 에는 UploadFile 이
        # 없으므로 **접수 시점에 확정**해야 한다.
        "content_type": content_type or file.content_type,
        "docs_id": docs_id,
    }
    job_id = submit_job(
        repo, blobs, kind="parse", payload=payload, file_bytes=data,
        workspace_key=workspace_id, batch_key=batch_key,
        idem_key=derive_idem_key(kind="parse", payload=payload, file_bytes=data,
                                 explicit=idempotency_key, workspace_key=workspace_id),
    )
    return {"job_id": str(job_id), "status": "queued"}


@router.post("/jobs/chunk", status_code=202)
def submit_chunk(
    enriched_content: str | None = Body(None, embed=True),
    parse_job_id: str | None = Body(None, embed=True),
    doc_name: str = Body("", embed=True),
    page_spans: list | None = Body(None, embed=True),
    pages: list | None = Body(None, embed=True),
    table_blocks: list | None = Body(None, embed=True),
    methods: list | None = Body(None, embed=True),
    skip_scoring: bool = Body(False, embed=True),
    llm_regex_pattern: str | None = Body(None, embed=True),
    workspace_id: str | None = Body(None, embed=True),
    batch_key: str | None = Body(None, embed=True),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    repo=Depends(get_job_repo), blobs=Depends(get_job_blobs),
):
    if (enriched_content is None) == (parse_job_id is None):
        raise HTTPException(status_code=400,
                            detail="exactly one of enriched_content / parse_job_id")
    payload = {"enriched_content": enriched_content, "doc_name": doc_name,
               "page_spans": page_spans, "pages": pages, "table_blocks": table_blocks,
               "methods": methods, "skip_scoring": skip_scoring,
               "llm_regex_pattern": llm_regex_pattern}
    job_id = submit_job(
        repo, blobs, kind="chunk", payload=payload,
        workspace_key=workspace_id, batch_key=batch_key, parent_job_id=parse_job_id,
        idem_key=derive_idem_key(kind="chunk", payload=payload, file_bytes=None,
                                 explicit=idempotency_key, workspace_key=workspace_id,
                                 parent_job_id=parse_job_id),
    )
    return {"job_id": str(job_id), "status": "queued"}


@router.post("/jobs/insert", status_code=202)
def submit_insert(
    workspace_id: str = Body(..., embed=True),
    doc_id: str = Body(..., embed=True),
    chunks: list[str] | None = Body(None, embed=True),
    chunk_job_id: str | None = Body(None, embed=True),
    title: str = Body("", embed=True),
    extract_graph: bool = Body(True, embed=True),
    batch_key: str | None = Body(None, embed=True),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    repo=Depends(get_job_repo), blobs=Depends(get_job_blobs),
):
    if (chunks is None) == (chunk_job_id is None):
        raise HTTPException(status_code=400,
                            detail="exactly one of chunks / chunk_job_id")
    payload = {"workspace_id": workspace_id, "doc_id": doc_id, "chunks": chunks,
               "title": title, "extract_graph": extract_graph}
    job_id = submit_job(
        repo, blobs, kind="insert", payload=payload,
        workspace_key=workspace_id, batch_key=batch_key, parent_job_id=chunk_job_id,
        idem_key=derive_idem_key(kind="insert", payload=payload, file_bytes=None,
                                 explicit=idempotency_key, workspace_key=workspace_id,
                                 parent_job_id=chunk_job_id),
    )
    return {"job_id": str(job_id), "status": "queued"}


@router.post("/jobs/ingest", status_code=202)
async def submit_ingest(
    file: UploadFile = File(...),
    workspace_id: str = Form(...),
    doc_id: str = Form(...),
    content_type: str | None = Form(None),
    batch_key: str | None = Form(None),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    repo=Depends(get_job_repo), blobs=Depends(get_job_blobs),
):
    from service.app import _safe_basename

    data = await file.read()
    payload = {
        # ingest 의 filename 폴백은 parse 와 **다르다** — doc_id 로 떨어진다(현행 유지).
        "filename": _safe_basename(file.filename or doc_id),
        "content_type": content_type or file.content_type,
        "workspace_id": workspace_id, "doc_id": doc_id,
    }
    job_id = submit_job(
        repo, blobs, kind="ingest", payload=payload, file_bytes=data,
        workspace_key=workspace_id, batch_key=batch_key,
        idem_key=derive_idem_key(kind="ingest", payload=payload, file_bytes=data,
                                 explicit=idempotency_key, workspace_key=workspace_id),
    )
    return {"job_id": str(job_id), "status": "queued"}


# ── 조회 ───────────────────────────────────────────────────────────────────
#
# `/jobs/workers` 는 반드시 `/jobs/{job_id}` **앞에** 선언한다. FastAPI 는 선언 순서로
# 매칭하므로 뒤에 두면 workers 가 path-param 핸들러로 흡수된다.


@router.get("/jobs/workers")
def jobs_workers(repo=Depends(get_job_repo)):
    return repo.worker_stats()


@router.get("/jobs")
def list_jobs(
    workspace_id: str | None = Query(None), batch_key: str | None = Query(None),
    status: str | None = Query(None), kind: str | None = Query(None),
    limit: int = Query(100),
    repo=Depends(get_job_repo),
):
    rows = repo.list_jobs(workspace_key=workspace_id, batch_key=batch_key,
                          status=status, kind=kind, limit=limit)
    return {"jobs": [_public(repo, r) for r in rows]}


@router.get("/jobs/{job_id}")
def get_job(job_id: uuid.UUID, repo=Depends(get_job_repo)):
    row = repo.get(job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="job not found")
    return _public(repo, row)


@router.get("/jobs/{job_id}/result")
def get_job_result(job_id: uuid.UUID, repo=Depends(get_job_repo),
                   blobs=Depends(get_job_blobs)):
    row = repo.get(job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="job not found")
    if row["status"] not in TERMINAL:
        raise HTTPException(status_code=409, detail={"status": row["status"]})
    if row["status"] != "succeeded":
        raise HTTPException(status_code=422,
                            detail={"status": row["status"], "error": row["error"]})
    return result_body(blobs, row)


@router.delete("/jobs/{job_id}")
def cancel_job(job_id: uuid.UUID, repo=Depends(get_job_repo)):
    outcome = repo.cancel(job_id)
    if outcome == "canceled":
        return {"job_id": str(job_id), "status": "canceled"}
    if outcome == "running":
        return {"job_id": str(job_id), "status": "cancel_requested"}
    if repo.get(job_id) is None:
        raise HTTPException(status_code=404, detail="job not found")
    raise HTTPException(status_code=409, detail="job already finished")


def _public(repo, row) -> dict[str, Any]:
    """소비자에게 주는 잡 상태.

    `queue_position` 은 두지 않는다 — claim 은 kind 무관 전역 FIFO 스캔이고 승인은
    버킷·workspace·로컬 슬롯 3중 조건이라 "앞에 N건" 이 대기 시간을 예측하지 못한다.
    """
    queued = row["status"] == "queued"
    return {
        "id": str(row["id"]), "kind": row["kind"], "status": row["status"],
        "stage": row["stage"], "workspace_key": row["workspace_key"],
        "batch_key": row["batch_key"], "attempt_count": row["attempt_count"],
        "ahead_in_partition": repo.ahead_in_partition(row) if queued else None,
        "created_at": _iso(row.get("created_at")),
        "started_at": _iso(row.get("started_at")),
        "completed_at": _iso(row.get("completed_at")),
        "error": row["error"],
    }


def _iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else value
