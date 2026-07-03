"""Excel 도메인 파서 — parse+chunk 결합(자체청킹) → chunk_needed=False.

Phase 2a: excel-rag-parser(:18055) HTTP 위임(잡 제출→폴링) — facade 의
service/excel_parser_client.py 로직을 이동. 2b 에서 excel_parser_rag in-process 로 대체.
"""
from __future__ import annotations

import time

import httpx

from parse_service.parsers import RouteResult, ParserError

EXCEL_EXTS = {"xlsx", "xlsm", "xls"}
_TERMINAL = {"succeeded", "failed", "cancelled"}
_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def normalize_rag_chunk(rc: dict, index: int) -> dict | None:
    """RagChunk dict → facade 청크 {chunk_index, text, titles_context, pages}. 빈 텍스트면 None.

    facade service/excel_parser_client.py:normalize_rag_chunk 본문 byte-identical 이동.
    """
    text = (rc.get("content_text") or rc.get("title") or "").strip()
    if not text:
        return None
    title = rc.get("title")
    path = rc.get("path") or []
    titles_context = path or ([title] if title else None)
    return {"chunk_index": index, "text": text, "titles_context": titles_context, "pages": []}


def normalize_chunks(rag_chunks: list[dict]) -> list[dict]:
    out: list[dict] = []
    for rc in rag_chunks or []:
        norm = normalize_rag_chunk(rc, len(out))
        if norm is not None:
            out.append(norm)
    return out


def _fetch_rag_chunks(file_bytes: bytes, filename: str, excel_url: str) -> list[dict]:
    """POST /parse/jobs/file → poll — facade ExcelRagParserClient.parse_chunks 이동."""
    base = excel_url.rstrip("/")
    with httpx.Client(timeout=600.0) as http:
        r = http.post(f"{base}/parse/jobs/file",
                      files={"file": (filename, file_bytes, _XLSX_MIME)},
                      data={"doc_name": filename})
        r.raise_for_status()
        job_id = (r.json() or {}).get("job_id")
        if not job_id:
            raise ParserError("excel-rag-parser returned no job_id")
        deadline = time.monotonic() + 1800.0
        while time.monotonic() < deadline:
            t = http.get(f"{base}/parse/jobs/{job_id}")
            t.raise_for_status()
            body = t.json() or {}
            status = (body.get("status") or "").lower()
            if status in _TERMINAL:
                if status != "succeeded":
                    raise ParserError(f"excel job {status}: {body.get('error')}")
                return (body.get("result") or {}).get("chunks") or []
            time.sleep(3.0)
        raise ParserError("excel job poll timeout")


def parse(file_bytes: bytes, filename: str, *, excel_url: str) -> RouteResult:
    try:
        rag_chunks = _fetch_rag_chunks(file_bytes, filename, excel_url)
    except ParserError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ParserError(f"excel parse failed for {filename}: {e}") from e
    chunks = normalize_chunks(rag_chunks)
    if not chunks:
        raise ParserError(f"excel produced no chunks for {filename}")
    return RouteResult(kind="chunks", chunk_needed=False, chunks=chunks)
