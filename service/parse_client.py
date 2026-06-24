"""Thin HTTP client for parse-svc (:19001).

The facade ``/parse`` endpoint uses this client to delegate parsing to parse-svc
while keeping the heavy parser fleet (java/OpenDataLoader/markitdown/OCR) hidden
behind the facade contract. parse-svc's ``POST /parse`` returns
``{enriched_content, n_blocks, modal_spans:[{id,type,char_range}]}``.
"""
from __future__ import annotations

import httpx


class ParseSvcClient:
    def __init__(self, base_url: str, timeout: float = 600.0):
        self.base = base_url.rstrip("/")
        self.http = httpx.Client(timeout=timeout)

    def parse(self, *, file_bytes: bytes, filename: str,
              content_type: str | None = None,
              docs_id: str | None = None) -> dict:
        """POST the upload to parse-svc ``/parse`` and return its response dict.

        Sends the raw bytes as the multipart ``file`` part (with ``filename`` and
        ``content_type``) plus ``filename`` as a form field (parse-svc reads it to
        route + sanitize). When ``docs_id`` is supplied (the orchestrator's
        ``content_hash(file_bytes)[:16]``) it is forwarded as a form field so
        parse-svc keys its page-image uploads to the SAME ``docs_id`` (key-rule
        agreement); when omitted parse-svc derives the fallback itself. The raw
        parse-svc response is returned unchanged — including the additive page
        fields (``docs_id``/``page_count``/``pages``/``page_spans``) when present.
        """
        data: dict[str, str] = {"filename": filename}
        if docs_id is not None:
            data["docs_id"] = docs_id
        r = self.http.post(
            f"{self.base}/parse",
            files={"file": (filename, file_bytes,
                            content_type or "application/octet-stream")},
            data=data,
        )
        r.raise_for_status()
        return r.json() or {}
