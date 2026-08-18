"""kordoc 네이티브 문서 파서 — HWP/HWPX/DOCX → Markdown + inline HTML 표."""
from __future__ import annotations

from parse_service.parsers import RouteResult, ParserError
from parse_service.tools import ToolError
from parse_service.tools.kordoc import convert_to_markdown


#: kordoc 4.9.0 이 공식적으로 받는 워드프로세서 포맷 중 이 도메인이 소유하는 확장자.
#: 구형 DOC 는 지원 목록에 없으므로 FileConverter → PDF 경로에 남긴다.
KORDOC_EXTS = {"hwp", "hwpx", "docx"}


def _to_markdown(file_bytes: bytes, filename: str) -> str:
    return convert_to_markdown(file_bytes, filename)


def parse(file_bytes: bytes, filename: str, **_) -> RouteResult:
    """kordoc Markdown을 구조 블록으로 변환한다.

    kordoc은 단순 표를 pipe Markdown으로, 병합 표를 ``rowspan``/``colspan``이 있는 inline
    HTML로 낼 수 있다. ``hybrid_to_blocks``는 전자를 HTML로 렌더하고 후자는 원문 그대로
    table block에 보존하므로 여기서 Markdown 전체를 다시 변환하거나 표를 치환하지 않는다.
    """
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
