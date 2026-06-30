#!/usr/bin/env bash
# doc_guard (:8000) launcher / restarter — 엑셀 게이트의 판정·메시지 서비스.
#
# 새 엔드포인트 POST /v1/check-excel (파서 후단 엑셀 게이트)을 제공한다. 코드 변경
# (app/excel_gate_policy.py, app/main.py) 후 재기동 필요 — --reload 없음.
# pydantic Settings(config.py) 기본값으로 동작(.env 있으면 자동 로드). LLM 규칙은
# 기본 비활성(llm_enabled=False)이라 엑셀 게이트엔 영향 없음.
#
# Usage:  bash scripts/run-doc-guard.sh
set -euo pipefail
DG_DIR="${DOC_GUARD_DIR:-/Users/xxx/workspace/99.projects/shinhan_trust/doc_guard}"
PORT=8000
cd "$DG_DIR"
[ -x .venv/bin/uvicorn ] || { echo "ERROR: $DG_DIR/.venv/bin/uvicorn missing"; exit 1; }

# restart — :8000 점유 PID 를 포트 기준으로 종료(brittle pkill 패턴 회피).
kill $(lsof -nP -iTCP:$PORT -sTCP:LISTEN -t 2>/dev/null) 2>/dev/null || true
for _ in $(seq 1 20); do lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1 || break; sleep 0.5; done
lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1 && { kill -9 $(lsof -nP -iTCP:$PORT -sTCP:LISTEN -t 2>/dev/null) 2>/dev/null || true; sleep 1; }

LOG="${DOC_GUARD_LOG:-/tmp/doc_guard.log}"
nohup .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port $PORT > "$LOG" 2>&1 &
echo "doc_guard launched (pid $!) on :$PORT — log: $LOG"
for i in $(seq 1 15); do
  code="$(curl -s -m 3 -o /dev/null -w '%{http_code}' http://localhost:$PORT/healthz 2>/dev/null || true)"
  if [ "$code" = "200" ]; then break; fi
  sleep 1
done
# 새 엔드포인트가 실제로 응답하는지 검증(헬스만으론 옛/새 구분 불가).
resp="$(curl -s -m 5 -X POST http://localhost:$PORT/v1/check-excel \
  -H 'Content-Type: application/json' \
  -d '{"filename":"t.xlsx","gate_summary":{"ok":true,"sheets":[]}}' 2>/dev/null || true)"
if echo "$resp" | grep -q '"result"'; then
  echo "up: POST /v1/check-excel -> $(echo "$resp" | python3 -c 'import sys,json;print(json.load(sys.stdin)["result"])' 2>/dev/null)"
  exit 0
fi
echo "WARN: doc_guard /v1/check-excel 미응답(옛 코드?) — check $LOG: $resp" >&2
exit 1
