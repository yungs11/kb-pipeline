"""parsers/ocr — 통파일 OCR elements → 페이지별 PageDoc, chunk_needed=True."""
import pytest
from parse_service.parsers import RouteResult, ParserError
from parse_service.parsers import ocr as ocr_parser


def test_elements_grouped_into_pages(monkeypatch):
    monkeypatch.setattr(ocr_parser, "_whole_file_elements",
                        lambda fb, fn, ocr_url, prompt_override=None: [
                            {"category": "text", "content": {"markdown": "a"}, "page_idx": 0},
                            {"category": "text", "content": {"markdown": "b"}, "page_idx": 1},
                        ])
    res = ocr_parser.parse(b"PK", "diagram.png", ocr_url="http://ocr")
    assert isinstance(res, RouteResult)
    assert res.kind == "pages" and res.chunk_needed is True
    assert [p["page_number"] for p in res.pages] == [1, 2]  # 0-based → 1-based


def test_page_traces_record_single_vl_direct_lane(monkeypatch):
    """2026-08-18 — 이미지/pptx 도메인도 admin 로그 화면에 뜨려면 page_traces가 있어야 한다."""
    monkeypatch.setattr(ocr_parser, "_whole_file_elements",
                        lambda fb, fn, ocr_url, prompt_override=None: [
                            {"category": "text", "content": {"markdown": "hello"}, "page_idx": 0},
                        ])
    res = ocr_parser.parse(b"\x89PNG", "photo.jpg", ocr_url="http://ocr")
    assert res.page_traces is not None
    assert len(res.page_traces) == 1
    t = res.page_traces[0]
    assert t["page_number"] == 1
    assert t["lane"] == "vl_ocr_direct"
    assert t["source"] == "vl"
    assert t["chars"] > 0
    assert t["attempts"][0][0] == "route"


def test_empty_elements_raise(monkeypatch):
    monkeypatch.setattr(ocr_parser, "_whole_file_elements", lambda fb, fn, ocr_url, prompt_override=None: [])
    with pytest.raises(ParserError):
        ocr_parser.parse(b"\x89PNG", "img.png", ocr_url="http://ocr")
