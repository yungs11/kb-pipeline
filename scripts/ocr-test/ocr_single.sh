#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# ocr_single.sh — 단건 OCR: 파일 하나 던지고 결과를 받는다.
#   출력: 성공/실패 · 소요(초) · (실패 시) 사유 · (성공 시) 요약 + result JSON 저장.
#
# 계약: OCR Gateway (multipart). 비동기 경로(제출 → 폴링 → result)를 쓴다
#       (큰 PDF 도 프록시 타임아웃 없이 안전).
#
# 사용:
#   HOST=15.164.81.29:18081 ./ocr_single.sh doc.pdf
#   HOST=http://15.164.81.29:18081 ENGINE=paddleocr_vl ./ocr_single.sh page.jpg
#   CHART=1 ./ocr_single.sh report.pdf              # opts use_chart_recognition
# 환경변수: HOST(스킴 없으면 http://) · ENGINE(paddleocr_vl) · LANG_(korean)
#           POLL_TIMEOUT(1800) · OUTDIR(결과 저장 폴더, 기본 현재)
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail

HOST="${HOST:-127.0.0.1:18081}"
ENGINE="${ENGINE:-paddleocr_vl}"
LANG_="${LANG_:-korean}"
case "$HOST" in http://*|https://*) BASE="$HOST" ;; *) BASE="http://${HOST}" ;; esac
FILE="${1:-}"
POLL_TIMEOUT="${POLL_TIMEOUT:-1800}"
POLL_INTERVAL="${POLL_INTERVAL:-3}"
OUTDIR="${OUTDIR:-.}"
UA="${OCR_UA:-ocr-single/1.0 (curl-like)}"

RED=$'\033[1;31m'; GRN=$'\033[1;32m'; YEL=$'\033[1;33m'; CY=$'\033[1;36m'; RST=$'\033[0m'
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

# JSON 필드 추출: jget <파일> <dot.path>. python3 있으면 정확 파싱, 없으면 sed 폴백
# (폴백은 평면 "key":"val"/"key":num 만 — task_id/status/error/elapsed_s 등엔 충분).
if command -v python3 >/dev/null 2>&1; then
  jget() {
    python3 - "$1" "$2" <<'PY' 2>/dev/null
import json,sys
try: d=json.load(open(sys.argv[1]))
except Exception: print(""); sys.exit()
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
else
  # python 없는 폴백: dot-path 의 '마지막 키'로 평면 매칭. 문자열/숫자 모두 처리.
  jget() {
    local key="${2##*.}"
    sed -n 's/.*"'"$key"'"[[:space:]]*:[[:space:]]*"\{0,1\}\([^",}]*\)"\{0,1\}.*/\1/p' "$1" | head -1
  }
fi

[ -n "$FILE" ] || { echo "사용법: $0 <파일>  (pdf/png/jpg/jpeg/webp/tiff/bmp, ≤50MB)"; exit 2; }
[ -f "$FILE" ] || { echo "${RED}파일 없음: $FILE${RST}"; exit 2; }

OPTS=(); [ "${CHART:-0}" = "1" ] && OPTS=(-F 'opts={"use_chart_recognition": true}')
printf '%s단건 OCR%s  base=%s engine=%s\n  file=%s\n' "$CY" "$RST" "$BASE" "$ENGINE" "$FILE"

t0=$(date +%s)
# 1) 제출
code=$(curl -s -A "$UA" -o "$TMP/sub" -w '%{http_code}' -m 60 -X POST \
        -F "file=@${FILE}" -F "lang=${LANG_}" ${OPTS[@]+"${OPTS[@]}"} "$BASE/ocr/$ENGINE/tasks")
TID=$(jget "$TMP/sub" task_id)
if ! { [ "$code" = "202" ] || [ "$code" = "200" ]; } || [ -z "$TID" ]; then
  printf '%s❌ 실패%s  (제출 단계)  소요 %ds\n   사유: 제출 code=%s  %s\n' \
    "$RED" "$RST" "$(( $(date +%s)-t0 ))" "$code" "$(head -c 200 "$TMP/sub")"
  exit 1
fi
printf '  제출 OK · task_id=%s\n' "$TID"

# 2) 폴링
deadline=$(( $(date +%s) + POLL_TIMEOUT )); st=""
while :; do
  curl -s -A "$UA" -o "$TMP/st" -m 15 "$BASE/ocr/$ENGINE/tasks/$TID" >/dev/null
  st=$(jget "$TMP/st" status)
  printf '\r  폴링… status=%s  (%ds)   ' "$st" "$(( $(date +%s)-t0 ))"
  [ "$st" = "completed" ] || [ "$st" = "failed" ] && break
  [ "$(date +%s)" -ge "$deadline" ] && { st="TIMEOUT"; break; }
  sleep "$POLL_INTERVAL"
done
echo

# 3) 결과
curl -s -A "$UA" -o "$TMP/res" -m 30 "$BASE/ocr/$ENGINE/tasks/$TID/result" >/dev/null
elapsed=$(( $(date +%s)-t0 ))
rstatus=$(jget "$TMP/res" status)

mkdir -p "$OUTDIR"
OUT="$OUTDIR/ocr_result_$(basename "$FILE").json"
cp "$TMP/res" "$OUT" 2>/dev/null

if [ "$st" = "completed" ] && [ "$rstatus" = "ok" ]; then
  if command -v python3 >/dev/null 2>&1; then
    tlen=$(python3 -c "print(len((__import__('json').load(open('$TMP/res')).get('text') or '')))" 2>/dev/null)
    pcnt=$(python3 -c "print(len(__import__('json').load(open('$TMP/res')).get('layout') or []))" 2>/dev/null)
  else
    tlen="?(python없음)"
    pcnt=$(grep -o '"page_index"' "$TMP/res" | wc -l | tr -d ' ')
  fi
  printf '%s✅ 성공%s   소요 %ss\n' "$GRN" "$RST" "$elapsed"
  printf '   text=%s자 · pages=%s · server_elapsed=%ss · device=%s · finish=%s\n' \
    "$tlen" "$pcnt" \
    "$(jget "$TMP/res" metrics.elapsed_s)" "$(jget "$TMP/res" metrics.device)" "$(jget "$TMP/res" finish_reason)"
  printf '   result 저장: %s\n' "$OUT"
  exit 0
else
  reason=$(jget "$TMP/res" error); [ -z "$reason" ] && reason="status=$st (폴링 종료상태)"
  printf '%s❌ 실패%s   소요 %ss\n   사유: %s\n' "$RED" "$RST" "$elapsed" "$reason"
  printf '   응답 저장: %s\n' "$OUT"
  exit 1
fi
