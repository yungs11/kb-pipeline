#!/usr/bin/env bash
# facade (:19000, service.app:app) launcher / restarter.
#
# The facade reads its config straight from os.environ (no dotenv), so it needs the
# KBP_* vars exported. They live in the gitignored scripts/facade.env (captured from
# the running process). Unlike parse-svc, the facade does NOT need java.
#
# DOCKER-SHADOW gotcha (2026-07-07): facade + parse-svc are HOST dev processes; the
# docker-compose stack is BACKING services only. `docker compose up -d` also starts the
# facade/parse-svc CONTAINERS (stale image code) which SHADOW these host processes and
# serve old code (you see "옛날 파싱"/old behavior). This script stops the shadow facade
# container so the host source serves. Do NOT `docker compose up` facade/parse-svc for
# dev — use run-facade.sh / run-parse-svc.sh (or `docker compose build` to rebuild).
#
# Usage:  bash scripts/run-facade.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# 0) Guard against the docker-shadow gotcha: stop any compose facade container so this
#    host process (and kb-backend's localhost:19000 calls) hit the live source.
for _cid in $(docker ps -q --filter "label=com.docker.compose.service=facade" 2>/dev/null); do
  echo "guard: stopping shadow docker facade container ($_cid) — host source must serve"
  docker stop "$_cid" >/dev/null 2>&1 || true
done

# env + secrets (gitignored). set -a auto-exports every KEY=value.
ENV_FILE="$ROOT/scripts/facade.env"
if [ -f "$ENV_FILE" ]; then set -a; . "$ENV_FILE"; set +a; fi
: "${KBP_OPENAI_API_KEY:?missing — scripts/facade.env must set KBP_OPENAI_API_KEY}"
: "${KBP_PG_DSN:?missing — scripts/facade.env must set KBP_PG_DSN}"
# Raised parse read-timeout (multi-table PDFs take ~400s+). Code default is 1800.
export KBP_PARSE_SVC_TIMEOUT="${KBP_PARSE_SVC_TIMEOUT:-1800}"

# restart — wait for :19000 to free (sleep 1 races uvicorn graceful shutdown).
pkill -f "uvicorn service.app:app" 2>/dev/null || true
for _ in $(seq 1 20); do
  if ! lsof -nP -iTCP:19000 -sTCP:LISTEN >/dev/null 2>&1; then break; fi
  sleep 0.5
done
lsof -nP -iTCP:19000 -sTCP:LISTEN >/dev/null 2>&1 && { pkill -9 -f "uvicorn service.app:app" 2>/dev/null || true; sleep 1; }

LOG="${FACADE_LOG:-/tmp/facade-kbp.log}"
nohup "$ROOT/.venv-kb/bin/python" -m uvicorn service.app:app \
  --host 127.0.0.1 --port 19000 > "$LOG" 2>&1 &
echo "facade launched (pid $!) on :19000 — log: $LOG (parse timeout ${KBP_PARSE_SVC_TIMEOUT}s)"
for i in $(seq 1 10); do
  r="$(curl -s -m 3 http://localhost:19000/healthz 2>/dev/null || true)"
  if [ -n "$r" ]; then echo "healthz: $r"; exit 0; fi
  sleep 1
done
echo "WARN: facade healthz not ready after 10s — check $LOG" >&2
exit 1
