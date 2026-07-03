"""parsers/ocr — 통파일 OCR elements → 페이지별 PageDoc, chunk_needed=True."""
import pytest
from parse_service.parsers import RouteResult, ParserError
from parse_service.parsers import ocr as ocr_parser


def test_elements_grouped_into_pages(monkeypatch):
    monkeypatch.setattr(ocr_parser, "_whole_file_elements",
                        lambda fb, fn, ocr_url: [
                            {"category": "text", "content": {"markdown": "a"}, "page_idx": 0},
                            {"category": "text", "content": {"markdown": "b"}, "page_idx": 1},
                        ])
    res = ocr_parser.parse(b"PK", "slide.pptx", ocr_url="http://ocr")
    assert isinstance(res, RouteResult)
    assert res.kind == "pages" and res.chunk_needed is True
    assert [p["page_number"] for p in res.pages] == [1, 2]  # 0-based → 1-based


def test_empty_elements_raise(monkeypatch):
    monkeypatch.setattr(ocr_parser, "_whole_file_elements", lambda fb, fn, ocr_url: [])
    with pytest.raises(ParserError):
        ocr_parser.parse(b"\x89PNG", "img.png", ocr_url="http://ocr")
