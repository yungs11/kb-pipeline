"""/parse 가 RouteResult.kind 에 따라 chunk_needed 를 응답에 싣는다."""
from fastapi.testclient import TestClient
import parse_service.app as appmod
from parse_service.parsers import RouteResult


def _client():
    return TestClient(appmod.app)


def test_pages_path_sets_chunk_needed_true(monkeypatch):
    monkeypatch.setattr(appmod, "_route",
        lambda fb, fn, **kw: RouteResult(kind="pages", chunk_needed=True, pages=[
            {"page_number": 1, "blocks": [{"type": "text", "text": "hello", "page_idx": 1}]}]))
    r = _client().post("/parse", files={"file": ("a.pdf", b"%PDF")},
                       data={"filename": "a.pdf"})
    assert r.status_code == 200
    body = r.json()
    assert body["chunk_needed"] is True
    assert "enriched_content" in body


def test_chunks_path_sets_chunk_needed_false(monkeypatch):
    monkeypatch.setattr(appmod, "_route",
        lambda fb, fn, **kw: RouteResult(kind="chunks", chunk_needed=False, chunks=[
            {"chunk_index": 0, "text": "표1", "titles_context": ["s1"], "pages": []}]))
    r = _client().post("/parse", files={"file": ("a.xlsx", b"PK")},
                       data={"filename": "a.xlsx"})
    assert r.status_code == 200
    body = r.json()
    assert body["chunk_needed"] is False
    assert body["chunks"][0]["text"] == "표1"
    assert body["modal_spans"] == []
