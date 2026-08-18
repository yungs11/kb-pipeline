#!/usr/bin/env bash
# parser_test_ui (:8601) launcher / restarter — 호스트 dev 전용.
#
# kb-backend/frontend 없이 facade 에 직접 붙는 standalone PARSER 테스트 화면.
# 무인증, 0.0.0.0 오픈(의도적 — plan §v2→v3 정정 2). 로컬 전체 스택
# (docker-compose.yml)에는 컨테이너로 편입하지 않는다 — parse-only 번들에만
# 편입한다(docker-compose.airgap.yml). 개발자는 이 스크립트로 호스트에서 직접 띄운다.
#
# 의존: fastapi/uvicorn/httpx/python-multipart 뿐 — parse_service/service 의존성 없음.
#   .venv-kb 에 이미 fastapi/uvicorn/httpx 가 있으면 그걸 쓰고, 없으면 개별 설치한다.
#
# Usage:  bash scripts/run-parser-test-ui.sh
#         KBP_FACADE_URL=http://localhost:3000 bash scripts/run-parser-test-ui.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PORT="${KBP_PARSER_TEST_UI_PORT:-8601}"
export KBP_FACADE_URL="${KBP_FACADE_URL:-http://localhost:${KBP_FACADE_PORT:-3000}}"

PY="$ROOT/.venv-kb/bin/python"
if [ ! -x "$PY" ]; then PY="python3"; fi
"$PY" -c "import fastapi, uvicorn, httpx" 2>/dev/null || {
  echo "fastapi/uvicorn/httpx 없음 — 설치한다: $PY -m pip install -r parser_test_ui/requirements.txt"
  "$PY" -m pip install -r "$ROOT/parser_test_ui/requirements.txt"
}

# restart — 포트 기준 kill(모듈패턴 kill 은 다른 서비스와 충돌 가능 — :8601 은 이 서비스 고정).
pkill -f "uvicorn app:app --host 0.0.0.0 --port $PORT" 2>/dev/null || true
for _ in $(seq 1 20); do
  if ! lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then break; fi
  sleep 0.5
done
lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 && {
  pkill -9 -f "uvicorn app:app --host 0.0.0.0 --port $PORT" 2>/dev/null || true
  sleep 1
}

LOG="${PARSER_TEST_UI_LOG:-/tmp/parser-test-ui-kbp.log}"
cd "$ROOT/parser_test_ui"
nohup "$PY" -m uvicorn app:app --host 0.0.0.0 --port "$PORT" > "$LOG" 2>&1 &
UPID=$!
cd "$ROOT"
echo "parser_test_ui launched (pid $UPID) on :$PORT — facade=$KBP_FACADE_URL log: $LOG"

for i in $(seq 1 30); do
  r="$(curl -s -m 3 "http://localhost:$PORT/healthz" 2>/dev/null || true)"
  case "$r" in *'"status"'*'"ok"'*) echo "healthz: $r"; exit 0;; esac
  if ! kill -0 "$UPID" 2>/dev/null; then
    echo "✗ uvicorn 이 죽었다(:$PORT 바인드 실패?). 로그 마지막 줄:" >&2
    tail -5 "$LOG" >&2
    exit 1
  fi
  if [ -n "$r" ]; then
    echo "✗ :$PORT 가 parser_test_ui 가 아니다 — 다른 서비스가 점유했다:" >&2
    printf '  %.120s\n' "$r" >&2
    exit 1
  fi
  sleep 1
done
echo "✗ parser_test_ui healthz 가 30초 안에 준비되지 않았다 — $LOG 확인" >&2
exit 1
