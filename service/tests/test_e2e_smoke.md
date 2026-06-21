# Phase C — kb-pipeline integration smoke (Task C1)

Run date: 2026-06-19. Branch: `feat/kb-pipeline-provider`.

## Prerequisites (live services)

- Dedicated adaptive edgequake `:8081` (PG `:5433` container `eq-pg-kbp`) — `service/scripts/start_dedicated_edgequake.sh`
- bge-m3 `:7997` (200), adaptive_chunk `:18060` (listening), OCR/VLM `:18050` (200)
- **Java runtime required for PDF parsing** (OpenDataLoader CLI jar). This host had no `java` on PATH;
  `openjdk@17` was installed via brew at `/usr/local/opt/openjdk@17`. The uvicorn process MUST be
  started with that on PATH, e.g. `export PATH="/usr/local/opt/openjdk@17/bin:$PATH"`, otherwise
  `/ingest` of a PDF fails with `parse failed ... java ... returned non-zero exit status 1`.

## Start the service

```bash
cd /Users/xxx/workspace/8.kb-pipeline
export PATH="/usr/local/opt/openjdk@17/bin:$PATH"   # REQUIRED for PDF (OpenDataLoader/Java)
export KBP_OPENAI_API_KEY=$(grep -E "^OPENAI_API_KEY=" /Users/xxx/workspace/99.projects/rag-edgequake-benchmark/docker/.env | cut -d= -f2-)
export KBP_PG_DSN="postgres://edgequake:edgequake_secret@localhost:5433/edgequake" KBP_EDGEQUAKE_URL=http://localhost:8081
nohup .venv-kb/bin/uvicorn service.app:app --port 19000 >/tmp/kbp_svc.log 2>&1 & disown
```

## PRIMARY — proven LIVE

- `GET /healthz` -> `200 {"status":"ok"}`
- `POST /ingest` (real `test_doc/3-3. 휴가규정(2025.12.05. 개정).pdf`, workspace_id=smoke-ws-c1, doc_id=smoke1)
  -> `200 {"document_id":"00c2c10f-...","chunk_count":12,"status":"completed"}`
  - Full live pipeline ran: parse (OpenDataLoader+Java) -> blockify -> modal (qwen) -> adaptive edgequake.
- `GET /chunks` (workspace_id = edgequake stored workspace, doc_id = returned document_id)
  -> `200`, 12 chunks, **4 contain a `〈MODAL id="T1" type="table"〉` span** with the qwen table summary:
  ```
  〈MODAL id="T1" type="table"〉This table outlines a **Special Leave Policy** (likely for employees
  in a Korean organization), detailing the number of paid leave days granted for specific
  family-related events. ...
  ```

## COMMUNITY (B5 service side) — proven

- `POST /communities/build?workspace_id=smoke-ws-c1` -> `202 {"status":"started","workspace_id":"smoke-ws-c1"}`
  immediately (non-blocking BackgroundTask). `community_reports` table exists in PG :5433.
  W3/qwen build is slow and was not waited to completion; no failure logged in the window.

## KNOWN GAP — edgequake workspace binding (BLOCKS chunk read in real provider flow)

The edgequake fork on :8081 **ignores the `X-Workspace-ID` string header on write** and pins every
document to a fixed internal workspace UUID `00000000-0000-0000-0000-000000000003`
(tenant `00000000-0000-0000-0000-000000000002`). Verified twice:

- ingest with workspace_id=`smoke-ws-c1` -> doc stored under ws `...003`
- ingest with workspace_id=`kbid-deadbeef` -> doc ALSO stored under ws `...003`

Consequence: `GET /chunks?workspace_id=<arbitrary-string>` (which is exactly what the knowledge_base
Phase-B tail sends, `workspace_id = kb.kb_id`) -> edgequake returns **403 Forbidden**, surfaced by the
service as **500**. `GET /chunks` only succeeds when the caller passes the actual stored workspace
(`...003`). So the live end-to-end ingest+chunk-render via the real `kb_pipeline` provider is currently
BLOCKED on this mapping mismatch between `EdgequakeClient` (forwards `workspace_id` verbatim) and the
edgequake fork's workspace auth model.

Fix options (not applied here — out of Task C1 scope): map `workspace_id`->a registered edgequake
workspace UUID in `service/edgequake.py` (ensure/register-workspace handshake), or send no
`X-Workspace-ID` and rely on the tenant default, or register `kb.kb_id` as a workspace before ingest.

## SECONDARY — knowledge_base provider wiring (UNIT-proven, full-UI e2e deferred)

Full knowledge_base stack (:8001 backend + arq worker + docker deps + Next.js frontend) was NOT brought
up in-session (heavy). Per the plan's fallback, Phase-B wiring is proven by unit tests in
`/Users/xxx/workspace/99.projects/shinhan_trust/knowledge_base`:

```
.venv/bin/python -m pytest backend/tests/test_kb_pipeline_client.py \
  backend/tests/test_pipeline_kb_pipeline.py backend/tests/test_kb_provider_accept.py \
  backend/tests/test_community_job.py -q
-> 24 passed
```

This covers: KbPipelineClient (B1, 9), `_ingest_kb_pipeline_tail` + branch (B2, 5), config/DI +
provider validation accepting `kb_pipeline` (B3, 3), community-build enqueue (B5, 7). The tail uses
`returned_doc_id = outcome.document_id` and `workspace_id = kb.kb_id` (pipeline.py L1884/L1896/L1941),
which is exactly the path the edgequake-workspace gap above would break in a live run.

### Full-UI e2e runbook (deferred)

1. Start backend `:8001` with `kb_pipeline_base_url=http://localhost:19000` (config.py default already
   that) + arq worker + docker deps (postgres/qdrant/redis/minio) + frontend.
2. `POST /kb {provider:"kb_pipeline"}` -> create KB.
3. Upload the same test_doc; doc_guard + dedup run; ingest_document routes to `_ingest_kb_pipeline_tail`.
4. Expect document status=ready and chunks rendered at `/kb/{id}/documents/{docId}` — **blocked today by
   the edgequake workspace-binding gap** until the client maps `kb.kb_id` to a real edgequake workspace.
5. On ready, B5 enqueues BUILD_COMMUNITIES_TASK(kb_id) -> service `/communities/build`.
