"""Ingest pipeline: parse -> blockify -> modal-enrich -> edgequake.

``run_ingest`` returns an ``IngestOutcome`` dict with stable keys
``{document_id, chunk_count, status, detail}`` where ``status`` is
``"completed"`` or ``"failed"`` (the provider-contract values).

Edgequake's synchronous ``POST /api/v1/documents`` returns ``status``
``"processed"`` with a populated ``chunk_count`` (see UploadDocumentResponse /
text_upload.rs). We treat ``processed``/``completed``/``indexed`` as success as
long as ``chunk_count > 0``.
"""
from __future__ import annotations

from typing import Any, Callable

from service.parsing import parse_to_markdown, ParseError
from kb_pipeline.blockify import hybrid_to_blocks
from kb_pipeline.modal import enrich

#: edgequake sync-upload statuses that count as a successful index.
_OK_STATUSES = {"completed", "indexed", "processed"}

#: shinhan_trust default tenant (matches existing providers).
_TENANT_ID = "00000000-0000-0000-0000-000000000002"


def run_ingest(file_bytes: bytes, filename: str, *, workspace_id: str, doc_id: str,
               content_type: str | None, edgequake: Any,
               text_llm: Callable[[str, str], str], vision_llm: Callable[[str, str], str] | None,
               ocr_url: str, excel_url: str) -> dict:
    try:
        md = parse_to_markdown(file_bytes, filename, ocr_url=ocr_url, excel_url=excel_url)
        blocks = hybrid_to_blocks(md)
        enriched, _modal_ids = enrich(blocks, text_llm=text_llm, vision_llm=vision_llm)
    except Exception as e:  # noqa: BLE001  (ParseError is a subclass of Exception)
        return {"document_id": None, "chunk_count": 0, "status": "failed", "detail": f"front: {e}"}

    res = edgequake.post_document(enriched, workspace_id=workspace_id,
                                  tenant_id=_TENANT_ID, filename=filename)
    status = res.get("status")
    chunk_count = int(res.get("chunk_count", 0) or 0)
    ok = status in _OK_STATUSES and chunk_count > 0
    return {
        "document_id": res.get("document_id"),
        "chunk_count": chunk_count,
        "status": "completed" if ok else "failed",
        "detail": None if ok else f"edgequake status={status}",
    }
