<!-- plan-version: v1 -->
<!-- codex-validation: PENDING -->

# kb-pipeline knowledge_base provider — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `kb_pipeline` as a selectable provider in the shinhan_trust `knowledge_base` service so uploads run our pipeline (parse → blockify → modal → adaptive-chunk@edgequake → embed → store) and chunks render on the document-detail screen; build community reports (W3) as a non-blocking job after ingest.

**Architecture:** Two components. (A) a new thin FastAPI service in `8.kb-pipeline/service/` (`:19000`) that wraps the existing `kb_pipeline` package + parsers and ingests into a DEDICATED adaptive edgequake (`:8081`, own Postgres `:5433`). (B) additive provider wiring in `knowledge_base` mirroring the existing `raganything` provider pattern — client + Protocol + tail + branching + config/DI + frontend dropdown. `doc_guard`, dedup, `chunks_meta`, and the UI are reused unchanged.

**Tech Stack:** Python 3.14 / FastAPI / httpx / pytest (both repos), Next.js/React (frontend dropdown), edgequake fork binary (Rust, prebuilt debug), Postgres(pgvector+AGE) via docker, bge-m3 (:7997), qwen/qwen3.5-122b-a10b (OpenRouter).

## Global Constraints

- kb-pipeline service repo: `/Users/xxx/workspace/8.kb-pipeline`, branch `feat/kb-pipeline-provider`. Service code under `service/`. Reuse the existing `kb_pipeline` package (blockify/modal/community); do NOT duplicate it.
- knowledge_base repo: `/Users/xxx/workspace/99.projects/shinhan_trust/knowledge_base`. All provider changes are ADDITIVE (new files; ≤1-line edits to `routers/kb.py` validation tuple, `frontend/app/kb/page.tsx` dropdown, `ingest_document()` branch). Do NOT modify existing provider tails.
- Provider contract (verbatim from existing providers): `workspace_id = kb_id`; `doc_id = content_hash[:16]`; success = `status == "completed" and chunk_count > 0`.
- Embeddings 1024d `BAAI/bge-m3`; LLM `qwen/qwen3.5-122b-a10b` via OpenRouter (key file `/Users/xxx/workspace/99.projects/rag-edgequake-benchmark/docker/.env`, `OPENAI_API_KEY`; never print it).
- Dedicated adaptive edgequake on `:8081` with its OWN Postgres `:5433`; the existing vanilla edgequake (`:8080`) and its DB are untouched.
- W6 parser routing: use `kb_pipeline.blockify.recommended_parser(filename)` → pptx/docx/pdf → structural, xlsx → markitdown.
- Community build (W3) is ALWAYS a non-blocking job; never block the `/ingest` response on it.
- Tests: each task ends green via the named pytest command.

---

## Phase 0 — Dedicated adaptive edgequake

### Task 0: Bring up dedicated edgequake (:8081) + Postgres (:5433)

**Files:**
- Create: `service/scripts/start_dedicated_edgequake.sh`

**Interfaces:**
- Produces: a running edgequake at `http://localhost:8081` (adaptive chunker, bge-m3 1024d, qwen) backed by Postgres container `eq-pg-kbp:5433`.

- [ ] **Step 1: Write the start script**

```bash
# service/scripts/start_dedicated_edgequake.sh
#!/usr/bin/env bash
set -euo pipefail
KEY=$(grep -E '^OPENAI_API_KEY=' /Users/xxx/workspace/99.projects/rag-edgequake-benchmark/docker/.env | head -1 | cut -d= -f2-)
docker rm -f eq-pg-kbp 2>/dev/null || true
docker run -d --name eq-pg-kbp -p 5433:5432 \
  -e POSTGRES_USER=edgequake -e POSTGRES_PASSWORD=edgequake_secret -e POSTGRES_DB=edgequake \
  ghcr.io/raphaelmansuy/edgequake-postgres:latest
until docker exec eq-pg-kbp pg_isready -U edgequake >/dev/null 2>&1; do sleep 1; done
EQ=/Users/xxx/workspace/8.kb-pipeline/edgequake/edgequake
nohup env \
  EDGEQUAKE_HOST=0.0.0.0 EDGEQUAKE_PORT=8081 EDGEQUAKE_CHUNKER=adaptive \
  ADAPTIVE_CHUNK_URL=http://localhost:18060 \
  DATABASE_URL='postgres://edgequake:edgequake_secret@localhost:5433/edgequake?options=-c%20search_path%3Dpublic' \
  EDGEQUAKE_LLM_PROVIDER=openai OPENAI_BASE_URL=https://openrouter.ai/api/v1 OPENAI_API_KEY="$KEY" \
  EDGEQUAKE_DEFAULT_LLM_MODEL=qwen/qwen3.5-122b-a10b EDGEQUAKE_LLM_MODEL=qwen/qwen3.5-122b-a10b \
  EDGEQUAKE_EMBEDDING_PROVIDER=openai EDGEQUAKE_EMBEDDING_BASE_URL=http://localhost:7997/v1 \
  EDGEQUAKE_EMBEDDING_API_KEY=dummy EDGEQUAKE_EMBEDDING_MODEL=BAAI/bge-m3 EDGEQUAKE_EMBEDDING_DIMENSION=1024 \
  PDFIUM_AUTO_CACHE_DIR=/tmp/eqkbp-pdfium RUST_LOG=info \
  "$EQ/target/debug/edgequake" > /tmp/edgequake_kbp.log 2>&1 &
disown
```

- [ ] **Step 2: Run it and verify health**

Run: `bash service/scripts/start_dedicated_edgequake.sh && sleep 60 && curl -s http://localhost:8081/health | head -c 200`
Expected: JSON with `"status":"healthy"`, embedding `dimension":1024`, llm model qwen. (bge-m3 :7997 and adaptive_chunk :18060 must already be running.)

- [ ] **Step 3: Commit**

```bash
git add service/scripts/start_dedicated_edgequake.sh
git commit -m "feat(svc): dedicated adaptive edgequake bring-up script (:8081/:5433)"
```

---

## Phase A — kb-pipeline FastAPI service (8.kb-pipeline)

### Task A1: Parser adapter (file bytes → markdown+HTML)

**Files:**
- Create: `service/parsing.py`
- Test: `service/tests/test_parsing.py`

**Interfaces:**
- Produces: `parse_to_markdown(file_bytes: bytes, filename: str, *, ocr_url: str, excel_url: str) -> str` — returns "markdown + inline HTML tables". Routes by `recommended_parser(filename)`: structural→ (pdf: OpenDataLoader lib; pptx/docx: structural via OCR/MinerU service; image/scanned: VLM OCR `:18050`); markitdown→ markitdown lib (xlsx). Raises `ParseError` on failure.

- [ ] **Step 1: Write the failing test** (markitdown path with a tiny xlsx is heavy; test the ROUTING + a markdown passthrough seam instead)

```python
# service/tests/test_parsing.py
from service.parsing import parse_to_markdown, ParseError, _route

def test_route_uses_recommended_parser():
    assert _route("a.pptx") == "structural"
    assert _route("a.xlsx") == "markitdown"
    assert _route("a.pdf") == "structural"

def test_parse_dispatches_and_returns_markdown(monkeypatch):
    monkeypatch.setattr("service.parsing._parse_structural", lambda b, f, **k: "## H\n<table><tr><td>x</td></tr></table>")
    out = parse_to_markdown(b"bytes", "doc.pptx", ocr_url="http://x", excel_url="http://y")
    assert "<table>" in out and "## H" in out

def test_parse_error_propagates(monkeypatch):
    def boom(*a, **k): raise RuntimeError("parser down")
    monkeypatch.setattr("service.parsing._parse_structural", boom)
    try:
        parse_to_markdown(b"b", "doc.pdf", ocr_url="http://x", excel_url="http://y"); assert False
    except ParseError: pass
```

- [ ] **Step 2: Run test to verify it fails** — Run: `cd /Users/xxx/workspace/8.kb-pipeline && .venv-kb/bin/python -m pytest service/tests/test_parsing.py -q` → FAIL (module missing).

- [ ] **Step 3: Implement `service/parsing.py`**

```python
# service/parsing.py
from __future__ import annotations
from kb_pipeline.blockify import recommended_parser

class ParseError(Exception): ...

def _route(filename: str) -> str:
    return recommended_parser(filename)  # "structural" | "markitdown"

def _parse_markitdown(file_bytes: bytes, filename: str, **_) -> str:
    from markitdown import MarkItDown  # markitdown lib
    import io
    res = MarkItDown().convert_stream(io.BytesIO(file_bytes), file_extension="." + filename.rsplit(".",1)[-1])
    return res.text_content

def _parse_structural(file_bytes: bytes, filename: str, *, ocr_url: str, excel_url: str) -> str:
    # PDF → OpenDataLoader (markdown_with_html=True); image/scanned/pptx/docx → OCR/VLM service (:18050)
    # returns markdown + inline HTML tables. Implementation calls the shared services by extension.
    import httpx
    ext = filename.rsplit(".",1)[-1].lower()
    if ext == "pdf":
        import opendataloader_pdf, glob, os, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, filename); open(src,"wb").write(file_bytes)
            opendataloader_pdf.convert(input_path=src, output_dir=tmp, format="markdown",
                                       markdown_with_html=True, quiet=True)
            mds = sorted(glob.glob(os.path.join(tmp, "**", "*.md"), recursive=True))
            if not mds: raise ParseError(f"opendataloader produced no md for {filename}")
            return "\n\n".join(open(m, encoding="utf-8", errors="replace").read() for m in mds)
    # pptx/docx/image → OCR/VLM structured service (:18050) returning content.{markdown|html}
    r = httpx.post(f"{ocr_url}/api/v1/ocr", files={"file": (filename, file_bytes)}, timeout=600)
    r.raise_for_status()
    content = (r.json() or {}).get("content", {}) or {}
    md = content.get("markdown") or content.get("html") or content.get("text")
    if not md: raise ParseError(f"ocr/vlm empty for {filename}")
    return md

def parse_to_markdown(file_bytes: bytes, filename: str, *, ocr_url: str, excel_url: str) -> str:
    fn = _parse_structural if _route(filename) == "structural" else _parse_markitdown
    try:
        return fn(file_bytes, filename, ocr_url=ocr_url, excel_url=excel_url)
    except ParseError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ParseError(f"parse failed for {filename}: {e}") from e
```

- [ ] **Step 4: Run tests to verify pass** — Run: `.venv-kb/bin/python -m pytest service/tests/test_parsing.py -q` → PASS (install `markitdown`, `opendataloader-pdf` into `.venv-kb` first if import error: `.venv-kb/bin/pip install markitdown opendataloader-pdf httpx`).

- [ ] **Step 5: Commit** — `git add service/parsing.py service/tests/test_parsing.py && git commit -m "feat(svc): parser adapter with W6 routing"`

### Task A2: Ingest pipeline (parse → blockify → modal → edgequake)

**Files:**
- Create: `service/ingest.py`
- Test: `service/tests/test_ingest.py`

**Interfaces:**
- Consumes: `parse_to_markdown` (A1); `kb_pipeline.blockify.hybrid_to_blocks`; `kb_pipeline.modal.enrich`.
- Produces: `run_ingest(file_bytes, filename, *, workspace_id, doc_id, content_type, edgequake, text_llm, vision_llm, ocr_url, excel_url) -> IngestOutcome` where `IngestOutcome = {document_id: str|None, chunk_count: int, status: "completed"|"failed", detail: str|None}`. `edgequake` is an injected client with `post_document(content, workspace_id, tenant_id, filename) -> {document_id, chunk_count, status}`.

- [ ] **Step 1: Write the failing test**

```python
# service/tests/test_ingest.py
from service.ingest import run_ingest

class FakeEq:
    def __init__(self): self.posted=None
    def post_document(self, content, *, workspace_id, tenant_id, filename):
        self.posted=content
        return {"document_id":"d1","chunk_count":3,"status":"indexed"}

def test_run_ingest_pipes_enriched_content_and_succeeds(monkeypatch):
    monkeypatch.setattr("service.ingest.parse_to_markdown", lambda b,f,**k: "## T\n<table><tr><td>x</td></tr></table>")
    eq=FakeEq()
    out=run_ingest(b"b","doc.pdf", workspace_id="ws", doc_id="dc", content_type=None,
                   edgequake=eq, text_llm=lambda p,payload:"표 요약", vision_llm=None,
                   ocr_url="http://x", excel_url="http://y")
    assert out["status"]=="completed" and out["chunk_count"]==3
    assert "〈MODAL" in eq.posted  # table block became a modal span in enriched content
```

- [ ] **Step 2: Run → FAIL** — `.venv-kb/bin/python -m pytest service/tests/test_ingest.py -q`

- [ ] **Step 3: Implement `service/ingest.py`**

```python
# service/ingest.py
from __future__ import annotations
from typing import Any, Callable
from service.parsing import parse_to_markdown, ParseError
from kb_pipeline.blockify import hybrid_to_blocks
from kb_pipeline.modal import enrich

def run_ingest(file_bytes: bytes, filename: str, *, workspace_id: str, doc_id: str,
               content_type: str | None, edgequake: Any,
               text_llm: Callable[[str, str], str], vision_llm: Callable[[str, str], str] | None,
               ocr_url: str, excel_url: str) -> dict:
    try:
        md = parse_to_markdown(file_bytes, filename, ocr_url=ocr_url, excel_url=excel_url)
        blocks = hybrid_to_blocks(md)
        enriched, _modal_ids = enrich(blocks, text_llm=text_llm, vision_llm=vision_llm)
    except (ParseError, Exception) as e:  # noqa: BLE001
        return {"document_id": None, "chunk_count": 0, "status": "failed", "detail": f"front: {e}"}
    res = edgequake.post_document(enriched, workspace_id=workspace_id,
                                  tenant_id="00000000-0000-0000-0000-000000000002", filename=filename)
    status = res.get("status")
    ok = status in {"completed", "indexed"} and int(res.get("chunk_count", 0)) > 0
    return {"document_id": res.get("document_id"), "chunk_count": int(res.get("chunk_count", 0)),
            "status": "completed" if ok else "failed", "detail": None if ok else f"edgequake status={status}"}
```

- [ ] **Step 4: Run → PASS** — `.venv-kb/bin/python -m pytest service/tests/test_ingest.py -q`
- [ ] **Step 5: Commit** — `git add service/ingest.py service/tests/test_ingest.py && git commit -m "feat(svc): ingest pipeline (parse→blockify→modal→edgequake)"`

### Task A3: edgequake client + FastAPI app (/ingest, /chunks, /doc, /healthz)

**Files:**
- Create: `service/edgequake.py` (thin HTTP client: `post_document`, `fetch_chunks`, `delete_doc`)
- Create: `service/app.py` (FastAPI)
- Create: `service/llm.py` (qwen text_llm via OpenRouter; key from env)
- Test: `service/tests/test_app.py`

**Interfaces:**
- Consumes: `run_ingest` (A2).
- Produces: FastAPI app `app`. `POST /ingest` (multipart file + form workspace_id, doc_id, content_type?) → `{document_id, chunk_count, status}`. `GET /chunks?workspace_id&doc_id` → `[{chunk_id, text, hierarchy_path, page_number}]`. `DELETE /doc?workspace_id&doc_id` → 204. `GET /healthz` → `{status:"ok"}`.

- [ ] **Step 1: Write the failing test** (TestClient with injected fake edgequake)

```python
# service/tests/test_app.py
from fastapi.testclient import TestClient
from service.app import app, get_edgequake

class FakeEq:
    def post_document(self, content, **k): return {"document_id":"d1","chunk_count":2,"status":"indexed"}
    def fetch_chunks(self, workspace_id, doc_id): return [{"chunk_id":"c0","text":"t","hierarchy_path":"##H","page_number":1}]
    def delete_doc(self, workspace_id, doc_id): return None

def test_ingest_and_chunks(monkeypatch):
    monkeypatch.setattr("service.app.parse_to_markdown", lambda b,f,**k: "## H\n<table><tr><td>x</td></tr></table>")
    monkeypatch.setattr("service.app.get_text_llm", lambda: (lambda p,payload:"요약"))
    app.dependency_overrides[get_edgequake] = lambda: FakeEq()
    c = TestClient(app)
    r = c.post("/ingest", data={"workspace_id":"ws","doc_id":"dc"}, files={"file":("d.pdf", b"b","application/pdf")})
    assert r.status_code==200 and r.json()["chunk_count"]==2 and r.json()["status"]=="completed"
    g = c.get("/chunks", params={"workspace_id":"ws","doc_id":"dc"})
    assert g.status_code==200 and g.json()[0]["chunk_id"]=="c0"
    assert c.get("/healthz").json()["status"]=="ok"
    app.dependency_overrides.clear()
```

- [ ] **Step 2: Run → FAIL** — `.venv-kb/bin/python -m pytest service/tests/test_app.py -q`

- [ ] **Step 3: Implement** (`service/edgequake.py`, `service/llm.py`, `service/app.py`)

```python
# service/edgequake.py
import httpx
class EdgequakeClient:
    def __init__(self, base_url: str, timeout: float = 600.0):
        self.base=base_url.rstrip("/"); self.http=httpx.Client(timeout=timeout)
    def post_document(self, content, *, workspace_id, tenant_id, filename):
        r=self.http.post(f"{self.base}/api/v1/documents",
            headers={"X-Workspace-ID":workspace_id,"X-Tenant-ID":tenant_id},
            json={"content":content,"title":filename,"async_processing":False})
        r.raise_for_status(); j=r.json()
        return {"document_id":j.get("document_id") or j.get("id"),
                "chunk_count":int(j.get("chunk_count",0)),"status":j.get("status")}
    def fetch_chunks(self, workspace_id, doc_id):
        r=self.http.get(f"{self.base}/api/v1/documents/{doc_id}/chunks",
            headers={"X-Workspace-ID":workspace_id,"X-Tenant-ID":"00000000-0000-0000-0000-000000000002"})
        r.raise_for_status()
        return [{"chunk_id":c.get("chunk_id") or c.get("id"),"text":c.get("text") or c.get("content",""),
                 "hierarchy_path":c.get("hierarchy_path") or c.get("titles_context",""),
                 "page_number":c.get("page_number")} for c in (r.json().get("chunks") or r.json() or [])]
    def delete_doc(self, workspace_id, doc_id):
        self.http.delete(f"{self.base}/api/v1/workspace/{workspace_id}/doc/{doc_id}")
```

```python
# service/llm.py
import os, httpx
def get_text_llm():
    key=os.environ["KBP_OPENAI_API_KEY"]; base=os.environ.get("KBP_OPENAI_BASE_URL","https://openrouter.ai/api/v1")
    model=os.environ.get("KBP_LLM_MODEL","qwen/qwen3.5-122b-a10b")
    def call(prompt: str, payload: str) -> str:
        r=httpx.post(f"{base}/chat/completions", headers={"Authorization":f"Bearer {key}"},
            json={"model":model,"messages":[{"role":"user","content":f"{prompt}\n\n{payload}"}]}, timeout=120)
        r.raise_for_status(); return r.json()["choices"][0]["message"]["content"]
    return call
```

```python
# service/app.py
import os
from fastapi import FastAPI, UploadFile, File, Form, Depends, HTTPException
from service.parsing import parse_to_markdown
from service.ingest import run_ingest
from service.edgequake import EdgequakeClient
from service.llm import get_text_llm

app = FastAPI(title="kb-pipeline")
def get_edgequake(): return EdgequakeClient(os.environ.get("KBP_EDGEQUAKE_URL","http://localhost:8081"))

@app.get("/healthz")
def healthz(): return {"status":"ok"}

@app.post("/ingest")
async def ingest(file: UploadFile = File(...), workspace_id: str = Form(...), doc_id: str = Form(...),
                 content_type: str | None = Form(None), eq = Depends(get_edgequake)):
    data = await file.read()
    out = run_ingest(data, file.filename, workspace_id=workspace_id, doc_id=doc_id,
                     content_type=content_type or file.content_type, edgequake=eq,
                     text_llm=get_text_llm(), vision_llm=None,
                     ocr_url=os.environ.get("KBP_OCR_URL","http://localhost:18050"),
                     excel_url=os.environ.get("KBP_EXCEL_URL","http://localhost:18055"))
    return out

@app.get("/chunks")
def chunks(workspace_id: str, doc_id: str, eq = Depends(get_edgequake)):
    return eq.fetch_chunks(workspace_id, doc_id)

@app.delete("/doc", status_code=204)
def delete(workspace_id: str, doc_id: str, eq = Depends(get_edgequake)):
    eq.delete_doc(workspace_id, doc_id)
```

NOTE: verify the real edgequake chunk-fetch route in `edgequake/edgequake/crates/edgequake-api/src/routes.rs` (the `/documents/{id}/chunks` path above is the planned shape; adjust the client to the actual route during this task, keeping the returned dict keys stable).

- [ ] **Step 4: Run → PASS** — `.venv-kb/bin/python -m pytest service/tests/test_app.py -q` (install `fastapi`, `python-multipart`, `uvicorn` into `.venv-kb`).
- [ ] **Step 5: Commit** — `git add service/edgequake.py service/llm.py service/app.py service/tests/test_app.py && git commit -m "feat(svc): FastAPI app /ingest /chunks /doc /healthz"`

### Task A4: Community build endpoint (W3, async)

**Files:**
- Modify: `service/app.py` (add `POST /communities/build`)
- Test: `service/tests/test_app.py` (add a case)

**Interfaces:**
- Produces: `POST /communities/build?workspace_id` → `202 {status:"started", workspace_id}`. Runs `kb_pipeline.community.build_workspace_communities(workspace_id, llm=qwen, dsn=KBP_PG_DSN)` in a FastAPI BackgroundTask (non-blocking).

- [ ] **Step 1: Failing test** — assert `POST /communities/build` returns 202 and schedules a background callable (monkeypatch `build_workspace_communities` to a recorder; assert called after request via a threading.Event or direct BackgroundTasks injection).
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** — add endpoint using `from fastapi import BackgroundTasks`; call `community.build_workspace_communities(workspace_id, llm=get_text_llm(), dsn=os.environ["KBP_PG_DSN"])` inside `background_tasks.add_task(...)`; return 202 immediately. Wrap the task body in try/except → log; never raise to caller.
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** — `git commit -am "feat(svc): POST /communities/build (W3, background)"`

---

## Phase B — knowledge_base provider wiring (additive)

> Repo: `/Users/xxx/workspace/99.projects/shinhan_trust/knowledge_base`. Create its own feature branch `feat/kb-pipeline-provider` there. Follow the existing `raganything` provider as the template (see explore findings: `core/pipeline.py` RagAnythingLike + `_ingest_raganything_tail`).

### Task B1: KbPipelineClient

**Files:**
- Create: `backend/app/clients/kb_pipeline_client.py`
- Test: `backend/tests/test_kb_pipeline_client.py`

**Interfaces:**
- Produces: `KbPipelineClient(base_url, timeout=600, max_retries=3)` with `ingest(*, file_bytes, filename, content_type, workspace_id, doc_id) -> KbPipelineIngestOutcome`, `fetch_chunk_meta(workspace_id, doc_id) -> list[dict]`, `delete_doc(workspace_id, doc_id) -> None`, `build_communities(workspace_id) -> None`. `KbPipelineIngestOutcome` has `document_id, chunk_count, status` and `succeeded = status=="completed" and chunk_count>0`.

- [ ] **Step 1: Failing test** — with a mocked httpx transport, assert `ingest` POSTs multipart to `{base}/ingest` and maps `{document_id,chunk_count,status}` → outcome with `succeeded` True when status=="completed" & chunk_count>0; `fetch_chunk_meta` GETs `/chunks`; `delete_doc` DELETEs `/doc`.
- [ ] **Step 2: Run → FAIL** — `cd <kb> && <venv>/bin/python -m pytest backend/tests/test_kb_pipeline_client.py -q`
- [ ] **Step 3: Implement** the client (mirror `raganything_client.py`; multipart upload via httpx; dataclass outcome with `succeeded` property).
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** — `git add backend/app/clients/kb_pipeline_client.py backend/tests/test_kb_pipeline_client.py && git commit -m "feat(provider): KbPipelineClient"`

### Task B2: Protocol + deps + tail + branching

**Files:**
- Modify: `backend/app/core/pipeline.py` (add `KbPipelineLike` Protocol; `PipelineDeps.kb_pipeline`; `_ingest_kb_pipeline_tail`; one branch in `ingest_document`)
- Test: `backend/tests/test_pipeline_kb_pipeline.py`

**Interfaces:**
- Consumes: `KbPipelineClient` (B1); existing `DocumentRecord`, `ChunkingDecision`, `IngestResult`, `deps.repo.persist_success/replace_chunks_meta/set_status` (same as raganything tail).
- Produces: `_ingest_kb_pipeline_tail(file_bytes, filename, *, kb, deps, content_type, rec, decision, replaced_docs_id) -> IngestResult`; `ingest_document` routes `kb.provider=="kb_pipeline"` to it.

- [ ] **Step 1: Failing test** — fake `deps.kb_pipeline` returning a succeeded outcome + chunk meta; assert tail calls `persist_success`, `replace_chunks_meta` with mapped rows, `set_status(...,"ready")`, returns status "ready"; and a failed outcome → `delete_doc` + status "failed".
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** the Protocol, the `PipelineDeps.kb_pipeline: KbPipelineLike | None = None` field, the tail (copy `_ingest_raganything_tail` structure: workspace_id=kb.kb_id, new_docs_id=content_hash[:16], call ingest, judge `succeeded`, persist, fetch_chunk_meta → chunk_rows mapping `{chunk_id,text,hierarchy_path,page_number}`, swap+ready; on success also `deps.kb_pipeline.build_communities(workspace_id)` is NOT called here — enqueued by B5), and the branch `if kb.provider=="kb_pipeline": return _ingest_kb_pipeline_tail(...)` placed right after the raganything branch.
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** — `git commit -am "feat(provider): KbPipelineLike protocol + tail + branching"`

### Task B3: config + DI + provider validation

**Files:**
- Modify: `backend/app/config.py` (add `kb_pipeline_base_url="http://localhost:19000"`, `kb_pipeline_timeout_seconds=600.0`, `kb_pipeline_max_retries=3`)
- Modify: `backend/app/dependencies.py` (`build_pipeline_deps`: build `KbPipelineClient` when `kb_pipeline_base_url` set → `PipelineDeps(..., kb_pipeline=...)`)
- Modify: `backend/app/routers/kb.py` (provider validation tuple: add `"kb_pipeline"`)
- Test: `backend/tests/test_kb_provider_accept.py`

- [ ] **Step 1: Failing test** — `POST /kb` with `provider=kb_pipeline` returns 2xx (not 422); `build_pipeline_deps()` attaches a non-None `kb_pipeline`.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** the three edits.
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** — `git commit -am "feat(provider): config/DI + accept kb_pipeline provider"`

### Task B4: Frontend dropdown option

**Files:**
- Modify: `frontend/app/kb/page.tsx` (add `<option value="kb_pipeline">kb-pipeline (내부 파이프라인)</option>`)

- [ ] **Step 1: Add the option** under the existing `<select id="kb-provider">`.
- [ ] **Step 2: Verify build** — Run: `cd <kb>/frontend && npm run build` (or `npx tsc --noEmit`) → no type errors.
- [ ] **Step 3: Commit** — `git commit -am "feat(provider): kb_pipeline frontend dropdown option"`

### Task B5: Community job after ingest (non-blocking)

**Files:**
- Modify: `backend/app/workers/tasks.py` (after `ingest_document` returns status=="ready" for a kb_pipeline KB, enqueue a community-build job)
- Create: a worker task `BUILD_COMMUNITIES_TASK` that calls `deps.kb_pipeline.build_communities(kb_id)`
- Test: `backend/tests/test_community_job.py`

**Interfaces:**
- Consumes: `KbPipelineClient.build_communities` (B1).
- Produces: an arq task that, on a ready kb_pipeline ingest, calls `build_communities(kb_id)` exactly once; failures are logged, not raised.

- [ ] **Step 1: Failing test** — simulate a ready kb_pipeline ingest result → assert the community task is enqueued with the kb_id; the task calls `build_communities`.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** — guard on `kb.provider=="kb_pipeline" and result.status=="ready"`; enqueue `BUILD_COMMUNITIES_TASK(kb_id)`; the task body try/except → log.
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** — `git commit -am "feat(provider): enqueue community build after kb_pipeline ingest"`

---

## Phase C — Integration smoke

### Task C1: End-to-end smoke with test_doc

**Files:**
- Create: `service/tests/test_e2e_smoke.md` (manual/automated runbook)

- [ ] **Step 1:** Bring up: dedicated edgequake (Task 0), bge-m3 :7997, adaptive_chunk :18060, OCR :18050; start kb-pipeline service `cd /Users/xxx/workspace/8.kb-pipeline && KBP_OPENAI_API_KEY=$(...) KBP_PG_DSN=postgres://edgequake:edgequake_secret@localhost:5433/edgequake .venv-kb/bin/uvicorn service.app:app --port 19000`.
- [ ] **Step 2:** Verify `curl :19000/healthz` and a direct `POST /ingest` with a real `test_doc` PDF (`/Users/xxx/workspace/8.kb-pipeline/test_doc/3-3. 휴가규정*.pdf`) → `{chunk_count>0,status:"completed"}`; `GET /chunks` returns chunks whose text includes a `〈MODAL` table span.
- [ ] **Step 3:** In knowledge_base: start backend (:8001) with `kb_pipeline_base_url=http://localhost:19000` + frontend; create a KB with provider=kb_pipeline; upload the same test_doc; confirm doc_guard runs, document reaches status=ready, and chunks render on `/kb/{id}/documents/{docId}`.
- [ ] **Step 4:** Confirm a community-build job was enqueued (and, when it finishes, `community_reports` rows exist for the KB workspace).
- [ ] **Step 5: Commit** the runbook + a short results note.

---

## Self-Review

- **Spec coverage:** A (service: parse A1, ingest A2, endpoints A3, community A4) + dedicated edgequake (Task 0) + B (client B1, protocol/tail B2, config/validation B3, frontend B4, community job B5) + smoke (C1). doc_guard/dedup/chunks_meta/UI reused (no task — intentional). All spec §3/§4/§7/§8 mapped.
- **Open trace items to resolve during implementation (flagged, not placeholders):** (i) exact edgequake chunk-fetch route for `EdgequakeClient.fetch_chunks` (Task A3 note — verify in routes.rs); (ii) exact OCR/VLM `:18050` request/response contract for `_parse_structural` (Task A1 — verify against ocr_http_adapter); (iii) the knowledge_base `_ingest_raganything_tail` exact signature to mirror in B2. Each names where to confirm.
- **Types:** `IngestOutcome`/`KbPipelineIngestOutcome` keys (`document_id, chunk_count, status`, `succeeded`) consistent A2↔B1↔B2; chunk row keys (`chunk_id, text, hierarchy_path, page_number`) consistent A3↔B2.
