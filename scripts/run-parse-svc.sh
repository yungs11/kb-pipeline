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
#      adaptive/doc_guard). If you `docker compose up -d` the WHOLE stack, it
#      also starts the parse-svc/facade CONTAINERS (stale baked-in image code). facade
#      calls parse-svc via compose DNS `parse-svc:19001`, so the CONTAINER serves and
#      your host source edits are IGNORED → "옛날 파싱"/old behavior. This script stops
#      that shadow container so the host source actually serves. Do NOT `docker compose
#      up` facade/parse-svc for dev; use run-facade.sh / run-parse-svc.sh (or rebuild the
#      image with `docker compose build parse-svc facade` if you truly want containers).
#
# ─────────────────────────────────────────────────────────────────────────────
# 통합 런처 (2026-08-10) — parse-svc 와 레거시 excel-parser 를 한 스크립트가 관리한다.
#
#   bash scripts/run-parse-svc.sh                # parse-svc(:19001) 만 — 기본
#   bash scripts/run-parse-svc.sh --with-excel   # + excel-parser(:18055)
#   bash scripts/run-parse-svc.sh --excel-only   # excel-parser(:18055) 만
#
# 왜 합쳤나 — Phase 2e 에서 엑셀 파싱이 parse-svc **in-process** 로 흡수됐는데
# kordoc env(KORDOC_BIN/KORDOC_MD_OUT/EXCEL_PARSER_BACKEND)는 `run-excel-parser.sh`
# 에만 남아 있었다. 그래서 컨테이너에서는 되고 호스트 dev 에서만 엑셀 파싱이
# `'*.md' 를 찾을 수 없습니다` 로 죽었다. 두 스크립트가 같은 env 를 각자 관리하면
# 또 어긋난다 — 한 곳에서 세팅한다.
#
# ⚠️ excel-parser(:18055)를 **지우지 않은 이유**: kb 의 `provider=excel_parser` 코호트가
#    아직 그 서비스를 부른다(`config.py:224 excel_parser_base_url`,
#    `dependencies.py:97 ExcelParserClient`). kb_pipeline 경로의 게이트는 parse-svc 의
#    gate_summary 가 소스지만(`:18055 폐기 후 파서 게이트 소스`), 그 코호트는 살아 있다.
# ─────────────────────────────────────────────────────────────────────────────
# Usage:  bash scripts/run-parse-svc.sh         # kills any running parse-svc, relaunches
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

RUN_PARSE=1; RUN_EXCEL=0
case "${1:-}" in
  --with-excel) RUN_EXCEL=1 ;;
  --excel-only) RUN_EXCEL=1; RUN_PARSE=0 ;;
  "") ;;
  *) echo "usage: $0 [--with-excel|--excel-only]" >&2; exit 2 ;;
esac
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

# 1-b) kordoc CLI(node) → PATH + KORDOC_* env.
#
# ★ Phase 2e 에서 excel-parser 서비스를 parse-svc 안으로 흡수했는데(파서 일원화) **이 env 가
#   따라오지 않았다.** `Dockerfile.parse-svc:10` 은 `ENV KORDOC_BIN=kordoc
#   KORDOC_MD_OUT=/tmp/kordoc_md_out EXCEL_PARSER_BACKEND=auto` 를 갖는데 호스트 런처엔
#   없어서, 컨테이너에서는 되고 **호스트 dev 에서만** 엑셀 파싱이 죽었다:
#     parse_failed: excel parse failed for X.xlsx: kordoc backend:
#     'excel_parser_7_xxxx.md' 를 찾을 수 없습니다.
#   기본 backend=auto 가 비-전결 xlsx 를 kordoc 으로 보내는데 CLI 도, 자동생성 경로도
#   없으면 이 에러가 난다. `run-excel-parser.sh` 에는 있던 처리를 여기로 옮긴다.
KORDOC_PATH="$(command -v kordoc 2>/dev/null || ls "$HOME"/.nvm/versions/node/*/bin/kordoc 2>/dev/null | head -1 || true)"
if [ -n "$KORDOC_PATH" ]; then
  export PATH="$(dirname "$KORDOC_PATH"):$PATH"
  export KORDOC_BIN="${KORDOC_BIN:-kordoc}"
else
  echo "WARN: kordoc CLI 미발견 — 비-전결 xlsx 파싱이 \"*.md 를 찾을 수 없습니다\" 로 실패한다." >&2
  echo "      npm i -g kordoc  또는 KORDOC_BIN 을 수동 지정할 것." >&2
fi
export KORDOC_MD_OUT="${KORDOC_MD_OUT:-/tmp/kordoc_md_out}"
export EXCEL_PARSER_BACKEND="${EXCEL_PARSER_BACKEND:-auto}"
mkdir -p "$KORDOC_MD_OUT"

# 2) env + secrets (gitignored). set -a auto-exports every KEY=value.
ENV_FILE="$ROOT/scripts/parse-svc.env"
# ★ 호출자가 준 값이 parse-svc.env 보다 **우선**한다(dotenv 관례).
#   `set -a; . "$ENV_FILE"` 만 하면 파일 값이 CLI 값을 덮어써서
#   `KBP_PADDLE_OCR_GATEWAY_URL=... bash scripts/run-parse-svc.sh` 가 **조용히 무시**된다.
#   실측 2026-08-10: OCR 게이트웨이 주소 변경 검증이 이 때문에 옛 주소로 갔다
#   (run-facade.sh 에서 이미 같은 버그를 고쳤다 — 두 런처가 같은 함정을 갖고 있었다).
_CALLER_OVERRIDES=""
for _k in KBP_PADDLE_OCR_GATEWAY_URL KBP_GATE_OCR_LANE KORDOC_BIN KORDOC_MD_OUT EXCEL_PARSER_BACKEND MODEL_API_URL MODEL_NAME; do
  eval "_v=\${$_k:-}"
  [ -n "$_v" ] && _CALLER_OVERRIDES="$_CALLER_OVERRIDES $_k=$_v"
done
if [ -f "$ENV_FILE" ]; then set -a; . "$ENV_FILE"; set +a; fi
for _kv in $_CALLER_OVERRIDES; do export "$_kv"; done
true
: "${KBP_OPENAI_API_KEY:?missing — create scripts/parse-svc.env with KBP_OPENAI_API_KEY=...}"
# ── OCR 주소 env — **레인마다 하나씩, 총 두 개다**(2026-08-10 정리) ─────────────
#
# 예전엔 OCR 주소처럼 보이는 env 가 넷이라 "어느 걸 바꿔야 하나" 가 안 보였다.
# 죽은 둘(`KBP_OCR_URL` :18050 / `KBP_EXCEL_URL` :18055)을 여기서 **제거**한다 —
# Phase 2c/2e 에서 OCR·엑셀이 parse-svc **in-process** 로 들어가 소비자가 무시하는데
# 런처가 계속 export 해서 오해를 만들었다(`parse_service/app.py:460` "무시하는 dead 파라미터").
#
# 남은 둘은 **합칠 수 없다** — 레인이 다른 별개 서비스다(`parsers/pdf/gate.py:24`):
#   KBP_GATE_OCR_LANE=vl        → MODEL_API_URL              (in-process VL, qwen)
#   KBP_GATE_OCR_LANE=paddle_gw → KBP_PADDLE_OCR_GATEWAY_URL (외부 OCR 게이트웨이)
#   KBP_GATE_OCR_LANE=odl       → 외부 주소 없음(OpenDataLoader 로컬 CLI)
# 즉 "OCR 주소를 바꾼다" 는 **먼저 레인을 정한 뒤 그 레인의 env 하나**를 바꾸는 것이다.
# 반영 확인: bash scripts/ocr-test/verify-ocr-gw-url.sh

# ── excel-parser(:18055) 기동 — 레거시 provider=excel_parser 코호트용 ──────────
start_excel_parser () {
  local EP_DIR="${EXCEL_PARSER_DIR:-/Users/xxx/workspace/7.excel-parser}"
  local PORT=18055
  [ -d "$EP_DIR" ] || { echo "ERROR: $EP_DIR 없음 (EXCEL_PARSER_DIR 로 지정)" >&2; return 1; }
  [ -x "$EP_DIR/.venv/bin/python" ] || { echo "ERROR: $EP_DIR/.venv/bin/python 없음" >&2; return 1; }
  # ⚠️ 포트 기준 종료. `service.main:app` 은 adaptive_chunk(:18060)도 쓰므로 모듈 패턴 kill 은
  #    그쪽까지 죽인다(광역 kill 금지).
  kill $(lsof -nP -iTCP:$PORT -sTCP:LISTEN -t 2>/dev/null) 2>/dev/null || true
  for _ in $(seq 1 20); do lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1 || break; sleep 0.5; done
  lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1 && { kill -9 $(lsof -nP -iTCP:$PORT -sTCP:LISTEN -t 2>/dev/null) 2>/dev/null || true; sleep 1; }
  local EPLOG="${EXCEL_PARSER_LOG:-/tmp/excel_parser.log}"
  ( cd "$EP_DIR" && nohup .venv/bin/python -m uvicorn service.main:app \
      --host 127.0.0.1 --port $PORT > "$EPLOG" 2>&1 & )
  echo "excel-parser launched on :$PORT — log: $EPLOG (KORDOC_BIN=${KORDOC_BIN:-unset}, backend=$EXCEL_PARSER_BACKEND)"
  for _ in $(seq 1 20); do curl -s -m 3 "http://localhost:$PORT/healthz" >/dev/null 2>&1 && break; sleep 1; done
  # 헬스만으론 옛 코드/kordoc 깨짐을 구분 못 한다 — /parse 가 gate_summary 를 내는지 본다.
  local SMOKE="${EXCEL_PARSER_SMOKE_FILE:-$EP_DIR/test_doc_excel/신한자산신탁_외부테이터_필요사이트 정리.xlsx}"
  if [ -f "$SMOKE" ]; then
    local ok; ok="$(curl -s -m 120 -F "file=@$SMOKE" "http://localhost:$PORT/parse" 2>/dev/null \
      | python3 -c "import sys,json
try:
    d=json.load(sys.stdin)
    print('ERR:'+d['detail'][:80] if 'detail' in d else ('gate_summary='+('ok='+str((d.get('stats') or {}).get('gate_summary',{}).get('ok')) if (d.get('stats') or {}).get('gate_summary') is not None else 'MISSING(옛코드?)')))
except Exception as e: print('FAIL:'+str(e)[:80])" 2>/dev/null || true)"
    echo "excel-parser /parse: $ok"
    case "$ok" in *MISSING*|ERR:*|FAIL:*) echo "WARN: /parse 검증 실패 — $EPLOG 확인" >&2; return 1;; esac
  else
    echo "excel-parser: healthz OK (smoke 파일 없음 — /parse 미검증)"
  fi
  return 0
}

if [ "$RUN_PARSE" = "0" ]; then
  start_excel_parser; exit $?
fi

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
echo "parse-svc: KORDOC_BIN=${KORDOC_BIN:-unset} backend=$EXCEL_PARSER_BACKEND md_out=$KORDOC_MD_OUT"
echo "parse-svc launched (pid $!) on :19001 — log: $LOG"
echo "java: $(command -v java)"

# 4) health check.
RC=1
for i in $(seq 1 10); do
  r="$(curl -s -m 3 http://localhost:19001/healthz 2>/dev/null || true)"
  if [ -n "$r" ]; then echo "healthz: $r"; RC=0; break; fi
  sleep 1
done
[ "$RC" = "0" ] || { echo "WARN: healthz not ready after 10s — check $LOG" >&2; exit 1; }

[ "$RUN_EXCEL" = "1" ] && { start_excel_parser || exit 1; }
exit 0
