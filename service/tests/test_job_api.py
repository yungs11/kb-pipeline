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


# ── 제출 멱등키 (D1) — Phase 2 선행 조건 ───────────────────────────────────

def test_retrying_same_upload_reuses_job(client, job_queue_inline):
    """kb 는 5xx 를 3회까지 재시도한다. 재시도가 새 잡을 만들면 중복 적재다."""
    _use(get_parse_client, FakeParse())
    files = {"file": ("a.pdf", b"same-bytes", "application/pdf")}
    first = client.post("/jobs/parse", files=files).json()["job_id"]
    second = client.post("/jobs/parse", files=files).json()["job_id"]
    assert first == second
    assert len(job_queue_inline["repo"].rows) == 1


def test_different_content_creates_new_job(client):
    _use(get_parse_client, FakeParse())
    a = client.post("/jobs/parse",
                    files={"file": ("a.pdf", b"one", "application/pdf")}).json()["job_id"]
    b = client.post("/jobs/parse",
                    files={"file": ("a.pdf", b"two", "application/pdf")}).json()["job_id"]
    assert a != b


def test_explicit_idempotency_key_wins(client):
    """헤더가 있으면 내용이 달라도 같은 요청으로 본다(소비자가 수명을 정한다)."""
    _use(get_parse_client, FakeParse())
    h = {"Idempotency-Key": "upload-42"}
    a = client.post("/jobs/parse", headers=h,
                    files={"file": ("a.pdf", b"one", "application/pdf")}).json()["job_id"]
    b = client.post("/jobs/parse", headers=h,
                    files={"file": ("b.pdf", b"two", "application/pdf")}).json()["job_id"]
    assert a == b


def test_different_explicit_keys_create_separate_jobs(client):
    _use(get_parse_client, FakeParse())
    files = {"file": ("a.pdf", b"x", "application/pdf")}
    a = client.post("/jobs/parse", headers={"Idempotency-Key": "k1"}, files=files).json()["job_id"]
    b = client.post("/jobs/parse", headers={"Idempotency-Key": "k2"}, files=files).json()["job_id"]
    assert a != b


def test_idem_collision_does_not_leak_staging_object(client, job_queue_inline):
    """충돌 시 방금 올린 staging 은 어떤 행도 참조하지 않는다 — 즉시 지워야 한다.

    GC 가 없으므로(D2) 그냥 두면 영구 고아다.
    """
    _use(get_parse_client, FakeParse())
    files = {"file": ("a.pdf", b"same", "application/pdf")}
    client.post("/jobs/parse", files=files)
    blobs = job_queue_inline["blobs"]
    before = len(blobs.objects)
    client.post("/jobs/parse", files=files)          # 충돌
    assert len(blobs.objects) == before              # 새 객체가 남지 않았다
    assert blobs.deleted                              # 정리가 실제로 돌았다


def test_chunk_idem_accounts_for_parent(client, job_queue_inline):
    """payload 가 같아도 선행 잡이 다르면 다른 요청이다."""
    repo = job_queue_inline["repo"]
    p1 = repo.submit(kind="parse"); repo.rows[p1]["status"] = "succeeded"
    p2 = repo.submit(kind="parse"); repo.rows[p2]["status"] = "succeeded"
    a = client.post("/jobs/chunk", json={"parse_job_id": str(p1)}).json()["job_id"]
    b = client.post("/jobs/chunk", json={"parse_job_id": str(p2)}).json()["job_id"]
    assert a != b


def test_legacy_path_is_not_deduped_across_calls(client, job_queue_inline):
    """레거시 /parse 는 멱등키를 쓰지 않는다 — 호출마다 실제로 파싱해야 한다.

    레거시는 동기라 소비자가 최종 결과를 보고 재시도를 판단한다. 여기에 멱등 캐시가
    끼면 "설정을 고치고 다시 파싱" 같은 정상 재요청이 옛 결과를 돌려받는다.
    """
    fake = _use(get_parse_client, FakeParse())
    files = {"file": ("a.pdf", b"same", "application/pdf")}
    assert client.post("/parse", files=files).status_code == 200
    assert client.post("/parse", files=files).status_code == 200
    assert len(job_queue_inline["repo"].rows) == 2, "레거시가 잡을 재사용했다"
    assert len(fake.calls) == 2, "두 번째 호출이 실제로 파싱하지 않았다"


# ── 이벤트루프 블로킹 방지 ─────────────────────────────────────────────────

def test_legacy_handlers_are_sync_def():
    """레거시 4경로는 **반드시 동기 `def`** 여야 한다.

    async 로 두면 대기 루프의 `time.sleep` 이 이벤트루프를 통째로 막아 같은 프로세스의
    `/healthz`·`/jobs/*` 가 최대 `KBP_JOB_LEGACY_WAIT_SECONDS`(3300s) 동안 멎는다.
    compose healthcheck 가 unhealthy 로 넘어가고 depends_on 체인이 무너진다.

    실제로 이 전환을 설계에만 적고 구현을 빠뜨렸다 — 인라인 모드에서는 폴링 루프에
    도달하지 않아 다른 테스트가 못 잡았다. 그래서 구조로 못박는다.
    """
    import inspect

    from service.app import chunk, ingest, insert, parse

    for fn in (parse, chunk, insert, ingest):
        assert not inspect.iscoroutinefunction(fn), (
            f"{fn.__name__} must be a sync def — async blocks the event loop while waiting"
        )


def test_legacy_wait_timeout_returns_409_with_job_id(monkeypatch, job_queue_inline):
    """대기 초과는 409 + job_id — 잡은 계속 진행한다.

    (원래 여기에 "대기 중에도 /healthz 가 응답한다"는 행동 테스트를 뒀는데, TestClient 가
    인스턴스마다 별도 이벤트루프를 만들어 **핸들러가 async 여도 통과했다** — 주장을
    검증하지 못하는 테스트였다. 이벤트루프 블로킹은 위의 구조 단언으로 막는다.)
    """
    monkeypatch.setenv("KBP_JOB_LEGACY_WAIT_SECONDS", "1")
    monkeypatch.setenv("KBP_JOB_WAIT_POLL_INTERVAL_SECONDS", "0")
    app.state.job_inline = False
    _use(get_parse_client, FakeParse())
    try:
        r = TestClient(app).post("/parse",
                                 files={"file": ("a.pdf", b"x", "application/pdf")})
    finally:
        app.state.job_inline = True
    assert r.status_code == 409
    assert uuid.UUID(r.json()["detail"]["job_id"])
