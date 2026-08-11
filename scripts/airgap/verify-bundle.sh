#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# verify-bundle.sh — 번들 무결성 / .env 준비상태 검사
#
# 세 가지 검사를 독립적으로 수행한다(Phase A 는 --images/--imports, Phase B 는 --env 를 씀):
#   --env     [.env경로]  : 【A. 온프렘 재설정 필수】 키가 채워졌는지 + 아직 인터넷
#                           주소(openrouter.ai/litellm.ax-demo.com)를 가리키는지 경고.
#   --images [tar경로]    : 로컬 이미지 스토어에 9종 이미지가 존재하고 arch 가 맞는지.
#                           (docker/podman 자동탐지)
#   --imports             : kbp-parse-svc 이미지에 kordoc 이 실제로 설치돼 있는지
#                           (healthy 로 뜨지만 엑셀 파서 백엔드가 조용히 깨지는 것을 막음, A6)
# 인자 없으면 셋 다(기본 경로) 시도.
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RED=$'\033[1;31m'; GRN=$'\033[1;32m'; YEL=$'\033[1;33m'; RST=$'\033[0m'
rc=0

# 전부 로컬 태그(kbp-*:airgap)다 — 인프라 이미지도 build-bundle.sh 가 digest pull 후
# 로컬 태그를 붙여 save 하기 때문(그래야 podman load 후에도 이름이 살아남는다).
IMAGES=(
  kbp-edgequake kbp-parse-svc kbp-facade kbp-adaptive_chunk kbp-doc_guard kbp-edgequake_webui
  kbp-postgres kbp-minio
)
# .env 에서 반드시 채워야 하는 시크릿/엔드포인트 키
REQUIRED_ENV=(
  KBP_FILECONVERT_URL KBP_FILECONVERT_TOKEN
  OPENROUTER_API_KEY KBP_OPENAI_API_KEY KBP_OPENAI_BASE_URL KBP_LLM_MODEL
  MODEL_API_URL MODEL_API_KEY
  LITELLM_EMBEDDING_BASE_URL LITELLM_EMBEDDING_API_KEY
  ADAPTIVE_CHUNK_OPENROUTER_API_KEY ADAPTIVE_CHUNK_OPENROUTER_BASE_URL
  ADAPTIVE_CHUNK_SCORING_EMBEDDING_BASE_URL ADAPTIVE_CHUNK_SCORING_EMBEDDING_API_KEY
  ADAPTIVE_CHUNK_RERANK_BASE_URL ADAPTIVE_CHUNK_RERANK_API_KEY
  EDGEQUAKE_RERANK_BASE_URL EDGEQUAKE_RERANK_API_KEY
  MINIO_ACCESS_KEY MINIO_SECRET_KEY POSTGRES_PASSWORD
  # 비면 facade 게이트가 꺼진 채 호스트 3000 으로 노출된다 — 무인증 적재·삭제.
  KBP_FACADE_KEY
)

# .env 에서 키 하나의 값을 뽑는다 — '=' 뒤 → 인라인 주석 제거 → 공백 제거.
#
# ⚠️ 이 함수가 없으면 이걸 쓰는 가드가 **조용히 통과한다**. `val` 이 `command not found`
#    로 죽어 빈 문자열이 되고, `[ "$(val X)" = "paddle_gw" ]` 같은 비교가 항상 거짓이
#    되기 때문이다. 가드가 사라진 게 아니라 **통과해버리는** 쪽이라 더 위험하다
#    (실제로 그렇게 죽어 있었다 — 2026-08-10 발견).
val() {
  local k="$1" f="$2"
  grep -E "^${k}=" "$f" 2>/dev/null | head -1 | cut -d= -f2- \
    | sed 's/[[:space:]]*#.*$//' | tr -d '[:space:]'
}

# 파서 전용 배포에서 **필요 없는** 필수키 — 그 서비스를 아예 띄우지 않는다.
# (실측 판정: compose 에서 edgequake/adaptive_chunk 만 쓰는 키들. parse-only 가 띄우는 건
#  postgres·minio·parse-svc·facade·facade-worker 다섯이다.)
# ★ 이걸 안 걸러내면 운영자가 빨간 줄 9개를 보고 **가드를 무시하게 된다** — 그게 더 위험하다.
PARSE_ONLY_SKIP=(
  OPENROUTER_API_KEY
  LITELLM_EMBEDDING_BASE_URL LITELLM_EMBEDDING_API_KEY
  ADAPTIVE_CHUNK_OPENROUTER_API_KEY ADAPTIVE_CHUNK_OPENROUTER_BASE_URL
  ADAPTIVE_CHUNK_SCORING_EMBEDDING_BASE_URL ADAPTIVE_CHUNK_SCORING_EMBEDDING_API_KEY
  ADAPTIVE_CHUNK_RERANK_BASE_URL ADAPTIVE_CHUNK_RERANK_API_KEY
  EDGEQUAKE_RERANK_BASE_URL EDGEQUAKE_RERANK_API_KEY
)

check_env() {
  local envf="${1:-$REPO_ROOT/.env}"
  echo "== .env 검사: $envf =="
  [ -f "$envf" ] || { echo "${RED}✗ 파일 없음${RST}"; return 1; }
  local miss=0 inet=0
  # ★ compose 가 `${VAR:?}` 로 **강제**하는 변수를 자동으로 필수 목록에 더한다(2026-08-10).
  #   손으로 관리하는 REQUIRED_ENV 와 compose 가 어긋나면 **가드는 통과하고 배포가 죽는다** —
  #   실측: `MODEL_NAME` 이 compose 에서 `:?` 인데 가드에 없어서
  #   "✓ 필수키 모두 채워짐" 뒤에 `required variable MODEL_NAME is missing a value` 로 실패했다.
  #   compose 에서 파생하면 다시 어긋날 수 없다.
  local COMPOSE_YML="$REPO_ROOT/docker-compose.airgap.yml"
  local REQ_FROM_COMPOSE=()
  if [ -f "$COMPOSE_YML" ]; then
    while IFS= read -r v; do [ -n "$v" ] && REQ_FROM_COMPOSE+=("$v"); done < <(
      grep -oE '\$\{[A-Z_][A-Z0-9_]*:\?' "$COMPOSE_YML" | sed 's/\${//;s/:?//' | sort -u)
    [ "${#REQ_FROM_COMPOSE[@]}" -gt 0 ] && \
      echo "  (compose 가 강제하는 변수 ${#REQ_FROM_COMPOSE[@]}개를 필수에 포함: ${REQ_FROM_COMPOSE[*]})"
  fi

  # --parse-only 면 그 배포에 없는 서비스의 키를 건너뛴다(위 PARSE_ONLY_SKIP).
  local skip
  for k in "${REQUIRED_ENV[@]}" ${REQ_FROM_COMPOSE[@]+"${REQ_FROM_COMPOSE[@]}"}; do
    if [ "${PROFILE:-full}" = "parse-only" ]; then
      skip=0
      for _s in "${PARSE_ONLY_SKIP[@]}"; do [ "$k" = "$_s" ] && { skip=1; break; }; done
      [ "$skip" = "1" ] && continue
    fi
    # 값 추출: '=' 뒤 → 인라인 주석(` #...`) 제거 → 공백 제거
    local v; v="$(grep -E "^${k}=" "$envf" | head -1 | cut -d= -f2- | sed 's/[[:space:]]*#.*$//' | tr -d '[:space:]')"
    if [ -z "$v" ]; then echo "  ${RED}✗ 비어있음: $k${RST}"; miss=1; fi
  done
  # 아직 인터넷 엔드포인트면 폐쇄망에서 불통 → 경고(에러는 아님)
  if grep -Eq '^(KBP_OPENAI_BASE_URL|MODEL_API_URL|LITELLM_EMBEDDING_BASE_URL|ADAPTIVE_CHUNK_OPENROUTER_BASE_URL|ADAPTIVE_CHUNK_SCORING_EMBEDDING_BASE_URL|ADAPTIVE_CHUNK_RERANK_BASE_URL|EDGEQUAKE_RERANK_BASE_URL)=.*(openrouter\.ai|litellm\.ax-demo\.com)' "$envf"; then
    echo "  ${YEL}! 일부 엔드포인트가 아직 인터넷 주소(openrouter.ai / litellm.ax-demo.com)입니다."
    echo "    폐쇄망에서는 도달 불가 — 사내 온프렘 주소로 바꾸세요.${RST}"
    inet=1
  fi
  # paddle_gw 레인을 켜놓고 게이트웨이 주소를 비우면, 스캔 문서가 RuntimeError 후
  # **조용히 ODL/VL 로 폴백**한다 — 파싱은 "성공"으로 보여 배포 후에도 드러나지 않는다.
  # 켰으면 주소가 있어야 하고, 안 쓸 거면 레인을 vl 로 바꿔 명시적으로 끄게 한다.
  if [ "$(val KBP_GATE_OCR_LANE "$envf")" = "paddle_gw" ] && [ -z "$(val KBP_PADDLE_OCR_GATEWAY_URL "$envf")" ]; then
    echo "  ${RED}✗ KBP_GATE_OCR_LANE=paddle_gw 인데 KBP_PADDLE_OCR_GATEWAY_URL 이 비었다${RST}"
    echo "    ${RED}  → 스캔 PDF 가 조용히 ODL/VL 로 폴백한다(에러가 안 보인다).${RST}"
    echo "    ${RED}  → 주소를 채우거나, 안 쓸 거면 KBP_GATE_OCR_LANE=vl 로 명시적으로 끌 것.${RST}"
    miss=1
  fi
  # ── 야간 커뮤니티 배치(A1) ────────────────────────────────────────────────
  # 컨테이너 기본 TZ 는 UTC 다. 야간 배치를 켜놓고 TZ 를 안 주면 KBP_COMMUNITY_BUILD_AT
  # =03:00 이 **KST 정오**에 열려 목적(주간 LLM 부하 회피)이 정확히 뒤집힌다.
  # 실패가 아니라 **잘못된 시각에 성공**하므로 로그만 봐서는 드러나지 않는다.
  local cbe; cbe="$(val KBP_COMMUNITY_BUILD_ENABLED "$envf")"
  if [ "$cbe" != "false" ]; then      # 미설정(=기본 true) 또는 true
    # 스케줄 존은 KBP_COMMUNITY_TZ 다(컨테이너 TZ 아님 — D33). **비어도 정확하다**:
    # zone() 기본값이 Asia/Seoul 이고 이 모듈은 naive datetime 을 쓰지 않는다.
    # 그래서 빈 값을 차단하지 않는다 — 차단하면 정상 배포를 막는다.
    local ctz; ctz="$(val KBP_COMMUNITY_TZ "$envf")"
    if [ -n "$ctz" ] && [ "$ctz" != "Asia/Seoul" ]; then
      echo "  ${YEL}! KBP_COMMUNITY_TZ=$ctz — 야간 창이 KST 기준이 아니다. 의도한 것인지 확인.${RST}"
    fi
    # 컨테이너 TZ 를 세우면 facade 로그가 KST 가 되어 parse-svc(UTC)와 시각축이 섞인다.
    # 스케줄에는 **아무 영향이 없다**(zone() 이 TZ 를 읽지 않는다) — 로그 관측 문제다.
    if [ -n "$(val TZ "$envf")" ]; then
      echo "  ${YEL}! TZ 가 설정돼 있다 — facade 로그가 UTC 가 아니게 되어 parse-svc 와"
      echo "    시각축이 섞인다(스케줄에는 영향 없음). 로그 상관분석을 하려면 지우세요.${RST}"
    fi
    # 마감이 창보다 짧으면 창 안에 제출된 잡이 그 밤에 곧바로 취소된다(제출 0건과 같다).
    local win dl; win="$(val KBP_COMMUNITY_WINDOW_MINUTES "$envf")"; dl="$(val KBP_COMMUNITY_DEADLINE_MINUTES "$envf")"
    if [ -n "$win" ] && [ -n "$dl" ] && [ "$dl" -le "$win" ] 2>/dev/null; then
      echo "  ${RED}✗ KBP_COMMUNITY_DEADLINE_MINUTES($dl) <= WINDOW_MINUTES($win)${RST}"
      echo "    ${RED}  → 창 안에 제출한 잡을 그 밤에 즉시 취소한다(제출 0건과 같아진다).${RST}"
      miss=1
    fi
  fi
  # ── global 검색(B) ────────────────────────────────────────────────────────
  # 0 이면 슬롯을 못 잡아 프론트 버튼이 항상 503 이다. 파서 전용 배포에서는 의도지만
  # 전체 스택에서 0 이면 기능이 조용히 죽은 것이다 — 버튼은 보이는데 안 된다.
  local gsc; gsc="$(val KBP_GLOBAL_SEARCH_CONCURRENCY "$envf")"
  if [ "$gsc" = "0" ]; then
    echo "  ${YEL}! KBP_GLOBAL_SEARCH_CONCURRENCY=0 — global(전체 요약) 검색이 항상 503 이다."
    echo "    파서 전용 배포면 정상. 전체 스택이면 프론트 버튼이 보이는데 안 된다.${RST}"
  fi
  if [ "$miss" -eq 0 ]; then echo "  ${GRN}✓ 필수키 모두 채워짐${RST}"; else return 1; fi
  return 0
}

check_images() {
  local ARCH_EXPECT="${ARCH_EXPECT:-amd64}"
  echo "== 이미지 검사 (arch=$ARCH_EXPECT) =="
  local ENGINE=""
  command -v docker >/dev/null && ENGINE=docker
  command -v podman >/dev/null && ENGINE="${ENGINE:-podman}"
  [ -n "$ENGINE" ] || { echo "${RED}✗ docker/podman 둘 다 없음${RST}"; return 1; }
  local bad=0
  local EXPECT_TAG="${EXPECT_TAG:-airgap}"
  for img in "${IMAGES[@]}"; do
    # 번들 태그(기본 airgap)를 우선 매칭 — 개발기에 같은 이름의 :latest/:local 등이
    # 같이 있으면 태그 무시 첫 매치가 엉뚱한(번들과 무관한) 이미지를 "확인됨"으로 오판정한다.
    local ref; ref="$($ENGINE images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep -m1 -E "^(localhost/)?${img}:${EXPECT_TAG}\$" || true)"
    [ -z "$ref" ] && ref="$($ENGINE images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep -m1 -E "^(localhost/)?${img}(:|$)" || true)"
    if [ -z "$ref" ]; then echo "  ${RED}✗ 없음: $img${RST}"; bad=1; continue; fi
    local a; a="$($ENGINE image inspect --format '{{.Architecture}}' "$ref" 2>/dev/null || echo '?')"
    if [ "$a" = "$ARCH_EXPECT" ]; then echo "  ${GRN}✓ $ref ($a)${RST}"
    else echo "  ${RED}✗ $ref arch=$a (기대 $ARCH_EXPECT)${RST}"; bad=1; fi
  done
  return $bad
}

check_imports() {
  echo "== 런타임 의존성 스모크(kbp-parse-svc: kordoc) =="
  local ENGINE=""
  command -v docker >/dev/null && ENGINE=docker
  command -v podman >/dev/null && ENGINE="${ENGINE:-podman}"
  [ -n "$ENGINE" ] || { echo "${RED}✗ docker/podman 둘 다 없음${RST}"; return 1; }
  local ref; ref="$($ENGINE images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep -m1 -E '^(localhost/)?kbp-parse-svc(:|$)' || true)"
  [ -z "$ref" ] && { echo "  ${RED}✗ kbp-parse-svc 이미지 없음 — --images 먼저 통과해야 함${RST}"; return 1; }
  # kordoc(docx 폴백 레인은 2026-08-06 제거됐지만 엑셀 파서 백엔드로는 여전히 쓴다,
  # A6·Dockerfile.parse-svc 참고). 이미지에 바이너리가 빠진 채로 healthy 가 뜨는
  # 것을 배포 전에 잡는다.
  # timeout(1) 은 macOS 기본 설치엔 없다(GNU coreutils 전용) — 검증을 macOS 개발기에서도
  # 돌릴 수 있어야 하므로 gtimeout(brew coreutils) 폴백, 둘 다 없으면 타임아웃 없이 실행.
  local TIMEOUT_BIN=""
  command -v timeout >/dev/null && TIMEOUT_BIN=timeout
  [ -z "$TIMEOUT_BIN" ] && command -v gtimeout >/dev/null && TIMEOUT_BIN=gtimeout
  local out
  if [ -n "$TIMEOUT_BIN" ]; then
    out="$("$TIMEOUT_BIN" "${IMPORTS_CHECK_TIMEOUT:-60}" "$ENGINE" run --rm --entrypoint sh "$ref" \
      -c 'command -v kordoc && kordoc --version' 2>&1)"
  else
    echo "  ${YEL}! timeout/gtimeout 없음 — 타임아웃 없이 실행${RST}"
    out="$("$ENGINE" run --rm --entrypoint sh "$ref" \
      -c 'command -v kordoc && kordoc --version' 2>&1)"
  fi
  if [ $? -eq 0 ]; then
    echo "  ${GRN}✓ kordoc 설치 확인 ($ref)${RST}"
  else
    echo "  ${RED}✗ kordoc 없음/실행 실패($ref) — 엑셀 파서 kordoc 백엔드가 조용히 깨진다:${RST}"
    echo "$out" | sed 's/^/    /'
    return 1
  fi
  # ★ 바이너리 존재만으로는 부족하다(2026-08-10). kordoc 백엔드는 **env 3종**에도 의존한다 —
  #   KORDOC_BIN / KORDOC_MD_OUT / EXCEL_PARSER_BACKEND. 실제로 호스트 dev 에서 그 env 가
  #   빠져 "'*.md' 를 찾을 수 없습니다" 로 엑셀 파싱이 죽었다(컨테이너는 Dockerfile ENV 로
  #   살아 있었다). 이미지 쪽도 언젠가 같은 식으로 어긋날 수 있으므로 **실제 xlsx 를 한 번
  #   파싱**해 왕복을 확인한다 — fitz 때와 같은 부류(있는데 안 돌려서 놓친 것)를 막는다.
  echo "== 엑셀 파싱 왕복 스모크(kordoc 백엔드 + env) =="
  local XLS_PY='
import os, sys, io
need = ["KORDOC_BIN", "KORDOC_MD_OUT", "EXCEL_PARSER_BACKEND"]
missing = [k for k in need if not os.environ.get(k)]
if missing:
    print("ENV_MISSING:" + ",".join(missing)); sys.exit(1)
md = os.environ["KORDOC_MD_OUT"]
if not os.path.isdir(md):
    print("MD_OUT_MISSING:" + md); sys.exit(1)
from openpyxl import Workbook
wb = Workbook(); ws = wb.active; ws.title = "smoke"
ws.append(["a", "b"]); ws.append(["1", "2"])
buf = io.BytesIO(); wb.save(buf)
from parse_service.parsers.excel import parse as xparse
r = xparse(buf.getvalue(), "smoke.xlsx")
n = len(getattr(r, "chunks", None) or [])
gs = getattr(r, "gate_summary", None)
if n < 1 or gs is None:
    print("PARSE_EMPTY: chunks=%d gate_summary=%r" % (n, gs)); sys.exit(1)
print("OK chunks=%d gate_ok=%s" % (n, (gs or {}).get("ok")))
'
  local xout
  if [ -n "$TIMEOUT_BIN" ]; then
    xout="$("$TIMEOUT_BIN" "${IMPORTS_CHECK_TIMEOUT:-120}" "$ENGINE" run --rm -w /app -e PYTHONPATH=/app \
      --entrypoint python "$ref" -c "$XLS_PY" 2>&1)"
  else
    xout="$("$ENGINE" run --rm -w /app -e PYTHONPATH=/app --entrypoint python "$ref" -c "$XLS_PY" 2>&1)"
  fi
  case "$xout" in
    *OK\ chunks=*) echo "  ${GRN}✓ 엑셀 왕복 성공 — ${xout##*OK }${RST}" ;;
    *ENV_MISSING:*) echo "  ${RED}✗ 이미지에 kordoc env 가 없다: ${xout#*ENV_MISSING:}${RST}"
                    echo "    ${RED}  → Dockerfile.parse-svc 의 ENV KORDOC_BIN/KORDOC_MD_OUT/EXCEL_PARSER_BACKEND 확인${RST}"; return 1 ;;
    *MD_OUT_MISSING:*) echo "  ${RED}✗ KORDOC_MD_OUT 디렉터리 부재: ${xout#*MD_OUT_MISSING:}${RST}"; return 1 ;;
    *) echo "  ${RED}✗ 엑셀 파싱 왕복 실패 — 이미지가 healthy 로 떠도 xlsx 적재가 깨진다:${RST}"
       echo "$xout" | tail -6 | sed 's/^/    /'; return 1 ;;
  esac
  # ★ html 레인은 2026-08-11 형변환 API 밖으로 나왔다(parsers/html + markdownify).
  #   markdownify 가 이미지에 빠지면 html 적재만 조용히 죽는다 — requirements.txt 에
  #   넣었어도 `pip install .` 이 건너뛰는 전례가 있었다(kb 이미지 문서 추출기 누락).
  #   import 존재만이 아니라 **병합셀 표가 <table> 로 살아 나오는지**까지 확인한다.
  echo "== html 파싱 왕복 스모크(markdownify + 표 보존) =="
  local HTML_PY='
import sys
from parse_service.parsers.html import parse as hparse
raw = b"<html><body><h1>T</h1><table><tr><th rowspan=\"2\">a</th><th colspan=\"2\">b</th></tr><tr><td>1</td><td>2</td></tr></table></body></html>"
rr = hparse(raw, "smoke.html")
blocks = (rr.pages or [{}])[0].get("blocks") or []
tables = [b for b in blocks if b.get("type") == "table"]
if not tables:
    print("NO_TABLE_BLOCK: %d blocks" % len(blocks)); sys.exit(1)
body = tables[0].get("table_body") or ""
if "rowspan" not in body or "colspan" not in body:
    print("MERGE_LOST: %s" % body[:120]); sys.exit(1)
print("OK html_blocks=%d" % len(blocks))
'
  local hout
  if [ -n "$TIMEOUT_BIN" ]; then
    hout="$("$TIMEOUT_BIN" "${IMPORTS_CHECK_TIMEOUT:-120}" "$ENGINE" run --rm -w /app -e PYTHONPATH=/app \
      --entrypoint python "$ref" -c "$HTML_PY" 2>&1)"
  else
    hout="$("$ENGINE" run --rm -w /app -e PYTHONPATH=/app --entrypoint python "$ref" -c "$HTML_PY" 2>&1)"
  fi
  case "$hout" in
    *OK\ html_blocks=*) echo "  ${GRN}✓ html 왕복 성공 — ${hout##*OK }${RST}" ;;
    *NO_TABLE_BLOCK:*|*MERGE_LOST:*) echo "  ${RED}✗ html 표 보존 실패 — pipe 평탄화 회귀다:${RST}"
       echo "$hout" | tail -4 | sed 's/^/    /'; return 1 ;;
    *) echo "  ${RED}✗ html 파싱 왕복 실패 — markdownify 누락 또는 parsers/html import 실패:${RST}"
       echo "$hout" | tail -6 | sed 's/^/    /'; return 1 ;;
  esac
  # ⚠️ 파일변환(한컴) API 는 이미지 안 도구가 아니라 온프렘 HTTP 엔드포인트라 여기서
  # 도달성을 못 확인한다 — check_env() 의 KBP_FILECONVERT_URL 값 존재 확인이 유일한
  # 사전 방어선이다. docx/hwp/ppt 파싱은 그 서비스가 실제로 응답해야 성공한다
  # (A6 — 구 kordoc docx 폴백은 제거됨, 지금은 이 경로가 유일하다).
  # (html 은 2026-08-11 이 경로에서 빠졌다 — parsers/html 이 형변환 없이 처리한다.)
}

# --parse-only 를 어디에 붙여도 받는다(순서 무관):  --env .env --parse-only  /  --parse-only --env .env
PROFILE=full
for _a in "$@"; do [ "$_a" = "--parse-only" ] && PROFILE=parse-only; done
set -- $(printf '%s\n' "$@" | grep -v '^--parse-only$' || true)
[ "$PROFILE" = "parse-only" ] && echo "프로필: parse-only (edgequake/adaptive_chunk 전용 키 ${#PARSE_ONLY_SKIP[@]}개 건너뜀)"

case "${1:-}" in
  --env)     check_env "${2:-}"    || rc=1 ;;
  --images)  check_images          || rc=1 ;;
  --imports) check_imports         || rc=1 ;;
  *)         check_env  || rc=1; echo; check_images || rc=1; echo; check_imports || rc=1 ;;
esac
exit $rc
