from service.ingest import run_ingest


class FakeEq:
    """Fake modelling the ASYNC submit+poll path of EdgequakeClient.post_document.

    The real client submits with ``async_processing:true``, polls the task to
    ``indexed``, and returns ``{document_id, chunk_count, status:"indexed"}`` read
    from the task ``result``. The fake records the submitted content and emits the
    same terminal shape, accepting the poll knobs the real signature now exposes.
    """

    def __init__(self):
        self.posted = None

    def post_document(self, content, *, workspace_id, tenant_id, filename,
                      poll_timeout=1200.0, poll_interval=3.0):
        self.posted = content
        # submit -> poll(indexed) collapsed: terminal result of the async flow.
        return {"document_id": "d1", "chunk_count": 3, "status": "indexed"}


def test_run_ingest_pipes_enriched_content_and_succeeds(monkeypatch):
    monkeypatch.setattr("service.ingest.parse_to_markdown", lambda b, f, **k: "## T\n<table><tr><td>x</td></tr></table>")
    eq = FakeEq()
    out = run_ingest(b"b", "doc.pdf", workspace_id="ws", doc_id="dc", content_type=None,
                     edgequake=eq, text_llm=lambda p, payload: "표 요약", vision_llm=None,
                     ocr_url="http://x", excel_url="http://y")
    assert out["status"] == "completed" and out["chunk_count"] == 3
    assert "〈MODAL" in eq.posted  # table block became a modal span in enriched content
