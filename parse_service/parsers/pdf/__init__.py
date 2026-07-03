"""PDF 도메인 파서 — OpenDataLoader(도구) 페이지별 md → blocks. 스캔 페이지는 OCR 보충."""
from __future__ import annotations

import logging

from parse_service.parsers import RouteResult, ParserError
from parse_service.tools import ToolError
from parse_service.tools.opendataloader import convert_pdf_to_page_markdowns

log = logging.getLogger("kb_pipeline.parse_service.parsers.pdf")
_DIGITAL_MIN_CHARS = 1  # 기존 parsing.py 와 동일(보수적)


def _page_markdowns(file_bytes: bytes, filename: str) -> list[str]:
    return convert_pdf_to_page_markdowns(file_bytes, filename)


def _render_pages(file_bytes: bytes):
    from parse_service.pdf_pages import render_pdf_pages
    return render_pdf_pages(file_bytes)


def _ocr_elements_for_page(jpeg: bytes, name: str, ocr_url: str) -> list[dict]:
    # Phase 2a: 기존 HTTP OCR 재사용(parsing._ocr_page). 2c 에서 in-process 로 대체.
    from parse_service.parsing import _ocr_page
    return _ocr_page(jpeg, name, ocr_url=ocr_url)


def parse(file_bytes: bytes, filename: str, *, ocr_url: str) -> RouteResult:
    from kb_pipeline.blockify import hybrid_to_blocks, elements_to_blocks
    try:
        md_texts = _page_markdowns(file_bytes, filename)
    except ToolError as e:
        raise ParserError(str(e)) from e

    rendered = None
    pages: list[dict] = []
    for i, md in enumerate(md_texts):
        page_number = i + 1
        if len((md or "").strip()) >= _DIGITAL_MIN_CHARS:
            pages.append({"page_number": page_number,
                          "blocks": hybrid_to_blocks(md, page_idx=page_number)})
            continue
        if rendered is None:
            rendered = _render_pages(file_bytes)
        page_jpeg = next((rp.jpeg for rp in rendered if rp.page_number == page_number), None)
        if page_jpeg is None:
            log.warning("scanned page %d has no rendered image", page_number)
            pages.append({"page_number": page_number, "blocks": []})
            continue
        try:
            elements = _ocr_elements_for_page(page_jpeg, f"page-{page_number}.jpeg", ocr_url)
        except Exception:  # noqa: BLE001 — 페이지 단위 OCR 실패는 비치명
            log.exception("OCR failed for scanned page %d", page_number)
            pages.append({"page_number": page_number, "blocks": []})
            continue
        blocks = elements_to_blocks(elements)
        for b in blocks:
            b["page_idx"] = page_number
        pages.append({"page_number": page_number, "blocks": blocks})
    return RouteResult(kind="pages", chunk_needed=True, pages=pages)
