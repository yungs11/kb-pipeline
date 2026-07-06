<!-- plan-version: v7 -->
<!-- codex-validation: READY v7 at 2026-07-06T06:45:41Z (ultracode competitive validation — 4-lens panel × 7 rounds: 9→3→4→3→3→4→0 must-fix; codex backend hung, per-pref substitute) -->
<!-- v6→v7: enumerate all 3 kbp fakes (_FakeKbp / _ParseKbp / FakeKbPipeline) + rewrite _ExplodingParseKbp (reject-skips-parse premise inverted); hoist kb_pipeline-None guard + docs_id above the parse in C2; FakeKbPipeline gains a gate_summary ctor param. -->
<!-- v5→v6: reject row keeps FULL content_hash (docs_id[:16] only for kbp.parse arg); corrected test line refs — KEEP check_excel_calls (154/189, 281-283), FLIP kbp.parse_calls (159, 291) + excel_parser.calls (287-289). -->
<!-- v3→v4: C3 precision — kb.pre_parsed field (not a tail kwarg); preserve gate guards; docs_id=content_hash[:16] at gate; FakeKbPipeline exists. -->
<!-- v4→v5: FAIL-CLOSED at both gate sites (missing gate_summary → reject, not ungated pass); B2 live xlsx /parse top-level gate_summary check before C5 cutover; C3 reject test asserts chunk_calls/insert_calls empty (parse_calls now non-empty on reject). -->

# parse-preview off :18055 — parse-svc surfaces gate_summary (finish Phase 2b) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Excel parse-preview (and main-ingest Excel gating) work without the dead `:18055` service by having parse-svc compute `gate_summary` in-process (via the already-vendored `excel_parser_rag.gate.compute_gate_summary`) and surface it on `/parse`; then rewire kb-backend to consume that `gate_summary` and retire the `:18055` `ExcelParserClient`.

**Architecture:** Completes the Phase-2b in-process migration that vendored the parser but left `gate_summary` orphaned. parse-svc's excel path already writes a temp file and calls `get_backend().parse(tmp_path)` — we add `compute_gate_summary(tmp_path, chunks)` there and thread it into the `/parse` response. The facade `/parse` already returns the whole parse-svc dict (passthrough), so `gate_summary` flows through unchanged. kb-backend's `parse_preview_task` and main `pipeline.py` stop calling `:18055` and instead read `gate_summary` from the `kb_pipeline.parse()` result, still handing it to `doc_guard.check_excel(...)` (identical shape, since it's the same `compute_gate_summary`). `ExcelParserClient` + `:18055` config are removed.

**Tech Stack:** parse-svc + facade = kb-pipeline repo (FastAPI, pytest via `.venv-kb/bin/python`, deployed as compose `kbp-parse-svc-1`/`kbp-facade-1` **baked images → rebuild to deploy**). kb-backend = FastAPI (pytest via `.venv/bin/python`, sqlite in-memory), host uvicorn `:8088`.

## Global Constraints

- **Repos:** kb-pipeline = `/Users/xxx/workspace/8.kb-pipeline` (branch `feat/kb-pipeline-provider`); kb-backend = `/Users/xxx/workspace/99.projects/shinhan_trust/knowledge_base` (branch `feat/group-admin-ui`).
- **gate_summary is the SAME function everywhere:** `excel_parser_rag.gate.compute_gate_summary(input_path, chunks)` (vendored at `parse_service/parsers/excel/excel_parser_rag/gate/excel_gate.py:78`; the `:18055` service used the identical function at `7.excel-parser/service/main.py:123`). Do NOT reimplement or reshape it — call it and pass its output through verbatim, so `doc_guard.check_excel` needs zero change.
- **`chunks` passed to `compute_gate_summary` are the RAW rag-chunk dicts** (the `get_backend().parse()` output), NOT the facade-normalized chunks. In parse-svc call it inside `_fetch_rag_chunks` where the raw `chunks` + `tmp_path` are both in scope, BEFORE `tmp_path.unlink()`.
- **Excel gating happens post-parse** (per `docs/superpowers/specs/2026-06-29-excel-gate-postparse-design.md`): parse → gate on `gate_summary` → reject/pass. The rewire must preserve that order and the existing `gate_popup` reject shape (`doc_guard` returns `CheckReport`, UI `GatePopup` unchanged).
- **parse-svc/facade deploy = compose rebuild** (baked images, no bind mount): `docker compose -p kbp build parse-svc facade && docker compose -p kbp up -d parse-svc facade`. A restart alone does NOT pick up code.
- **The 11 pre-existing failures are ORTHOGONAL PDF-gate tests** (verified: all in test_pipeline/test_job_status/raganything/ragflow, driven by `make_pdf` + RRN/PII doc_guard rules — ZERO xlsx/gate_summary/excel_parser/check_excel references). This Excel rewire does NOT touch or flip them. Success criterion: after the change, failures stay ≤ the same 11, **none Excel-related**. Do not attempt to fix the PDF-gate 11.
- **Scope = Excel GATE call sites ONLY.** `deps.excel_parser` (`:18055` `ExcelParserClient`) has a SECOND live consumer besides the gate: the **dify Excel-ingest lane** (`pipeline.py:638` `if deps.excel_dispatch_enabled and _is_excel_filename(...)` → `_ingest_excel_tail` → `pipeline.py:1162` `deps.excel_parser.parse(...)`; `excel_dispatch_enabled = settings.excel_parser_enabled`, default True). This rewire ONLY replaces the two GATE call sites (`tasks.py:420-421`, `pipeline.py:490-496`). It MUST keep `ExcelParserClient`, `PipelineDeps.excel_parser`, and `excel_parser_*` config intact for the dify lane. Retiring the dify lane is explicitly OUT OF SCOPE.
- **kb-backend test conventions:** sqlite in-memory + create_all; `db_session`/`app_client` fixtures; `seed_user`/`auth_header_for`/`make_kb` are FUNCTIONS imported `from .conftest import ...`.
- **The Excel-gate tests (verified — currently PASSING, this rewire WILL flip/break them). THREE files, THREE kbp fakes — each kbp fake's `parse()` must ADD `gate_summary` to its return so the gate reads it from kbp.parse:**
  - `backend/tests/test_parse_preview_gate.py` — kbp fake `_FakeKbp` (`:52-62`); excel fake `_FakeExcelParser`; docguard `check_excel_calls`.
  - `backend/tests/test_worker_parse_preview.py` — kbp fake `_ParseKbp` (`:43-51`) + **`_ExplodingParseKbp(_ParseKbp)` (`:54-58`) whose `.parse()` RAISES `AssertionError("게이트 rejected 인데 kbp.parse() 가 호출됨")`**, used by `test_gate_rejected_writes_rejected_sidecar_and_skips_parse` (`:136-166`, instantiated `:153`); result fake `_FakeParseResult` (`:61`).
  - `backend/tests/test_pipeline_excel_gate.py` — main-ingest gate; excel fakes `FakeGateExcelParser`/`FakeGateSummaryParseResult`/`FakeGateDocGuard`; kbp fake **`FakeKbPipeline`** imported `:27` (defined `test_pipeline_kb_pipeline.py:100`, `.parse()` `~:148`, ctor has NO gate_summary param).
  - **KEEP** the `check_excel_calls == 1` assertions (`test_parse_preview_gate.py:154,189`; `test_pipeline_excel_gate.py:281-283`) — check_excel is still called once. **FLIP/REMOVE** the reject-skips-parse assertions inverted by parse-first: `test_parse_preview_gate.py:159` (`kbp.parse_calls == []`), `test_pipeline_excel_gate.py:287-289` (`excel_parser.calls`) + `:291` (`kbp.parse_calls == []`), and the whole `_ExplodingParseKbp`/`test_gate_rejected_..._skips_parse` premise (parse NOT called on reject) — the reject path now MUST call kbp.parse to obtain gate_summary. Keep a fake excel_parser only where the **dify lane** test still needs it.
- **parse-svc tests** run via `.venv-kb/bin/python -m pytest parse_service/tests/...`.

---

## File Structure

### kb-pipeline (parse-svc + facade)
- **Modify** `parse_service/parsers/excel/excel_parser_rag/gate/__init__.py` — already exports `compute_gate_summary` (no change; verify import path).
- **Modify** `parse_service/parsers/excel/__init__.py` — `_fetch_rag_chunks` computes `gate_summary`; `parse()` carries it out.
- **Modify** `parse_service/parsers/__init__.py` — add `gate_summary` to `RouteResult`.
- **Modify** `parse_service/app.py` — excel branch of `run_parse`/`/parse` includes `gate_summary` in the response dict.
- **Verify (likely no change)** `service/app.py` `/parse` — already passthrough; add a passthrough test.
- **Create/Modify** `parse_service/tests/test_excel_gate_summary.py` — assert `/parse` on an xlsx returns `gate_summary`.

### kb-backend
- **Modify** `backend/app/clients/kb_pipeline_client.py` — ensure `parse()` return dict surfaces `gate_summary`.
- **Modify** `backend/app/workers/tasks.py` — `parse_preview_task`: drop `:18055` call; gate on `result["gate_summary"]`.
- **Modify** `backend/app/core/pipeline.py` — main-ingest Excel GATE call site only (`~490-496`): gate on `gate_summary` from `kbp.parse(...)`. **Leave the dify Excel-ingest lane (`pipeline.py:638`/`1162`) and `deps.excel_parser` intact.**
- **KEEP** `backend/app/clients/excel_parser_client.py`, `PipelineDeps.excel_parser`, and `excel_parser_*` config — still used by the dify lane. (No deletion. `dependencies.py` unchanged.)
- **Modify** `backend/tests/*` — parse_preview/pipeline Excel-GATE tests: drive the gate via `gate_summary` in the fake kb_pipeline parse result instead of a fake excel-parser gate call.

---

## Phase A — parse-svc surfaces `gate_summary` (kb-pipeline)

### Task A1: `RouteResult` carries `gate_summary`

**Files:**
- Modify: `parse_service/parsers/__init__.py`

**Interfaces:**
- Produces: `RouteResult` gains an optional `gate_summary: dict | None = None` field (default None so non-excel routes are unaffected).

- [ ] **Step 1: Read `RouteResult`** to see its shape (dataclass? NamedTuple?).

Run: `grep -n "class RouteResult\|RouteResult\|gate_summary\|kind\|chunks" parse_service/parsers/__init__.py`
Expected: locate the definition + fields (`kind`, `chunks`, …).

- [ ] **Step 2: Add the field.** If it's a dataclass, add `gate_summary: dict | None = None` (must come after any non-default fields). If NamedTuple, add with a default. Keep all existing fields/order for the text path.

- [ ] **Step 3: Verify import + default.**

Run: `cd /Users/xxx/workspace/8.kb-pipeline && .venv-kb/bin/python -c "from parse_service.parsers import RouteResult; r=RouteResult(kind='chunks', chunk_needed=False, chunks=[]); print('gate_summary default:', r.gate_summary)"`
Expected: `gate_summary default: None` (use the REAL required fields — `RouteResult` requires `chunk_needed`; confirm the full required-arg set in Step 1 and match it here).

- [ ] **Step 4: Commit.**

```bash
git add parse_service/parsers/__init__.py
git commit -m "feat(parse-svc): RouteResult carries optional gate_summary"
```

### Task A2: Excel path computes `gate_summary`

**Files:**
- Modify: `parse_service/parsers/excel/__init__.py`

**Interfaces:**
- Consumes: `RouteResult.gate_summary` (A1); `excel_parser_rag.gate.compute_gate_summary`.
- Produces: `parse(file_bytes, filename)` returns a `RouteResult` whose `gate_summary` = `compute_gate_summary(tmp_path, raw_chunks)`.

- [ ] **Step 1: Write the failing test** — `parse_service/tests/test_excel_gate_summary.py`. There is **no `.xlsx` fixture anywhere in kb-pipeline** (verified `find . -name '*.xlsx'` → empty), so BUILD one in-memory with openpyxl and force the `openpyxl` backend (avoids kordoc/java deps):

```python
def _tiny_xlsx_bytes() -> bytes:
    import io
    from openpyxl import Workbook
    wb = Workbook(); ws = wb.active
    ws.append(["항목", "값"]); ws.append(["a", "1"]); ws.append(["b", "2"])
    buf = io.BytesIO(); wb.save(buf); return buf.getvalue()


def test_excel_parse_returns_gate_summary(monkeypatch):
    monkeypatch.setenv("EXCEL_PARSER_BACKEND", "openpyxl")  # no kordoc/java in CI
    from parse_service.parsers.excel import parse
    rr = parse(_tiny_xlsx_bytes(), "tiny.xlsx")
    assert rr.gate_summary is not None
    assert isinstance(rr.gate_summary, dict)
```

> Confirm `openpyxl` is importable in `.venv-kb` (it is — the parser depends on it). If the `openpyxl` backend name differs (`get_backend("openpyxl")`), match the real backend registry name.

- [ ] **Step 2: Run — expect fail.**

Run: `cd /Users/xxx/workspace/8.kb-pipeline && .venv-kb/bin/python -m pytest parse_service/tests/test_excel_gate_summary.py -v`
Expected: FAIL (`rr.gate_summary is None`).

- [ ] **Step 3: Implement** — in `_fetch_rag_chunks`, compute gate_summary before unlink and return it alongside chunks; in `parse()`, put it on the RouteResult:

```python
def _fetch_rag_chunks(file_bytes, filename, excel_url=None):
    from parse_service.parsers.excel.excel_parser_rag.backends import get_backend
    from parse_service.parsers.excel.excel_parser_rag.config import ParserConfig
    from parse_service.parsers.excel.excel_parser_rag.gate import compute_gate_summary
    # ... existing config + tmp file write ...
    try:
        chunks, _stats = get_backend(config.backend).parse(tmp_path, config)
        raw = [c if isinstance(c, dict) else c.__dict__ for c in (chunks or [])]
        try:
            gate_summary = compute_gate_summary(tmp_path, raw)
        except Exception as exc:  # gate must never break parsing
            gate_summary = {"ok": False, "sheets": [], "error": str(exc)}
        return raw, gate_summary
    finally:
        tmp_path.unlink(missing_ok=True)
```

Update `parse()`. **Critical:** the current code raises `ParserError` on zero chunks (verified `parsers/excel/__init__.py:76-77` `if not chunks: raise ParserError(...)`), which would discard the just-computed `gate_summary` — exactly the broken-xlsx case that must reject cleanly via the gate. Do NOT raise on empty when a gate_summary exists; return a `RouteResult` with empty chunks + gate_summary so kb-backend's gate can fire:

```python
def parse(file_bytes, filename, *, excel_url=None):
    try:
        rag_chunks, gate_summary = _fetch_rag_chunks(file_bytes, filename, excel_url)
    except ParserError:
        raise
    except Exception as e:
        raise ParserError(f"excel parse failed for {filename}: {e}") from e
    chunks = normalize_chunks(rag_chunks)
    # was: `if not chunks: raise ParserError(...)` — REMOVE the raise. Empty chunks +
    # a computed gate_summary is a valid "broken excel -> let the gate reject" outcome.
    return RouteResult(kind="chunks", chunk_needed=False, chunks=chunks, gate_summary=gate_summary)
```

> Match the REAL `RouteResult(...)` construction the file already uses (fields/order) — only ADD `gate_summary=gate_summary`. `_fetch_rag_chunks` now returns a `(raw_chunks, gate_summary)` tuple; `parse()` is its only caller (grep to confirm). For the gate error fallback inside `_fetch_rag_chunks`, mirror the `:18055` service's shape (`7.excel-parser/service/main.py:126`) — a dict with the same keys `compute_gate_summary` normally emits plus an `error` key (confirm the real success shape from `excel_parser_rag/gate/excel_gate.py`, do not invent `{ok:False}`).

- [ ] **Step 3b: Update the existing tests that break on the tuple return + removed raise** in `parse_service/tests/test_parser_excel.py`:
  - The two monkeypatch fakes (`:10` `lambda fb, fn, excel_url: rag`, `:33` `lambda fb, fn, excel_url: []`) must return `(chunks, gate_summary)` tuples: `lambda fb, fn, excel_url: (rag, {"sheets": []})` and `lambda fb, fn, excel_url: ([], {"sheets": []})`.
  - **`test_empty_chunks_raise` (`:32-35`) must be rewritten to the NEW contract** — empty chunks no longer raise; they return a RouteResult carrying gate_summary (so a broken excel is gate-rejected downstream, not crashed). Replace the `with pytest.raises(ParserError):` body with:

```python
    res = excel_parser.parse(b"PK", "a.xlsx", excel_url="http://x")
    assert res.kind == "chunks" and res.chunks == [] and res.gate_summary is not None
```

  Rename it (e.g. `test_empty_chunks_returns_gate_summary`). Then run `test_parser_excel.py` to confirm no regression.

- [ ] **Step 4: Run — expect pass.**

Run: `.venv-kb/bin/python -m pytest parse_service/tests/test_excel_gate_summary.py -v`
Expected: PASS

- [ ] **Step 5: Run the excel parser test suite for regressions.**

Run: `.venv-kb/bin/python -m pytest parse_service/tests/ -q 2>&1 | tail -8`
Expected: existing parse-svc tests still pass.

- [ ] **Step 6: Commit.**

```bash
git add parse_service/parsers/excel/__init__.py parse_service/tests/test_excel_gate_summary.py parse_service/tests/fixtures/ 2>/dev/null
git commit -m "feat(parse-svc): compute gate_summary in-process on excel parse (finish Phase 2b)"
```

### Task A3: `/parse` response includes `gate_summary`

**Files:**
- Modify: `parse_service/app.py`

**Interfaces:**
- Produces: `POST /parse` on an xlsx returns a body that includes `gate_summary` (from `RouteResult.gate_summary`) alongside `enriched_content`, `chunks`, `chunk_strategy`.

- [ ] **Step 1: Read the excel branch** of `run_parse`/`/parse` (`app.py` around the `rr.kind == "chunks"` return, ~line 230) to see the response dict.

Run: `sed -n '215,250p' parse_service/app.py`

- [ ] **Step 2: Write failing test** — extend `test_excel_gate_summary.py` with an app-level test using `TestClient(app)` posting the xlsx fixture to `/parse` and asserting `gate_summary` in the JSON. (Mirror any existing parse-svc app test for the multipart shape: `file` + `filename` form field.)

- [ ] **Step 3: Run — expect fail** (response omits gate_summary).

- [ ] **Step 4: Implement** — in the `rr.kind == "chunks"` return dict, add `"gate_summary": rr.gate_summary`:

```python
        if rr.kind == "chunks":
            return {
                "enriched_content": "\n\n".join(c.get("text", "") for c in rr.chunks),
                "n_blocks": len(rr.chunks),
                "chunks": rr.chunks,
                "gate_summary": rr.gate_summary,
                # ... existing keys (docs_id, chunk_strategy, page_*, ...) ...
            }
```

> There is exactly ONE excel-return block (verified `grep -c 'rr.kind == "chunks"' parse_service/app.py` → 1, in `run_parse` at ~`app.py:230-247`; the `/parse` handler just delegates to `run_parse`). Add `"gate_summary": rr.gate_summary` ONCE to that single dict; keep every other key byte-identical.

- [ ] **Step 5: Run — expect pass.**
- [ ] **Step 6: Commit.**

```bash
git add parse_service/app.py parse_service/tests/test_excel_gate_summary.py
git commit -m "feat(parse-svc): /parse surfaces gate_summary for excel"
```

---

## Phase B — facade passthrough (kb-pipeline)

### Task B1: Verify facade `/parse` passes `gate_summary` through

**Files:**
- Modify (test only, likely): `service/tests/` + possibly `service/app.py`

**Interfaces:**
- Produces: facade `POST /parse` returns `gate_summary` when parse-svc does (it already `return parsed`, passthrough at `service/app.py:112`).

- [ ] **Step 1: Confirm passthrough.** Read `service/app.py:93-112` — it returns the whole `parsed` dict (only `setdefault("chunk_strategy", ...)`). So `gate_summary` flows through with NO code change.

- [ ] **Step 2: Add a passthrough test** in `service/tests/` — mock/fake the `ParseSvcClient.parse` to return a dict containing `gate_summary`, POST to facade `/parse`, assert the response includes `gate_summary`. (Mirror an existing facade parse test's fake-client pattern.)

- [ ] **Step 3: Run.**

Run: `cd /Users/xxx/workspace/8.kb-pipeline && .venv-kb/bin/python -m pytest service/tests/ -q 2>&1 | tail -8`
Expected: PASS (45 existing + new).

- [ ] **Step 4: Commit.**

```bash
git add service/app.py service/tests/
git commit -m "test(facade): assert /parse passes gate_summary through"
```

### Task B2: Rebuild + deploy parse-svc + facade

**Files:** none (deploy).

- [ ] **Step 1: Rebuild + recreate** (baked compose images):

Run: `cd /Users/xxx/workspace/8.kb-pipeline && docker compose -p kbp build parse-svc facade && docker compose -p kbp up -d parse-svc facade`
Expected: both images built, containers recreated.

- [ ] **Step 2: Verify healthy AND that `/parse` actually returns a TOP-LEVEL `gate_summary`** (health alone is insufficient — a silent-ungate window opens if kb-backend cuts over while the OLD parse-svc image serves). Build a tiny xlsx in-memory and POST it to the running **facade** `:19000/parse`:

```bash
for s in parse-svc facade; do echo "$s:" $(docker inspect -f "{{.State.Health.Status}}" kbp-$s-1); done
.venv-kb/bin/python - <<'PY'
import io, requests
from openpyxl import Workbook
wb=Workbook(); ws=wb.active; ws.append(["항목","값"]); ws.append(["a","1"])
buf=io.BytesIO(); wb.save(buf)
r=requests.post("http://localhost:19000/parse",
    files={"file":("tiny.xlsx", buf.getvalue())},
    data={"filename":"tiny.xlsx","content_type":"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
    timeout=120)
j=r.json()
assert "gate_summary" in j, f"NO top-level gate_summary: keys={list(j)}"
assert "stats" not in j or "gate_summary" not in (j.get("stats") or {}), "gate_summary must be TOP-LEVEL, not nested under stats"
print("OK: facade /parse returns top-level gate_summary")
PY
```
Expected: both `healthy` + `OK: facade /parse returns top-level gate_summary`. **Do NOT restart kb-backend (Task C5) until this check passes** — Phase C code assumes top-level `gate_summary`; cutting over against an old parse-svc image would (correctly, per the C2/C3 fail-closed guard) reject all xlsx. This live check + fail-closed together close the rollout-window ungate.

- [ ] **Step 3: Commit** — no code; note the deploy in the runbook if one is tracked.

---

## Phase C — kb-backend rewire off `:18055`

### Task C1: `KbPipelineClient.parse` surfaces `gate_summary`

**Files:**
- Modify: `backend/app/clients/kb_pipeline_client.py`
- Test: `backend/tests/` (client unit or via task test)

**Interfaces:**
- Produces: `KbPipelineClient.parse(...)` return dict includes `gate_summary` (None for non-excel).

- [ ] **Step 1: Read `parse()`** (`kb_pipeline_client.py:145-188`) — verified: it is **synchronous + keyword-only** and returns an **explicitly key-mapped dict** (`enriched_content, n_blocks, modal_spans, chunks, chunk_strategy, docs_id, page_count, pages, page_spans, timing_metrics`), NOT `resp.json()` verbatim. So `gate_summary` is currently DROPPED — this is a REQUIRED change, not conditional.

Run: `sed -n '145,190p' backend/app/clients/kb_pipeline_client.py`

- [ ] **Step 2: Add** `"gate_summary": body.get("gate_summary"),` (top-level; `None` for non-excel) to that returned dict. Keep it keyword-only + sync.

- [ ] **Step 3: Test** — a unit test with a fake HTTP layer returning a body with `gate_summary`, assert `.parse(...)["gate_summary"]` is surfaced. Run via `.venv/bin/python -m pytest`.

- [ ] **Step 4: Commit.**

```bash
git add backend/app/clients/kb_pipeline_client.py backend/tests/
git commit -m "feat(kb-backend): KbPipelineClient.parse surfaces gate_summary"
```

### Task C2: `parse_preview_task` gates on `gate_summary` (drop :18055)

**Files:**
- Modify: `backend/app/workers/tasks.py` (`parse_preview_task`, lines ~377-508)
- Test: `backend/tests/` (parse_preview task test)

**Interfaces:**
- Consumes: `deps.kb_pipeline.parse(...)["gate_summary"]`, `deps.docguard.check_excel(gate_summary, file_name)`.
- Produces: excel gating uses the parse result's `gate_summary`; NO `deps.excel_parser` call. Reject/pass/sidecar behavior unchanged.

- [ ] **Step 1: Read the current excel-gate block** (tasks.py:418-456) — the `deps.excel_parser.parse()` call (line 421) → `gate_summary` → `deps.docguard.check_excel(...)` (line 423), then the main `deps.kb_pipeline.parse()` (line 451).

- [ ] **Step 2: Adapt the existing parse-preview gate tests** across BOTH files:
  - `backend/tests/test_parse_preview_gate.py` — add `gate_summary` to `_FakeKbp.parse()` (`:52-62`). **KEEP** `:154,189` (`len(docguard.check_excel_calls) == 1`). **Replace `:159`** (`assert kbp.parse_calls == []`, "rejected 시 kbp.parse 미호출") — the reject case now calls `kbp.parse` first, so `parse_calls` has 1 entry; assert reject evidence still true instead: sidecar `status == "rejected"` + no chunk/insert follow-on.
  - `backend/tests/test_worker_parse_preview.py` — add `gate_summary` to `_ParseKbp.parse()` (`:43-51`). **Rewrite `_ExplodingParseKbp` + `test_gate_rejected_writes_rejected_sidecar_and_skips_parse` (`:54-58`, `:136-166`):** its premise (parse NOT called on reject) is INVERTED by the rewire — the reject path MUST call `kbp.parse` to get gate_summary. Delete the exploding fake; use a `_ParseKbp` returning a fail-inducing `gate_summary`; assert reject via sidecar `status=="rejected"` + `enriched_content` absent + no chunk/insert follow-on (drop the "skips_parse" expectation).
  - Both files: keep asserting fake gate_summary → docguard `result:"fail"` ⇒ sidecar `{status:"rejected", gate, doc_guard_result}`; `result:"pass"` ⇒ `{status:"ready", ...}` (advisory findings still surfaced).

- [ ] **Step 3: Rewire** — reorder so parse happens first, then gate on its `gate_summary`. **Verified facts:** `deps.kb_pipeline.parse` is **sync + keyword-only** (NO `await`); `deps.docguard.check_excel(gate_summary, file_name)` returns a **plain `dict`** (`docguard_client.py:102`), and the reject condition is `report.get("result") == "fail"` (tasks.py:424); the advisory-pass path keys off `report.get("findings")` (tasks.py:470). `docs_id = content_hash(file_bytes)[:16]` must be preserved.

```python
    # HOIST above the parse (they currently sit AFTER the gate, tasks.py:441-448 + :450):
    if deps.kb_pipeline is None:
        # write the existing "failed" sidecar and return (as today, tasks.py:441-448)
        <existing kb_pipeline-None failure path>
        return
    docs_id = content_hash(file_bytes)[:16]                 # was tasks.py:450 (after gate) — move up
    parsed = deps.kb_pipeline.parse(
        file_bytes=file_bytes, filename=filename,
        content_type=mime_type, docs_id=docs_id,
    )
    gate_summary = parsed.get("gate_summary")
    if _is_excel_filename(file_name):                      # guard on file type ONLY (fail-closed)
        if gate_summary is None:
            # FAIL-CLOSED (spec §8 추출불가=적재불가): an xlsx MUST carry a gate_summary.
            # Its absence (e.g. parse-svc rollout incomplete / route not computing it) REJECTS —
            # never pass ungated. Build a synthetic fail report and reuse the reject sidecar.
            report = {"result": "fail", "findings": [], "customer_message": "gate_summary 없음 — 파서 게이트 미계산", "error": "gate_summary absent"}
            <write the exact same rejected sidecar as today, doc_guard_result=report>
            return
        report = deps.docguard.check_excel(gate_summary, file_name)
        if report.get("result") == "fail":
            # REUSE the existing rejected-sidecar construction VERBATIM (tasks.py:425-437):
            #   {status:"rejected", gate:_build_gate_popup(report).model_dump(),
            #    doc_guard_result:report, kb_id, file_name, ...}
            <write the exact same rejected sidecar as today>
            return
        # advisory-pass warnings still surface via report.get("findings") in the success sidecar (tasks.py:470)
    # ... existing validation (enriched_content/chunks) + success sidecar, using `parsed` ...
```

> Do NOT invent the sidecar dict — copy the EXACT keys the current code writes at `tasks.py:425-437` (reject) and the advisory branch at `~470`, only changing the `gate_summary` SOURCE (now `parsed["gate_summary"]` instead of `deps.excel_parser.parse(...)`). Remove ONLY the `deps.excel_parser`-based gate branch (tasks.py:420-421); keep `_is_excel_filename`. Do not touch the dify lane.
>
> **Fail-closed semantics (deliberate, spec §8):** the guard is `if _is_excel_filename(...)` — NOT `and gate_summary is not None`. An xlsx with a MISSING gate_summary rejects (never passes ungated). This replaces the old `gate_summary = ... or {'ok':True,'sheets':[]}` pass-default, which is unsafe now that gate_summary comes from parse-svc (rollout window). Note the two distinct None-vs-fail cases: (a) compute FAILURE inside parse-svc → gate_summary = `{...,"error":...}` from `compute_gate_summary`'s fallback (A2), which `check_excel` judges; (b) gate_summary KEY ABSENT (old parse-svc image) → synthetic `{"result":"fail",...}` reject here. Verify `_build_gate_popup(report)` renders the synthetic report (reads `result`/`findings`/`customer_message`).

- [ ] **Step 4: Run — expect pass.**

Run: `cd /Users/xxx/workspace/99.projects/shinhan_trust/knowledge_base && .venv/bin/python -m pytest backend/tests/ -q -k "parse_preview or preview" -v 2>&1 | tail -20`

- [ ] **Step 5: Commit.**

```bash
git add backend/app/workers/tasks.py backend/tests/
git commit -m "feat(kb-backend): parse_preview gates on parse-svc gate_summary (drop :18055)"
```

### Task C3: Main-ingest `pipeline.py` excel gating off :18055

**Files:**
- Modify: `backend/app/core/pipeline.py`
- Test: `backend/tests/test_pipeline*.py`

**Interfaces:**
- Produces: the main-ingest Excel GATE (in `ingest_document`) gates on `gate_summary` from `kbp.parse(...)`, then sets `kb.pre_parsed = parsed` so the tail reuses it (no second parse). `deps.excel_parser` stays (dify lane).

> **Structural reality (verified — a source swap is IMPOSSIBLE):** the Excel gate is at `pipeline.py:490-520` inside `ingest_document` (def `454`); it currently calls `deps.excel_parser.parse(file_bytes)` for `gate_summary`, and on `report.get("result")=="fail"` builds a **rejected documents row** (`content_hash` → `create_document(status="rejected")` → `set_doc_guard_result`) and `return IngestResult(status="rejected", document_id=..., gate_popup=_build_gate_popup(report), detail=...)`. Only on pass does it `return _ingest_kb_pipeline_tail(...)` (line `607`), and `kbp.parse(...)` lives in the tail (`line 2010`, def `1915`). So there is NO `parsed` dict at the gate site to read.

- [ ] **Step 1: Read** `ingest_document` gate (`pipeline.py:454-607`, esp. `490-520`) + the tail's parse (`_ingest_kb_pipeline_tail:1915`, `pre_parsed` handling `~2004-2028` where `pre_parsed is None → kbp.parse` else reuse). Confirm the tail already accepts + reuses `pre_parsed`.
- [ ] **Step 2: Rewire (reuse the existing `kb.pre_parsed` field — no double parse).** At the Excel gate site in `ingest_document` (`pipeline.py:479-521`):
  - **Keep the existing gate guard verbatim** — `ext in ("xlsx", "xlsm")` (literal, NOT `_is_excel_filename` which also matches `xls`) AND `kb.provider == "kb_pipeline"` AND `kb.pre_parsed is None`. Replace only the `deps.excel_parser is not None` clause with `deps.kb_pipeline is not None`.
  - Compute `docs_id` at the gate (it is NOT in scope there): `docs_id = content_hash(file_bytes)[:16]` — use it ONLY as the `deps.kb_pipeline.parse(..., docs_id=docs_id)` arg. **Do NOT write this truncated value into the reject row's `content_hash`** — keep the reject row's `content_hash = content_hash(file_bytes)` FULL, exactly as today (`pipeline.py:499/511`; the normal row is full at `524`; the tail truncates a full stored hash: `new_docs_id = (rec.content_hash or content_hash(file_bytes))[:16]` at `1968`). Both `docs_id` and the row's hash derive from the same full hash, so they agree without overwriting `content_hash`.
  - Replace `deps.excel_parser.parse(file_bytes)` with `parsed = deps.kb_pipeline.parse(file_bytes=file_bytes, filename=filename, content_type=content_type, docs_id=docs_id)`; read `gate_summary = parsed.get("gate_summary")`.
  - **Fail-closed (spec §8, same as C2):** if `gate_summary is None`, set `report = {"result":"fail","findings":[],"customer_message":"gate_summary 없음 — 파서 게이트 미계산","error":"gate_summary absent"}` (do NOT call `check_excel`); else `report = deps.docguard.check_excel(gate_summary, filename)`. (The gate guard is file-type based — `ext in ("xlsx","xlsm")` — so a missing gate_summary rejects, never passes ungated. This replaces the old `or {'ok':True,'sheets':[]}` pass-default.)
  - On `report.get("result") == "fail"`: keep the EXISTING reject path VERBATIM (content_hash → `create_document(status="rejected")` → `set_doc_guard_result` → `return IngestResult(status="rejected", ..., gate_popup=_build_gate_popup(report), detail=...)`).
  - On PASS: **set `kb.pre_parsed = parsed`** (KbContext is a non-frozen dataclass; the tail reads `pre_parsed = kb.pre_parsed` at `~2004` and skips `kbp.parse` at `~2010`). Do NOT add a `pre_parsed=` kwarg to the `_ingest_kb_pipeline_tail(...)` call at `607` — that param does not exist; leave the `607` call as-is (`kb=kb`).
  - **Do NOT move the reject-row construction; keep it at the gate.** Leave the dify lane (`638`/`1162`) untouched.
- [ ] **Step 3: Adapt the main-ingest gate test** — `backend/tests/test_pipeline_excel_gate.py` (excel fakes `FakeGateExcelParser`/`FakeGateSummaryParseResult`/`FakeGateDocGuard`; kbp fake `FakeKbPipeline` imported from `test_pipeline_kb_pipeline.py:100`, `.parse()` at `~:148`). **Give `FakeKbPipeline` a `gate_summary=None` ctor param** (`test_pipeline_kb_pipeline.py:100` ctor currently has none), store `self._gate_summary`, and return it from `.parse()`'s dict (`~:148`). Then in `test_pipeline_excel_gate.py` construct `FakeKbPipeline(gate_summary=gate_summary)` in the fail/pass tests (a fixed return dict CANNOT equal the per-test local `gate_summary` at `:263`, and `FakeKbPipeline` is shared by many other passing tests, so a hardcoded value would break them). **KEEP `test_pipeline_excel_gate.py:281-283`** (`check_excel_calls == 1` / filename / `gate_summary == gate_summary`) — valid once the ctor threads the per-test `gate_summary` through. **Remove the reject-path breakers:** `:287-289` (`len(excel_parser.calls) == 1` — the gate no longer calls excel_parser) and `:291` (`kbp.parse_calls == []` — the gate now calls `kbp.parse` before the fail check, so it's non-empty). Replace the "tail-not-entered" evidence with `kbp.chunk_calls == []` and `kbp.insert_calls == []` (`FakeKbPipeline` tracks both, `test_pipeline_kb_pipeline.py:148-150`) — the chunk/insert stages must not run on a gate reject. Assert: reject row + `gate_popup` on `result:"fail"`; on `result:"pass"`, `kb.pre_parsed` is set and the tail runs (no re-parse). (The 11 pre-existing failures are PDF-gate — `make_pdf`, RRN/PII — ORTHOGONAL; do not touch.)
- [ ] **Step 4: Run pipeline + job-status suites.**

Run: `.venv/bin/python -m pytest backend/tests/test_pipeline.py backend/tests/test_job_status.py backend/tests/test_pipeline_raganything.py backend/tests/test_pipeline_ragflow.py -q 2>&1 | tail -20`
Expected: Excel-gate tests green; failures stay ≤ the same 11 pre-existing PDF-gate ones, **none Excel-related**.

- [ ] **Step 5: Commit.**

```bash
git add backend/app/core/pipeline.py backend/tests/
git commit -m "feat(kb-backend): main-ingest excel gating via gate_summary (drop :18055)"
```

### Task C4: Full suite + confirm dify lane intact (NO deletion)

**Files:** tests only (verification).

**Interfaces:**
- Produces: the two GATE call sites no longer touch `:18055`; the dify Excel-ingest lane still uses `deps.excel_parser`; full suite failures ≤ the 11 pre-existing PDF-gate ones (none Excel).

> **Do NOT delete `ExcelParserClient` / `excel_parser_*` config / `PipelineDeps.excel_parser`** — the dify lane (`pipeline.py:638`/`1162`) still consumes them. This task only VERIFIES the gate rewire didn't break anything.

- [ ] **Step 1: Confirm the gate no longer calls :18055, but the dify lane still does.**

Run: `grep -rn "deps.excel_parser\|check_excel" backend/app/workers/tasks.py backend/app/core/pipeline.py`
Expected: `deps.excel_parser.parse` remains ONLY in the dify lane (`pipeline.py:~1162`); the two GATE sites (`tasks.py:~420`, `pipeline.py:~490`) now use `check_excel(gate_summary=...)` with `gate_summary` from `kbp.parse`.

- [ ] **Step 2: Run the FULL suite.**

Run: `cd /Users/xxx/workspace/99.projects/shinhan_trust/knowledge_base && .venv/bin/python -m pytest backend/tests -q 2>&1 | tail -8`
Expected: no import errors; failures == the same 11 pre-existing PDF-gate ones (test_job_status 5, test_pipeline 4, raganything 1, ragflow 1), **none Excel-related**.

- [ ] **Step 3: Commit.**

```bash
git add -A backend
git commit -m "test(kb-backend): verify excel gate rewire; dify lane intact"
```

### Task C5: Restart kb-backend + end-to-end smoke

- [ ] **Step 1: Restart** the host kb-backend on the new code (it loads current checkout + pg .env):

Run:
```bash
cd /Users/xxx/workspace/99.projects/shinhan_trust/knowledge_base
kill $(lsof -tiTCP:8088 -sTCP:LISTEN -n) 2>/dev/null; sleep 3
.venv/bin/uvicorn app.main:app --app-dir backend --port 8088 > /tmp/kb-backend-gate.log 2>&1 &
sleep 8 && curl -sf -o /dev/null -w "8088 -> %{http_code}\n" http://localhost:8088/openapi.json
```

- [ ] **Step 2: E2E smoke** — as a developer/owner, run an Excel parse-preview through the UI or via API against a real xlsx and confirm: (a) no `:18055` connection error, (b) `gate_summary`-driven gate verdict (advisory pass or reject with `gate_popup`). Document the result. (Requires kb-backend on pg + parse-svc/facade rebuilt from Phase B.)

---

## Self-Review

- **Spec coverage:** parse-svc computes+surfaces gate_summary → A1–A3; facade passthrough → B1; deploy → B2; kb-backend consumes gate_summary + retires :18055 → C1–C4; smoke → C5. Matches the approved design (finish Phase 2b, same `compute_gate_summary`, doc_guard unchanged).
- **Same-function invariant:** gate_summary produced by the vendored `compute_gate_summary` (identical to :18055's) → `doc_guard.check_excel` unchanged (C2/C3 don't touch doc_guard).
- **Deploy correctness:** parse-svc/facade are baked compose images → Phase B2 rebuilds; kb-backend is a host process → C5 restarts.
- **Pre-existing 11 failures:** verified to be PDF/generic doc_guard-gate tests (make_pdf, RRN/PII), ORTHOGONAL to this Excel rewire — untouched; success = failures stay ≤ the same 11, none Excel-related.
- **v2→v3 fixes (competitive validation round 2, 3 must-fix resolved):** (A2) also rewrite `test_empty_chunks_raise`→`..._returns_gate_summary` (raise removed) + fake at `:33` returns a tuple; (C3) the gate is in `ingest_document` (454), `kbp.parse` in the tail (2010) — a source-swap is impossible; rewired to call `kbp.parse` at the gate for `gate_summary`, keep the reject-row build at the gate, and set `kb.pre_parsed = parsed` (existing KbContext field the tail reads, no double parse); (C2/C3 tests) real fake names + 3 files (`test_parse_preview_gate.py`, `test_worker_parse_preview.py` with `_FakeExcelParser`/`_FakeParseResult`/`_FakeKbp`; `test_pipeline_excel_gate.py` with `FakeGate*`) — gate_summary now flows from the fake `kbp.parse`, excel-parser gate assertions dropped, these currently-passing tests WILL flip so they're updated.
- **v1→v2 fixes (competitive validation, 9 must-fix resolved):** (A3) single excel-return block, not two; (A2) in-memory openpyxl fixture (no .xlsx in repo) + update the two monkeypatch fakes to tuples + do NOT raise on zero-chunks (return gate_summary so gate can reject); (C1) `KbPipelineClient.parse` maps keys → MUST add `gate_summary` (not automatic); (C2) `check_excel` returns a dict, reject on `report.get("result")=="fail"` (not `.blocks`), `parse` is sync + keyword-only (no `await`), reuse the exact existing reject/advisory sidecar; (C3) same `.result==fail` fix; (C4) do NOT delete `ExcelParserClient`/config — the dify Excel lane (`pipeline.py:638`/`1162`) still uses it, scope to GATE sites only; (11-failures) are PDF-gate, orthogonal — no flip; (A1) `RouteResult` requires `chunk_needed`.
- **Open confirmations for the implementer:** `RouteResult` type/shape (A1); the exact excel-return block(s) in app.py (one or two copies) (A3); whether `KbPipelineClient.parse` returns json verbatim or maps keys (C1); the real reject-condition + sidecar shape in `parse_preview_task` and the fake names (`FakeDocGuard`/`FakeExcelParser`) in tests (C2/C3); every `excel_parser` reference before deletion (C4); a real `.xlsx` fixture path (A2).
