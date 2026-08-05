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
[ -x .venv/bin/alembic ] || { echo "ERROR: $KB_DIR/.venv/bin/alembic missing"; exit 1; }

# DB schema를 먼저 최신화. batch worker가 신규 queue table 없이 떠 반복 실패하는 상태를 막는다.
.venv/bin/alembic upgrade head

# restart — kill whoever actually holds :8088 (by port, robust to extra flags like
# --host 127.0.0.1 that break a brittle pkill -f cmdline pattern), then wait for free.
kill $(lsof -nP -iTCP:8088 -sTCP:LISTEN -t 2>/dev/null) 2>/dev/null || true
for _ in $(seq 1 20); do
  if ! lsof -nP -iTCP:8088 -sTCP:LISTEN >/dev/null 2>&1; then break; fi
  sleep 0.5
done
lsof -nP -iTCP:8088 -sTCP:LISTEN >/dev/null 2>&1 && { kill -9 $(lsof -nP -iTCP:8088 -sTCP:LISTEN -t 2>/dev/null) 2>/dev/null || true; sleep 1; }

LOG="${KB_BACKEND_LOG:-/tmp/kb_backend.log}"
WORKER_LOG="${KB_BATCH_WORKER_LOG:-/tmp/kb_batch_worker.log}"
WORKER_PID_FILE="${KB_BATCH_WORKER_PID_FILE:-/tmp/kb_batch_worker.pid}"

# 별도 durable batch worker 재기동. 웹 프로세스와 분리되어 요청 스레드를 점유하지 않는다.
if [ -f "$WORKER_PID_FILE" ]; then
  old_worker_pid="$(tr -cd '0-9' < "$WORKER_PID_FILE")"
  if [ -n "$old_worker_pid" ] && kill -0 "$old_worker_pid" 2>/dev/null; then
    kill "$old_worker_pid" 2>/dev/null || true
    for _ in $(seq 1 20); do
      kill -0 "$old_worker_pid" 2>/dev/null || break
      sleep 0.5
    done
  fi
fi
nohup env PYTHONPATH=backend .venv/bin/python -m app.workers.batch_worker > "$WORKER_LOG" 2>&1 &
worker_pid=$!
echo "$worker_pid" > "$WORKER_PID_FILE"
echo "kb-batch-worker launched (pid $worker_pid) — log: $WORKER_LOG"

nohup .venv/bin/uvicorn app.main:app --app-dir backend --port 8088 > "$LOG" 2>&1 &
echo "kb-backend launched (pid $!) on :8088 — log: $LOG"
for i in $(seq 1 15); do
  code="$(curl -s -m 3 -o /dev/null -w '%{http_code}' http://localhost:8088/openapi.json 2>/dev/null || true)"
  if [ "$code" = "200" ] && kill -0 "$worker_pid" 2>/dev/null; then
    echo "up: backend=200 batch-worker=running"
    exit 0
  fi
  sleep 1
done
echo "WARN: kb-backend not ready after 15s — check $LOG" >&2
exit 1
