#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# ocr_batch_stress.sh — OCR Gateway 배치/동시처리 '안정성' 테스트 (python 불요).
#   bash + curl + sed + awk 만 사용 → 폐쇄망/파이썬 없는 서버에서도 동작.
#   (파이썬 있는 환경이면 ocr_batch_stress.py 가 지연 백분위 등 더 자세하다.)
#
# 목적: 테스트 폴더의 문서들을 동시 C건으로 게이트웨이에 몰아넣고, GW(MAX_CONCURRENT)+vLLM
#       이 흔들리지 않는지 — 성공률·실패사유·소요를 본다. ROUNDS 로 지속 부하.
#
# 사용:
#   HOST=api-doc.ys-helperai.com ./ocr_batch_stress.sh ../../test_doc
#   HOST=https://api-doc.ys-helperai.com CONCURRENCY=8 ROUNDS=3 ./ocr_batch_stress.sh ./docs
# 환경변수: HOST(스킴없으면 http://) ENGINE(paddleocr_vl) LANG_(korean)
#           CONCURRENCY(8) ROUNDS(1) POLL_TIMEOUT(1800)
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail

HOST="${HOST:-127.0.0.1:18081}"
ENGINE="${ENGINE:-paddleocr_vl}"
LANG_="${LANG_:-korean}"
case "$HOST" in http://*|https://*) BASE="$HOST" ;; *) BASE="http://${HOST}" ;; esac
FOLDER="${1:-../../test_doc}"
CONCURRENCY="${CONCURRENCY:-8}"
ROUNDS="${ROUNDS:-1}"
POLL_TIMEOUT="${POLL_TIMEOUT:-1800}"
POLL_INTERVAL="${POLL_INTERVAL:-3}"
UA="${OCR_UA:-ocr-batch/1.0 (curl-like)}"

RED=$'\033[1;31m'; GRN=$'\033[1;32m'; YEL=$'\033[1;33m'; CY=$'\033[1;36m'; RST=$'\033[0m'
RESDIR="$(mktemp -d)"; trap 'rm -rf "$RESDIR"' EXIT
export BASE ENGINE LANG_ UA POLL_TIMEOUT POLL_INTERVAL RESDIR

[ -d "$FOLDER" ] || { echo "${RED}폴더 없음: $FOLDER${RST}"; exit 2; }
command -v curl >/dev/null || { echo "curl 필요"; exit 2; }

# 지원 확장자 파일 수집(대소문자 무시)
shopt -s nullglob nocaseglob 2>/dev/null
files=()
for ext in pdf png jpg jpeg webp tiff tif bmp; do
  for f in "$FOLDER"/*."$ext"; do [ -f "$f" ] && files+=("$f"); done
done
shopt -u nocaseglob 2>/dev/null
[ ${#files[@]} -gt 0 ] || { echo "${RED}지원 파일 없음: $FOLDER${RST}"; exit 2; }

# 평면 JSON 값 추출(sed): jval <파일> <key>
jval() { sed -n 's/.*"'"$2"'"[[:space:]]*:[[:space:]]*"\{0,1\}\([^",}]*\)"\{0,1\}.*/\1/p' "$1" | head -1; }

# 워커: 파일 1개 제출→폴링→result. 결과 1줄을 RESDIR 에 기록: ok|wall|pages|사유|파일명
worker() {
  local f="$1" tmp t0 tid st code res
  tmp="$(mktemp -d)"; t0=$(date +%s)
  curl -s -A "$UA" -o "$tmp/sub" -m 120 -X POST -F "file=@${f}" -F "lang=${LANG_}" \
       "$BASE/ocr/$ENGINE/tasks" >/dev/null 2>&1
  tid=$(jval "$tmp/sub" task_id)
  if [ -z "$tid" ]; then
    echo "0|$(( $(date +%s)-t0 ))|0|제출실패|$(basename "$f")" > "$RESDIR/$$.$RANDOM"
    rm -rf "$tmp"; return
  fi
  local deadline=$(( $(date +%s) + POLL_TIMEOUT ))
  while :; do
    curl -s -A "$UA" -o "$tmp/st" -m 15 "$BASE/ocr/$ENGINE/tasks/$tid" >/dev/null 2>&1
    st=$(jval "$tmp/st" status)
    [ "$st" = "completed" ] || [ "$st" = "failed" ] && break
    [ "$(date +%s)" -ge "$deadline" ] && { st="timeout"; break; }
    sleep "$POLL_INTERVAL"
  done
  curl -s -A "$UA" -o "$tmp/res" -m 30 "$BASE/ocr/$ENGINE/tasks/$tid/result" >/dev/null 2>&1
  local wall=$(( $(date +%s)-t0 ))
  local rstatus=$(jval "$tmp/res" status)
  if [ "$st" = "completed" ] && [ "$rstatus" = "ok" ]; then
    local pages=$(grep -o '"page_index"' "$tmp/res" | wc -l | tr -d ' ')
    echo "1|$wall|$pages|-|$(basename "$f")" > "$RESDIR/$$.$RANDOM"
  else
    local reason=$(jval "$tmp/res" error); [ -z "$reason" ] && reason="task $st"
    echo "0|$wall|0|$reason|$(basename "$f")" > "$RESDIR/$$.$RANDOM"
  fi
  rm -rf "$tmp"
}
export -f worker jval

printf '%sOCR 배치 안정성 테스트%s  base=%s engine=%s\n' "$CY" "$RST" "$BASE" "$ENGINE"
printf '  폴더=%s  파일=%s개 × %s회 = %s건  동시=%s\n' "$FOLDER" "${#files[@]}" "$ROUNDS" "$(( ${#files[@]} * ROUNDS ))" "$CONCURRENCY"
code=$(curl -s -A "$UA" -o "$RESDIR/health" -w '%{http_code}' -m 15 "$BASE/health")
[ "$code" = "200" ] && printf '  health OK · device=%s\n' "$(jval "$RESDIR/health" device)" \
  || { echo "${RED}✗ /health $code — 게이트웨이 미기동?${RST}"; exit 1; }

# 작업목록(라운드 반복) → 동시 C개씩 배치 실행
START=$(date +%s); i=0
for r in $(seq 1 "$ROUNDS"); do
  for f in "${files[@]}"; do
    worker "$f" &
    i=$((i+1))
    [ $(( i % CONCURRENCY )) -eq 0 ] && wait
  done
done
wait
WALL=$(( $(date +%s)-START ))

# 진행 로그 출력
for r in "$RESDIR"/[0-9]*.*; do
  IFS='|' read -r ok wall pages reason name < "$r"
  [ "$ok" = "1" ] && printf '  %s✓%s %s  wall=%ss pages=%s\n' "$GRN" "$RST" "$name" "$wall" "$pages" \
                  || printf '  %s✗%s %s  wall=%ss  ERR: %s\n' "$RED" "$RST" "$name" "$wall" "$reason"
done

# 집계(awk)
printf '\n%s─────────── 안정성 요약 ───────────%s\n' "$CY" "$RST"
cat "$RESDIR"/[0-9]*.* | awk -F'|' -v wall="$WALL" '
  { n++; if($1==1){ok++; wsum+=$2; if($2>wmax)wmax=$2; if(wmin==""||$2<wmin)wmin=$2; psum+=$3}
    else { fail++; f[$4]++ } }
  END{
    printf "  총 %d건 · 성공 %d · 실패 %d  → 성공률 %.1f%%\n", n, ok, fail, (n?100*ok/n:0)
    printf "  전체 소요(wall)   : %ds\n", wall
    if(wall>0) printf "  처리량            : %.1f docs/min  (%.1f pages/min)\n", ok/wall*60, psum/wall*60
    if(ok>0)   printf "  요청 지연(성공)   : min=%ds avg=%ds max=%ds\n", wmin, wsum/ok, wmax
    if(fail>0){ print "\n  \033[1;33m실패 상세:\033[0m"; for(k in f) printf "    %d건 · %s\n", f[k], k
               print "  \033[1;33m→ 실패 있으면 CONCURRENCY 낮추거나 GW 로그 확인(대개 vLLM 일시 끊김).\033[0m" }
    else print "\n  \033[1;32m✅ 전건 성공 — 이 동시성/부하에서 게이트웨이 안정.\033[0m"
  }'
# 실패 있으면 비정상 종료
grep -q '^0|' "$RESDIR"/[0-9]*.* 2>/dev/null && exit 1 || exit 0
