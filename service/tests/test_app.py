from fastapi.testclient import TestClient
from service.app import app, get_edgequake


#: fixed edgequake-assigned workspace uuid the fake resolves every kb_id to.
EQ_WS = "99999999-9999-9999-9999-999999999999"


class FakeEq:
    def __init__(self):
        self.ensured = []

    def ensure_workspace(self, kb_id, name, tenant_id="00000000-0000-0000-0000-000000000002"):
        self.ensured.append((kb_id, name))
        return EQ_WS

    def post_document(self, content, **k):
        # the resolved edgequake uuid (not the raw kb id) must be used downstream.
        assert k["workspace_id"] == EQ_WS
        # terminal shape of the async submit+poll flow (task reached "indexed").
        return {"document_id": "d1", "chunk_count": 2, "status": "indexed"}

    def fetch_chunks(self, workspace_id, doc_id):
        assert workspace_id == EQ_WS
        return [{"chunk_id": "c0", "text": "t", "hierarchy_path": "##H", "page_number": 1}]

    def delete_doc(self, workspace_id, doc_id):
        assert workspace_id == EQ_WS
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


def test_communities_build_returns_202_and_schedules_job(monkeypatch):
    import threading
    called = threading.Event()
    seen = {}

    def recorder(workspace_id, *, llm, dsn, **k):
        seen["workspace_id"] = workspace_id
        seen["dsn"] = dsn
        called.set()
        return {"reports_written": 0}

    monkeypatch.setenv("KBP_PG_DSN", "postgres://edgequake:edgequake_secret@localhost:5433/edgequake")
    monkeypatch.setattr("service.app.build_workspace_communities", recorder)
    monkeypatch.setattr("service.app.get_text_llm", lambda: (lambda p, payload: "요약"))
    app.dependency_overrides[get_edgequake] = lambda: FakeEq()
    c = TestClient(app)
    r = c.post("/communities/build", params={"workspace_id": "ws1"})
    assert r.status_code == 202
    # the kb id "ws1" is resolved to the edgequake workspace uuid for the build job.
    assert r.json() == {"status": "started", "workspace_id": EQ_WS}
    # TestClient runs BackgroundTasks synchronously after the response is sent.
    assert called.is_set()
    assert seen["workspace_id"] == EQ_WS
    app.dependency_overrides.clear()

