from service.excel_parser_client import normalize_rag_chunk, normalize_chunks


def test_normalize_drops_empty_text():
    assert normalize_rag_chunk({"content_text": "   "}, 0) is None
    assert normalize_rag_chunk({"content_text": "", "title": None}, 0) is None


def test_normalize_uses_content_text_and_path():
    out = normalize_rag_chunk({"content_text": "셀값", "path": ["시트1", "표A"], "title": "표A"}, 3)
    assert out == {"chunk_index": 3, "text": "셀값", "titles_context": ["시트1", "표A"], "pages": []}


def test_normalize_title_fallback_when_no_path():
    out = normalize_rag_chunk({"content_text": "x", "title": "제목"}, 0)
    assert out["titles_context"] == ["제목"]


def test_normalize_chunks_reindexes_after_dropping_empties():
    rag = [{"content_text": "a"}, {"content_text": ""}, {"content_text": "b"}]
    out = normalize_chunks(rag)
    assert [c["chunk_index"] for c in out] == [0, 1]
    assert [c["text"] for c in out] == ["a", "b"]
