"""확장자 → 도메인 파서 디스패치. 파싱 로직 없음(얇은 계층).

Phase 2a 매핑(동작 보존): pdf→pdf, 엑셀→excel(chunk_needed=False),
pptx/docx/이미지→ocr, 그 외→markitdown 폴백(임시 — 2d 에서 kordoc 로 교체).
"""
from __future__ import annotations

from parse_service.parsers import RouteResult, ParserError
from parse_service.parsers import pdf as _pdf
from parse_service.parsers import ocr as _ocr
from parse_service.parsers import excel as _excel


def _fallback_parse(file_bytes: bytes, filename: str, *, ocr_url: str, excel_url: str) -> RouteResult:
    # 임시(2a): 기존 markitdown 경로 보존 — 단일 페이지 강등. 2d 에서 kordoc 폴백으로 교체.
    from kb_pipeline.blockify import hybrid_to_blocks
    from parse_service.parsing import _parse_markitdown
    md = _parse_markitdown(file_bytes, filename)
    return RouteResult(kind="pages", chunk_needed=True,
                       pages=[{"page_number": 1, "blocks": hybrid_to_blocks(md, page_idx=1)}])


def _pdf_parse(fb, fn, *, ocr_url, excel_url):
    return _pdf.parse(fb, fn, ocr_url=ocr_url)


def _ocr_parse(fb, fn, *, ocr_url, excel_url):
    return _ocr.parse(fb, fn, ocr_url=ocr_url)


def _excel_parse(fb, fn, *, ocr_url, excel_url):
    return _excel.parse(fb, fn, excel_url=excel_url)


_PARSERS = {"pdf": _pdf_parse, "excel": _excel_parse, "ocr": _ocr_parse,
            "fallback": _fallback_parse}


def _domain(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext == "pdf":
        return "pdf"
    if ext in _excel.EXCEL_EXTS:
        return "excel"
    if ext in ({"pptx", "docx"} | _ocr.IMAGE_EXTS):
        return "ocr"
    return "fallback"


def route(file_bytes: bytes, filename: str, *, ocr_url: str, excel_url: str) -> RouteResult:
    return _PARSERS[_domain(filename)](file_bytes, filename,
                                       ocr_url=ocr_url, excel_url=excel_url)
