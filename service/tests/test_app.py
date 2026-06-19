from fastapi.testclient import TestClient
from service.app import app, get_edgequake


class FakeEq:
    def post_document(self, content, **k):
        return {"document_id": "d1", "chunk_count": 2, "status": "indexed"}

    def fetch_chunks(self, workspace_id, doc_id):
        return [{"chunk_id": "c0", "text": "t", "hierarchy_path": "##H", "page_number": 1}]

    def delete_doc(self, workspace_id, doc_id):
        return None


def test_ingest_and_chunks(monkeypatch):
    monkeypatch.setattr("service.app.parse_to_markdown", lambda b, f, **k: "## H\n<table><tr><td>x</td></tr></table>")
    monkeypatch.setattr("service.app.get_text_llm", lambda: (lambda p, payload: "요약"))
    app.dependency_overrides[get_edgequake] = lambda: FakeEq()
    c = TestClient(app)
    r = c.post("/ingest", data={"workspace_id": "ws", "doc_id": "dc"}, files={"file": ("d.pdf", b"b", "application/pdf")})
    assert r.status_code == 200 and r.json()["chunk_count"] == 2 and r.json()["status"] == "completed"
    g = c.get("/chunks", params={"workspace_id": "ws", "doc_id": "dc"})
    assert g.status_code == 200 and g.json()[0]["chunk_id"] == "c0"
    assert c.get("/healthz").json()["status"] == "ok"
    app.dependency_overrides.clear()
