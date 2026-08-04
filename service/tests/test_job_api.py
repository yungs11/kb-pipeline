"""신규 `/jobs/*` 경로 — 제출·조회·취소, 그리고 레거시 래퍼의 응답 매핑.

conftest 가 인메모리 repo/blobs + 인라인 실행을 자동 주입한다(§6.2).
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from service.app import app, get_adaptive_chunk, get_edgequake, get_parse_client


class FakeParse:
    def __init__(self, result=None, raises=None):
        self.result = result or {"enriched_content": "## H\nbody", "n_blocks": 1}
        self.raises = raises
        self.calls = []

    def parse(self, **kw):
        self.calls.append(kw)
        if self.raises:
            raise self.raises
        return self.result


class FakeChunk:
    def chunk(self, **kw):
        self.last = kw
        return {"chunks": [{"chunk_index": 0, "chunk_text": "c0", "chunk_pages": [1]}],
                "method_selected": "recursive_1100", "scores": {}, "methods_compared": []}


class FakeEq:
    def ensure_workspace(self, workspace_id, name=None):
        return "ws-uuid"

    def insert_chunks(self, **kw):
        self.last = kw
        return {"document_id": "d1", "chunk_count": 1, "status": "indexed"}


@pytest.fixture()
def client():
    return TestClient(app)


def _use(dep, obj):
    app.dependency_overrides[dep] = lambda: obj
    return obj


# ── 제출 ───────────────────────────────────────────────────────────────────

def test_submit_parse_returns_202_with_job_id(client):
    _use(get_parse_client, FakeParse())
    r = client.post("/jobs/parse", files={"file": ("a.pdf", b"x", "application/pdf")})
    assert r.status_code == 202
    assert uuid.UUID(r.json()["job_id"])
    assert r.json()["status"] == "queued"


def test_submitted_parse_result_is_retrievable(client, job_queue_inline):
    """제출 → worker(여기선 인라인) 실행 → /jobs/{id}/result 로 회수."""
    _use(get_parse_client, FakeParse())
    job_id = client.post("/jobs/parse",
                         files={"file": ("a.pdf", b"x", "application/pdf")}).json()["job_id"]
    # 인라인 디스패처는 레거시 대기 경로에만 붙는다 — 여기서는 직접 돌려 완료시킨다.
    from service.jobs.api import _run_inline, get_job_runner

    repo, blobs = job_queue_inline["repo"], job_queue_inline["blobs"]
    _run_inline(repo, blobs, uuid.UUID(job_id), get_job_runner(repo=repo, blobs=blobs))

    body = client.get(f"/jobs/{job_id}/result")
    assert body.status_code == 200
    assert body.json()["enriched_content"].startswith("## H")


def test_result_before_completion_is_409(client):
    _use(get_parse_client, FakeParse())
    job_id = client.post("/jobs/parse",
                         files={"file": ("a.pdf", b"x", "application/pdf")}).json()["job_id"]
    assert client.get(f"/jobs/{job_id}/result").status_code == 409


def test_chunk_requires_exactly_one_source(client):
    assert client.post("/jobs/chunk", json={}).status_code == 400
    assert client.post("/jobs/chunk",
                       json={"enriched_content": "x", "parse_job_id": str(uuid.uuid4())}
                       ).status_code == 400


def test_insert_requires_exactly_one_source(client):
    body = {"workspace_id": "kb-1", "doc_id": "d1"}
    assert client.post("/jobs/insert", json=body).status_code == 400
    assert client.post("/jobs/insert",
                       json={**body, "chunks": ["a"], "chunk_job_id": str(uuid.uuid4())}
                       ).status_code == 400


def test_chunk_referencing_unfinished_parse_job_is_409(client):
    _use(get_parse_client, FakeParse())
    parse_job = client.post("/jobs/parse",
                            files={"file": ("a.pdf", b"x", "application/pdf")}).json()["job_id"]
    r = client.post("/jobs/chunk", json={"parse_job_id": parse_job})
    assert r.status_code == 409


def test_chunk_referencing_unknown_job_is_404(client):
    r = client.post("/jobs/chunk", json={"parse_job_id": str(uuid.uuid4())})
    assert r.status_code == 404


def test_upload_over_limit_is_413(client, monkeypatch):
    monkeypatch.setenv("KBP_JOB_MAX_UPLOAD_BYTES", "10")
    r = client.post("/jobs/parse", files={"file": ("a.pdf", b"x" * 100, "application/pdf")})
    assert r.status_code == 413


def test_submit_without_live_worker_is_503(client, job_queue_inline):
    """worker 가 없으면 잡을 만들지 않고 거절한다 — 무한 대기 방지(§4.4)."""
    repo = job_queue_inline["repo"]
    repo.live_worker_count = lambda **kw: 0
    r = client.post("/jobs/parse", files={"file": ("a.pdf", b"x", "application/pdf")})
    assert r.status_code == 503
    assert r.headers.get("Retry-After")
    assert repo.rows == {}, "거절했는데 잡 행이 생겼다"


# ── 조회·취소 ──────────────────────────────────────────────────────────────

def test_workers_route_is_not_swallowed_by_path_param(client):
    """`/jobs/workers` 가 `/jobs/{job_id}` 로 흡수되면 안 된다(선언 순서 + uuid 타입)."""
    r = client.get("/jobs/workers")
    assert r.status_code == 200
    assert set(r.json()) >= {"online", "capacity", "active", "available",
                             "queued", "processing"}


def test_worker_stats_uses_kb_compatible_key_names(client):
    """kb 의 worker_capacity() 와 같은 키 — 마지막은 running 이 아니라 processing."""
    body = client.get("/jobs/workers").json()
    assert "processing" in body and "running" not in body


def test_get_unknown_job_is_404(client):
    assert client.get(f"/jobs/{uuid.uuid4()}").status_code == 404


def test_list_jobs_filters_by_batch_key(client):
    _use(get_parse_client, FakeParse())
    client.post("/jobs/parse", data={"batch_key": "b1"},
                files={"file": ("a.pdf", b"x", "application/pdf")})
    client.post("/jobs/parse", data={"batch_key": "b2"},
                files={"file": ("b.pdf", b"y", "application/pdf")})
    rows = client.get("/jobs", params={"batch_key": "b1"}).json()["jobs"]
    assert len(rows) == 1 and rows[0]["batch_key"] == "b1"


def test_cancel_queued_job(client):
    _use(get_parse_client, FakeParse())
    job_id = client.post("/jobs/parse",
                         files={"file": ("a.pdf", b"x", "application/pdf")}).json()["job_id"]
    r = client.delete(f"/jobs/{job_id}")
    assert r.status_code == 200 and r.json()["status"] == "canceled"


def test_cancel_unknown_job_is_404(client):
    assert client.delete(f"/jobs/{uuid.uuid4()}").status_code == 404


def test_job_status_has_no_queue_position(client):
    """`queue_position` 은 대기 예측에 못 쓴다 — 대신 ahead_in_partition 을 준다."""
    _use(get_parse_client, FakeParse())
    job_id = client.post("/jobs/parse",
                         files={"file": ("a.pdf", b"x", "application/pdf")}).json()["job_id"]
    body = client.get(f"/jobs/{job_id}").json()
    assert "queue_position" not in body
    assert body["ahead_in_partition"] == 0
    assert body["status"] == "queued"


# ── 레거시 래퍼 응답 매핑 ──────────────────────────────────────────────────

def test_legacy_parse_returns_same_body_as_before(client):
    fake = _use(get_parse_client, FakeParse())
    r = client.post("/parse", files={"file": ("a.pdf", b"x", "application/pdf")})
    assert r.status_code == 200
    assert r.json()["enriched_content"].startswith("## H")
    assert fake.calls[0]["filename"] == "a.pdf"


def test_legacy_job_failure_maps_to_500(client):
    """현행에서도 다운스트림 예외는 500 으로 샜다 — 계약 유지."""
    import httpx

    req = httpx.Request("POST", "http://x/parse")
    err = httpx.HTTPStatusError("bad", request=req,
                                response=httpx.Response(400, request=req))
    _use(get_parse_client, FakeParse(raises=err))
    assert client.post("/parse",
                       files={"file": ("a.pdf", b"x", "application/pdf")}).status_code == 500


def test_legacy_wait_timeout_maps_to_409_not_504(client, monkeypatch, job_queue_inline):
    """504 로 내면 kb 가 5xx 재시도로 두 번째 잡을 만든다 — 멱등키가 없다(D1).

    4xx 라 kb 는 재시도하지 않고, 본문의 job_id 로 결과를 회수할 수 있다.
    """
    monkeypatch.setenv("KBP_JOB_LEGACY_WAIT_SECONDS", "0")
    monkeypatch.setenv("KBP_JOB_WAIT_POLL_INTERVAL_SECONDS", "0")
    app.state.job_inline = False  # 아무도 실행하지 않아 queued 로 남는다
    try:
        r = client.post("/parse", files={"file": ("a.pdf", b"x", "application/pdf")})
    finally:
        app.state.job_inline = True
    assert r.status_code == 409
    assert uuid.UUID(r.json()["detail"]["job_id"])


def test_legacy_parse_records_job_as_legacy(client, job_queue_inline):
    """legacy=true 여야 terminal 시 staging·payload 객체가 즉시 삭제된다(§5.3)."""
    _use(get_parse_client, FakeParse())
    client.post("/parse", files={"file": ("a.pdf", b"x", "application/pdf")})
    rows = list(job_queue_inline["repo"].rows.values())
    assert rows and rows[0]["legacy"] is True
