from service.ingest import run_ingest


class FakeEq:
    def __init__(self):
        self.posted = None

    def post_document(self, content, *, workspace_id, tenant_id, filename):
        self.posted = content
        return {"document_id": "d1", "chunk_count": 3, "status": "indexed"}


def test_run_ingest_pipes_enriched_content_and_succeeds(monkeypatch):
    monkeypatch.setattr("service.ingest.parse_to_markdown", lambda b, f, **k: "## T\n<table><tr><td>x</td></tr></table>")
    eq = FakeEq()
    out = run_ingest(b"b", "doc.pdf", workspace_id="ws", doc_id="dc", content_type=None,
                     edgequake=eq, text_llm=lambda p, payload: "표 요약", vision_llm=None,
                     ocr_url="http://x", excel_url="http://y")
    assert out["status"] == "completed" and out["chunk_count"] == 3
    assert "〈MODAL" in eq.posted  # table block became a modal span in enriched content
