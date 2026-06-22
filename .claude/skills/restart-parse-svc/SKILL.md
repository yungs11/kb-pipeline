---
name: restart-parse-svc
description: Use when (re)starting the kb-pipeline parse-svc (:19001) — after editing parse_service/ or kb_pipeline/ code, when /parse returns empty enriched_content, or when "Unable to locate a Java Runtime" appears. Pins openjdk@17 on PATH and loads KBP_* env (OpenRouter key) so OpenDataLoader PDF parsing and modal LLM calls actually work.
---

# Restart parse-svc (:19001)

parse-svc owns the heavy parse→blockify→modal path. It runs with **no `--reload`**, so
any change to `kb_pipeline/` or `parse_service/` needs a manual restart to take effect.

## The two failure modes this prevents

1. **Empty `enriched_content` / `parse_failed`** — OpenDataLoader shells out to `java`.
   macOS ships a `/usr/bin/java` **stub** that errors `Unable to locate a Java Runtime`
   → CLI exit 1 → the PDF never parses. Fix: put **openjdk@17** on PATH
   (`/usr/local/opt/openjdk@17/bin`, or `/opt/homebrew/...` on Apple Silicon).
2. **`KeyError: 'KBP_OPENAI_API_KEY'`** when a table/image/equation block is described —
   `service/llm.py` reads that env var with no default. The OpenRouter key lives in the
   **gitignored** `scripts/parse-svc.env`.

> A common trap: running `export KBP_OPENAI_API_KEY=...` and the `uvicorn ... &` launch as
> **separate** shell commands. Each runs in its own shell, so the export never reaches the
> launched process. Always launch via the script (one shell) — never hand-export then launch.

## How to restart (preferred)

```bash
bash scripts/run-parse-svc.sh
```

The launcher: pins openjdk@17 → loads `scripts/parse-svc.env` (`set -a; source`) →
`pkill` any running parse-svc → relaunches uvicorn (log `/tmp/parse_svc.log`) → polls
`/healthz`. Success looks like:

```
java: /usr/local/opt/openjdk@17/bin/java
healthz: {"status":"ok","deps":{"ocr":"http://localhost:18050"}}
```

`deps.ocr` being **non-null** confirms `KBP_OCR_URL` (and thus the env file) loaded.

## First-time setup

If `scripts/parse-svc.env` is missing, create it (it is gitignored — never commit a key):

```
KBP_OPENAI_API_KEY=sk-or-v1-...          # OpenRouter key (modal Korean summary + boundary)
KBP_OCR_URL=http://localhost:18050
KBP_EXCEL_URL=http://localhost:18055
# KBP_LLM_MODEL=qwen/qwen3.5-122b-a10b   # optional; default in service/llm.py
```

## Verify it actually parses

`healthz` only proves the process is up. To prove java + the modal LLM work end-to-end,
POST a real PDF and confirm `enriched_content` is non-empty with modal spans:

```bash
curl -s -m 600 -F "file=@<some.pdf>;type=application/pdf" -F "filename=test.pdf" \
  http://localhost:19001/parse | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('status'),d.get('n_blocks'),len(d.get('enriched_content','')))"
```

## Sibling services (not restarted by this script)

facade :19000 (`service.app`), adaptive_chunk :18060, edgequake :8081, OCR :18050,
bge-m3, kb-backend :8088. See `docs/runbook-v2-smoke.md` for their launch commands. Only
parse-svc has the java/openjdk gotcha.
