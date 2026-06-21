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
