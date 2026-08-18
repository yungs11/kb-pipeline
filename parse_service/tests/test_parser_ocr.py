"""parsers/ocr — 통파일 OCR elements → 페이지별 PageDoc, chunk_needed=True."""
import pytest
from parse_service.parsers import RouteResult, ParserError
from parse_service.parsers import ocr as ocr_parser
from parse_service.parsers.ocr.vl_api import VLCallMeta


def test_elements_grouped_into_pages(monkeypatch):
    monkeypatch.setattr(ocr_parser, "_whole_file_elements",
                        lambda fb, fn, ocr_url, prompt_override=None, with_meta=False: ([
                            {"category": "text", "content": {"markdown": "a"}, "page_idx": 0},
                            {"category": "text", "content": {"markdown": "b"}, "page_idx": 1},
                        ], [VLCallMeta(), VLCallMeta()]))
    res = ocr_parser.parse(b"PK", "diagram.png", ocr_url="http://ocr")
    assert isinstance(res, RouteResult)
    assert res.kind == "pages" and res.chunk_needed is True
    assert [p["page_number"] for p in res.pages] == [1, 2]  # 0-based → 1-based


def test_page_traces_record_single_vl_direct_lane(monkeypatch):
    """이미지 도메인도 admin 로그 화면에 뜨려면 page_traces가 있어야 한다."""
    monkeypatch.setattr(ocr_parser, "_whole_file_elements",
                        lambda fb, fn, ocr_url, prompt_override=None, with_meta=False: ([
                            {"category": "text", "content": {"markdown": "hello"}, "page_idx": 0},
                        ], [VLCallMeta(elapsed=0.15)]))
    res = ocr_parser.parse(b"\x89PNG", "photo.jpg", ocr_url="http://ocr")
    assert res.page_traces is not None
    assert len(res.page_traces) == 1
    t = res.page_traces[0]
    assert t["page_number"] == 1
    assert t["lane"] == "vl_ocr_direct"
    assert t["source"] == "vl"
    assert t["chars"] > 0
    assert t["attempts"][0][0] == "route"
    assert t["processing_ms"] == 150.0


def test_page_traces_processing_ms_none_without_call_meta(monkeypatch):
    """call_metas가 없거나 개수가 안 맞으면 방어적으로 processing_ms=None."""
    monkeypatch.setattr(ocr_parser, "_whole_file_elements",
                        lambda fb, fn, ocr_url, prompt_override=None, with_meta=False: ([
                            {"category": "text", "content": {"markdown": "hello"}, "page_idx": 0},
                        ], []))
    res = ocr_parser.parse(b"\x89PNG", "photo.jpg", ocr_url="http://ocr")
    assert res.page_traces[0]["processing_ms"] is None


def test_empty_elements_raise(monkeypatch):
    monkeypatch.setattr(ocr_parser, "_whole_file_elements",
                        lambda fb, fn, ocr_url, prompt_override=None, with_meta=False: ([], []))
    with pytest.raises(ParserError):
        ocr_parser.parse(b"\x89PNG", "img.png", ocr_url="http://ocr")
