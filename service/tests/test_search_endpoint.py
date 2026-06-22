"""Facade ``POST /search`` — edgequake ``/api/v1/query`` hidden, results normalized.

The facade ADDS VALUE (R5): it (a) resolves the kb id to the edgequake workspace
UUID so the query is workspace-scoped (isolation), (b) maps the consumer's
``top_k`` to edgequake's ``max_results``, and (c) normalizes edgequake's
``sources`` (source_type/id/snippet/score/document_id) into a stable ``results``
shape (chunk_id/text/score/document_id) plus the generated ``answer`` — the
consumer never sees edgequake's query schema.
"""
from fastapi.testclient import TestClient

from service.app import app, get_edgequake


EQ_WS = "99999999-9999-9999-9999-999999999999"


class FakeEq:
    def __init__(self):
        self.ensured = []
        self.search_calls = []

    def ensure_workspace(self, kb_id, name, tenant_id="00000000-0000-0000-0000-000000000002"):
        self.ensured.append((kb_id, name))
        return EQ_WS

    def search(self, *, workspace_id, query, top_k):
        # the resolved edgequake uuid (not the raw kb id) scopes the query.
        assert workspace_id == EQ_WS
        self.search_calls.append({"workspace_id": workspace_id, "query": query,
                                  "top_k": top_k})
        # edgequake /api/v1/query response shape.
        return {
            "answer": "the answer",
            "mode": "hybrid",
            "sources": [
                {"source_type": "chunk", "id": "d1-chunk-0", "score": 0.91,
                 "snippet": "alpha text", "document_id": "d1"},
                {"source_type": "chunk", "id": "d1-chunk-3", "score": 0.42,
                 "snippet": "beta text", "document_id": "d1"},
            ],
        }


def test_search_scopes_workspace_and_normalizes_results():
    eq = FakeEq()
    app.dependency_overrides[get_edgequake] = lambda: eq
    c = TestClient(app)
    r = c.post("/search", json={"workspace_id": "kb1", "query": "what?", "top_k": 5})
    assert r.status_code == 200
    j = r.json()

    # kb id resolved to the edgequake workspace uuid; query scoped to it.
    assert eq.ensured == [("kb1", "kb1")]
    assert eq.search_calls == [{"workspace_id": EQ_WS, "query": "what?", "top_k": 5}]

    # results normalized from edgequake sources; answer surfaced.
    assert j["answer"] == "the answer"
    assert j["results"] == [
        {"chunk_id": "d1-chunk-0", "text": "alpha text", "score": 0.91, "document_id": "d1"},
        {"chunk_id": "d1-chunk-3", "text": "beta text", "score": 0.42, "document_id": "d1"},
    ]
    app.dependency_overrides.clear()


def test_search_top_k_defaults():
    eq = FakeEq()
    app.dependency_overrides[get_edgequake] = lambda: eq
    c = TestClient(app)
    r = c.post("/search", json={"workspace_id": "kb1", "query": "q"})
    assert r.status_code == 200
    # a sensible default top_k is applied when the consumer omits it.
    assert eq.search_calls[0]["top_k"] == 10
    app.dependency_overrides.clear()


def test_edgequake_search_calls_query_with_workspace_header():
    """EdgequakeClient.search POSTs /api/v1/query with the workspace header and
    maps top_k -> max_results, returning the raw query response."""
    from service.edgequake import EdgequakeClient

    captured = {}

    class FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"answer": "a", "mode": "hybrid",
                    "sources": [{"source_type": "chunk", "id": "x-chunk-0",
                                 "score": 0.5, "snippet": "s", "document_id": "x"}]}

    class FakeHttp:
        def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return FakeResp()

    eq = EdgequakeClient.__new__(EdgequakeClient)
    eq.base = "http://eq:8081"
    eq.http = FakeHttp()

    out = eq.search(workspace_id=EQ_WS, query="hello", top_k=7)
    assert captured["url"] == "http://eq:8081/api/v1/query"
    # workspace-scoped via X-Workspace-ID header.
    assert captured["headers"].get("X-Workspace-ID") == EQ_WS
    # top_k mapped to edgequake's max_results.
    assert captured["json"]["query"] == "hello"
    assert captured["json"]["max_results"] == 7
    assert out["answer"] == "a"
    assert out["sources"][0]["id"] == "x-chunk-0"
