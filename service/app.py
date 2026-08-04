"""kb-pipeline FastAPI service (:19000).

Endpoints:
  * ``POST   /ingest``         multipart file + form ``workspace_id, doc_id, content_type?``
                               -> ``{document_id, chunk_count, status, detail}`` (BLOCKING;
                               parse-svc 위임 orchestration).
  * ``GET    /chunks``         query ``workspace_id, doc_id`` -> list of chunk rows
  * ``DELETE /doc``            query ``workspace_id, doc_id`` -> 204
  * ``GET    /healthz``        -> ``{status: "ok"}``

파싱은 전부 parse-svc(:19001) 소유 — facade 는 얇은 orchestration 만 한다(Phase 2d:
service/parsing.py·excel_parser_client.py·ingest.py 및 /ingest/submit,/ingest/status 제거).
"""
from __future__ import annotations

import logging
import os
import re

from contextlib import asynccontextmanager

from fastapi import (FastAPI, UploadFile, File, Form, Depends, BackgroundTasks,
                     Body, Header, HTTPException, Query)

from service.jobs.api import get_job_blobs as _job_blobs
from service.jobs.api import get_job_repo as _job_repo
from service.jobs.api import router as _jobs_router
from service.edgequake import EdgequakeClient
from service.adaptive_chunk import AdaptiveChunkClient, MODAL_ATOMIC_MARKERS
from service.parse_client import ParseSvcClient
from service.llm import get_text_llm
from kb_pipeline.community import build_workspace_communities

logger = logging.getLogger("kb_pipeline.service")

#: shinhan_trust default tenant (구 service/ingest.py 에서 이동 — Phase 2d).
_TENANT_ID = "00000000-0000-0000-0000-000000000002"

#: 〈MODAL …〉 open 마커(U+3008/U+3009). /chunk 응답 text 는 표시사본(chunks_meta)으로
#: 저장되므로 마커를 스트립한다(청킹 INPUT 은 마커 유지 — 원자화용).
_MODAL_OPEN_RE = re.compile(r"〈MODAL[^〉]*〉")


def _strip_modal(s: str) -> str:
    """원자경계 마커(〈MODAL…〉·〈/MODAL〉)만 제거, 내부(제목+raw table HTML+각주)는 보존."""
    return _MODAL_OPEN_RE.sub("", s.replace("〈/MODAL〉", ""))


def _safe_basename(name: str) -> str:
    import os
    import unicodedata

    base = os.path.basename((name or "").replace("\\", "/")).replace("\x00", "")
    # 경로 탈출·제어문자는 차단하되 한글·공백·괄호 등 정상 유니코드는 보존한다.
    # 이 이름은 parse-svc가 document_title을 만들 때 사용하므로 ASCII 치환 시
    # 검색 청크 제목이 `____`로 영구 오염된다.
    base = "".join(
        "_" if unicodedata.category(char).startswith("C") else char
        for char in base
    ) or "upload"
    if base in {".", ".."}:
        base = "upload"
    return "_" + base if base.startswith(".") else base

#: Startup visibility only. The gate itself reads the env **per request** (below) —
#: 모듈 스코프에 고정하면 값을 바꾸는 유일한 방법이 ``importlib.reload`` 인데, reload 는
#: ``app`` 객체를 갈아치워 다른 테스트 모듈이 든 참조를 무효화한다(실제로 그렇게 깨졌다:
#: 오버라이드가 옛 app 에 걸려 단위 테스트가 진짜 parse-svc·MinIO 를 때렸다).
_FACADE_KEY = os.environ.get("KBP_FACADE_KEY")

if _FACADE_KEY is None:
    logger.warning(
        "KBP_FACADE_KEY is unset — the facade X-Facade-Key gate is DISABLED "
        "(dev mode). Set KBP_FACADE_KEY in production to lock down stateful "
        "endpoints (/search, /insert, /insert/status, /ingest, /chunks, /doc, "
        "/communities/build)."
    )


def require_facade_key(x_facade_key: str | None = Header(default=None)):
    """Reject requests lacking a valid ``X-Facade-Key`` header (vs env key).

    키는 **요청 시점에** 읽는다. 모듈 스코프에 고정하면 값을 바꾸려면 모듈을 reload 해야
    하고, reload 는 ``app`` 객체를 새로 만들어 이미 그 객체를 import 해 둔 다른 테스트
    모듈의 ``dependency_overrides`` 를 통째로 무효화한다.

    ``KBP_FACADE_KEY`` 미설정이면 no-op(dev). 레거시 ``/parse``·``/chunk`` 는 stateless
    라 Phase 1 동안 게이트 대상이 아니다(신규 ``/jobs/*`` 는 대상).
    """
    key = os.environ.get("KBP_FACADE_KEY")
    if not key:
        return  # gate disabled (dev). 빈 문자열도 미설정과 동일 취급.
    if x_facade_key != key:
        raise HTTPException(status_code=401, detail="invalid or missing X-Facade-Key")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """DB 스키마·버킷 준비. **모듈 import 시점이 아니라 여기서** 한다.

    import 시점에 하면 테스트 수집만으로 DB 접속을 시도한다. postgres 가 늦게 뜨는 첫
    기동은 ``ensure_schema`` 의 백오프(≥120s)가 흡수하고, 그래도 실패하면 컨테이너
    ``restart: unless-stopped`` 가 자가치유한다.
    """
    dsn = os.environ.get("KBP_PG_DSN")
    if dsn:
        try:
            from service.jobs.blobs import JobBlobStore
            from service.jobs.schema import ensure_schema

            ensure_schema(dsn)
            JobBlobStore.from_env().check_bucket()
        except Exception:  # noqa: BLE001 - 로그를 남기고 기동은 계속(가시성 우선)
            logger.exception("job queue bootstrap failed; /jobs/* will error")
    else:
        logger.warning("KBP_PG_DSN unset — job queue disabled")
    yield


app = FastAPI(title="kb-pipeline", lifespan=lifespan)


def get_edgequake():
    return EdgequakeClient(os.environ.get("KBP_EDGEQUAKE_URL", "http://localhost:8081"))


def get_adaptive_chunk():
    return AdaptiveChunkClient(os.environ.get("KBP_ADAPTIVE_CHUNK_URL", "http://localhost:18060"))


def get_parse_client():
    # Multi-table PDFs make parse-svc call the modal LLM once per table (sequential),
    # so a 4-table doc can take ~400s+. Default the read timeout high (1800s) and allow
    # env override so the facade does not ReadTimeout before parse-svc finishes.
    return ParseSvcClient(
        os.environ.get("KBP_PARSE_SVC_URL", "http://localhost:19001"),
        timeout=float(os.environ.get("KBP_PARSE_SVC_TIMEOUT", "1800")),
    )


# ── 잡 큐 배선 ─────────────────────────────────────────────────────────────
#
# 레거시 4경로는 **응답 계약을 유지**하되 내부적으로 잡을 경유한다(설계 §0.2). 소비자
# kb 는 `raise_for_status()` 후 `body.get("enriched_content") or ""` 라 202 를 예외 없이
# 삼켜 빈 문서를 적재하기 때문이다. 유량제어는 이 경유만으로 이미 확보된다 —
# 다운스트림 호출이 worker 슬롯 안에서만 일어난다.


def _resolve(dep):
    """의존성 팩토리를 **오버라이드를 존중해서** 호출한다.

    `app.dependency_overrides` 는 FastAPI 가 `Depends(dep)` 를 해석할 때만 적용된다.
    함수를 그대로 넘기면 오버라이드가 무시돼, 테스트가 fake 대신 진짜 parse-svc 를
    때린다(실제로 그렇게 깨졌다).

    runner 에 `Depends(get_parse_client)` 3개를 직접 걸지 않는 이유는, 그러면 레거시
    요청마다 안 쓰는 httpx 클라이언트 2개가 함께 생성되기 때문이다. 지연 팩토리로
    두면 그 kind 가 실제로 쓰는 것 하나만 만들어진다.
    """
    return app.dependency_overrides.get(dep, dep)()


def _job_runner(repo=Depends(_job_repo), blobs=Depends(_job_blobs)):
    from service.jobs.runner import JobRunner

    return JobRunner(repo=repo, blobs=blobs,
                     parse_factory=lambda: _resolve(get_parse_client),
                     chunk_factory=lambda: _resolve(get_adaptive_chunk),
                     eq_factory=lambda: _resolve(get_edgequake))


def _legacy_job(repo, blobs, runner, *, kind, payload, file_bytes=None,
                workspace_key=None):
    """제출 → 완료까지 대기 → **현행과 동일한 본문**을 반환한다.

    waiter permit 을 **잡 생성 전에** 잡는다. 제출-후-거절이면 worker 는 그 잡을 정상
    실행하는데 kb 는 5xx 재시도로 두 번째 잡을 만든다(멱등키 없음 — D1).
    """
    from service.jobs import api as jobs_api

    with jobs_api._Permit():
        job_id = jobs_api.submit_job(repo, blobs, kind=kind, payload=payload,
                                     file_bytes=file_bytes,
                                     workspace_key=workspace_key, legacy=True)
        row = jobs_api.wait_for_job(
            repo, blobs, job_id,
            timeout=float(os.environ.get("KBP_JOB_LEGACY_WAIT_SECONDS", "3300")),
            runner=runner, inline=bool(getattr(app.state, "job_inline", False)),
        )
    if row["status"] == "succeeded":
        return jobs_api.result_body(blobs, row)
    if row["status"] == "canceled":
        raise HTTPException(status_code=409, detail="job canceled")
    # 현행에서도 다운스트림 예외는 500 으로 샜다 — 계약 유지.
    raise HTTPException(status_code=500, detail=row.get("error") or "job failed")


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


# 신규 잡 경로는 전부 X-Facade-Key 게이트 대상이다(파일 staging·DB 행·worker 시간을
# 소비하므로). 레거시 4경로의 인증 요구는 Phase 1 동안 바뀌지 않는다 — /parse·/chunk 는
# 계속 무인증이다(문서가 "의도적으로 열려 있다"고 명시하고 있고, kb 파사드 키가 미설정인
# 배포에서 게이트를 채우면 kb 가 즉시 401 을 맞는다).
app.include_router(_jobs_router, dependencies=[Depends(require_facade_key)])


@app.post("/parse")
async def parse(file: UploadFile = File(...), content_type: str | None = Form(None),
                docs_id: str | None = Form(None),
                repo=Depends(_job_repo), blobs=Depends(_job_blobs),
                runner=Depends(_job_runner)):
    """Parse one upload — 전부 parse-svc 위임(Phase 2a: excel 분기는 parse-svc 로 이동).

    parse-svc 가 확장자 라우팅(pdf/excel/ocr/폴백)을 소유하고 ``chunk_needed`` 를 내린다.
    excel(chunk_needed=false) 소비자 호환을 위해 ``chunk_strategy`` 를 재구성한다.

    ``docs_id`` (optional form) is the orchestrator's ``content_hash(file_bytes)[:16]``;
    when present it is forwarded to parse-svc so the page-image MinIO keys agree with
    the keys the orchestrator/UI assemble. The response passes through the additive
    page fields (``docs_id``/``page_count``/``pages``/``page_spans``) unchanged.
    """
    data = await file.read()
    payload = {
        "filename": _safe_basename(file.filename or "upload"),
        # 폼 필드가 없으면 파트 헤더로 폴백 — worker 에는 UploadFile 이 없으므로
        # **접수 시점에 확정**해서 payload 에 넣는다.
        "content_type": content_type or file.content_type,
        "docs_id": docs_id,
    }
    return _legacy_job(repo, blobs, runner, kind="parse", payload=payload,
                       file_bytes=data)


@app.post("/chunk")
def chunk(enriched_content: str = Body(..., embed=True),
          doc_name: str = Body("", embed=True),
          page_spans: list | None = Body(None, embed=True),
          pages: list | None = Body(None, embed=True),
          table_blocks: list | None = Body(None, embed=True),
          methods: list | None = Body(None, embed=True),
          skip_scoring: bool = Body(False, embed=True),
          llm_regex_pattern: str | None = Body(None, embed=True),
          repo=Depends(_job_repo), blobs=Depends(_job_blobs),
          runner=Depends(_job_runner)):
    """Chunk enriched content via the adaptive_chunk hub (hidden) and normalize.

    Value added (R5, not a bare forward):
      * forwards the modal markers as ``atomic_markers`` so each 〈MODAL…〈/MODAL〉
        span stays a single atomic chunk;
      * normalizes the hub's R1 chunk schema (``chunk_text``/``chunk_pages``) into
        the facade contract (``text``/``pages``), dropping internal fields;
      * surfaces the real selection rationale (method_selected/scores/
        methods_compared) for the UI's "why this chunker" card.

    ``page_spans`` (``[{page_number, char_start, char_end}]``) and the optional
    ``pages`` (``[{page_number, markdown}]``) are additive body fields forwarded to
    adaptive so each chunk gets a ``chunk_pages`` attribution. The R1
    ``chunk_pages``→``pages`` normalization (below) is unchanged.

    ``methods``/``skip_scoring``/``llm_regex_pattern`` are the chunk-method
    selection passthrough (all optional). They are forwarded to the hub's
    ``options`` (validation/semantics owned by adaptive_chunk). When unspecified
    (``methods=None``, ``skip_scoring=False``, ``llm_regex_pattern=None``) the hub
    runs its default auto behavior (every method competes, then scored/selected) —
    byte-identical to the legacy request (regression).
    """
    # table_blocks(facade contract) → adaptive 는 blocks 로 이름이 바뀐다. 변환·정규화·
    # 마커 스트립은 전부 runner 가 한다(현행과 동일한 본문을 돌려준다).
    payload = {"enriched_content": enriched_content, "doc_name": doc_name,
               "page_spans": page_spans, "pages": pages, "table_blocks": table_blocks,
               "methods": methods, "skip_scoring": skip_scoring,
               "llm_regex_pattern": llm_regex_pattern}
    return _legacy_job(repo, blobs, runner, kind="chunk", payload=payload)


@app.post("/search", dependencies=[Depends(require_facade_key)])
def search(workspace_id: str = Body(..., embed=True),
           query: str = Body(..., embed=True),
           top_k: int = Body(10, embed=True),
           eq=Depends(get_edgequake)):
    """Search a workspace via edgequake ``/api/v1/query`` (edgequake hidden).

    Value added (R5): resolves the kb id to the edgequake workspace UUID so the
    retrieval is workspace-scoped (isolation), maps ``top_k`` to edgequake's
    ``max_results``, and normalizes edgequake's ``sources`` into a stable
    ``results`` shape (chunk_id/text/score/document_id) plus the generated answer.
    """
    eq_ws = eq.ensure_workspace(workspace_id, name=workspace_id)
    res = eq.search(workspace_id=eq_ws, query=query, top_k=top_k)
    results = [
        {
            "chunk_id": src.get("id"),
            "text": src.get("snippet") or "",
            "score": src.get("score"),
            "document_id": src.get("document_id"),
        }
        for src in (res.get("sources") or [])
    ]
    return {"answer": res.get("answer"), "results": results}


@app.post("/insert", dependencies=[Depends(require_facade_key)])
def insert(workspace_id: str = Body(..., embed=True),
           doc_id: str = Body(..., embed=True),
           title: str = Body("", embed=True),
           chunks: list[str] = Body(..., embed=True),
           extract_graph: bool = Body(True, embed=True),
           repo=Depends(_job_repo), blobs=Depends(_job_blobs),
           runner=Depends(_job_runner)):
    """Insert pre-chunked texts into edgequake as a passthrough document.

    Value added (R5, policy ownership): the consumer hands a list of chunk texts
    and never touches edgequake — the facade resolves the kb id to the edgequake
    workspace UUID, joins the chunks with the U+001E passthrough separator, submits
    a passthrough document, and polls to terminal. Returns the stable
    ``{document_id, chunk_count, status}`` contract.
    """
    # 반환 필드(edgequake_workspace_id/entity_count/relationship_count/phases)는
    # runner 가 현행과 동일하게 성형한다.
    payload = {"workspace_id": workspace_id, "doc_id": doc_id, "chunks": chunks,
               "title": title, "extract_graph": extract_graph}
    return _legacy_job(repo, blobs, runner, kind="insert", payload=payload,
                       workspace_key=workspace_id)


@app.get("/insert/status", dependencies=[Depends(require_facade_key)])
def insert_status(workspace_id: str, doc_id: str, eq=Depends(get_edgequake)):
    """Relay the live edgequake phase for a passthrough insert (edgequake hidden).

    ``doc_id`` is the edgequake document_id returned by ``/insert``. Returns
    ``{phase, chunk_count, terminal, succeeded}`` from ``document_phase`` so the
    consumer's UI ticks without knowing edgequake's internal vocabulary.
    """
    eq_ws = eq.ensure_workspace(workspace_id, name=workspace_id)
    ph = eq.document_phase(eq_ws, doc_id)
    return {
        "phase": ph.get("phase"),
        "chunk_count": ph.get("chunk_count"),
        "terminal": ph.get("terminal"),
        "succeeded": ph.get("succeeded"),
    }


@app.post("/ingest", dependencies=[Depends(require_facade_key)])
async def ingest(file: UploadFile = File(...), workspace_id: str = Form(...),
                 doc_id: str = Form(...), content_type: str | None = Form(None),
                 repo=Depends(_job_repo), blobs=Depends(_job_blobs),
                 runner=Depends(_job_runner)):
    """End-to-end orchestration (parse→chunk→insert) for one-shot consumers.

    Value added (R5, orchestration ownership): drives the three capabilities in
    order so a consumer that doesn't want phase-by-phase control still gets the
    SAME result as the step-by-step path — including the real chunking selection
    rationale. Returns ``{document_id, chunk_count, status, chunking_selection}``.
    """
    data = await file.read()
    payload = {
        # ingest 의 filename 폴백은 parse 와 **다르다** — doc_id 로 떨어진다(현행 유지).
        "filename": _safe_basename(file.filename or doc_id),
        "content_type": content_type or file.content_type,
        "workspace_id": workspace_id, "doc_id": doc_id,
    }
    return _legacy_job(repo, blobs, runner, kind="ingest", payload=payload,
                       file_bytes=data, workspace_key=workspace_id)


@app.get("/chunks", dependencies=[Depends(require_facade_key)])
def chunks(workspace_id: str, doc_id: str, eq=Depends(get_edgequake)):
    eq_ws = eq.ensure_workspace(workspace_id, name=workspace_id)
    return eq.fetch_chunks(eq_ws, doc_id)


@app.delete("/doc", status_code=204, dependencies=[Depends(require_facade_key)])
def delete(workspace_id: str, doc_id: str, eq=Depends(get_edgequake)):
    eq_ws = eq.ensure_workspace(workspace_id, name=workspace_id)
    eq.delete_doc(eq_ws, doc_id)


def _build_communities_job(workspace_id: str) -> None:
    # W3 community build runs as a background task; never raise to the caller.
    try:
        build_workspace_communities(
            workspace_id, llm=get_text_llm(), dsn=os.environ["KBP_PG_DSN"]
        )
    except Exception:  # noqa: BLE001
        logger.exception("community build failed for workspace_id=%s", workspace_id)


@app.post("/communities/build", status_code=202,
          dependencies=[Depends(require_facade_key)])
def communities_build(workspace_id: str, background_tasks: BackgroundTasks,
                      eq=Depends(get_edgequake)):
    # Community graph rows are scoped by the edgequake workspace UUID (stored in node
    # properties), so resolve the kb id to that uuid for the DSN/workspace scope.
    eq_ws = eq.ensure_workspace(workspace_id, name=workspace_id)
    background_tasks.add_task(_build_communities_job, eq_ws)
    return {"status": "started", "workspace_id": eq_ws}
