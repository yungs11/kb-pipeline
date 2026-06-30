#!/usr/bin/env bash
# excel-parser (:18055) launcher / restarter — 엑셀 파싱 + 게이트 요약(gate_summary) 산출.
#
# /parse 응답 stats.gate_summary 가 kb-backend 의 파서-후단 엑셀 게이트 입력이다.
# 코드 변경(excel_parser_rag/gate/*, service/main.py) 후 재기동 필요 — --reload 없음.
#
# ⚠️ 두 가지 함정(이게 안 맞으면 /parse 가 500 → 게이트가 무력화/오작동):
#  1) KORDOC: 기본 backend=auto 는 비-전결 문서를 kordoc 으로 보낸다. kordoc CLI(node)가
#     PATH 에 없고 KORDOC_BIN 미설정이면 "*.md 를 찾을 수 없습니다" 로 500. → node bin 을
#     PATH 에 넣고 KORDOC_BIN=kordoc, KORDOC_MD_OUT(자동생성 md 저장)을 준다.
#  2) 포트 기준 종료: service.main:app 는 adaptive_chunk(:18060)도 쓰므로 모듈 패턴 kill
#     금지(광역 kill 위험). 반드시 :18055 포트 점유 PID 만 종료한다.
#
# Usage:  bash scripts/run-excel-parser.sh
set -euo pipefail
EP_DIR="${EXCEL_PARSER_DIR:-/Users/xxx/workspace/7.excel-parser}"
PORT=18055
cd "$EP_DIR"
[ -x .venv/bin/python ] || { echo "ERROR: $EP_DIR/.venv/bin/python missing"; exit 1; }

# kordoc CLI(node) 경로 탐색: PATH 우선, 없으면 nvm 글롭.
KORDOC_PATH="$(command -v kordoc 2>/dev/null || ls "$HOME"/.nvm/versions/node/*/bin/kordoc 2>/dev/null | head -1 || true)"
if [ -n "$KORDOC_PATH" ]; then
  NODE_BIN="$(dirname "$KORDOC_PATH")"
  export PATH="$NODE_BIN:$PATH"
  export KORDOC_BIN="${KORDOC_BIN:-kordoc}"
else
  echo "WARN: kordoc CLI 미발견 — 비-전결 xlsx /parse 가 500 날 수 있음(KORDOC_BIN 수동 지정 필요)" >&2
fi
export KORDOC_MD_OUT="${KORDOC_MD_OUT:-/tmp/kordoc_md_out}"
export EXCEL_PARSER_BACKEND="${EXCEL_PARSER_BACKEND:-auto}"
mkdir -p "$KORDOC_MD_OUT"

# restart — :18055 점유 PID 만 종료(포트 기준).
kill $(lsof -nP -iTCP:$PORT -sTCP:LISTEN -t 2>/dev/null) 2>/dev/null || true
for _ in $(seq 1 20); do lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1 || break; sleep 0.5; done
lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1 && { kill -9 $(lsof -nP -iTCP:$PORT -sTCP:LISTEN -t 2>/dev/null) 2>/dev/null || true; sleep 1; }

LOG="${EXCEL_PARSER_LOG:-/tmp/excel_parser.log}"
nohup .venv/bin/python -m uvicorn service.main:app --host 127.0.0.1 --port $PORT > "$LOG" 2>&1 &
echo "excel-parser launched (pid $!) on :$PORT — log: $LOG (KORDOC_BIN=${KORDOC_BIN:-unset}, backend=$EXCEL_PARSER_BACKEND)"
for i in $(seq 1 20); do
  curl -s -m 3 http://localhost:$PORT/healthz >/dev/null 2>&1 && break
  sleep 1
done

# 실제 /parse 가 gate_summary 를 내는지 검증(헬스만으론 옛/새·kordoc 깨짐 구분 불가).
SMOKE="${EXCEL_PARSER_SMOKE_FILE:-$EP_DIR/test_doc_excel/신한자산신탁_외부테이터_필요사이트 정리.xlsx}"
if [ -f "$SMOKE" ]; then
  ok="$(curl -s -m 120 -F "file=@$SMOKE" http://localhost:$PORT/parse 2>/dev/null \
        | python3 -c "import sys,json
try:
    d=json.load(sys.stdin)
    if 'detail' in d: print('ERR:'+d['detail'][:80])
    else:
        gs=d.get('stats',{}).get('gate_summary'); print('gate_summary='+('present:ok='+str(gs.get('ok')) if gs is not None else 'MISSING(옛코드?)'))
except Exception as e: print('FAIL:'+str(e)[:80])" 2>/dev/null || true)"
  echo "up: /parse $ok"
  case "$ok" in *MISSING*|ERR:*|FAIL:*) echo "WARN: /parse 검증 실패 — check $LOG" >&2; exit 1;; esac
else
  echo "up: healthz OK (smoke 파일 없음 — /parse 미검증)"
fi
exit 0
