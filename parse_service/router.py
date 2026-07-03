"""확장자 → 도메인 파서 디스패치. 파싱 로직 없음(얇은 계층).

Phase 2d 매핑: pdf→pdf(ODL), 엑셀→excel(자체청킹, chunk_needed=False),
docx→docx(kordoc), pptx/이미지→ocr(in-process VL), 그 외 폴백→docx(kordoc).
markitdown 폴백은 제거됨(Phase 2d).
"""
from __future__ import annotations

from parse_service.parsers import RouteResult, ParserError
from parse_service.parsers import pdf as _pdf
from parse_service.parsers import ocr as _ocr
from parse_service.parsers import excel as _excel
from parse_service.parsers import docx as _docx


def _pdf_parse(fb, fn, *, ocr_url, excel_url):
    return _pdf.parse(fb, fn, ocr_url=ocr_url)


def _ocr_parse(fb, fn, *, ocr_url, excel_url):
    return _ocr.parse(fb, fn, ocr_url=ocr_url)


def _excel_parse(fb, fn, *, ocr_url, excel_url):
    return _excel.parse(fb, fn, excel_url=excel_url)


def _docx_parse(fb, fn, **kw):
    return _docx.parse(fb, fn)


_PARSERS = {"pdf": _pdf_parse, "excel": _excel_parse, "ocr": _ocr_parse,
            "docx": _docx_parse, "fallback": _docx_parse}   # 폴백 = kordoc


def _domain(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext == "pdf":
        return "pdf"
    if ext in _excel.EXCEL_EXTS:
        return "excel"
    if ext == "docx":
        return "docx"
    if ext in ({"pptx"} | _ocr.IMAGE_EXTS):
        return "ocr"
    return "fallback"


def route(file_bytes: bytes, filename: str, *, ocr_url: str, excel_url: str) -> RouteResult:
    return _PARSERS[_domain(filename)](file_bytes, filename,
                                       ocr_url=ocr_url, excel_url=excel_url)
