#!/usr/bin/env bash
# facade-worker (잡 큐 소비자) 런처 / 재기동.
#
# facade(:19000)와 **별도 프로세스**다. 다운스트림(parse-svc/adaptive_chunk/edgequake)
# 호출은 오직 이 프로세스의 슬롯 안에서만 일어나므로, 이게 안 떠 있으면 facade 는
# 접수를 503 으로 거절한다(`GET /jobs/workers` 의 online=false).
#
# ⚠️ 이 worker 는 **HTTP 포트가 없다**. 다른 런처들처럼 포트로 죽일 수 없으므로 PID 파일 +
#    정확한 cmdline 패턴으로 스코프한다. 패턴에 두 가지 함정이 있어 실측으로 고쳤다:
#      * `python -m service.worker` → 안 맞는다. Homebrew 파이썬의 실제 cmdline 은
#        `/usr/local/Cellar/.../MacOS/Python -m service.worker` 로 **대문자 Python** 이다.
#      * `service.worker` → 너무 넓다. 정규식에서 `.` 이 아무 문자나 매치해
#        VS Code 의 `--service-worker-schemes=...` 까지 잡는다(실제로 잡혔다).
#    그래서 `-m service\.worker` 로 점을 이스케이프하고 `-m ` 접두사를 붙인다.
#
# Usage:  bash scripts/run-facade-worker.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# facade 와 **동일한** env 를 쓴다(같은 DB·같은 MinIO 를 봐야 한다).
ENV_FILE="$ROOT/scripts/facade.env"
if [ -f "$ENV_FILE" ]; then set -a; . "$ENV_FILE"; set +a; fi

: "${KBP_PG_DSN:?missing — scripts/facade.env must set KBP_PG_DSN}"
# staging 업로드가 여기 없으면 /parse·/ingest 접수가 전면 실패한다. 조용히 죽는 대신
# 기동 시점에 잡는다.
: "${MINIO_ENDPOINT:?missing — scripts/facade.env must set MINIO_ENDPOINT (호스트 dev 는 localhost:9000)}"
: "${MINIO_ACCESS_KEY:?missing — scripts/facade.env must set MINIO_ACCESS_KEY}"
: "${MINIO_SECRET_KEY:?missing — scripts/facade.env must set MINIO_SECRET_KEY}"

PID_FILE="${KBP_WORKER_PID_FILE:-/tmp/kbp_facade_worker.pid}"
LOG="${KBP_WORKER_LOG:-/tmp/kbp_facade_worker.log}"
PATTERN='-m service\.worker'

# ── 기존 프로세스 종료 ────────────────────────────────────────────────────────
# PID 파일**만** 보면 안 된다. 런처를 안 거치고 띄운 worker(수동 기동, 옛 PID 파일 유실)가
# 살아남아 같은 큐를 이중으로 소비한다 — 실제로 그렇게 두 개가 동시에 돌았다.
# 패턴에 맞는 프로세스를 전부 모아서 죽인다.
stop_worker() {
  local pids=""
  if [ -f "$PID_FILE" ]; then
    pids="$(tr -cd '0-9' < "$PID_FILE" || true)"
  fi
  pids="$pids $(pgrep -f -- "$PATTERN" || true)"
  # 중복 제거 + 자기 자신 제외
  pids="$(printf '%s\n' $pids | sort -u | grep -v "^$$\$" || true)"
  [ -n "$pids" ] || return 0
  local pid="$pids"
  # SIGTERM 은 새 claim 만 멈추고 진행 중 잡은 완주시킨다(드레인). 완주 동안에도
  # heartbeat 가 돌아 다른 worker 가 회수하지 못한다.
  echo "stopping existing worker(s): $(echo $pid | tr '\n' ' ')"
  kill $pid 2>/dev/null || true
  for _ in $(seq 1 30); do
    local alive=""
    for one in $pid; do kill -0 "$one" 2>/dev/null && alive="$alive $one"; done
    [ -n "$alive" ] || return 0
    sleep 1
  done
  echo "WARN: worker(s) did not drain in 30s; forcing" >&2
  kill -9 $pid 2>/dev/null || true
}
stop_worker
rm -f "$PID_FILE"

nohup "$ROOT/.venv-kb/bin/python" -m service.worker > "$LOG" 2>&1 &
worker_pid=$!
echo "$worker_pid" > "$PID_FILE"
echo "facade-worker launched (pid $worker_pid) — log: $LOG"

# ── 검증: 프로세스 생존 + worker 레지스트리 등록 ─────────────────────────────
# PID 만 보면 안 된다. DB 에 heartbeat 를 못 쓰면 facade 는 여전히 worker 가 없다고
# 판단해 접수를 거절한다.
for i in $(seq 1 15); do
  if ! kill -0 "$worker_pid" 2>/dev/null; then
    echo "ERROR: worker died on startup — see $LOG" >&2
    tail -20 "$LOG" >&2
    exit 1
  fi
  online="$("$ROOT/.venv-kb/bin/python" - <<'PY' 2>/dev/null || true
import os
from service.jobs.repo import JobRepo
try:
    print("yes" if JobRepo(os.environ["KBP_PG_DSN"]).live_worker_count() > 0 else "no")
except Exception:
    print("no")
PY
)"
  if [ "$online" = "yes" ]; then
    echo "up: worker registered (live_worker_count > 0)"
    exit 0
  fi
  sleep 1
done
echo "WARN: worker running but not registered after 15s — check $LOG" >&2
exit 1
