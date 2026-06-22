"""kb-pipeline FastAPI service (:19000).

Endpoints:
  * ``POST   /ingest``         multipart file + form ``workspace_id, doc_id, content_type?``
                               -> ``{document_id, chunk_count, status, detail}`` (BLOCKING).
  * ``POST   /ingest/submit``  same multipart inputs as /ingest -> ``{document_id,
                               status:"submitted"}`` (async; returns immediately for polling).
  * ``GET    /ingest/status``  query ``workspace_id, doc_id`` (doc_id = edgequake document_id
                               from submit) -> ``{phase, chunk_count, terminal, succeeded}``.
  * ``GET    /chunks``         query ``workspace_id, doc_id`` -> list of chunk rows
  * ``DELETE /doc``            query ``workspace_id, doc_id`` -> 204
  * ``GET    /healthz``        -> ``{status: "ok"}``
"""
from __future__ import annotations

import logging
import os

from fastapi import FastAPI, UploadFile, File, Form, Depends, BackgroundTasks, Body

from service.parsing import parse_to_markdown, _safe_basename
from service.ingest import run_front, FrontError, _TENANT_ID
from service.edgequake import EdgequakeClient
from service.adaptive_chunk import AdaptiveChunkClient, MODAL_ATOMIC_MARKERS
from service.parse_client import ParseSvcClient
from service.excel_parser_client import ExcelRagParserClient
from service.llm import get_text_llm
from kb_pipeline.community import build_workspace_communities

logger = logging.getLogger("kb_pipeline.service")

app = FastAPI(title="kb-pipeline")


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


_EXCEL_EXTS = {"xlsx", "xlsm", "xls"}


def _is_excel(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[-1].lower() in _EXCEL_EXTS


def get_excel_client():
    return ExcelRagParserClient(
        os.environ.get("KBP_EXCEL_URL", "http://localhost:18055"),
        timeout=float(os.environ.get("KBP_EXCEL_TIMEOUT", "1800")),
    )


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/parse")
async def parse(file: UploadFile = File(...), content_type: str | None = Form(None),
                pc=Depends(get_parse_client), ec=Depends(get_excel_client)):
    """Parse one upload. Excel → excel-rag-parser (parse+chunk, LLM-free); else → parse-svc.

    Excel returns native chunks (+ ``chunk_strategy``) so the caller skips adaptive /chunk.
    """
    data = await file.read()
    safe_name = _safe_basename(file.filename or "upload")
    if _is_excel(safe_name):
        chunks = ec.parse_chunks(file_bytes=data, filename=safe_name)
        return {
            "enriched_content": "\n\n".join(c.get("text", "") for c in chunks),
            "n_blocks": len(chunks),
            "modal_spans": [],
            "chunks": chunks,
            "chunk_strategy": "excel_rag_parser",
        }
    return pc.parse(file_bytes=data, filename=safe_name,
                    content_type=content_type or file.content_type)


@app.post("/chunk")
def chunk(enriched_content: str = Body(..., embed=True),
          doc_name: str = Body("", embed=True),
          ac=Depends(get_adaptive_chunk)):
    """Chunk enriched content via the adaptive_chunk hub (hidden) and normalize.

    Value added (R5, not a bare forward):
      * forwards the modal markers as ``atomic_markers`` so each 〈MODAL…〈/MODAL〉
        span stays a single atomic chunk;
      * normalizes the hub's R1 chunk schema (``chunk_text``/``chunk_pages``) into
        the facade contract (``text``/``pages``), dropping internal fields;
      * surfaces the real selection rationale (method_selected/scores/
        methods_compared) for the UI's "why this chunker" card.
    """
    res = ac.chunk(text=enriched_content, doc_name=doc_name,
                   atomic_markers=MODAL_ATOMIC_MARKERS)
    chunks = [
        {
            "chunk_index": ch.get("chunk_index"),
            "text": ch.get("chunk_text", ""),
            "titles_context": ch.get("titles_context"),
            "pages": ch.get("chunk_pages") or [],
        }
        for ch in (res.get("chunks") or [])
    ]
    return {
        "chunks": chunks,
        "method_selected": res.get("method_selected"),
        "scores": res.get("scores") or {},
        "methods_compared": res.get("methods_compared") or [],
    }


@app.post("/search")
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


@app.post("/insert")
def insert(workspace_id: str = Body(..., embed=True),
           doc_id: str = Body(..., embed=True),
           title: str = Body("", embed=True),
           chunks: list[str] = Body(..., embed=True),
           eq=Depends(get_edgequake)):
    """Insert pre-chunked texts into edgequake as a passthrough document.

    Value added (R5, policy ownership): the consumer hands a list of chunk texts
    and never touches edgequake — the facade resolves the kb id to the edgequake
    workspace UUID, joins the chunks with the U+001E passthrough separator, submits
    a passthrough document, and polls to terminal. Returns the stable
    ``{document_id, chunk_count, status}`` contract.
    """
    eq_ws = eq.ensure_workspace(workspace_id, name=workspace_id)
    res = eq.insert_chunks(workspace_id=eq_ws, tenant_id=_TENANT_ID,
                           title=title or doc_id, chunk_texts=chunks)
    return {
        "document_id": res.get("document_id"),
        "chunk_count": res.get("chunk_count"),
        "status": res.get("status"),
    }


@app.get("/insert/status")
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


@app.post("/ingest")
async def ingest(file: UploadFile = File(...), workspace_id: str = Form(...),
                 doc_id: str = Form(...), content_type: str | None = Form(None),
                 pc=Depends(get_parse_client), ac=Depends(get_adaptive_chunk),
                 eq=Depends(get_edgequake)):
    """End-to-end orchestration (parse→chunk→insert) for one-shot consumers.

    Value added (R5, orchestration ownership): drives the three capabilities in
    order so a consumer that doesn't want phase-by-phase control still gets the
    SAME result as the step-by-step path — including the real chunking selection
    rationale. Returns ``{document_id, chunk_count, status, chunking_selection}``.
    """
    data = await file.read()
    safe_name = _safe_basename(file.filename or doc_id)

    # 1) parse-svc → enriched content (modal markers embedded).
    parsed = pc.parse(file_bytes=data, filename=safe_name,
                      content_type=content_type or file.content_type)
    enriched = parsed.get("enriched_content", "")

    # 2) adaptive_chunk hub → chunks + real selection rationale. Modal spans are
    #    forwarded as atomic markers so each 〈MODAL…〈/MODAL〉 region stays one chunk.
    chunk_res = ac.chunk(text=enriched, doc_name=doc_id,
                         atomic_markers=MODAL_ATOMIC_MARKERS)
    chunk_texts = [ch.get("chunk_text", "") for ch in (chunk_res.get("chunks") or [])]
    chunking_selection = {
        "method_selected": chunk_res.get("method_selected"),
        "scores": chunk_res.get("scores") or {},
        "methods_compared": chunk_res.get("methods_compared") or [],
    }

    # 3) edgequake passthrough insert (kb id → workspace uuid; chunks joined with
    #    U+001E by the client; polled to terminal).
    eq_ws = eq.ensure_workspace(workspace_id, name=workspace_id)
    ins = eq.insert_chunks(workspace_id=eq_ws, tenant_id=_TENANT_ID,
                           title=doc_id, chunk_texts=chunk_texts)
    return {
        "document_id": ins.get("document_id"),
        "chunk_count": ins.get("chunk_count"),
        "status": ins.get("status"),
        "chunking_selection": chunking_selection,
    }


@app.post("/ingest/submit")
async def ingest_submit(file: UploadFile = File(...), workspace_id: str = Form(...),
                        doc_id: str = Form(...), content_type: str | None = Form(None),
                        eq=Depends(get_edgequake)):
    """POLLABLE ingest — run the FRONT (parse→blockify→modal) then submit ASYNC to
    edgequake and return immediately with the edgequake ``document_id``. The caller
    then polls ``GET /ingest/status`` to observe the live per-phase progress.

    Returns ``{document_id, status:"submitted"}`` on success, or
    ``{status:"failed", detail}`` if the front fails (parse/blockify/modal).
    """
    data = await file.read()
    eq_ws = eq.ensure_workspace(workspace_id, name=workspace_id)
    try:
        enriched = run_front(
            data, file.filename,
            text_llm=get_text_llm(), vision_llm=None,
            ocr_url=os.environ.get("KBP_OCR_URL", "http://localhost:18050"),
            excel_url=os.environ.get("KBP_EXCEL_URL", "http://localhost:18055"),
            parse=parse_to_markdown,
        )
    except FrontError as exc:
        return {"status": "failed", "detail": exc.detail}
    res = eq.submit_document(enriched, workspace_id=eq_ws, tenant_id=_TENANT_ID,
                             filename=file.filename)
    return {"document_id": res.get("document_id"), "status": "submitted"}


@app.get("/ingest/status")
def ingest_status(workspace_id: str, doc_id: str, eq=Depends(get_edgequake)):
    """Live phase snapshot for a submitted document. ``doc_id`` is the edgequake
    document_id returned by ``/ingest/submit``. Returns
    ``{phase, chunk_count, terminal, succeeded}``."""
    eq_ws = eq.ensure_workspace(workspace_id, name=workspace_id)
    ph = eq.document_phase(eq_ws, doc_id)
    return {
        "phase": ph.get("phase"),
        "chunk_count": ph.get("chunk_count"),
        "terminal": ph.get("terminal"),
        "succeeded": ph.get("succeeded"),
    }


@app.get("/chunks")
def chunks(workspace_id: str, doc_id: str, eq=Depends(get_edgequake)):
    eq_ws = eq.ensure_workspace(workspace_id, name=workspace_id)
    return eq.fetch_chunks(eq_ws, doc_id)


@app.delete("/doc", status_code=204)
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


@app.post("/communities/build", status_code=202)
def communities_build(workspace_id: str, background_tasks: BackgroundTasks,
                      eq=Depends(get_edgequake)):
    # Community graph rows are scoped by the edgequake workspace UUID (stored in node
    # properties), so resolve the kb id to that uuid for the DSN/workspace scope.
    eq_ws = eq.ensure_workspace(workspace_id, name=workspace_id)
    background_tasks.add_task(_build_communities_job, eq_ws)
    return {"status": "started", "workspace_id": eq_ws}
