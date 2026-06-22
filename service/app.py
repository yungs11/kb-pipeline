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

from service.parsing import parse_to_markdown
from service.ingest import run_ingest, run_front, FrontError, _TENANT_ID
from service.edgequake import EdgequakeClient
from service.adaptive_chunk import AdaptiveChunkClient, MODAL_ATOMIC_MARKERS
from service.llm import get_text_llm
from kb_pipeline.community import build_workspace_communities

logger = logging.getLogger("kb_pipeline.service")

app = FastAPI(title="kb-pipeline")


def get_edgequake():
    return EdgequakeClient(os.environ.get("KBP_EDGEQUAKE_URL", "http://localhost:8081"))


def get_adaptive_chunk():
    return AdaptiveChunkClient(os.environ.get("KBP_ADAPTIVE_CHUNK_URL", "http://localhost:18060"))


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


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
async def ingest(file: UploadFile = File(...), workspace_id: str = Form(...), doc_id: str = Form(...),
                 content_type: str | None = Form(None), eq=Depends(get_edgequake)):
    data = await file.read()
    # The incoming workspace_id is the kb id; edgequake addresses storage by an
    # assigned workspace UUID, so register (idempotently) and use THAT uuid.
    eq_ws = eq.ensure_workspace(workspace_id, name=workspace_id)
    out = run_ingest(data, file.filename, workspace_id=eq_ws, doc_id=doc_id,
                     content_type=content_type or file.content_type, edgequake=eq,
                     text_llm=get_text_llm(), vision_llm=None,
                     ocr_url=os.environ.get("KBP_OCR_URL", "http://localhost:18050"),
                     excel_url=os.environ.get("KBP_EXCEL_URL", "http://localhost:18055"),
                     parse=parse_to_markdown)
    return out


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
