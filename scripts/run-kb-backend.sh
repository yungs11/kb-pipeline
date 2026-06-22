#!/usr/bin/env bash
# kb-backend (:8088, knowledge_base backend) launcher / restarter.
#
# Unlike the facade, kb-backend uses pydantic Settings with env_file=".env", so it
# auto-loads knowledge_base/.env — no env capture needed here. Restarting it picks up
# config.py changes (e.g. kb_pipeline_timeout_seconds=1800).
#
# Usage:  bash scripts/run-kb-backend.sh
set -euo pipefail
KB_DIR="${KB_BACKEND_DIR:-/Users/xxx/workspace/99.projects/shinhan_trust/knowledge_base}"
cd "$KB_DIR"
[ -f .env ] || { echo "ERROR: $KB_DIR/.env missing (pydantic env_file)"; exit 1; }
[ -x .venv/bin/uvicorn ] || { echo "ERROR: $KB_DIR/.venv/bin/uvicorn missing"; exit 1; }

# restart — wait for :8088 to free.
pkill -f "app.main:app --app-dir backend --port 8088" 2>/dev/null || true
for _ in $(seq 1 20); do
  if ! lsof -nP -iTCP:8088 -sTCP:LISTEN >/dev/null 2>&1; then break; fi
  sleep 0.5
done
lsof -nP -iTCP:8088 -sTCP:LISTEN >/dev/null 2>&1 && { pkill -9 -f "app.main:app --app-dir backend --port 8088" 2>/dev/null || true; sleep 1; }

LOG="${KB_BACKEND_LOG:-/tmp/kb_backend.log}"
nohup .venv/bin/uvicorn app.main:app --app-dir backend --port 8088 > "$LOG" 2>&1 &
echo "kb-backend launched (pid $!) on :8088 — log: $LOG"
for i in $(seq 1 15); do
  code="$(curl -s -m 3 -o /dev/null -w '%{http_code}' http://localhost:8088/openapi.json 2>/dev/null || true)"
  if [ "$code" = "200" ]; then echo "up: GET /openapi.json -> 200"; exit 0; fi
  sleep 1
done
echo "WARN: kb-backend not ready after 15s — check $LOG" >&2
exit 1
