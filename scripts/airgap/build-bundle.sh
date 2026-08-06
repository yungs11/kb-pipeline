#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# build-bundle.sh — [Phase A / 온라인] kb-pipeline 폐쇄망 배포 번들 생성
#
# 인터넷이 되는 개발기(Apple Silicon Mac 등)에서 실행한다. amd64 이미지를
# 크로스빌드/pull → docker save → 단일 tar.gz 번들로 묶는다. 이 번들을 폐쇄망
# RHEL 서버로 옮겨 scripts/airgap/load-and-up.sh 로 기동한다.
#
# 요구: docker + buildx (Docker Desktop 기본 포함). QEMU 에뮬 크로스빌드라
#       edgequake(Rust) 최초 빌드가 ~10분+ 걸릴 수 있다.
#
# 사용:
#   scripts/airgap/build-bundle.sh                 # 전체 빌드+번들
#   scripts/airgap/build-bundle.sh --no-build      # 이미 빌드된 이미지로 번들만
#   PLATFORM=linux/arm64 scripts/airgap/build-bundle.sh   # 대상 arch 변경
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

PLATFORM="${PLATFORM:-linux/amd64}"
TAG="${TAG:-airgap}"
ARCH_SHORT="$(echo "$PLATFORM" | sed 's#linux/##')"

# 리포 루트 = 이 스크립트의 ../../
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"
DIST="$REPO_ROOT/dist"
BUNDLE="$DIST/bundle"
IMAGES_TAR="$BUNDLE/images/kbp-images-${ARCH_SHORT}.tar"

# 빌드 대상: "태그|dockerfile|context"  (context 상대경로는 REPO_ROOT 기준)
BUILDS=(
  "kbp-edgequake:${TAG}|docker/edgequake.Dockerfile|."
  "kbp-parse-svc:${TAG}|Dockerfile.parse-svc|."
  "kbp-facade:${TAG}|Dockerfile.facade|."
  "kbp-adaptive_chunk:${TAG}|-|../99.projects/adaptive_chunk"
  "kbp-doc_guard:${TAG}|-|../99.projects/shinhan_trust/doc_guard"
  "kbp-edgequake_webui:${TAG}|edgequake/edgequake_webui/Dockerfile|edgequake"
)
# pull 대상(업스트림 태그 유지 — compose 가 이 이름으로 참조)
PULLS=(
  "ghcr.io/raphaelmansuy/edgequake-postgres:latest"
  "minio/minio:latest"
  
)

log() { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }

NO_BUILD=0
[ "${1:-}" = "--no-build" ] && NO_BUILD=1

command -v docker >/dev/null || { echo "docker 없음"; exit 1; }
docker buildx version >/dev/null 2>&1 || { echo "docker buildx 없음 (Docker Desktop 필요)"; exit 1; }

if [ "$NO_BUILD" -eq 0 ]; then
  log "buildx 크로스빌드 ($PLATFORM) — 6개 앱 이미지"
  for spec in "${BUILDS[@]}"; do
    IFS='|' read -r img dfile ctx <<<"$spec"
    log "build $img  (context=$ctx, dockerfile=${dfile})"
    if [ "$dfile" = "-" ]; then
      docker buildx build --platform "$PLATFORM" -t "$img" --load "$ctx"
    else
      docker buildx build --platform "$PLATFORM" -f "$dfile" -t "$img" --load "$ctx"
    fi
  done

  log "base 이미지 pull ($PLATFORM) — 3개 인프라 이미지"
  for img in "${PULLS[@]}"; do
    log "pull $img"
    docker pull --platform "$PLATFORM" "$img"
  done
fi

# arch 검증 — 빌드/pull 결과가 실제로 대상 arch 인지 확인
log "arch 검증 ($ARCH_SHORT)"
ALL_IMAGES=()
for spec in "${BUILDS[@]}"; do ALL_IMAGES+=("${spec%%|*}"); done
ALL_IMAGES+=("${PULLS[@]}")
for img in "${ALL_IMAGES[@]}"; do
  got="$(docker image inspect --format '{{.Architecture}}' "$img" 2>/dev/null || echo MISSING)"
  if [ "$got" != "$ARCH_SHORT" ]; then
    echo "  ✗ $img → arch=$got (기대 $ARCH_SHORT)"; exit 1
  fi
  echo "  ✓ $img → $got"
done

# save + 번들 스테이징
log "docker save → $IMAGES_TAR (gzip)"
mkdir -p "$BUNDLE/images" "$BUNDLE/scripts/airgap" "$BUNDLE/docs"
docker save "${ALL_IMAGES[@]}" -o "$IMAGES_TAR"
gzip -f "$IMAGES_TAR"
IMAGES_TAR="${IMAGES_TAR}.gz"

log "배포물 스테이징 → $BUNDLE"
cp docker-compose.airgap.yml            "$BUNDLE/"
cp .env.airgap.example                  "$BUNDLE/"
cp scripts/airgap/load-and-up.sh        "$BUNDLE/scripts/airgap/"
cp scripts/airgap/verify-bundle.sh      "$BUNDLE/scripts/airgap/"
cp docs/airgap-deploy.md                "$BUNDLE/docs/" 2>/dev/null || true
chmod +x "$BUNDLE"/scripts/airgap/*.sh

log "단일 번들 tar.gz 생성"
OUT="$DIST/kbp-airgap-bundle-${ARCH_SHORT}.tar.gz"
tar czf "$OUT" -C "$BUNDLE" .
SHA="$(shasum -a 256 "$OUT" | awk '{print $1}')"
echo "$SHA  $(basename "$OUT")" > "${OUT}.sha256"

# 2GB 초과 시 2GB 단위 분할(SPLIT_SIZE 로 조정). 전송매체 파일크기 한도 대응.
SPLIT_SIZE="${SPLIT_SIZE:-2g}"
OUT_BYTES=$(wc -c < "$OUT")
SPLIT_MSG="(분할 안 함 — 2GB 이하)"
if [ "$OUT_BYTES" -gt 2147483648 ]; then
  log "2GB 초과 → ${SPLIT_SIZE} 단위 분할"
  rm -f "${OUT}".part-* 2>/dev/null || true
  split -b "$SPLIT_SIZE" "$OUT" "${OUT}.part-"
  shasum -a 256 "${OUT}".part-* > "${OUT}.parts.sha256"
  if [ "$(cat "${OUT}".part-* | shasum -a 256 | awk '{print $1}')" = "$SHA" ]; then
    SPLIT_MSG="분할 $(ls "${OUT}".part-* | wc -l | tr -d ' ')개 (재결합 해시 검증 ✓): $(ls "${OUT}".part-* | xargs -n1 basename | tr '\n' ' ')"
    [ "${KEEP_WHOLE:-0}" = "1" ] || rm -f "$OUT"
  else
    echo "✗ 분할 재결합 해시 불일치 — 중단"; exit 1
  fi
fi

cat <<EOF

✅ 완료
   번들:      $OUT
   sha256:    $SHA
   분할:      $SPLIT_MSG
   이미지tar: $IMAGES_TAR ($(du -h "$IMAGES_TAR" | cut -f1))

다음 단계 (폐쇄망 RHEL 서버에서):
  1) $OUT 를 서버로 전송 후 풀기:   mkdir kbp && tar xzf kbp-airgap-bundle-${ARCH_SHORT}.tar.gz -C kbp && cd kbp
  2) cp .env.airgap.example .env && vi .env   # 【A. 온프렘 재설정 필수】 블록 채우기
  3) ./scripts/airgap/load-and-up.sh
EOF
