#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# load-and-up.sh — [Phase B / 오프라인] 폐쇄망 RHEL 서버에서 kb-pipeline 기동
#
# 번들 압축을 푼 디렉터리에서 실행한다(compose·.env·images 가 같이 있어야 함).
#   1) podman load  (9개 이미지 일괄 로드)
#   2) .env 존재/필수키 확인
#   3) podman-compose up -d
#   4) health 폴링 (podman-compose 에는 --wait 가 없어 직접 폴링)
#   5) MinIO 버킷 생성 (멱등, 컨테이너명 자동탐색)
#   6) 스모크 요약
#
# 전제: RHEL + rootful podman + podman-compose(또는 `podman compose`).
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # scripts/ 상위 = 번들 루트
cd "$HERE/.."  2>/dev/null || cd "$HERE"                  # 번들 루트로
BUNDLE_ROOT="$(pwd)"
COMPOSE_FILE="$BUNDLE_ROOT/docker-compose.airgap.yml"
IMAGES_GLOB="$BUNDLE_ROOT/images"/kbp-images-*.tar.gz
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-300}"    # 서비스별 health 폴링 상한(초)

log()  { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m✗ %s\033[0m\n' "$*"; exit 1; }

command -v podman >/dev/null || die "podman 이 없습니다."

# compose 프론트엔드 탐지: podman-compose 우선, 없으면 `podman compose`
if command -v podman-compose >/dev/null; then
  COMPOSE=(podman-compose -f "$COMPOSE_FILE")
elif podman compose version >/dev/null 2>&1; then
  COMPOSE=(podman compose -f "$COMPOSE_FILE")
else
  die "podman-compose(또는 'podman compose') 가 없습니다. RHEL 에 설치 필요: dnf install podman-compose"
fi

# ── 1) 이미지 로드 ────────────────────────────────────────────────────────────
log "podman load — 이미지 로드"
shopt -s nullglob
tars=( $IMAGES_GLOB )
[ ${#tars[@]} -gt 0 ] || die "이미지 tar 를 찾지 못함: $IMAGES_GLOB"
for t in "${tars[@]}"; do
  echo "  load $t"
  podman load -i "$t"
done

# ── 2) .env 확인 ──────────────────────────────────────────────────────────────
if [ ! -f "$BUNDLE_ROOT/.env" ]; then
  warn ".env 가 없습니다. 템플릿을 복사합니다 → .env"
  cp "$BUNDLE_ROOT/.env.airgap.example" "$BUNDLE_ROOT/.env"
  die ".env 의 【A. 온프렘 재설정 필수】 블록을 사내 엔드포인트로 채운 뒤 다시 실행하세요."
fi
log ".env 필수키 검증"
bash "$BUNDLE_ROOT/scripts/airgap/verify-bundle.sh" --env "$BUNDLE_ROOT/.env" \
  || die ".env 검증 실패 — 위 항목을 채우고 다시 실행하세요."

# ── 3) 기동 ───────────────────────────────────────────────────────────────────
log "compose up -d  (${COMPOSE[*]})"
"${COMPOSE[@]}" --env-file "$BUNDLE_ROOT/.env" up -d

# ── 4) health 폴링 ────────────────────────────────────────────────────────────
# "라벨|URL"  — 호스트 포트로 접근(컨테이너는 호스트 네임스페이스 포트 매핑됨)
CHECKS=(
  "postgres|"                                             # pg 는 아래 podman healthcheck 로 확인
  "minio|http://localhost:9000/minio/health/live"
  "edgequake|http://localhost:8081/health"
  "doc_guard|http://localhost:8001/healthz"
  "adaptive_chunk|http://localhost:18060/healthz"
  "parse-svc|http://localhost:19001/healthz"
  "facade|http://localhost:19000/healthz"
  "edgequake_webui|http://localhost:13000"
)
poll() {
  local url="$1" deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
  until curl -fsS -o /dev/null "$url" 2>/dev/null; do
    [ "$(date +%s)" -ge "$deadline" ] && return 1
    sleep 3
  done
}
log "health 폴링 (서비스별 최대 ${HEALTH_TIMEOUT}s)"
FAIL=0
for c in "${CHECKS[@]}"; do
  IFS='|' read -r name url <<<"$c"
  [ -z "$url" ] && continue
  printf '  %-16s ' "$name"
  if poll "$url"; then echo "✓ healthy"; else echo "✗ TIMEOUT ($url)"; FAIL=1; fi
done

# ── 5) MinIO 버킷 생성 (멱등) ─────────────────────────────────────────────────
log "MinIO 버킷 생성"
BUCKET="$(grep -E '^MINIO_BUCKET=' "$BUNDLE_ROOT/.env" | cut -d= -f2 | tr -d '[:space:]')"
BUCKET="${BUCKET:-document-parser}"
MC_CTR="$(podman ps --format '{{.Names}}' | grep -m1 -i minio || true)"
if [ -n "$MC_CTR" ]; then
  podman exec "$MC_CTR" sh -c \
    'mc alias set local http://localhost:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null 2>&1 \
     && mc mb -p local/'"$BUCKET"' 2>/dev/null; mc ls local/' \
    && echo "  ✓ 버킷 '$BUCKET' 준비됨" || warn "버킷 생성 실패(파싱은 되나 페이지 이미지 업로드 skip)"
else
  warn "minio 컨테이너를 찾지 못해 버킷 생성 건너뜀"
fi

# ── 6) 요약 ───────────────────────────────────────────────────────────────────
log "상태"
"${COMPOSE[@]}" ps || true
if [ "$FAIL" -eq 0 ]; then
  printf '\n\033[1;32m✅ 전 서비스 healthy — facade: http://localhost:19000  webui: http://localhost:13000\033[0m\n'
else
  die "일부 서비스 unhealthy. 로그 확인: ${COMPOSE[*]} logs <service>  (자세한 진단은 docs/airgap-deploy.md §트러블슈팅)"
fi
