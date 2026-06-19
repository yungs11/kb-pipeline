"""Thin HTTP client for the dedicated adaptive edgequake (:8081).

Routes verified against ``edgequake/crates/edgequake-api/src/routes.rs`` and the
handlers:

  * ``POST  /api/v1/documents``                 -> UploadDocumentResponse
        ``{document_id, status, track_id, chunk_count?, ...}``. The synchronous
        path returns ``status == "processed"`` with a populated ``chunk_count``.
  * ``GET   /api/v1/documents/{document_id}``    -> DocumentDetailResponse
        ``{id, chunk_count, content, ...}``. There is NO ``/documents/{id}/chunks``
        route; chunk bodies are fetched per-chunk.
  * ``GET   /api/v1/chunks/{chunk_id}``          -> ChunkDetailResponse
        ``{chunk_id, content, index, document_name, ...}``. Chunk IDs are
        deterministic: ``{document_id}-chunk-{N}`` for N in 0..chunk_count.
  * ``DELETE /api/v1/documents/{document_id}``   -> delete the document.

``fetch_chunks`` therefore: GET the document for ``chunk_count``, then GET each
``{doc_id}-chunk-{i}``; it returns rows with the stable keys
``{chunk_id, text, hierarchy_path, page_number}``.
"""
from __future__ import annotations

import httpx

_TENANT_ID = "00000000-0000-0000-0000-000000000002"


class EdgequakeClient:
    def __init__(self, base_url: str, timeout: float = 600.0):
        self.base = base_url.rstrip("/")
        self.http = httpx.Client(timeout=timeout)

    def _headers(self, workspace_id: str | None = None) -> dict:
        h = {"X-Tenant-ID": _TENANT_ID}
        if workspace_id is not None:
            h["X-Workspace-ID"] = workspace_id
        return h

    def post_document(self, content, *, workspace_id, tenant_id, filename):
        r = self.http.post(
            f"{self.base}/api/v1/documents",
            headers={"X-Workspace-ID": workspace_id, "X-Tenant-ID": tenant_id},
            json={"content": content, "title": filename, "async_processing": False},
        )
        r.raise_for_status()
        j = r.json()
        return {
            "document_id": j.get("document_id") or j.get("id"),
            "chunk_count": int(j.get("chunk_count") or 0),
            "status": j.get("status"),
        }

    def fetch_chunks(self, workspace_id, doc_id):
        # 1) document detail -> chunk_count (no chunk-list route exists).
        d = self.http.get(
            f"{self.base}/api/v1/documents/{doc_id}",
            headers=self._headers(workspace_id),
        )
        d.raise_for_status()
        doc = d.json() or {}
        chunk_count = int(doc.get("chunk_count") or 0)
        doc_title = doc.get("title") or doc.get("file_name") or ""

        # 2) per-chunk detail via deterministic id {doc_id}-chunk-{i}.
        rows = []
        for i in range(chunk_count):
            chunk_id = f"{doc_id}-chunk-{i}"
            c = self.http.get(
                f"{self.base}/api/v1/chunks/{chunk_id}",
                headers=self._headers(workspace_id),
            )
            if c.status_code == 404:
                continue
            c.raise_for_status()
            cj = c.json() or {}
            rows.append({
                "chunk_id": cj.get("chunk_id") or chunk_id,
                "text": cj.get("content") or cj.get("text") or "",
                "hierarchy_path": cj.get("document_name") or doc_title or "",
                "page_number": cj.get("page_number"),
            })
        return rows

    def delete_doc(self, workspace_id, doc_id):
        self.http.delete(
            f"{self.base}/api/v1/documents/{doc_id}",
            headers=self._headers(workspace_id),
        )
