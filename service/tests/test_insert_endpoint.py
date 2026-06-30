"""Facade ``POST /insert`` + ``GET /insert/status`` — edgequake passthrough policy.

The facade ADDS VALUE (R5): it owns the *insert policy* so consumers never touch
edgequake directly —
  * resolves the kb id to the edgequake workspace UUID (``ensure_workspace``);
  * joins the chunk texts with the passthrough separator U+001E so edgequake's
    PassthroughStrategy splits them back into the exact same chunks;
  * submits as a passthrough document and polls to a terminal state;
  * ``/insert/status`` relays the live edgequake phase (``document_phase``) so the
    consumer's UI can tick without knowing edgequake's vocabulary.
"""
from fastapi.testclient import TestClient

from service.app import app, get_edgequake


SEP = chr(0x1E)  # U+001E RECORD SEPARATOR — passthrough chunk boundary.
EQ_WS = "99999999-9999-9999-9999-999999999999"


class FakeEq:
    def __init__(self):
        self.ensured = []
        self.inserted = None

    def ensure_workspace(self, kb_id, name, tenant_id="00000000-0000-0000-0000-000000000002"):
        self.ensured.append((kb_id, name))
        return EQ_WS

    def insert_chunks(self, *, workspace_id, tenant_id, title, chunk_texts,
                      skip_graph=False):
        # the resolved edgequake uuid (not the raw kb id) scopes the insert.
        assert workspace_id == EQ_WS
        self.inserted = {"workspace_id": workspace_id, "title": title,
                         "chunk_texts": list(chunk_texts), "skip_graph": skip_graph}
        return {"document_id": "d1", "chunk_count": 3, "status": "indexed"}

    def document_phase(self, workspace_id, document_id):
        assert workspace_id == EQ_WS
        return {"raw_status": "embedding", "phase": "embedding", "chunk_count": 2,
                "terminal": False, "succeeded": False}


def test_insert_joins_chunks_and_returns_success():
    eq = FakeEq()
    app.dependency_overrides[get_edgequake] = lambda: eq
    c = TestClient(app)
    body = {"workspace_id": "kb1", "doc_id": "dc", "title": "doc.pdf",
            "chunks": ["alpha", "beta", "gamma"]}
    r = c.post("/insert", json=body)
    assert r.status_code == 200
    rj = r.json()
    assert rj["document_id"] == "d1"
    assert rj["chunk_count"] == 3
    assert rj["status"] == "indexed"

    # kb id was resolved to the edgequake workspace uuid.
    assert eq.ensured == [("kb1", "kb1")]
    # chunk texts forwarded verbatim (join into one passthrough doc is the
    # client's job; the facade hands the list of chunk texts + title down).
    assert eq.inserted["chunk_texts"] == ["alpha", "beta", "gamma"]
    assert eq.inserted["title"] == "doc.pdf"
    # extract_graph unspecified → default True → skip_graph=False (graph extraction on).
    assert eq.inserted["skip_graph"] is False

    app.dependency_overrides.clear()


def test_insert_extract_graph_false_sets_skip_graph():
    """``extract_graph=false`` on /insert maps to ``skip_graph=True`` at insert_chunks."""
    eq = FakeEq()
    app.dependency_overrides[get_edgequake] = lambda: eq
    c = TestClient(app)
    body = {"workspace_id": "kb1", "doc_id": "dc", "title": "sheet.xlsx",
            "chunks": ["alpha", "beta"], "extract_graph": False}
    r = c.post("/insert", json=body)
    assert r.status_code == 200
    assert eq.inserted["skip_graph"] is True
    app.dependency_overrides.clear()


def test_insert_extract_graph_true_keeps_skip_graph_false():
    """Explicit ``extract_graph=true`` → skip_graph=False (graph extraction performed)."""
    eq = FakeEq()
    app.dependency_overrides[get_edgequake] = lambda: eq
    c = TestClient(app)
    body = {"workspace_id": "kb1", "doc_id": "dc", "title": "doc.pdf",
            "chunks": ["alpha"], "extract_graph": True}
    r = c.post("/insert", json=body)
    assert r.status_code == 200
    assert eq.inserted["skip_graph"] is False
    app.dependency_overrides.clear()


def test_insert_status_relays_live_phase():
    app.dependency_overrides[get_edgequake] = lambda: FakeEq()
    c = TestClient(app)
    r = c.get("/insert/status", params={"workspace_id": "kb1", "doc_id": "d1"})
    assert r.status_code == 200
    # only the consumer-facing phase fields are relayed (edgequake internals hidden).
    assert r.json() == {"phase": "embedding", "chunk_count": 2,
                        "terminal": False, "succeeded": False}
    app.dependency_overrides.clear()


def test_insert_chunks_joins_with_record_separator():
    """edgequake.insert_chunks joins chunk texts with U+001E and submits one
    passthrough document, polling to terminal."""
    from service.edgequake import EdgequakeClient

    captured = {}

    class StubClient(EdgequakeClient):
        def __init__(self):
            pass  # skip httpx setup

        def submit_document(self, content, *, workspace_id, tenant_id, filename,
                            skip_graph=False):
            captured["content"] = content
            captured["workspace_id"] = workspace_id
            captured["filename"] = filename
            captured["skip_graph"] = skip_graph
            return {"document_id": "d9", "track_id": "t9"}

        def document_phase(self, workspace_id, document_id):
            captured["polled"] = document_id
            return {"raw_status": "completed", "phase": "completed", "chunk_count": 2,
                    "terminal": True, "succeeded": True}

    eq = StubClient()
    out = eq.insert_chunks(workspace_id=EQ_WS, tenant_id="t",
                           title="doc.pdf", chunk_texts=["a", "b"])
    # the two chunk texts were joined with exactly one U+001E separator.
    assert captured["content"] == "a" + SEP + "b"
    assert captured["workspace_id"] == EQ_WS
    assert captured["filename"] == "doc.pdf"
    # polled to terminal then returned the stable insert shape.
    assert out["document_id"] == "d9"
    assert out["chunk_count"] == 2
    assert out["status"] == "indexed"
