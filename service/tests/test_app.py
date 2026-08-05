import uuid

from fastapi.testclient import TestClient
from service.app import app, get_edgequake, get_parse_client, get_adaptive_chunk


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

    def submit_document(self, content, *, workspace_id, tenant_id, filename):
        # async submit returns immediately with the edgequake document_id (NO poll).
        assert workspace_id == EQ_WS
        self.submitted = content
        return {"document_id": "d1", "track_id": "t1"}

    def insert_chunks(self, *, workspace_id, tenant_id, title, chunk_texts):
        # passthrough insert of the pre-chunked texts (used by v2 /ingest).
        assert workspace_id == EQ_WS
        self.inserted = list(chunk_texts)
        return {"document_id": "d1", "chunk_count": 2, "status": "completed"}

    def document_phase(self, workspace_id, document_id):
        # live phase snapshot for /ingest/status — resolved uuid scopes the read.
        assert workspace_id == EQ_WS
        return {"raw_status": "chunking", "phase": "chunking", "chunk_count": 0,
                "terminal": False, "succeeded": False}

    def fetch_chunks(self, workspace_id, doc_id):
        assert workspace_id == EQ_WS
        return [{"chunk_id": "c0", "text": "t", "hierarchy_path": "##H", "page_number": 1}]

    def delete_doc(self, workspace_id, doc_id):
        assert workspace_id == EQ_WS
        return None


class _FakeParse:
    def parse(self, *, file_bytes, filename, content_type=None):
        return {"enriched_content": "## H\n〈MODAL id=\"T1\" type=\"table\"〉d\np〈/MODAL〉",
                "n_blocks": 2, "modal_spans": []}


class _FakeChunk:
    def chunk(self, *, text, doc_name, atomic_markers):
        return {"method_selected": "semantic", "scores": {"avg": 0.8},
                "methods_compared": [{"method": "semantic", "selected": True}],
                "chunks": [{"chunk_index": 0, "chunk_text": "## H", "chunk_pages": [1],
                            "titles_context": "## H"},
                           {"chunk_index": 1, "chunk_text": "〈MODAL id=\"T1\" type=\"table\"〉d\np〈/MODAL〉",
                            "chunk_pages": [1], "titles_context": "## H"}],
                "timing_ms": 1.0}


def test_ingest_and_chunks(monkeypatch):
    # v2 /ingest orchestrates parse→chunk→insert (parse-svc/hub/edgequake mocked).
    app.dependency_overrides[get_parse_client] = lambda: _FakeParse()
    app.dependency_overrides[get_adaptive_chunk] = lambda: _FakeChunk()
    app.dependency_overrides[get_edgequake] = lambda: FakeEq()
    c = TestClient(app)
    r = c.post("/ingest", data={"workspace_id": "ws", "doc_id": "dc"}, files={"file": ("d.pdf", b"b", "application/pdf")})
    assert r.status_code == 200 and r.json()["chunk_count"] == 2 and r.json()["status"] == "completed"
    # the one-shot path still surfaces the real selection rationale.
    assert r.json()["chunking_selection"]["method_selected"] == "semantic"
    g = c.get("/chunks", params={"workspace_id": "ws", "doc_id": "dc"})
    assert g.status_code == 200 and g.json()[0]["chunk_id"] == "c0"
    assert c.get("/healthz").json()["status"] == "ok"
    app.dependency_overrides.clear()


def test_communities_build_enqueues_a_community_job(job_queue_inline):
    """D10 — BackgroundTask 가 아니라 **잡 큐**를 탄다.

    예전에는 응답 뒤 같은 gunicorn 워커에서 LLM 장시간 작업이 돌아, 유량제어 밖이면서
    그 워커가 요청을 못 받았다. 지금은 `community` kind 로 큐에 들어간다.
    """
    app.dependency_overrides[get_edgequake] = lambda: FakeEq()
    r = TestClient(app).post("/communities/build", params={"workspace_id": "ws1"})
    assert r.status_code == 202
    body = r.json()
    # 응답 계약 유지 — 기존 소비자가 읽는 두 키는 그대로다. job_id 는 더한 것.
    assert body["status"] == "started" and body["workspace_id"] == EQ_WS
    assert body["job_id"]

    row = job_queue_inline["repo"].get(uuid.UUID(body["job_id"]))
    assert row["kind"] == "community"
    assert row["status"] == "queued"                 # 웹 프로세스에서 안 돈다
    assert row["workspace_key"] == EQ_WS             # 버킷·workspace 상한에 잡힌다


def test_communities_build_is_idempotent_per_workspace(job_queue_inline):
    """적재마다 디바운스 없이 들어와도(실측: 한 배치에 3회) 잡은 하나여야 한다."""
    app.dependency_overrides[get_edgequake] = lambda: FakeEq()
    c = TestClient(app)
    ids = {c.post("/communities/build", params={"workspace_id": "ws1"}).json()["job_id"]
           for _ in range(3)}
    assert len(ids) == 1
    app.dependency_overrides.clear()

