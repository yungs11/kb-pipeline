"""kb-pipeline FastAPI service (:19000).

Endpoints:
  * ``POST   /ingest``   multipart file + form ``workspace_id, doc_id, content_type?``
                         -> ``{document_id, chunk_count, status, detail}``
  * ``GET    /chunks``   query ``workspace_id, doc_id`` -> list of chunk rows
  * ``DELETE /doc``      query ``workspace_id, doc_id`` -> 204
  * ``GET    /healthz``  -> ``{status: "ok"}``
"""
from __future__ import annotations

import os

from fastapi import FastAPI, UploadFile, File, Form, Depends

from service.parsing import parse_to_markdown
from service.ingest import run_ingest
from service.edgequake import EdgequakeClient
from service.llm import get_text_llm

app = FastAPI(title="kb-pipeline")


def get_edgequake():
    return EdgequakeClient(os.environ.get("KBP_EDGEQUAKE_URL", "http://localhost:8081"))


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/ingest")
async def ingest(file: UploadFile = File(...), workspace_id: str = Form(...), doc_id: str = Form(...),
                 content_type: str | None = Form(None), eq=Depends(get_edgequake)):
    data = await file.read()
    out = run_ingest(data, file.filename, workspace_id=workspace_id, doc_id=doc_id,
                     content_type=content_type or file.content_type, edgequake=eq,
                     text_llm=get_text_llm(), vision_llm=None,
                     ocr_url=os.environ.get("KBP_OCR_URL", "http://localhost:18050"),
                     excel_url=os.environ.get("KBP_EXCEL_URL", "http://localhost:18055"),
                     parse=parse_to_markdown)
    return out


@app.get("/chunks")
def chunks(workspace_id: str, doc_id: str, eq=Depends(get_edgequake)):
    return eq.fetch_chunks(workspace_id, doc_id)


@app.delete("/doc", status_code=204)
def delete(workspace_id: str, doc_id: str, eq=Depends(get_edgequake)):
    eq.delete_doc(workspace_id, doc_id)
