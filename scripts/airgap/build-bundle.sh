#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# build-bundle.sh — [Phase A / 온라인] kb-pipeline 폐쇄망 배포 번들 생성
#
# 인터넷이 되는 개발기(Apple Silicon Mac 등)에서 실행한다. amd64 이미지를
# 크로스빌드/pull → docker save → 단일 tar 번들로 묶는다(무압축). 이 번들을 폐쇄망
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
  BUNDLE_NAME="kbp-parse-bundle-${ARCH_SHORT}.tar"
else
  BUNDLE_NAME="kbp-airgap-bundle-${ARCH_SHORT}.tar"
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
# ★ **압축하지 않는다**(2026-08-13). `docker save` 는 containerd 스토어의 레이어 blob 을
#   **이미 압축된 채로** 뱉는다 — 실측: facade 이미지 274,554,880B → gzip 후 272,640,365B
#   (**0.7% 감소**). 그 1% 를 위해 2GB 대 파일을 한 번 더 읽고 쓰느라 번들당 수 분을 쓴다.
#   `docker/podman load -i` 는 평문 tar 도 그대로 받는다. 바깥 번들도 같은 이유로 `tar cf`.
#   받는 쪽은 `.tar`·`.tar.gz` 를 **둘 다** 받도록 해뒀다(옛 번들 호환) — parse-only-up.sh /
#   load-and-up.sh 의 IMAGES_GLOB, 그리고 문서의 `tar xf`(둘 다 자동인식).
log "docker save → $IMAGES_TAR (무압축 — 이미 압축된 레이어라 gzip 이득 0.7%)"
# ⚠️ 스테이징 디렉터리를 **매번 비운다**. 안 그러면 이전 실행의 이미지 tar 가 남아
# 다음 번들에 함께 들어간다 — 실측 2026-08-07: --parse-only 번들에 직전 전체 스택
# 이미지(2.8GB)가 딸려 들어가 1.9GB 여야 할 번들이 4.7GB(3분할)가 됐다.
# 받는 쪽은 필요도 없는 이미지를 로드하게 되고 용량·시간이 그만큼 낭비된다.
rm -rf "$BUNDLE"
mkdir -p "$BUNDLE/images" "$BUNDLE/scripts/airgap" "$BUNDLE/docs"
docker save "${ALL_IMAGES[@]}" -o "$IMAGES_TAR"

log "배포물 스테이징 → $BUNDLE"
cp docker-compose.airgap.yml            "$BUNDLE/"
cp .env.airgap.example                  "$BUNDLE/"
cp scripts/airgap/load-and-up.sh        "$BUNDLE/scripts/airgap/"
cp scripts/airgap/verify-bundle.sh      "$BUNDLE/scripts/airgap/"

# ★ 가드를 **강제 실행**한다(2026-08-10). 복사만 해두면 안 돌린다 — 실제로 그렇게
#   `fitz`(PyMuPDF) 누락 이미지가 폐쇄망까지 반입됐다. verify-bundle 의 import 검사가
#   잡을 수 있었는데 아무도 돌리지 않았다. 이미지가 healthy 로 떠도 신호수집이 조용히
#   폴백만 타므로 배포 후에도 드러나지 않는 부류다.
#   SKIP_VERIFY=1 로만 건너뛸 수 있게 하고, 건너뛰면 크게 경고한다.
if [ "${SKIP_VERIFY:-0}" = "1" ]; then
  echo "⚠️  SKIP_VERIFY=1 — 이미지/의존성 가드를 건너뛴다. 폐쇄망 반입 전에 반드시 수동 실행:"
  echo "     bash scripts/airgap/verify-bundle.sh --images && bash scripts/airgap/verify-bundle.sh --imports"
else
  echo "== 배포 가드 강제 실행(--images, --imports) =="
  if ! bash scripts/airgap/verify-bundle.sh --images; then
    echo "✗ 이미지 가드 실패 — 번들을 만들지 않는다(반입 후에야 드러나는 부류다)." >&2; exit 1
  fi
  if ! bash scripts/airgap/verify-bundle.sh --imports; then
    echo "✗ 런타임 의존성 가드 실패 — 번들을 만들지 않는다." >&2
    echo "  (이 검사가 잡는 것: 이미지에 파서/추출기 바이너리·모듈이 빠진 채 healthy 로 뜨는 상태)" >&2
    exit 1
  fi
fi
cp scripts/airgap/deploy-both.sh        "$BUNDLE/scripts/airgap/"
cp scripts/airgap/parse-only-up.sh      "$BUNDLE/scripts/airgap/"
cp docs/airgap-deploy.md                "$BUNDLE/docs/" 2>/dev/null || true
# 현장 체크리스트 — 이번 번들이 이전과 달라진 점(포트·fitz·CNI)과 미검증 항목.
cp docs/airgap-onsite-checklist.md      "$BUNDLE/docs/" 2>/dev/null || true
# --parse-only: 채워진 `.env` 를 함께 넣을 수 있다(PARSE_ONLY_ENV=<경로>).
# ⚠️ 그 번들은 **실 비밀값을 담는다** — 매체 취급에 주의. 지정하지 않으면 템플릿만 들어간다.
ENV_EMBEDDED=0
if [ "$PARSE_ONLY" -eq 1 ] && [ -n "${PARSE_ONLY_ENV:-}" ] && [ -f "$PARSE_ONLY_ENV" ]; then
  install -m 600 "$PARSE_ONLY_ENV" "$BUNDLE/.env"
  ENV_EMBEDDED=1
  log "채워진 .env 를 번들에 포함(600) — 비밀값 매체로 취급할 것"
fi
cp docs/architecture-ports.md           "$BUNDLE/docs/" 2>/dev/null || true
# OCR 게이트웨이 주소 반영을 현장에서 검증하는 도구(목업 + 컨테이너 실효 env 확인).
mkdir -p "$BUNDLE/scripts/ocr-test"
cp scripts/ocr-test/verify-ocr-gw-url.sh  "$BUNDLE/scripts/ocr-test/" 2>/dev/null || true
cp scripts/ocr-test/mock_ocr_gateway.py   "$BUNDLE/scripts/ocr-test/" 2>/dev/null || true
# --parse-only 번들은 전체 스택 문서/템플릿만 들어 있으면 받는 쪽이 쓰지도 않는 키
# (edgequake·adaptive_chunk·임베딩·리랭커)를 채우려다 헤맨다. 파서 전용 세트를 같이 넣는다.
if [ "$PARSE_ONLY" -eq 1 ]; then
  cp .env.parse-only.example            "$BUNDLE/" 2>/dev/null || \
    { echo "✗ .env.parse-only.example 없음 — 파서 전용 번들에 필수"; exit 1; }
  cp docs/parse-only-guide.md           "$BUNDLE/docs/" 2>/dev/null || \
    { echo "✗ docs/parse-only-guide.md 없음 — 파서 전용 번들에 필수"; exit 1; }
fi
chmod +x "$BUNDLE"/scripts/airgap/*.sh

if [ "$PARSE_ONLY" -eq 1 ] && [ "$ENV_EMBEDDED" -eq 1 ]; then
  # ★ 채워진 `.env` 가 이미 들어 있다. 여기서 `cp .env.parse-only.example .env` 를 안내하면
  #   **채워진 값을 빈 템플릿으로 덮어써서** 현장에서 전부 다시 입력하게 된다(실측 함정).
  #   덮어쓰지 말고 현장값(OCR 게이트웨이 주소 등)만 고치라고 안내한다.
  NEXT_ENV='vi .env   # ★ 채워진 .env 가 이미 들어 있다. cp 로 덮어쓰지 마라. 현장값만 수정'
  NEXT_UP='./scripts/airgap/parse-only-up.sh'
elif [ "$PARSE_ONLY" -eq 1 ]; then
  NEXT_ENV='cp .env.parse-only.example .env && vi .env   # 파서 전용 키만 (docs/parse-only-guide.md)'
  NEXT_UP='./scripts/airgap/parse-only-up.sh'
else
  NEXT_ENV='cp .env.airgap.example .env && vi .env   # 【A. 온프렘 재설정 필수】 블록 채우기'
  NEXT_UP='./scripts/airgap/load-and-up.sh'
fi

log "단일 번들 tar 생성(무압축 — 내용물이 이미 압축된 이미지 blob 이다)"
OUT="$DIST/$BUNDLE_NAME"
tar cf "$OUT" -C "$BUNDLE" .
SHA="$(shasum -a 256 "$OUT" | awk '{print $1}')"
echo "$SHA  $(basename "$OUT")" > "${OUT}.sha256"

# 분할은 **기본으로 하지 않는다**(단일 파일이 다루기 쉽다).
# 전송매체에 파일 크기 한도가 있을 때만 `SPLIT_SIZE=2g` 처럼 명시해서 켠다.
# ★ 기본은 **분할하지 않는다** — 2GB 를 넘어도 단일 파일이다(사용자 방침 2026-08-10).
#   예전에 분할을 켜뒀다가 dist/ 에 옛 분할본이 남아 새 전체본과 이름이 겹쳤고,
#   그 파트를 재결합하면 **3일 전 번들이 배포되면서 .parts.sha256 검증까지 통과**했다.
#   분할을 켤 때는 반드시 옛 파트를 지운다(아래 rm -f 가 그 역할).
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

다음 단계:
  1) $OUT 를 서버로 전송 후 풀기:   mkdir kbp && tar xf $(basename "$OUT") -C kbp && cd kbp
  2) $NEXT_ENV
  3) $NEXT_UP
EOF
