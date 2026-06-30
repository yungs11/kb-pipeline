---
name: restart-kbp-stack
description: Use when (re)starting kb-pipeline provider services — parse-svc (:19001), facade (:19000), kb-backend (:8088) — OR the excel-gate stack — doc_guard (:8000), excel-parser (:18055) — e.g. after editing their code, when a removed/old doc_guard gate still blocks uploads, when /parse returns empty enriched_content or missing gate_summary, on "Unable to locate a Java Runtime", on kordoc "*.md 를 찾을 수 없습니다", or on facade httpx.ReadTimeout. Each has its own launcher script that pins the right PATH/env (java, KBP_*, KORDOC_BIN) and kills the old process BY PORT so a code change actually takes effect. For the whole excel gate at once use scripts/restart-gate-stack.sh.
---

# Restart the kb-pipeline provider stack

Three services, three launcher scripts in `8.kb-pipeline/scripts/`. None run with
`--reload`, so a code change needs a restart. Each script kills the old process,
**waits for the port to actually free** (a bare `sleep 1` races uvicorn's graceful
shutdown → "address already in use"), relaunches, and health-checks.

| Service | Port | Script | Gotcha it handles |
|---|---|---|---|
| parse-svc | 19001 | `scripts/run-parse-svc.sh` | needs **openjdk@17** on PATH (OpenDataLoader) + `KBP_OPENAI_API_KEY` (modal LLM) |
| facade | 19000 | `scripts/run-facade.sh` | reads `os.environ` directly (no dotenv) → needs `KBP_*` from `scripts/facade.env` |
| kb-backend | 8088 | `scripts/run-kb-backend.sh` | pydantic `env_file=".env"` auto-loads `knowledge_base/.env`; kills :8088 holder **by port** |
| doc_guard | 8000 | `scripts/run-doc-guard.sh` | verifies `POST /v1/check-excel` answers (new excel-gate endpoint), not just healthz |
| excel-parser | 18055 | `scripts/run-excel-parser.sh` | pins **KORDOC_BIN + node PATH** (auto backend → kordoc); kills :18055 **by port** (module `service.main:app` is shared with adaptive_chunk :18060 — never module-pattern kill) |

```bash
bash scripts/run-parse-svc.sh    # after editing parse_service/ or kb_pipeline/
bash scripts/run-facade.sh       # after editing service/ (facade)
bash scripts/run-kb-backend.sh   # after editing knowledge_base backend/config
bash scripts/run-doc-guard.sh    # after editing doc_guard app/
bash scripts/run-excel-parser.sh # after editing 7.excel-parser excel_parser_rag/ or service/
bash scripts/restart-gate-stack.sh   # all 3 excel-gate services in dep order (doc_guard+excel-parser→kb-backend)
```

## Excel gate stack (doc_guard + excel-parser + kb-backend)

The parser-후단 엑셀 게이트 spans 3 services. If an edit doesn't take effect (e.g. an
old/removed doc_guard gate still blocks an upload), it is almost always a **stale
process**, not the code. `restart-gate-stack.sh` restarts all three in dependency order
(doc_guard + excel-parser must be up before kb-backend calls them) and verifies each is
running NEW code (doc_guard `/v1/check-excel`, excel-parser `/parse` returns
`stats.gate_summary`).

**Two traps that bit us (2026-06-30):**
1. **Kill by PORT, not by cmdline pattern.** kb-backend ran as
   `uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8088`, but the old
   launcher's `pkill -f "app.main:app --app-dir backend --port 8088"` didn't match
   (`--host 127.0.0.1` sits between `backend` and `--port`) → old process survived, new
   one failed to bind and died, **old code kept serving :8088**. All launchers now
   `kill $(lsof -nP -iTCP:<port> -sTCP:LISTEN -t)`.
2. **excel-parser needs kordoc env.** default `EXCEL_PARSER_BACKEND=auto` routes non-전결
   xlsx to the kordoc CLI; without `KORDOC_BIN=kordoc` + node on PATH, `/parse` 500s
   ("*.md 를 찾을 수 없습니다") → kb gets no `gate_summary` → gate silently passes
   everything. `run-excel-parser.sh` discovers kordoc (`command -v kordoc` / nvm glob)
   and exports `KORDOC_BIN`/`KORDOC_MD_OUT`.

**Which backend does the UI hit?** `knowledge_base/frontend/.env.local` →
`BACKEND_ORIGIN` (currently `http://localhost:8088`). If uploads still show old behavior
after a restart, confirm the frontend points at the backend you restarted, and hard-refresh.

## The gotchas, in detail

1. **parse-svc — java**: OpenDataLoader shells out to `java`. macOS `/usr/bin/java` is a
   stub (`Unable to locate a Java Runtime`) → CLI exit 1 → **empty `enriched_content`**.
   The script pins `/usr/local/opt/openjdk@17/bin` (or `/opt/homebrew/...`).
2. **parse-svc / facade — env**: `service/llm.py` reads `os.environ["KBP_OPENAI_API_KEY"]`
   with no default → KeyError when a modal block is described. Keys live in the
   **gitignored** `scripts/parse-svc.env` and `scripts/facade.env` (pattern `scripts/*.env`).
   > Trap: `export FOO=...` and `uvicorn ... &` as **separate** `!` commands run in
   > separate shells — the export never reaches the launched process. Always use the script.
3. **facade — ReadTimeout on big PDFs**: parse-svc calls the modal LLM **once per table,
   sequentially** (a 4-table PDF ≈ 400s+). The facade→parse-svc read timeout is 1800s
   (`KBP_PARSE_SVC_TIMEOUT`) and kb-backend→facade is `kb_pipeline_timeout_seconds=1800`
   so neither gives up early. If you see `httpx.ReadTimeout`, suspect a slow multi-table
   parse (or two parses colliding on the single-worker parse-svc).

## First-time setup (gitignored env files)

`scripts/facade.env` can be captured from a running facade without printing secrets:

```bash
ps eww "$(pgrep -f 'service.app:app' | head -1)" | tr ' ' '\n' \
  | grep -E '^KBP_[A-Z_]+=' > scripts/facade.env
```

`scripts/parse-svc.env` needs at least `KBP_OPENAI_API_KEY` and `KBP_OCR_URL=http://localhost:18050`.

## Verify a real parse works

`healthz` only proves the process booted. To prove java + modal LLM end-to-end:

```bash
curl -s -m 1800 -F "file=@<some.pdf>;type=application/pdf" -F "filename=t.pdf" \
  http://localhost:19001/parse \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('status'),d.get('n_blocks'),len(d.get('enriched_content','')))"
```

Avoid running this while a user upload is in flight — parse-svc is single-worker, so two
heavy parses serialize and can trip the facade timeout.
