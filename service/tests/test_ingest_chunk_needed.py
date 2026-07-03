"""/ingest 가 parse-svc 의 chunk_needed 로 청킹을 분기한다."""
from fastapi.testclient import TestClient
import service.app as appmod


class FakeParse:
    def __init__(self, resp):
        self.resp = resp
    def parse(self, **kw):
        return self.resp


class FakeAdaptive:
    def __init__(self):
        self.called = False
    def chunk(self, **kw):
        self.called = True
        return {"chunks": [{"chunk_index": 0, "chunk_text": "c0"}],
                "method_selected": "m", "scores": {}, "methods_compared": []}


class FakeEq:
    def ensure_workspace(self, wid, name=None):
        return "ws-uuid"
    def insert_chunks(self, **kw):
        self.last_chunks = kw["chunk_texts"]
        return {"document_id": "d1", "chunk_count": len(kw["chunk_texts"]),
                "status": "indexed"}


def _override(parse_resp, ac, eq):
    app = appmod.app
    app.dependency_overrides[appmod.get_parse_client] = lambda: FakeParse(parse_resp)
    app.dependency_overrides[appmod.get_adaptive_chunk] = lambda: ac
    app.dependency_overrides[appmod.get_edgequake] = lambda: eq
    return TestClient(app)


def test_chunk_needed_true_calls_adaptive():
    ac, eq = FakeAdaptive(), FakeEq()
    c = _override({"enriched_content": "text", "chunk_needed": True}, ac, eq)
    r = c.post("/ingest", data={"workspace_id": "w", "doc_id": "d"},
               files={"file": ("a.pdf", b"%PDF")})
    assert r.status_code == 200
    assert ac.called is True
    appmod.app.dependency_overrides.clear()


def test_chunk_needed_false_skips_adaptive_and_inserts_native_chunks():
    ac, eq = FakeAdaptive(), FakeEq()
    c = _override({"enriched_content": "표1", "chunk_needed": False,
                   "chunks": [{"chunk_index": 0, "text": "표1",
                               "titles_context": None, "pages": []}]}, ac, eq)
    r = c.post("/ingest", data={"workspace_id": "w", "doc_id": "d"},
               files={"file": ("a.xlsx", b"PK")})
    assert r.status_code == 200
    assert ac.called is False
    assert eq.last_chunks == ["표1"]
    appmod.app.dependency_overrides.clear()


def test_failed_parse_returns_immediately():
    """v3(리뷰 round2): parse 실패({status:"failed"})는 adaptive 미호출·그대로 반환."""
    ac, eq = FakeAdaptive(), FakeEq()
    c = _override({"status": "failed", "detail": "parse error"}, ac, eq)
    r = c.post("/ingest", data={"workspace_id": "w", "doc_id": "d"},
               files={"file": ("a.pdf", b"%PDF")})
    assert r.status_code == 200
    assert r.json()["status"] == "failed"
    assert ac.called is False
    appmod.app.dependency_overrides.clear()
