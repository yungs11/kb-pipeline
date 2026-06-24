"""Thin HTTP client for the dedicated adaptive edgequake (:8081).

Routes verified against ``edgequake/crates/edgequake-api/src/routes.rs`` and the
handlers:

  * ``POST  /api/v1/documents`` (``async_processing: true``) -> UploadDocumentResponse
        ``{document_id, status:"pending", task_id, track_id}``. The async path
        enqueues a background task; ``chunk_count`` is NOT populated here. We then
        poll ``GET /api/v1/tasks/{track_id}`` (workspace-scoped) until the
        ``TaskStatus`` reaches ``indexed`` (success) or ``failed``/``cancelled``,
        reading ``chunk_count``/``document_id`` from the task ``result``. Async is
        used (over sync ``async_processing:false``) so slow qwen extraction is not
        capped by edgequake's sync HTTP timeout (120s cloud / 600s local).
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

import time

import httpx

_TENANT_ID = "00000000-0000-0000-0000-000000000002"

#: Passthrough chunk-boundary separator (U+001E RECORD SEPARATOR). The facade joins
#: chunk texts with this; edgequake's PassthroughStrategy splits on the same byte so
#: the stored chunks match the upstream chunker's chunks exactly.
PASSTHROUGH_SEP = chr(0x1E)


class EdgequakeClient:
    def __init__(self, base_url: str, timeout: float = 600.0):
        self.base = base_url.rstrip("/")
        self.http = httpx.Client(timeout=timeout)

    def _headers(self, workspace_id: str | None = None) -> dict:
        h = {"X-Tenant-ID": _TENANT_ID}
        if workspace_id is not None:
            h["X-Workspace-ID"] = workspace_id
        return h

    @staticmethod
    def _slug_for(kb_id: str) -> str:
        """Deterministic, globally-unique slug derived from the kb id (idempotency key)."""
        return f"kb-{kb_id.replace('-', '')}"

    def ensure_workspace(
        self, kb_id: str, name: str, tenant_id: str = _TENANT_ID
    ) -> str:
        """Idempotently create-or-find an edgequake workspace; return its UUID.

        edgequake stores everything under an *assigned* workspace UUID and rejects
        arbitrary ``X-Workspace-ID`` strings (403). The kb_id must therefore be
        registered first: POST ``/api/v1/tenants/{tid}/workspaces`` (idempotent via a
        deterministic slug). On a 4xx (already-exists / unique-violation race) we list
        and find by slug. Returns the edgequake workspace UUID to use as the header.
        """
        slug = self._slug_for(kb_id)
        url = f"{self.base}/api/v1/tenants/{tenant_id}/workspaces"
        body = {"name": name, "slug": slug}
        # 5xx are retried: a freshly (re)started edgequake transiently returns a
        # 500 ``pool timed out`` while its sqlx pool warms up against postgres.
        r = self.http.post(url, headers={"X-Tenant-ID": tenant_id}, json=body)
        for _ in range(3):
            if r.status_code < 500:
                break
            time.sleep(1.0)
            r = self.http.post(url, headers={"X-Tenant-ID": tenant_id}, json=body)
        if 400 <= r.status_code < 500:
            return self._find_workspace_by_slug(tenant_id, slug)
        r.raise_for_status()
        data = r.json() or {}
        ws_id = data.get("id") or data.get("workspace_id")
        if not ws_id:
            raise ValueError(
                f"edgequake workspace create response missing id: keys={list(data.keys())}"
            )
        return str(ws_id)

    def _find_workspace_by_slug(self, tenant_id: str, slug: str) -> str:
        url = f"{self.base}/api/v1/tenants/{tenant_id}/workspaces"
        r = self.http.get(url, headers={"X-Tenant-ID": tenant_id})
        r.raise_for_status()
        body = r.json()
        # Live shape is `{"items":[...]}` (paginated); defend list/workspaces/data too.
        if isinstance(body, dict):
            spaces = body.get("items") or body.get("workspaces") or body.get("data") or []
        else:
            spaces = body
        for w in spaces or []:
            if isinstance(w, dict) and (w.get("slug") == slug or w.get("name") == slug):
                return str(w.get("id"))
        raise ValueError(f"edgequake workspace lookup failed (slug={slug})")

    #: Task-poll terminal states (TaskStatus Display strings, lowercase).
    #: success == "indexed"; "failed"/"cancelled" are terminal failures. The
    #: document-detail gate then re-confirms via the authoritative chunk_count.
    _POLL_OK = "indexed"
    _POLL_FAIL = ("failed", "cancelled")

    def post_document(
        self,
        content,
        *,
        workspace_id,
        tenant_id,
        filename,
        poll_timeout=1200.0,
        poll_interval=3.0,
    ):
        """Submit a document ASYNC and poll its task until terminal.

        WHY async: synchronous ``POST /api/v1/documents`` (async_processing:false)
        is wrapped in a provider-side HTTP timeout (120s for cloud, 600s for
        local providers) — too tight for slow qwen entity extraction on large
        docs in production. Async enqueues a background task and we poll
        ``GET /api/v1/tasks/{track_id}`` (workspace-scoped) until the
        ``TaskStatus`` reaches ``indexed`` (success) or ``failed``/``cancelled``,
        with our own generous overall ``poll_timeout``.

        Per-workspace isolation is unchanged: the same X-Workspace-ID/X-Tenant-ID
        headers scope both the submit and every poll (the task endpoint 404s if
        the header workspace does not own the task). The returned dict shape is
        identical to the old sync path: ``{document_id, chunk_count, status}``.
        """
        hdr = {"X-Workspace-ID": workspace_id, "X-Tenant-ID": tenant_id}
        # 1) Submit async — 201 returns {document_id, status:"pending", task_id, track_id};
        #    chunk_count is NOT populated on the async path (only on sync).
        r = self.http.post(
            f"{self.base}/api/v1/documents",
            headers=hdr,
            json={"content": content, "title": filename, "async_processing": True},
        )
        r.raise_for_status()
        j = r.json() or {}
        document_id = j.get("document_id") or j.get("id")
        # Prefer task_id (the queue's task.track_id) for polling; fall back to the
        # batch track_id, then any returned id.
        track_id = j.get("task_id") or j.get("track_id") or document_id
        submit_status = (j.get("status") or "").lower()

        # A duplicate that is still processing is reported by the server with no
        # new task (task_id:None, status:"duplicate_processing"); fall through to
        # the document gate which reads chunk_count/status for the existing doc.
        if not track_id or submit_status == "duplicate_processing":
            return self._gate_by_document(document_id, hdr)

        # 2) Poll the task until a terminal TaskStatus (indexed/failed/cancelled).
        poll_status, poll_err, result = self._poll_task(
            track_id, hdr, poll_timeout, poll_interval
        )
        if poll_status in self._POLL_FAIL:
            return {
                "document_id": document_id,
                "chunk_count": 0,
                "status": "failed",
                "detail": poll_err or f"task {poll_status}",
            }
        if poll_status != self._POLL_OK:
            # timed out without reaching a terminal state.
            return {
                "document_id": document_id,
                "chunk_count": 0,
                "status": "failed",
                "detail": f"task poll timeout after {poll_timeout:.0f}s (last={poll_status})",
            }

        # 3) Indexed → chunk_count/document_id are written atomically into the
        #    task ``result`` by mark_success ({document_id, chunk_count, ...}).
        #    Prefer that (provider-independent, no extra round-trip); only fall
        #    back to GET /documents/{id} if result lacks a usable chunk_count.
        rid = result.get("document_id") if isinstance(result, dict) else None
        rcc = result.get("chunk_count") if isinstance(result, dict) else None
        document_id = rid or document_id
        if rcc is not None:
            chunk_count = int(rcc or 0)
            ok = chunk_count > 0
            return {
                "document_id": document_id,
                "chunk_count": chunk_count,
                # normalize to the terminal-OK token the ingest layer accepts.
                "status": "indexed" if ok else "failed",
                "detail": None if ok else "task indexed but chunk_count=0",
            }
        return self._gate_by_document(document_id, hdr)

    #: Raw DOCUMENT `status` (GET /documents/{id}) → our coarse phase label. The
    #: live edgequake document status transitions
    #: ``pending → chunking → extracting → embedding → indexing/storing → completed``.
    #: This is the LIVE per-phase signal the UI ticks on (current_stage is None on
    #: the async path, so the document `status` field is the source of truth).
    _PHASE_MAP = {
        "chunking": "chunking",
        "extracting": "extracting",
        "embedding": "embedding",
        "indexing": "storing",
        "storing": "storing",
        "completed": "completed",
        "indexed": "completed",
        "failed": "failed",
        "partial_failure": "failed",
        "cancelled": "failed",
    }
    #: Document statuses that are terminal (no more transitions).
    _PHASE_TERMINAL = {"completed", "indexed", "failed", "partial_failure", "cancelled"}
    #: Terminal statuses that count as a SUCCESSFUL index (given chunk_count>0).
    _PHASE_SUCCESS = {"completed", "indexed"}

    def submit_document(self, content, *, workspace_id, tenant_id, filename):
        """Submit a document ASYNC and return immediately (NO poll).

        Unlike ``post_document`` (which blocks polling the task to terminal), this
        only fires ``POST /api/v1/documents`` with ``async_processing:true`` and
        returns the identifiers so the caller can poll ``document_phase`` itself to
        observe the LIVE per-phase progress (chunking→extracting→…→completed).

        Returns ``{document_id, track_id}`` (track_id = the task/batch id for the
        async pipeline; not required for ``document_phase`` polling, which keys off
        the document_id, but surfaced for parity with the submit response).
        """
        hdr = {"X-Workspace-ID": workspace_id, "X-Tenant-ID": tenant_id}
        r = self.http.post(
            f"{self.base}/api/v1/documents",
            headers=hdr,
            json={"content": content, "title": filename, "async_processing": True},
        )
        r.raise_for_status()
        j = r.json() or {}
        document_id = j.get("document_id") or j.get("id")
        track_id = j.get("task_id") or j.get("track_id") or document_id
        return {"document_id": document_id, "track_id": track_id}

    def document_phase(self, workspace_id, document_id):
        """GET /documents/{id} → live phase snapshot for per-phase progress.

        Reads the authoritative DOCUMENT ``status`` field (NOT current_stage, which
        is None on the async path) and maps it to a coarse phase the UI ticks on.

        Returns ``{raw_status, phase, chunk_count, terminal, succeeded}`` where:
          * ``raw_status`` — the document status string as returned by edgequake.
          * ``phase``      — mapped coarse phase (chunking/extracting/embedding/
                             storing/completed/failed, else "processing").
          * ``chunk_count``— authoritative chunk count (``-chunk-`` prefix scan).
          * ``terminal``   — status reached a terminal state (no more transitions).
          * ``succeeded``  — terminal-OK (completed/indexed) AND chunk_count>0.
        """
        d = self.http.get(
            f"{self.base}/api/v1/documents/{document_id}",
            headers=self._headers(workspace_id),
        )
        d.raise_for_status()
        dj = d.json() or {}
        raw_status = (dj.get("status") or "").lower()
        chunk_count = int(dj.get("chunk_count") or 0)
        phase = self._PHASE_MAP.get(raw_status, "processing")
        terminal = raw_status in self._PHASE_TERMINAL
        succeeded = raw_status in self._PHASE_SUCCESS and chunk_count > 0
        return {
            "raw_status": raw_status,
            "phase": phase,
            "chunk_count": chunk_count,
            "terminal": terminal,
            "succeeded": succeeded,
        }

    def _poll_task(self, track_id, headers, poll_timeout, poll_interval):
        """Poll GET /api/v1/tasks/{track_id} until terminal.

        Returns ``(status, error_message, result)`` where ``status`` is the
        lowercase TaskStatus, ``result`` is the task's nested result dict (holds
        ``document_id``/``chunk_count`` on success).

        The task endpoint is workspace-scoped (X-Workspace-ID must own the task),
        so we pass the same headers as the submit. A transient 404 right after
        submit (task not yet visible) or a 5xx (pool warmup) is tolerated and
        re-polled until the deadline.
        """
        url = f"{self.base}/api/v1/tasks/{track_id}"
        deadline = time.monotonic() + poll_timeout
        last_status = "pending"
        while True:
            t = self.http.get(url, headers=headers)
            if t.status_code == 404 or t.status_code >= 500:
                # not visible yet / pool warming — retry until deadline.
                if time.monotonic() >= deadline:
                    return last_status, None, None
                time.sleep(poll_interval)
                continue
            t.raise_for_status()
            tj = t.json() or {}
            last_status = (tj.get("status") or "").lower()
            if last_status == self._POLL_OK or last_status in self._POLL_FAIL:
                err = tj.get("error_message")
                if not err and isinstance(tj.get("error"), dict):
                    err = tj["error"].get("message")
                result = tj.get("result") if isinstance(tj.get("result"), dict) else None
                return last_status, err, result
            if time.monotonic() >= deadline:
                return last_status, None, None
            time.sleep(poll_interval)

    def _gate_by_document(self, document_id, headers):
        """Read chunk_count/status from GET /documents/{id} (fallback gate).

        Used only when the task result is unavailable (duplicate-processing path
        or a result missing chunk_count). The async pipeline stores chunks under
        the deterministic ``{document_id}-chunk-{N}`` key (chunker/mod.rs), which
        the detail handler counts via its ``-chunk-`` prefix scan — so this
        chunk_count is the same ground truth the old sync response carried.
        Returns the stable ``{document_id, chunk_count, status}`` shape; ``status``
        stays the doc's own string for the ingest layer's _OK_STATUSES check.
        """
        if not document_id:
            return {"document_id": None, "chunk_count": 0, "status": "failed",
                    "detail": "async submit returned no document_id"}
        d = self.http.get(
            f"{self.base}/api/v1/documents/{document_id}", headers=headers
        )
        d.raise_for_status()
        dj = d.json() or {}
        return {
            "document_id": dj.get("id") or document_id,
            "chunk_count": int(dj.get("chunk_count") or 0),
            "status": dj.get("status"),
        }

    def insert_chunks(self, *, workspace_id, tenant_id, title, chunk_texts,
                      poll_timeout=1200.0, poll_interval=3.0):
        """Insert pre-chunked texts as ONE passthrough document and poll to terminal.

        The chunk texts are joined with ``PASSTHROUGH_SEP`` (U+001E) into a single
        document body; edgequake's PassthroughStrategy splits on the same separator
        so the stored chunks correspond 1:1 (and in order) to ``chunk_texts``. The
        document is submitted async and polled via ``document_phase`` until terminal,
        returning the stable ``{document_id, chunk_count, status}`` shape where
        ``status`` is ``"indexed"`` on a terminal-OK success and ``"failed"`` otherwise.
        """
        content = PASSTHROUGH_SEP.join(chunk_texts)
        res = self.submit_document(content, workspace_id=workspace_id,
                                   tenant_id=tenant_id, filename=title)
        document_id = res.get("document_id")
        if not document_id:
            return {"document_id": None, "chunk_count": 0, "status": "failed",
                    "detail": "passthrough submit returned no document_id"}
        deadline = time.monotonic() + poll_timeout
        # 모니터링(P3): edgequake 내부 phase 체류시간 근사. edgequake 는 per-phase
        # 타임스탬프를 주지 않으므로, 각 폴링 간격을 "그 구간 동안 관측된 raw_status"에
        # 귀속해 누적한다(poll-derived approx). chunking→extracting→embedding→storing
        # 전이로 분해돼 "적재 안에서 어디가 느린지"(주로 extracting=엔티티 LLM)를 드러낸다.
        phase_ms: dict[str, float] = {}
        phase_order: list[str] = []

        def _accumulate(name, dt_ms):
            if not name:
                return
            if name not in phase_ms:
                phase_ms[name] = 0.0
                phase_order.append(name)
            phase_ms[name] += dt_ms

        def _phases():
            return [{"name": p, "ms": round(phase_ms[p], 1)} for p in phase_order]

        last_t = time.monotonic()
        ph = self.document_phase(workspace_id, document_id)
        last_phase = ph.get("raw_status") or ph.get("phase")
        while not ph.get("terminal"):
            if time.monotonic() >= deadline:
                _accumulate(last_phase, (time.monotonic() - last_t) * 1000.0)
                return {"document_id": document_id,
                        "chunk_count": int(ph.get("chunk_count") or 0),
                        "status": "failed",
                        "detail": f"insert poll timeout after {poll_timeout:.0f}s",
                        "phases": _phases()}
            time.sleep(poll_interval)
            now = time.monotonic()
            _accumulate(last_phase, (now - last_t) * 1000.0)
            last_t = now
            ph = self.document_phase(workspace_id, document_id)
            last_phase = ph.get("raw_status") or ph.get("phase")
        _accumulate(last_phase, (time.monotonic() - last_t) * 1000.0)
        succeeded = bool(ph.get("succeeded"))
        return {
            "document_id": document_id,
            "chunk_count": int(ph.get("chunk_count") or 0),
            "status": "indexed" if succeeded else "failed",
            "detail": None if succeeded else f"terminal status={ph.get('raw_status')}",
            "phases": _phases(),
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

    def search(self, *, workspace_id, query, top_k):
        """POST /api/v1/query (workspace-scoped) → raw query response dict.

        ``top_k`` maps to edgequake's ``max_results``. The X-Workspace-ID header
        scopes the retrieval to this workspace (isolation). The raw response
        (``{answer, mode, sources, ...}``) is returned unchanged; the facade
        normalizes it for its own ``/search`` contract.
        """
        r = self.http.post(
            f"{self.base}/api/v1/query",
            headers=self._headers(workspace_id),
            json={"query": query, "max_results": top_k},
        )
        r.raise_for_status()
        return r.json() or {}

    def delete_doc(self, workspace_id, doc_id):
        self.http.delete(
            f"{self.base}/api/v1/documents/{doc_id}",
            headers=self._headers(workspace_id),
        )
