"""parsers/pdf — 페이지 보존 + 스캔페이지 OCR 보충 + chunk_needed=True."""
import pytest
from parse_service.parsers import RouteResult, ParserError
from parse_service.parsers import pdf as pdf_parser


def test_digital_pdf_pages(monkeypatch):
    monkeypatch.setattr(pdf_parser, "_page_markdowns",
                        lambda fb, fn: ["# p1 text", "# p2 text"])
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert isinstance(res, RouteResult)
    assert res.kind == "pages" and res.chunk_needed is True
    assert [p["page_number"] for p in res.pages] == [1, 2]
    assert res.pages[0]["blocks"][0]["page_idx"] == 1


def test_scanned_page_gets_ocr(monkeypatch):
    # p2 가 빈 md → 렌더+OCR 보충 경로. 렌더/OCR 를 fake 로.
    monkeypatch.setattr(pdf_parser, "_page_markdowns", lambda fb, fn: ["# p1", "   "])
    class FakeRP:  # render_pdf_pages 반환 원소 흉내
        page_number, jpeg = 2, b"jpegbytes"
    monkeypatch.setattr(pdf_parser, "_render_pages", lambda fb: [FakeRP()])
    monkeypatch.setattr(pdf_parser, "_ocr_elements_for_page",
                        lambda jpeg, name, ocr_url: [
                            {"category": "text", "content": {"markdown": "ocr text"}, "page": 0}
                        ])
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert res.pages[1]["page_number"] == 2
    assert res.pages[1]["blocks"], "OCR 보충 블록이 있어야"
    assert res.pages[1]["blocks"][0]["page_idx"] == 2


def test_tool_error_becomes_parser_error(monkeypatch):
    from parse_service.tools import ToolError
    def boom(fb, fn):
        raise ToolError("no md")
    monkeypatch.setattr(pdf_parser, "_page_markdowns", boom)
    with pytest.raises(ParserError):
        pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
