#!/usr/bin/env bash
# facade (:19000, service.app:app) launcher / restarter.
#
# The facade reads its config straight from os.environ (no dotenv), so it needs the
# KBP_* vars exported. They live in the gitignored scripts/facade.env (captured from
# the running process). Unlike parse-svc, the facade does NOT need java.
#
# Usage:  bash scripts/run-facade.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

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
