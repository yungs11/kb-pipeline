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


def _fetch_rag_chunks(file_bytes: bytes, filename: str, excel_url: str | None = None) -> tuple[list[dict], dict]:
    """in-process: excel_parser_rag backend 직접 호출 (HTTP 제거).

    원본 excel-parser service/main.py:_run_parse 의 동기 파싱 본문 이식
    (임시파일 suffix, ParserConfig, get_backend(...).parse). backends 계약:
    get_backend(name).parse(input_path, config) -> (chunks: list[dict], stats: dict).
    excel_url 은 하위호환용 무시 파라미터(2e 에서 호출부와 함께 제거).

    반환: (raw_chunks, gate_summary) 튜플. gate_summary 는 raw 청크 + tmp_path 로
    in-process compute_gate_summary 로 계산(unlink 전). 계산 실패는 보수적 차단
    (spec §8 "추출 불가 = 적재 불가") 로 {"ok": False, "sheets": [], "error": ...}.
    """
    from parse_service.parsers.excel.excel_parser_rag.backends import get_backend
    from parse_service.parsers.excel.excel_parser_rag.config import ParserConfig
    from parse_service.parsers.excel.excel_parser_rag.gate import compute_gate_summary

    # 업로드 바이트는 안전한 임시 파일명으로 저장하지만, 파서가 그 임시 stem
    # (`excel_parser_…`)을 문서 제목으로 채택하면 청크 본문/검색어에 런타임 잡음이
    # 누출된다. :8600 excel-parser service 와 동일하게 원본 basename의 stem을
    # document_title로 명시한다. basename 정규화는 내부 직접 호출에도 경로 문자열이
    # 제목으로 섞이지 않게 하는 방어선이다.
    safe_filename = Path((filename or "upload.xlsx").replace("\x00", "")).name or "upload.xlsx"
    suffix = Path(safe_filename).suffix.lower() or ".xlsx"
    cfg_kwargs: dict = {
        "backend": os.environ.get("EXCEL_PARSER_BACKEND", "auto"),
        "document_title": Path(safe_filename).stem,
    }
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
        raw = [c if isinstance(c, dict) else c.__dict__ for c in (chunks or [])]
        try:
            gate_summary = compute_gate_summary(tmp_path, raw)
        except Exception as exc:  # noqa: BLE001 — gate 계산 실패는 보수적 차단(ok=False)
            gate_summary = {"ok": False, "sheets": [], "error": str(exc)}
        return raw, gate_summary
    finally:
        tmp_path.unlink(missing_ok=True)


def parse(file_bytes: bytes, filename: str, *, excel_url: str | None = None) -> RouteResult:
    try:
        rag_chunks, gate_summary = _fetch_rag_chunks(file_bytes, filename, excel_url)
    except ParserError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ParserError(f"excel parse failed for {filename}: {e}") from e
    chunks = normalize_chunks(rag_chunks)
    # 빈 청크 + 계산된 gate_summary 는 유효한 "깨진 엑셀 → 게이트가 reject" 결과다.
    # (예전엔 여기서 ParserError 를 던져 gate_summary 를 폐기했으나, 그러면 게이트가
    #  깨끗하게 reject 할 수 없다. raise 제거 — 다운스트림 게이트가 판정.)
    return RouteResult(kind="chunks", chunk_needed=False, chunks=chunks, gate_summary=gate_summary)
