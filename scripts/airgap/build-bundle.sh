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

# ─────────────────────────────────────────────────────────────────────────────
# 소스 출처 가드 — 각 빌드 컨텍스트가 **선언된 브랜치**인지 확인한다.
#
# `docker build` 는 브랜치가 아니라 **워킹트리**를 굽는다. 그래서 sibling 레포가
# 다른 브랜치에 체크아웃돼 있거나 미커밋 변경이 있으면, 번들에 **무엇이 실렸는지
# 아무 데도 안 남은 채** 조용히 다른 코드가 나간다. verify-bundle.sh 는 배포 시점
# 가드라 이걸 못 잡는다 — 브랜치는 빌드 시점에만 알 수 있다.
#
# 실측 계기(2026-08-11): doc_guard 가 `feat/conflicting-code-mapping` 에 체크아웃된
# 채로 8/10 번들이 나갔다. 이번엔 그 브랜치가 main 과 diff 0 이라 결과가 같았지만,
# 갈라져 있었으면 폐쇄망에만 다른 코드가 들어가고 아무도 몰랐다.
#
# EXPECT_BRANCH: "컨텍스트경로|기대브랜치". 컨텍스트가 여기 없으면 검사 생략.
#   feature 브랜치를 쓰는 컴포넌트는 **그 브랜치를 여기 적어 고정**한다 — 전부 main 을
#   요구하면 매번 override 를 쓰게 되고 그러면 가드가 죽는다. 브랜치를 바꿀 때
#   이 표도 같이 고치는 것이 "무엇으로 배포하는가"의 선언이다.
EXPECT_BRANCH=(
  ".|main"                                              # kb-pipeline (parse-svc/facade/edgequake 이미지) — 2026-08-14 feat/fileconvert-api 머지 후 main 배포
  "edgequake|edgequake-main"                            # 서브모듈 (webui) — 포크의 main 은 upstream 이름 그대로 `edgequake-main`
  "../99.projects/adaptive_chunk|main"                  # 2026-08-14 feat/adaptive-chunk-metric-weighting 머지 완료(main..feat = 0)
  "../99.projects/shinhan_trust/doc_guard|main"         # 2026-08-11 이후 main 배포 (룰 동기화 커밋 40fde0f)
)

# 빌드에 실제로 쓰인 출처. 번들 안 BUILD-PROVENANCE.txt 로 남긴다.
PROVENANCE=()

log "소스 출처 가드 — 브랜치/커밋 확인"
GUARD_FAIL=0
for spec in "${BUILDS[@]}"; do
  IFS='|' read -r _img _dfile ctx <<<"$spec"
  # 같은 컨텍스트가 여러 이미지에 쓰인다(., edgequake) — 한 번만 검사한다.
  case " ${PROVENANCE[*]-} " in *" ctx=$ctx "*) continue ;; esac

  if ! branch="$(git -C "$ctx" rev-parse --abbrev-ref HEAD 2>/dev/null)"; then
    echo "  · $ctx → git 레포 아님 (검사 생략)"
    continue
  fi
  commit="$(git -C "$ctx" rev-parse --short HEAD)"
  # 추적 파일만 본다 — .DS_Store·로그 같은 untracked 는 이미지에 안 들어가는 경우가 많고
  # 여기서 막으면 실효 없이 시끄럽기만 하다.
  dirty=""
  [ -n "$(git -C "$ctx" status --porcelain --untracked-files=no)" ] && dirty=" +uncommitted"
  PROVENANCE+=("ctx=$ctx branch=$branch commit=$commit${dirty}")

  want=""
  for e in "${EXPECT_BRANCH[@]}"; do
    [ "${e%%|*}" = "$ctx" ] && { want="${e##*|}"; break; }
  done

  if [ -z "$want" ]; then
    echo "  · $ctx → $branch@$commit${dirty}  (기대 브랜치 미선언 — EXPECT_BRANCH 에 추가하라)"
  elif [ "$branch" != "$want" ]; then
    echo "  ✗ $ctx → $branch@$commit${dirty}  (기대: $want)"
    GUARD_FAIL=1
  elif [ -n "$dirty" ]; then
    # 차단하지 않는다 — 개발기는 상시 dirty 라 여기서 막으면 매번 override 를 쓰게 되고
    # 그러면 브랜치 가드까지 함께 죽는다. 대신 BUILD-PROVENANCE.txt 에 `+uncommitted` 로 남는다.
    echo "  ! $ctx → $branch@$commit${dirty}  (미커밋 변경 — 이 커밋만으로는 재현 불가. 기록됨)"
  else
    echo "  ✓ $ctx → $branch@$commit"
  fi
done

if [ "$GUARD_FAIL" -eq 1 ] && [ "${ALLOW_SOURCE_DRIFT:-0}" != "1" ]; then
  cat >&2 <<'EOF'

  ✗ 소스 출처 가드 실패 — 위 ✗(브랜치 불일치)를 정리한 뒤 다시 실행하라.
      · 해당 레포에서 `git checkout <기대브랜치>`
      · 의도한 변경이면 이 스크립트의 EXPECT_BRANCH 표를 고쳐라 (그게 배포 선언이다)

  의도적으로 무시하려면:  ALLOW_SOURCE_DRIFT=1 scripts/airgap/build-bundle.sh ...
  (무시해도 실제 출처는 번들의 BUILD-PROVENANCE.txt 에 그대로 기록된다.)
EOF
  exit 1
fi

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

# 출처 기록 — 이 번들이 **어느 레포의 어느 커밋**으로 구워졌는지 남긴다. 현장에서
# "지금 도는 게 어느 버전이냐"를 이미지 안을 뒤지지 않고 답할 수 있게 하는 유일한 근거다.
{
  printf 'kb-pipeline airgap bundle\n'
  printf 'platform=%s tag=%s parse_only=%s\n' "$PLATFORM" "$TAG" "$PARSE_ONLY"
  printf '\n[sources]\n'
  # set -u 에서 빈 배열 전개가 죽는다(bash 3.2) — 방어.
  printf '%s\n' "${PROVENANCE[@]-(none)}"
} > "$BUNDLE/BUILD-PROVENANCE.txt"
docker save "${ALL_IMAGES[@]}" -o "$IMAGES_TAR"
gzip -f "$IMAGES_TAR"
IMAGES_TAR="${IMAGES_TAR}.gz"

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

log "단일 번들 tar.gz 생성"
OUT="$DIST/$BUNDLE_NAME"
tar czf "$OUT" -C "$BUNDLE" .
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
  1) $OUT 를 서버로 전송 후 풀기:   mkdir kbp && tar xzf $(basename "$OUT") -C kbp && cd kbp
  2) $NEXT_ENV
  3) $NEXT_UP
EOF
