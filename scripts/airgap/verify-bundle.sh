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

check_env() {
  local envf="${1:-$REPO_ROOT/.env}"
  echo "== .env 검사: $envf =="
  [ -f "$envf" ] || { echo "${RED}✗ 파일 없음${RST}"; return 1; }
  local miss=0 inet=0
  for k in "${REQUIRED_ENV[@]}"; do
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
    local ref; ref="$($ENGINE images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep -m1 -E "^${img}:${EXPECT_TAG}\$" || true)"
    [ -z "$ref" ] && ref="$($ENGINE images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep -m1 -E "^${img}(:|$)" || true)"
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
  local ref; ref="$($ENGINE images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep -m1 -E '^kbp-parse-svc(:|$)' || true)"
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
  # ⚠️ 파일변환(한컴) API 는 이미지 안 도구가 아니라 온프렘 HTTP 엔드포인트라 여기서
  # 도달성을 못 확인한다 — check_env() 의 KBP_FILECONVERT_URL 값 존재 확인이 유일한
  # 사전 방어선이다. docx/hwp/ppt/html 파싱은 그 서비스가 실제로 응답해야 성공한다
  # (A6 — 구 kordoc docx 폴백은 제거됨, 지금은 이 경로가 유일하다).
}

case "${1:-}" in
  --env)     check_env "${2:-}"    || rc=1 ;;
  --images)  check_images          || rc=1 ;;
  --imports) check_imports         || rc=1 ;;
  *)         check_env  || rc=1; echo; check_images || rc=1; echo; check_imports || rc=1 ;;
esac
exit $rc
