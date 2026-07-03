"""parsers/excel — 자체청킹 결과를 facade 청크 스키마로, chunk_needed=False."""
import pytest
from parse_service.parsers import RouteResult, ParserError
from parse_service.parsers import excel as excel_parser


def test_chunks_normalized_and_flag_false(monkeypatch):
    rag = [{"content_text": "표1 내용", "title": "시트1", "path": ["시트1"]},
           {"content_text": "표2 내용", "title": "시트2", "path": ["시트2"]}]
    monkeypatch.setattr(excel_parser, "_fetch_rag_chunks", lambda fb, fn, excel_url: rag)
    res = excel_parser.parse(b"PK", "a.xlsx", excel_url="http://x")
    assert res.kind == "chunks" and res.chunk_needed is False
    assert res.chunks[0]["chunk_index"] == 0
    assert res.chunks[0]["text"] == "표1 내용"
    assert "titles_context" in res.chunks[0] and "pages" in res.chunks[0]


def test_empty_chunks_raise(monkeypatch):
    monkeypatch.setattr(excel_parser, "_fetch_rag_chunks", lambda fb, fn, excel_url: [])
    with pytest.raises(ParserError):
        excel_parser.parse(b"PK", "a.xlsx", excel_url="http://x")
