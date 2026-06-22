"""Thin HTTP client for excel-rag-parser (:18055) — the facade's Excel chunk strategy.

The facade routes Excel uploads here instead of parse-svc+adaptive_chunk. excel-rag-parser
parses AND chunks the workbook region-by-region (LLM-free) and returns RagChunk dicts; this
client polls its async job and normalizes RagChunks into the facade chunk contract.
"""
from __future__ import annotations

import time

import httpx

_OK = "succeeded"
_FAIL = ("failed", "cancelled")
_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def normalize_rag_chunk(rc: dict, index: int) -> dict | None:
    """RagChunk dict → facade 청크 {chunk_index, text, titles_context, pages}. 빈 텍스트면 None."""
    text = (rc.get("content_text") or rc.get("title") or "").strip()
    if not text:
        return None
    title = rc.get("title")
    path = rc.get("path") or []
    titles_context = path or ([title] if title else None)
    return {"chunk_index": index, "text": text, "titles_context": titles_context, "pages": []}


def normalize_chunks(rag_chunks: list[dict]) -> list[dict]:
    out: list[dict] = []
    for rc in rag_chunks:
        norm = normalize_rag_chunk(rc, len(out))
        if norm is not None:
            out.append(norm)
    return out


class ExcelRagParserClient:
    def __init__(self, base_url: str, timeout: float = 600.0,
                 poll_timeout: float = 1800.0, poll_interval: float = 2.0):
        self.base = base_url.rstrip("/")
        self.http = httpx.Client(timeout=timeout)
        self.poll_timeout = poll_timeout
        self.poll_interval = poll_interval

    def parse_chunks(self, *, file_bytes: bytes, filename: str) -> list[dict]:
        """POST /parse/jobs/file → poll /parse/jobs/{id} → 정규화된 facade 청크."""
        r = self.http.post(
            f"{self.base}/parse/jobs/file",
            files={"file": (filename, file_bytes, _XLSX_MIME)},
            data={"doc_name": filename},
        )
        r.raise_for_status()
        job_id = (r.json() or {}).get("job_id")
        if not job_id:
            raise RuntimeError("excel-rag-parser POST /parse/jobs/file returned no job_id")
        deadline = time.monotonic() + self.poll_timeout
        while True:
            t = self.http.get(f"{self.base}/parse/jobs/{job_id}")
            t.raise_for_status()
            tj = t.json() or {}
            status = (tj.get("status") or "").lower()
            if status == _OK:
                return normalize_chunks((tj.get("result") or {}).get("chunks") or [])
            if status in _FAIL:
                raise RuntimeError(f"excel-rag-parser job {status}: {tj.get('error')}")
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"excel-rag-parser poll timeout after {self.poll_timeout:.0f}s (last={status})"
                )
            time.sleep(self.poll_interval)
