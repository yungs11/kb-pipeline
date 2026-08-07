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
# pull 대상: "업스트림참조|로컬태그"
#
# **pull 은 digest 로(재현성), save/compose 는 로컬 태그로** 한다. 두 가지를 분리하는 이유:
#  1) digest 로 pull 해야 번들 재빌드 시점마다 업스트림이 바뀌는 드리프트를 막는다
#     (실측: edgequake-postgres :latest 가 pg16→pg18 로 바뀌며 볼륨 마운트 규약이 깨졌다).
#  2) 그런데 **digest 참조를 compose 에 그대로 쓰면 podman 배포가 깨진다** — `docker save`
#     한 digest-only 이미지는 `podman load` 시 RepoTags/RepoDigests 가 **둘 다 비어**
#     `<none>:<none>` 로 들어오고, compose 의 `image: ...@sha256:...` 는
#     `image not known` 으로 실패해 그 서비스가 아예 안 뜬다(실측 2026-08-07,
#     Fedora/podman 5.8.2. docker 는 관대해서 넘어가므로 docker 테스트로는 절대 안 잡힌다).
#  3) 덤으로 unqualified name(`minio/minio`) 도 없앤다 — podman 은 이런 이름을
#     unqualified-search-registries 로 해석하려 들어 폐쇄망에서 재-pull 시도 위험이 있다.
#     로컬 태그(`kbp-*:airgap`)면 레지스트리 해석 자체가 일어나지 않는다.
PULLS=(
  "ghcr.io/raphaelmansuy/edgequake-postgres@sha256:61c4de562ea925c9ba7130c4e0e9649515eae7da9c0729207af8d0f79ba0471a|kbp-postgres:${TAG}"
  "docker.io/minio/minio:latest|kbp-minio:${TAG}"
)

log() { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }

NO_BUILD=0
PARSE_ONLY=0
for a in "$@"; do
  case "$a" in
    --no-build)  NO_BUILD=1 ;;
    --parse-only) PARSE_ONLY=1 ;;
  esac
done

# --parse-only: **파싱 배치 전용** 축소 번들(parse-only-up.sh 로 기동).
# edgequake(Rust, 이미지 최대) / adaptive_chunk / doc_guard / webui 를 빼므로 번들이 크게
# 작아지고 빌드도 빠르다. 청킹·적재·검색은 이 번들로 못 한다.
if [ "$PARSE_ONLY" -eq 1 ]; then
  BUILDS=(
    "kbp-parse-svc:${TAG}|Dockerfile.parse-svc|."
    "kbp-facade:${TAG}|Dockerfile.facade|."
  )
  IMAGES_TAR="$BUNDLE/images/kbp-parse-images-${ARCH_SHORT}.tar"
  BUNDLE_NAME="kbp-parse-bundle-${ARCH_SHORT}.tar.gz"
else
  BUNDLE_NAME="kbp-airgap-bundle-${ARCH_SHORT}.tar.gz"
fi

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

  log "base 이미지 pull ($PLATFORM) — 인프라 이미지 (digest pull → 로컬 태그 부여)"
  for spec in "${PULLS[@]}"; do
    IFS='|' read -r upstream localtag <<<"$spec"
    log "pull $upstream  →  $localtag"
    docker pull --platform "$PLATFORM" "$upstream"
    # 로컬 태그를 붙여야 podman load 후에도 이름이 살아남는다(위 PULLS 주석 참고).
    docker tag "$upstream" "$localtag"
  done
fi

# arch 검증 — 빌드/pull 결과가 실제로 대상 arch 인지 확인
log "arch 검증 ($ARCH_SHORT)"
ALL_IMAGES=()
for spec in "${BUILDS[@]}"; do ALL_IMAGES+=("${spec%%|*}"); done
# 인프라는 **로컬 태그**를 검증/저장 대상으로 쓴다(digest 참조가 아니라).
for spec in "${PULLS[@]}"; do ALL_IMAGES+=("${spec##*|}"); done
for img in "${ALL_IMAGES[@]}"; do
  got="$(docker image inspect --format '{{.Architecture}}' "$img" 2>/dev/null || echo MISSING)"
  if [ "$got" != "$ARCH_SHORT" ]; then
    echo "  ✗ $img → arch=$got (기대 $ARCH_SHORT)"; exit 1
  fi
  echo "  ✓ $img → $got"
done

# save + 번들 스테이징
log "docker save → $IMAGES_TAR (gzip)"
# ⚠️ 스테이징 디렉터리를 **매번 비운다**. 안 그러면 이전 실행의 이미지 tar 가 남아
# 다음 번들에 함께 들어간다 — 실측 2026-08-07: --parse-only 번들에 직전 전체 스택
# 이미지(2.8GB)가 딸려 들어가 1.9GB 여야 할 번들이 4.7GB(3분할)가 됐다.
# 받는 쪽은 필요도 없는 이미지를 로드하게 되고 용량·시간이 그만큼 낭비된다.
rm -rf "$BUNDLE"
mkdir -p "$BUNDLE/images" "$BUNDLE/scripts/airgap" "$BUNDLE/docs"
docker save "${ALL_IMAGES[@]}" -o "$IMAGES_TAR"
gzip -f "$IMAGES_TAR"
IMAGES_TAR="${IMAGES_TAR}.gz"

log "배포물 스테이징 → $BUNDLE"
cp docker-compose.airgap.yml            "$BUNDLE/"
cp .env.airgap.example                  "$BUNDLE/"
cp scripts/airgap/load-and-up.sh        "$BUNDLE/scripts/airgap/"
cp scripts/airgap/verify-bundle.sh      "$BUNDLE/scripts/airgap/"
cp scripts/airgap/deploy-both.sh        "$BUNDLE/scripts/airgap/"
cp scripts/airgap/parse-only-up.sh      "$BUNDLE/scripts/airgap/"
cp docs/airgap-deploy.md                "$BUNDLE/docs/" 2>/dev/null || true
chmod +x "$BUNDLE"/scripts/airgap/*.sh

log "단일 번들 tar.gz 생성"
OUT="$DIST/$BUNDLE_NAME"
tar czf "$OUT" -C "$BUNDLE" .
SHA="$(shasum -a 256 "$OUT" | awk '{print $1}')"
echo "$SHA  $(basename "$OUT")" > "${OUT}.sha256"

# 분할은 **기본으로 하지 않는다**(단일 파일이 다루기 쉽다).
# 전송매체에 파일 크기 한도가 있을 때만 `SPLIT_SIZE=2g` 처럼 명시해서 켠다.
SPLIT_SIZE="${SPLIT_SIZE:-}"
OUT_BYTES=$(wc -c < "$OUT")
SPLIT_MSG="(분할 안 함 — 단일 파일)"
if [ -n "$SPLIT_SIZE" ]; then
  log "${SPLIT_SIZE} 단위 분할 (SPLIT_SIZE 지정됨)"
  rm -f "${OUT}".part-* 2>/dev/null || true
  split -b "$SPLIT_SIZE" "$OUT" "${OUT}.part-"
  # basename 으로 기록한다 — 절대경로로 남기면 폐쇄망 서버에서 `sha256sum -c` 가
  # "No such file or directory" 로 100% 실패한다(빌드머신 경로를 찾으므로).
  # 실측 2026-08-07: RHEL+podman 시뮬레이션에서 docs/airgap-deploy.md §2 의 검증
  # 명령이 이 이유로 깨지는 것을 확인. 단일 파일 .sha256 은 이미 basename 이었다.
  ( cd "$DIST" && shasum -a 256 "$(basename "$OUT")".part-* > "$(basename "$OUT").parts.sha256" )
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
