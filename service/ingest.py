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

import logging
from typing import Any, Callable

from service.parsing import parse_to_markdown, ParseError
from kb_pipeline.blockify import hybrid_to_blocks
from kb_pipeline.modal import enrich

log = logging.getLogger(__name__)

#: edgequake sync-upload statuses that count as a successful index.
_OK_STATUSES = {"completed", "indexed", "processed"}

#: shinhan_trust default tenant (matches existing providers).
_TENANT_ID = "00000000-0000-0000-0000-000000000002"


class FrontError(Exception):
    """Front-end (parse→blockify→modal) failed. ``detail`` is the stable reason
    string ("parse_failed" / "internal_error") the caller maps into a failure
    response."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def run_front(file_bytes: bytes, filename: str, *,
              text_llm: Callable[[str, str], str], vision_llm: Callable[[str, str], str] | None,
              ocr_url: str, excel_url: str,
              parse: Callable[..., str] | None = None) -> str:
    """Run the ingest FRONT: parse → blockify → modal-enrich → enriched content str.

    Factored out of ``run_ingest`` so both the blocking (``run_ingest`` →
    ``post_document``) and the pollable (``/ingest/submit`` → ``submit_document``)
    paths share the exact same front. Raises ``FrontError(detail)`` on failure
    (``parse_failed`` for a ParseError, ``internal_error`` otherwise) so the caller
    decides the response shape.
    """
    # ``parse`` lets the caller inject (and tests patch) the parser used; it
    # defaults to this module's ``parse_to_markdown``.
    parse = parse or parse_to_markdown
    try:
        md = parse(file_bytes, filename, ocr_url=ocr_url, excel_url=excel_url)
        blocks = hybrid_to_blocks(md)
        enriched, _modal_ids = enrich(blocks, text_llm=text_llm, vision_llm=vision_llm)
    except ParseError:
        log.exception("parse failed for %s", filename)
        raise FrontError("parse_failed")
    except Exception:  # noqa: BLE001
        log.exception("ingest front-end failed for %s", filename)
        raise FrontError("internal_error")
    return enriched


def run_ingest(file_bytes: bytes, filename: str, *, workspace_id: str, doc_id: str,
               content_type: str | None, edgequake: Any,
               text_llm: Callable[[str, str], str], vision_llm: Callable[[str, str], str] | None,
               ocr_url: str, excel_url: str,
               parse: Callable[..., str] | None = None) -> dict:
    try:
        enriched = run_front(file_bytes, filename, text_llm=text_llm, vision_llm=vision_llm,
                             ocr_url=ocr_url, excel_url=excel_url, parse=parse)
    except FrontError as exc:
        return {"document_id": None, "chunk_count": 0, "status": "failed", "detail": exc.detail}

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
