"""DOCX 도메인 파서(+미지 확장자 폴백) — kordoc 네이티브. 병합표 <table> 보존."""
from __future__ import annotations

from parse_service.parsers import RouteResult, ParserError
from parse_service.tools import ToolError
from parse_service.tools.kordoc import convert_to_markdown


def _to_markdown(file_bytes: bytes, filename: str) -> str:
    return convert_to_markdown(file_bytes, filename)


def parse(file_bytes: bytes, filename: str, **_) -> RouteResult:
    from kb_pipeline.blockify import hybrid_to_blocks
    try:
        md = _to_markdown(file_bytes, filename)
    except ToolError as e:
        raise ParserError(str(e)) from e
    if not (md or "").strip():
        raise ParserError(f"kordoc produced empty markdown for {filename}")
    return RouteResult(kind="pages", chunk_needed=True,
                       pages=[{"page_number": 1,
                               "blocks": hybrid_to_blocks(md, page_idx=1)}])
