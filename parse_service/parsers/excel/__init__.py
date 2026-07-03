"""Excel 도메인 파서 — parse+chunk 결합(자체청킹) → chunk_needed=False.

Phase 2b: excel_parser_rag 를 in-process 로 직접 호출(HTTP 제거).
env: EXCEL_PARSER_BACKEND(기본 auto), KORDOC_BIN(기본 미설정), KORDOC_MD_OUT.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from parse_service.parsers import RouteResult, ParserError

EXCEL_EXTS = {"xlsx", "xlsm", "xls"}


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


def _fetch_rag_chunks(file_bytes: bytes, filename: str, excel_url: str | None = None) -> list[dict]:
    """in-process: excel_parser_rag backend 직접 호출 (HTTP 제거).

    원본 excel-parser service/main.py:_run_parse 의 동기 파싱 본문 이식
    (임시파일 suffix, ParserConfig, get_backend(...).parse). backends 계약:
    get_backend(name).parse(input_path, config) -> (chunks: list[dict], stats: dict).
    excel_url 은 하위호환용 무시 파라미터(2e 에서 호출부와 함께 제거).
    """
    from parse_service.parsers.excel.excel_parser_rag.backends import get_backend
    from parse_service.parsers.excel.excel_parser_rag.config import ParserConfig

    suffix = Path(filename).suffix.lower() or ".xlsx"
    cfg_kwargs: dict = {"backend": os.environ.get("EXCEL_PARSER_BACKEND", "auto")}
    if os.environ.get("KORDOC_BIN"):
        cfg_kwargs["kordoc_bin"] = os.environ["KORDOC_BIN"]
    if os.environ.get("KORDOC_MD_OUT"):
        cfg_kwargs["kordoc_md_out"] = os.environ["KORDOC_MD_OUT"]
    config = ParserConfig(**cfg_kwargs)
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False, prefix="excel_parser_") as tmp:
        tmp.write(file_bytes)
        tmp_path = Path(tmp.name)
    try:
        chunks, _stats = get_backend(config.backend).parse(tmp_path, config)
        return [c if isinstance(c, dict) else c.__dict__ for c in (chunks or [])]
    finally:
        tmp_path.unlink(missing_ok=True)


def parse(file_bytes: bytes, filename: str, *, excel_url: str | None = None) -> RouteResult:
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
