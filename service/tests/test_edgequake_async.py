"""Unit tests for the ASYNC edgequake ingest path (submit + task-poll).

These drive the real ``EdgequakeClient.post_document`` over an httpx
``MockTransport`` (no network) so we exercise the exact submit→poll→result
contract verified against edgequake-api source:

  * ``POST /api/v1/documents`` with ``async_processing: true`` -> 201
        ``{document_id, status:"pending", task_id, track_id}`` (no chunk_count).
  * ``GET /api/v1/tasks/{track_id}`` -> ``{status, result:{document_id, chunk_count}, ...}``
        status transitions pending -> processing -> indexed | failed | cancelled.

Isolation contract: BOTH the submit and every poll must carry the same
X-Workspace-ID / X-Tenant-ID headers (the task endpoint 404s across workspaces).
"""
import httpx
import pytest

from service.edgequake import EdgequakeClient

WS = "33333333-3333-3333-3333-333333333333"
TENANT = "00000000-0000-0000-0000-000000000002"
DOC = "doc-abc"


def _client_with(handler):
    """Build an EdgequakeClient whose http client routes through ``handler``."""
    eq = EdgequakeClient("http://eq.test")
    eq.http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://eq.test")
    return eq


def test_post_document_async_submit_then_poll_to_indexed():
    """Happy path: submit async, poll until indexed, read result.chunk_count."""
    calls = {"submit": 0, "poll": 0, "doc_detail": 0}
    # poll returns processing twice, then indexed with a populated result.
    poll_sequence = ["processing", "processing", "indexed"]

    def handler(request: httpx.Request) -> httpx.Response:
        # Both submit and poll MUST carry the isolation headers (per-workspace scope).
        assert request.headers.get("X-Workspace-ID") == WS
        assert request.headers.get("X-Tenant-ID") == TENANT
        if request.method == "POST" and request.url.path == "/api/v1/documents":
            calls["submit"] += 1
            import json
            body = json.loads(request.content)
            # async path must be requested.
            assert body["async_processing"] is True
            assert body["content"] == "enriched-content"
            return httpx.Response(201, json={
                "document_id": DOC,
                "status": "pending",
                "task_id": "task-xyz",
                "track_id": "upload_20260622_abcd1234",
            })
        if request.method == "GET" and request.url.path == "/api/v1/tasks/task-xyz":
            calls["poll"] += 1
            status = poll_sequence[min(calls["poll"] - 1, len(poll_sequence) - 1)]
            result = {"document_id": DOC, "chunk_count": 7} if status == "indexed" else None
            return httpx.Response(200, json={
                "track_id": "task-xyz", "status": status, "result": result,
            })
        if request.method == "GET" and request.url.path.startswith("/api/v1/documents/"):
            calls["doc_detail"] += 1
            return httpx.Response(200, json={"id": DOC, "chunk_count": 7, "status": "indexed"})
        raise AssertionError(f"unexpected {request.method} {request.url.path}")

    eq = _client_with(handler)
    out = eq.post_document("enriched-content", workspace_id=WS, tenant_id=TENANT,
                           filename="d.pdf", poll_interval=0)
    assert out["document_id"] == DOC
    assert out["chunk_count"] == 7
    assert out["status"] == "indexed"
    assert calls["submit"] == 1
    assert calls["poll"] == 3
    # result carried chunk_count -> no extra document-detail round-trip needed.
    assert calls["doc_detail"] == 0


def test_post_document_async_result_missing_chunk_count_falls_back_to_doc():
    """If the task result omits chunk_count, gate via GET /documents/{id}."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/v1/documents":
            return httpx.Response(201, json={
                "document_id": DOC, "status": "pending", "task_id": "task-1", "track_id": "t",
            })
        if request.url.path == "/api/v1/tasks/task-1":
            # indexed but NO result payload.
            return httpx.Response(200, json={"status": "indexed", "result": None})
        if request.url.path == f"/api/v1/documents/{DOC}":
            return httpx.Response(200, json={"id": DOC, "chunk_count": 4, "status": "indexed"})
        raise AssertionError(f"unexpected {request.url.path}")

    eq = _client_with(handler)
    out = eq.post_document("c", workspace_id=WS, tenant_id=TENANT, filename="d.pdf",
                           poll_interval=0)
    assert out["chunk_count"] == 4 and out["status"] == "indexed"


def test_post_document_async_failed_task_propagates_failure():
    """A failed task yields status=failed with the error message (no success fake)."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(201, json={
                "document_id": DOC, "status": "pending", "task_id": "task-2", "track_id": "t",
            })
        if request.url.path == "/api/v1/tasks/task-2":
            return httpx.Response(200, json={
                "status": "failed", "error_message": "qwen extraction blew up", "result": None,
            })
        raise AssertionError("should not gate a failed task via document detail")

    eq = _client_with(handler)
    out = eq.post_document("c", workspace_id=WS, tenant_id=TENANT, filename="d.pdf",
                           poll_interval=0)
    assert out["status"] == "failed"
    assert out["chunk_count"] == 0
    assert "qwen extraction blew up" in out["detail"]


def test_post_document_async_poll_tolerates_transient_404_then_indexed():
    """A task not-yet-visible (404) right after submit is retried, not fatal."""
    seq = [404, 200]
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(201, json={
                "document_id": DOC, "status": "pending", "task_id": "task-3", "track_id": "t",
            })
        if request.url.path == "/api/v1/tasks/task-3":
            code = seq.pop(0) if seq else 200
            if code == 404:
                return httpx.Response(404, json={"error": "Task not found"})
            return httpx.Response(200, json={
                "status": "indexed", "result": {"document_id": DOC, "chunk_count": 2},
            })
        raise AssertionError(f"unexpected {request.url.path}")

    eq = _client_with(handler)
    out = eq.post_document("c", workspace_id=WS, tenant_id=TENANT, filename="d.pdf",
                           poll_interval=0, poll_timeout=30)
    assert out["status"] == "indexed" and out["chunk_count"] == 2


def test_post_document_async_poll_timeout_is_failure():
    """If the task never reaches terminal within poll_timeout, fail (don't hang)."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(201, json={
                "document_id": DOC, "status": "pending", "task_id": "task-4", "track_id": "t",
            })
        if request.url.path == "/api/v1/tasks/task-4":
            return httpx.Response(200, json={"status": "processing", "result": None})
        raise AssertionError(f"unexpected {request.url.path}")

    eq = _client_with(handler)
    # poll_timeout=0 -> the loop checks once then hits the deadline immediately.
    out = eq.post_document("c", workspace_id=WS, tenant_id=TENANT, filename="d.pdf",
                           poll_interval=0, poll_timeout=0)
    assert out["status"] == "failed"
    assert "timeout" in out["detail"]


# ─────────────── submit_document + document_phase (pollable per-phase) ───────────────


def test_submit_document_returns_immediately_no_poll():
    """submit_document fires POST /documents async and returns ids WITHOUT polling."""
    calls = {"submit": 0, "poll": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("X-Workspace-ID") == WS
        assert request.headers.get("X-Tenant-ID") == TENANT
        if request.method == "POST" and request.url.path == "/api/v1/documents":
            calls["submit"] += 1
            import json
            body = json.loads(request.content)
            assert body["async_processing"] is True
            assert body["content"] == "enriched"
            return httpx.Response(201, json={
                "document_id": DOC, "status": "pending",
                "task_id": "task-9", "track_id": "batch-9",
            })
        calls["poll"] += 1  # must never happen
        raise AssertionError(f"submit_document must not poll: {request.url.path}")

    eq = _client_with(handler)
    out = eq.submit_document("enriched", workspace_id=WS, tenant_id=TENANT, filename="d.pdf")
    assert out == {"document_id": DOC, "track_id": "task-9"}
    assert calls["submit"] == 1 and calls["poll"] == 0


def _capture_submit_body(filename="d.pdf", **submit_kwargs):
    """Fire submit_document over a MockTransport and return the parsed POST json body."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/v1/documents":
            import json
            captured["body"] = json.loads(request.content)
            return httpx.Response(201, json={
                "document_id": DOC, "status": "pending",
                "task_id": "task-9", "track_id": "batch-9",
            })
        raise AssertionError(f"unexpected {request.method} {request.url.path}")

    eq = _client_with(handler)
    eq.submit_document("enriched", workspace_id=WS, tenant_id=TENANT,
                       filename=filename, **submit_kwargs)
    return captured["body"]


def test_submit_document_skip_graph_attaches_metadata_flag():
    """skip_graph=True → POST body carries metadata.skip_graph_extraction=true."""
    body = _capture_submit_body(skip_graph=True)
    assert body["metadata"] == {"skip_graph_extraction": True}
    # the legacy fields are untouched.
    assert body["content"] == "enriched"
    assert body["async_processing"] is True


def test_submit_document_default_omits_metadata_key():
    """Default (skip_graph=False / unspecified) → NO metadata key (byte-identical)."""
    assert "metadata" not in _capture_submit_body()
    assert "metadata" not in _capture_submit_body(skip_graph=False)


def test_insert_chunks_skip_graph_propagates_to_submit_body():
    """insert_chunks(skip_graph=...) flows the flag into the submit POST body."""
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/v1/documents":
            import json
            bodies.append(json.loads(request.content))
            return httpx.Response(201, json={
                "document_id": DOC, "status": "pending", "task_id": "t", "track_id": "t",
            })
        if request.method == "GET" and request.url.path == f"/api/v1/documents/{DOC}":
            return httpx.Response(200, json={"id": DOC, "status": "completed", "chunk_count": 2})
        raise AssertionError(f"unexpected {request.method} {request.url.path}")

    eq = _client_with(handler)
    eq.insert_chunks(workspace_id=WS, tenant_id=TENANT, title="t",
                     chunk_texts=["a", "b"], skip_graph=True,
                     poll_interval=0)
    assert bodies[0]["metadata"] == {"skip_graph_extraction": True}

    bodies.clear()
    eq.insert_chunks(workspace_id=WS, tenant_id=TENANT, title="t",
                     chunk_texts=["a", "b"], poll_interval=0)
    assert "metadata" not in bodies[0]


def test_document_phase_maps_status_to_phase():
    """document_phase maps the live document status into a coarse UI phase."""
    cases = [
        ("chunking", "chunking", False, False),
        ("extracting", "extracting", False, False),
        ("embedding", "embedding", False, False),
        ("indexing", "storing", False, False),
        ("storing", "storing", False, False),
        ("failed", "failed", True, False),
        ("partial_failure", "failed", True, False),
        ("cancelled", "failed", True, False),
        ("pending", "processing", False, False),
    ]
    for raw, phase, terminal, succeeded in cases:
        def handler(request, _raw=raw):
            assert request.headers.get("X-Workspace-ID") == WS
            return httpx.Response(200, json={"id": DOC, "status": _raw, "chunk_count": 0})
        eq = _client_with(handler)
        ph = eq.document_phase(WS, DOC)
        assert ph["phase"] == phase, raw
        assert ph["terminal"] is terminal, raw
        assert ph["succeeded"] is succeeded, raw
        assert ph["raw_status"] == raw


def test_document_phase_completed_with_chunks_succeeds():
    """completed + chunk_count>0 → terminal & succeeded."""
    def handler(request):
        return httpx.Response(200, json={"id": DOC, "status": "completed", "chunk_count": 9})
    eq = _client_with(handler)
    ph = eq.document_phase(WS, DOC)
    assert ph["phase"] == "completed"
    assert ph["chunk_count"] == 9
    assert ph["terminal"] is True
    assert ph["succeeded"] is True


def test_document_phase_completed_zero_chunks_not_succeeded():
    """completed but chunk_count==0 → terminal yet NOT succeeded (no success-faking)."""
    def handler(request):
        return httpx.Response(200, json={"id": DOC, "status": "completed", "chunk_count": 0})
    eq = _client_with(handler)
    ph = eq.document_phase(WS, DOC)
    assert ph["terminal"] is True
    assert ph["succeeded"] is False
