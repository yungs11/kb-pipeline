#!/usr/bin/env bash
# parse-svc (:19001) launcher / restarter.
#
# Three gotchas this script exists to prevent:
#   1) OpenDataLoader (PDF parsing) shells out to `java`. macOS ships a /usr/bin/java
#      STUB that errors "Unable to locate a Java Runtime" → CLI exit 1 → parse fails →
#      empty enriched_content. So we pin openjdk@17 onto PATH.
#   2) service/llm.py reads os.environ["KBP_OPENAI_API_KEY"] (no default). Missing it →
#      KeyError the moment a modal block is described. So we load scripts/parse-svc.env.
#   3) DOCKER-SHADOW (2026-07-07): parse-svc + facade are HOST dev processes here — the
#      docker-compose stack is BACKING services only (postgres/minio:9000/edgequake/
#      adaptive/doc_guard/gotenberg). If you `docker compose up -d` the WHOLE stack, it
#      also starts the parse-svc/facade CONTAINERS (stale baked-in image code). facade
#      calls parse-svc via compose DNS `parse-svc:19001`, so the CONTAINER serves and
#      your host source edits are IGNORED → "옛날 파싱"/old behavior. This script stops
#      that shadow container so the host source actually serves. Do NOT `docker compose
#      up` facade/parse-svc for dev; use run-facade.sh / run-parse-svc.sh (or rebuild the
#      image with `docker compose build parse-svc facade` if you truly want containers).
#
# Usage:  bash scripts/run-parse-svc.sh         # kills any running parse-svc, relaunches
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# 0) Guard against the docker-shadow gotcha (#3): stop any compose parse-svc container.
#    It holds :19001 via docker-proxy AND facade calls it via `parse-svc:19001` DNS, so
#    the CONTAINER's stale image code would serve instead of this host process.
for _cid in $(docker ps -q --filter "label=com.docker.compose.service=parse-svc" 2>/dev/null); do
  echo "guard: stopping shadow docker parse-svc container ($_cid) — host source must serve"
  docker stop "$_cid" >/dev/null 2>&1 || true
done

# 1) openjdk@17 → PATH (OpenDataLoader CLI needs a real JRE).
for j in /usr/local/opt/openjdk@17/bin /opt/homebrew/opt/openjdk@17/bin; do
  if [ -d "$j" ]; then export PATH="$j:$PATH"; break; fi
done
if ! command -v java >/dev/null 2>&1 || ! java -version >/dev/null 2>&1; then
  echo "ERROR: no working java on PATH — install openjdk@17 (brew install openjdk@17)" >&2
  exit 1
fi

# 2) env + secrets (gitignored). set -a auto-exports every KEY=value.
ENV_FILE="$ROOT/scripts/parse-svc.env"
if [ -f "$ENV_FILE" ]; then set -a; . "$ENV_FILE"; set +a; fi
: "${KBP_OPENAI_API_KEY:?missing — create scripts/parse-svc.env with KBP_OPENAI_API_KEY=...}"
export KBP_OCR_URL="${KBP_OCR_URL:-http://localhost:18050}"
export KBP_EXCEL_URL="${KBP_EXCEL_URL:-http://localhost:18055}"

# 3) restart (no --reload by design; relaunch to pick up code changes).
#    Wait for the old process to release :19001 — a bare `sleep 1` races the port
#    (graceful uvicorn shutdown can take a few seconds → "address already in use").
pkill -f "parse_service.app:app" 2>/dev/null || true
for _ in $(seq 1 20); do
  if ! lsof -nP -iTCP:19001 -sTCP:LISTEN >/dev/null 2>&1; then break; fi
  sleep 0.5
done
if lsof -nP -iTCP:19001 -sTCP:LISTEN >/dev/null 2>&1; then
  pkill -9 -f "parse_service.app:app" 2>/dev/null || true
  sleep 1
fi
LOG="${PARSE_SVC_LOG:-/tmp/parse_svc.log}"
nohup "$ROOT/.venv-kb/bin/python" -m uvicorn parse_service.app:app \
  --host 127.0.0.1 --port 19001 > "$LOG" 2>&1 &
echo "parse-svc launched (pid $!) on :19001 — log: $LOG"
echo "java: $(command -v java)"

# 4) health check.
for i in $(seq 1 10); do
  r="$(curl -s -m 3 http://localhost:19001/healthz 2>/dev/null || true)"
  if [ -n "$r" ]; then echo "healthz: $r"; exit 0; fi
  sleep 1
done
echo "WARN: healthz not ready after 10s — check $LOG" >&2
exit 1
