"""PDF 도메인 파서 — OpenDataLoader(도구) 페이지별 md → blocks. 스캔 페이지는 OCR 보충."""
from __future__ import annotations

import logging
import re

from parse_service.parsers import RouteResult, ParserError
from parse_service.tools import ToolError
from parse_service.tools.opendataloader import convert_pdf_to_page_markdowns

log = logging.getLogger("kb_pipeline.parse_service.parsers.pdf")
# digital(=텍스트 추출 성공) 판정 최소 **실제 텍스트** 글자수. 스캔 페이지도 ODL 이
# 이미지 참조/빈 표 구조를 non-empty markdown 으로 내므로, raw 길이가 아니라 태그·이미지
# 참조를 뺀 실 텍스트로 판정해야 스캔 페이지가 VL 로 넘어간다(2026-07-07 버그수정).
_DIGITAL_MIN_CHARS = 1
_HTML_TAG_RE = re.compile(r"<[^>]+>")   # <table>/<td> 등 (빈 표는 태그만 → 실텍스트 0)
_WS_RE = re.compile(r"\s+")


def _digital_text_len(md: str) -> int:
    """페이지 markdown 의 **실제 텍스트** 글자수 — 이미지 참조 줄/HTML 태그/공백 제외.

    OpenDataLoader 는 스캔 페이지에도 `![alt](path)` 이미지 참조나 빈 표
    (`<table><td> </td></table>`)를 non-empty 로 낸다. 이를 실텍스트로 세지 않아야
    그런 페이지가 digital 로 오판되지 않고 VL(OCR) 로 넘어간다. 이미지 참조는 경로에
    `)` 가 들어갈 수 있어(한글 파일명 등) 정규식 대신 **줄 단위**로 제거한다.
    """
    kept = [ln for ln in (md or "").splitlines() if not ln.lstrip().startswith("![")]
    stripped = _HTML_TAG_RE.sub("", "\n".join(kept))
    return len(_WS_RE.sub("", stripped))


def _page_markdowns(file_bytes: bytes, filename: str) -> list[str]:
    return convert_pdf_to_page_markdowns(file_bytes, filename)


def _render_pages(file_bytes: bytes):
    from parse_service.pdf_pages import render_pdf_pages
    return render_pdf_pages(file_bytes)


def _ocr_elements_for_page(jpeg: bytes, name: str, ocr_url: str | None = None) -> list[dict]:
    # Phase 2c: in-process VL OCR (HTTP 제거).
    from parse_service.parsers.ocr import ocr_elements_sync
    return ocr_elements_sync(jpeg, name)


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
        if _digital_text_len(md) >= _DIGITAL_MIN_CHARS:
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
