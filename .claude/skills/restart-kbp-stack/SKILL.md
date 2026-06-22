---
name: restart-kbp-stack
description: Use when (re)starting kb-pipeline provider services — parse-svc (:19001), facade (:19000), or kb-backend (:8088) — e.g. after editing their code, when /parse returns empty enriched_content, on "Unable to locate a Java Runtime", or on facade httpx.ReadTimeout. Each has its own launcher script that pins the right PATH/env so parsing + modal LLM calls + long multi-table parses actually work.
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
| kb-backend | 8088 | `scripts/run-kb-backend.sh` | pydantic `env_file=".env"` auto-loads `knowledge_base/.env`; just restarts the venv |

```bash
bash scripts/run-parse-svc.sh    # after editing parse_service/ or kb_pipeline/
bash scripts/run-facade.sh       # after editing service/ (facade)
bash scripts/run-kb-backend.sh   # after editing knowledge_base backend/config
```

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
