#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# test_ocr_api.sh — OCR Gateway API 단건 기능 테스트 (동기 + 비동기 전 경로)
#
# 계약 출처: "OCR Gateway API — PaddleOCR-VL" (API 정의서).
#   BASE http://<host>:18081, ENGINE paddleocr_vl.
#   GET  /health · /engines · /ocr/{engine}/health
#   POST /ocr/{engine}                  (동기 → envelope 200)
#   POST /ocr/{engine}/tasks            (비동기 제출 → 202 {task_id})
#   GET  /ocr/{engine}/tasks/{id}       (폴링: queued|running|completed|failed)
#   GET  /ocr/{engine}/tasks/{id}/result(완료 결과, 미완 409 · 없는 id 404)
#
# 의존: bash, curl, python3 (JSON 파싱 — jq 불필요, 폐쇄망 안전).
#
# 사용:
#   ./test_ocr_api.sh <파일>                         # 기본 127.0.0.1:18081
#   HOST=10.0.0.5:18081 ./test_ocr_api.sh doc.pdf    # 원격 호스트
#   CHART=1 ./test_ocr_api.sh report.pdf             # 차트 인식 opts 켜기
#   SKIP_ASYNC=1 ./test_ocr_api.sh doc.pdf           # 동기만
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail

HOST="${HOST:-127.0.0.1:18081}"
ENGINE="${ENGINE:-paddleocr_vl}"
LANG_="${LANG_:-korean}"
BASE="http://${HOST}"
FILE="${1:-}"
SYNC_TIMEOUT="${SYNC_TIMEOUT:-600}"     # 동기 OCR 최대 대기(초) — 페이지 많으면 상향
POLL_TIMEOUT="${POLL_TIMEOUT:-900}"     # 비동기 폴링 총 대기(초)
POLL_INTERVAL="${POLL_INTERVAL:-3}"

RED=$'\033[1;31m'; GRN=$'\033[1;32m'; YEL=$'\033[1;33m'; CY=$'\033[1;36m'; RST=$'\033[0m'
pass=0; fail=0
ok()   { printf '  %s✓%s %s\n' "$GRN" "$RST" "$*"; pass=$((pass+1)); }
bad()  { printf '  %s✗%s %s\n' "$RED" "$RST" "$*"; fail=$((fail+1)); }
hdr()  { printf '\n%s▶ %s%s\n' "$CY" "$*" "$RST"; }
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

# JSON 필드 추출: jget <파일> <dot.path>  (없으면 빈 문자열)
jget() {
  python3 - "$1" "$2" <<'PY' 2>/dev/null
import json,sys
try:
    d=json.load(open(sys.argv[1]))
except Exception:
    print(""); sys.exit()
cur=d
for k in sys.argv[2].split("."):
    if k=="": continue
    if isinstance(cur,list):
        try: cur=cur[int(k)]
        except Exception: print(""); sys.exit()
    elif isinstance(cur,dict): cur=cur.get(k)
    else: cur=None
    if cur is None: print(""); sys.exit()
print(cur if not isinstance(cur,(dict,list)) else json.dumps(cur,ensure_ascii=False))
PY
}
# HTTP: req <method> <url> [curl args...] → stdout=body(파일), 반환=http_code
req() {
  local method="$1" url="$2"; shift 2
  curl -s -o "$TMP/body" -w '%{http_code}' -X "$method" "$@" "$url"
}

[ -n "$FILE" ] || { echo "사용법: $0 <파일>  (pdf/png/jpg/jpeg/webp/tiff/bmp, ≤50MB)"; exit 2; }
[ -f "$FILE" ] || { echo "${RED}파일 없음: $FILE${RST}"; exit 2; }
command -v curl >/dev/null || { echo "curl 필요"; exit 2; }
command -v python3 >/dev/null || { echo "python3 필요"; exit 2; }

printf '%sOCR Gateway API 테스트%s  base=%s engine=%s file=%s\n' "$CY" "$RST" "$BASE" "$ENGINE" "$FILE"

# ── 1) GET /health ────────────────────────────────────────────────────────────
hdr "1. GET /health — 서버·GPU·로드된 모델"
code=$(req GET "$BASE/health" -m 10)
if [ "$code" = "200" ]; then
  ok "health 200"
  printf '     kv_cache=%s%%  gpu=%s\n' "$(jget "$TMP/body" kv_cache_usage_perc)" "$(jget "$TMP/body" device)"
  python3 -c "import json,sys;print('     ',json.dumps(json.load(open('$TMP/body')),ensure_ascii=False)[:200])" 2>/dev/null
else bad "health $code — 게이트웨이 미기동? (paddle_service.sh status)"; fi

# ── 2) GET /engines ───────────────────────────────────────────────────────────
hdr "2. GET /engines — 엔진 목록 + 가용성"
code=$(req GET "$BASE/engines" -m 10)
if [ "$code" = "200" ]; then ok "engines 200"; python3 -c "import json;print('     ',json.dumps(json.load(open('$TMP/body')),ensure_ascii=False)[:300])" 2>/dev/null
else bad "engines $code"; fi

# ── 3) GET /ocr/{engine}/health ───────────────────────────────────────────────
hdr "3. GET /ocr/$ENGINE/health — 엔진 단건 상태"
code=$(req GET "$BASE/ocr/$ENGINE/health" -m 15)
[ "$code" = "200" ] && ok "engine health 200" || bad "engine health $code (vLLM :8104 로딩 전이면 실패)"

# ── 4) POST /ocr/{engine} (동기) ──────────────────────────────────────────────
hdr "4. POST /ocr/$ENGINE — 동기 OCR → envelope"
OPTS_ARGS=()
[ "${CHART:-0}" = "1" ] && OPTS_ARGS=(-F 'opts={"use_chart_recognition": true}')
t0=$(date +%s)
code=$(req POST "$BASE/ocr/$ENGINE" -m "$SYNC_TIMEOUT" \
        -F "file=@${FILE}" -F "lang=${LANG_}" "${OPTS_ARGS[@]}")
t1=$(date +%s)
status=$(jget "$TMP/body" status)
if [ "$code" = "200" ] && [ "$status" = "ok" ]; then
  ok "동기 OCR 200 · status=ok  (왕복 $((t1-t0))s)"
  printf '     text_len=%s  page수=%s  elapsed_s=%s  device=%s  finish=%s\n' \
    "$(python3 -c "print(len((__import__('json').load(open('$TMP/body')).get('text') or '')))" 2>/dev/null)" \
    "$(python3 -c "print(len(__import__('json').load(open('$TMP/body')).get('layout') or []))" 2>/dev/null)" \
    "$(jget "$TMP/body" metrics.elapsed_s)" "$(jget "$TMP/body" metrics.device)" "$(jget "$TMP/body" finish_reason)"
  [ "$(jget "$TMP/body" finish_reason)" = "length" ] && printf '     %s! finish_reason=length → max_tokens 초과로 잘림%s\n' "$YEL" "$RST"
else
  bad "동기 OCR code=$code status=$status  error=$(jget "$TMP/body" error)"
fi

# ── 5) 비동기 경로: tasks 제출 → 폴링 → result ────────────────────────────────
if [ "${SKIP_ASYNC:-0}" != "1" ]; then
  hdr "5. 비동기 — POST /tasks → 폴링 → /result"
  code=$(req POST "$BASE/ocr/$ENGINE/tasks" -m 30 -F "file=@${FILE}" -F "lang=${LANG_}" "${OPTS_ARGS[@]}")
  TID=$(jget "$TMP/body" task_id)
  if { [ "$code" = "202" ] || [ "$code" = "200" ]; } && [ -n "$TID" ]; then
    ok "제출 $code · task_id=$TID · status=$(jget "$TMP/body" status)"
    # 폴링
    deadline=$(( $(date +%s) + POLL_TIMEOUT )); st=""
    while :; do
      code=$(req GET "$BASE/ocr/$ENGINE/tasks/$TID" -m 15)
      st=$(jget "$TMP/body" status)
      printf '\r     폴링… status=%s   ' "$st"
      [ "$st" = "completed" ] || [ "$st" = "failed" ] && break
      [ "$(date +%s)" -ge "$deadline" ] && { st="TIMEOUT"; break; }
      sleep "$POLL_INTERVAL"
    done
    echo
    if [ "$st" = "completed" ]; then
      ok "task completed"
      code=$(req GET "$BASE/ocr/$ENGINE/tasks/$TID/result" -m 30)
      rstatus=$(jget "$TMP/body" status)
      [ "$code" = "200" ] && [ "$rstatus" = "ok" ] \
        && ok "result 200 · status=ok · text_len=$(python3 -c "print(len((__import__('json').load(open('$TMP/body')).get('text') or '')))" 2>/dev/null)" \
        || bad "result code=$code status=$rstatus"
      # 미완 result 는 409 여야 함(계약 확인) — 이미 완료라 스킵
    else
      bad "task $st (failed/timeout) — error=$(jget "$TMP/body" error)"
    fi
    # 없는 id 404 계약 확인
    code=$(req GET "$BASE/ocr/$ENGINE/tasks/no-such-id-xyz/result" -m 10)
    [ "$code" = "404" ] && ok "없는 task result 404 (계약 일치)" || printf '     %s! 없는 id → %s (기대 404)%s\n' "$YEL" "$code" "$RST"
  else
    bad "제출 code=$code task_id 없음"
  fi
fi

# ── 요약 ──────────────────────────────────────────────────────────────────────
printf '\n%s─────────────────────────────%s\n' "$CY" "$RST"
if [ "$fail" -eq 0 ]; then printf '%s✅ PASS%s  %d개 통과\n' "$GRN" "$RST" "$pass"; exit 0
else printf '%s❌ FAIL%s  통과 %d · 실패 %d\n' "$RED" "$RST" "$pass" "$fail"; exit 1; fi
