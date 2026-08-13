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
# `.tar`(2026-08-13~, 무압축)·`.tar.gz`(그 이전) 둘 다 받는다.
IMAGES_GLOB="$BUNDLE_ROOT/images"/kbp-images-*.tar
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
tars=( $IMAGES_GLOB $IMAGES_GLOB.gz )
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

# ── 2.5) 컨테이너 이름 DNS 사전 점검 ──────────────────────────────────────────
# 이 스택은 **전부 컨테이너 이름으로** 서로를 찾는다(facade→parse-svc:19001,
# edgequake→postgres:5432, kb→facade:19000 …). 이름 해석이 안 되면 모든 서비스가
# "붙었는데 통신 불가" 상태가 되고, 증상이 서비스마다 제각각(타임아웃/커넥션거부)이라
# 원인 파악이 매우 오래 걸린다. 그래서 **띄우기 전에** 실제로 한 번 해석해 본다.
#
# 특히 RHEL 8 계열은 네트워크 백엔드가 CNI 인 경우가 있고, CNI 에서 이름 해석은 별도
# 패키지(`podman-plugins` 의 dnsname)에 의존한다 — 그게 없으면 여기서 잡힌다.
# netavark(RHEL 9 계열 기본)면 aardvark-dns 가 담당한다.
log "컨테이너 이름 DNS 사전 점검 (backend=$(podman info --format '{{.Host.NetworkBackend}}' 2>/dev/null || echo unknown))"
DNSNET="kbp-dnscheck-$$"
DNSPROBE_IMG="$(podman images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep -m1 -E '^(localhost/)?kbp-facade:' || true)"
if [ -z "$DNSPROBE_IMG" ]; then
  warn "kbp-facade 이미지를 못 찾아 DNS 사전점검을 건너뜁니다(이미지 로드 확인 필요)."
else
  podman network create "$DNSNET" >/dev/null 2>&1 || true
  podman rm -f kbp-dnsprobe-a >/dev/null 2>&1 || true
  podman run -d --name kbp-dnsprobe-a --network "$DNSNET" \
    --entrypoint sh "$DNSPROBE_IMG" -c 'sleep 60' >/dev/null 2>&1 || true
  if podman run --rm --network "$DNSNET" --entrypoint sh "$DNSPROBE_IMG" \
       -c 'getent hosts kbp-dnsprobe-a >/dev/null 2>&1 || python3 -c "import socket,sys; sys.exit(0 if socket.gethostbyname(\"kbp-dnsprobe-a\") else 1)"' \
       >/dev/null 2>&1; then
    echo "  ✓ 컨테이너 이름 DNS 정상"
  else
    podman rm -f kbp-dnsprobe-a >/dev/null 2>&1 || true
    podman network rm "$DNSNET" >/dev/null 2>&1 || true
    die "컨테이너 이름 DNS 해석 실패 — 이대로 기동하면 모든 서비스가 서로를 못 찾습니다.
     백엔드가 CNI 면 dnsname 플러그인이 필요합니다:  dnf install -y podman-plugins  (설치 후 재시도)
     netavark 면 aardvark-dns 를 확인하세요:        dnf install -y aardvark-dns
     현재 백엔드: $(podman info --format '{{.Host.NetworkBackend}}' 2>/dev/null || echo unknown)"
  fi
  podman rm -f kbp-dnsprobe-a >/dev/null 2>&1 || true
  podman network rm "$DNSNET" >/dev/null 2>&1 || true
fi

# ── 3) 기동 ───────────────────────────────────────────────────────────────────
log "compose up -d  (${COMPOSE[*]})"
"${COMPOSE[@]}" --env-file "$BUNDLE_ROOT/.env" up -d

# ── 4) health 폴링 ────────────────────────────────────────────────────────────
# **compose 가 정의한 healthcheck 결과를 읽는다**(`podman inspect`). 스크립트가 따로 URL 을
# 들고 있지 않으므로 두 곳이 어긋날 일이 없다.
#
# 예전에는 호스트 포트로 직접 폴링했는데 값이 compose 매핑과 전부 어긋나 있었다
# (P1 2026-08-10 기준: edgequake→3001, parse-svc→19001(내부=외부), facade→3000, webui→3002,
# webui 13000→3002, minio S3 9000 은 아예 미발행, doc_guard 는 facade 뒤로 숨어 노출 없음).
# 즉 이 단계가 사실상 전부 오탐이었다.
#
# 컨테이너 안에서 curl 을 쏘는 방식도 안 된다 — webui 이미지에는 curl 이 없다(자체
# healthcheck 가 wget 을 쓴다). 이미지마다 어떤 도구가 있는지를 스크립트가 알 필요가 없다.
SERVICES=(postgres minio edgequake doc_guard adaptive_chunk parse-svc facade
          edgequake_webui)

# compose 프로젝트명. 라벨로 컨테이너를 고를 때 다른 스택과 섞이지 않게 한정한다.
COMPOSE_PROJECT="${COMPOSE_PROJECT_NAME:-$(basename "$BUNDLE_ROOT")}"

ctr() {   # 서비스명 → 컨테이너명
  # 이름 규칙(`{project}-{svc}-1`)에 기대지 않는다. 같은 호스트에 다른 스택이 있으면
  # 정규식이 남의 컨테이너를 집는다(실측 2026-08-05: `postgres` 가 kb 스택의
  # `kb-postgres` 를, `minio` 가 `dify-1-7-minio-1` 을 집었다). 게다가 podman 이
  # 컨테이너를 재생성하면 `63c336ffd796_kbp-parse-svc-1` 처럼 접두사가 붙는다.
  # compose 가 붙이는 라벨이 유일하게 안정적인 식별자다.
  local n
  for lbl in com.docker.compose.service io.podman.compose.service; do
    n="$(podman ps -a --filter "label=$lbl=$1" \
                     --filter "label=com.docker.compose.project=$COMPOSE_PROJECT" \
                     --format '{{.Names}}' 2>/dev/null | head -1)"
    [ -n "$n" ] && { echo "$n"; return; }
    # 프로젝트 라벨을 안 붙이는 podman-compose 버전 대비(서비스 라벨만으로 재시도)
    n="$(podman ps -a --filter "label=$lbl=$1" --format '{{.Names}}' 2>/dev/null | head -1)"
    [ -n "$n" ] && { echo "$n"; return; }
  done
  # 최후 수단 — 라벨이 아예 없는 구버전.
  podman ps -a --format '{{.Names}}' | grep -m1 -E "(^|[_-])$1(_|-)?[0-9]*$" || true
}

health_of() {   # healthcheck 가 없으면 running 여부로 판정
  local name="$1" st
  st="$(podman inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{if .State.Running}}running{{else}}down{{end}}{{end}}' "$name" 2>/dev/null || echo down)"
  echo "$st"
}

log "health 폴링 (compose healthcheck, 서비스별 최대 ${HEALTH_TIMEOUT}s)"
FAIL=0
for svc in "${SERVICES[@]}"; do
  printf '  %-16s ' "$svc"
  deadline=$(( $(date +%s) + HEALTH_TIMEOUT )); st=""; name=""
  while :; do
    name="$(ctr "$svc")"
    if [ -n "$name" ]; then
      st="$(health_of "$name")"
      case "$st" in healthy|running) break;; esac
    fi
    [ "$(date +%s)" -ge "$deadline" ] && break
    sleep 3
  done
  case "$st" in
    healthy) echo "✓ healthy" ;;
    running) echo "✓ running (healthcheck 없음)" ;;
    *)       echo "✗ ${st:-컨테이너 없음}"; FAIL=1 ;;
  esac
done

# ── 4b) facade-worker 등록 확인 ───────────────────────────────────────────────
# worker 는 HTTP 를 열지 않아 위 폴링으로 못 본다. 그런데 **worker 가 없으면 /parse·
# /ingest 접수가 503("no live facade-worker")** 이라, 이걸 안 보면 "전부 healthy" 인데
# 적재가 통째로 안 되는 상태로 배포가 끝난다.
#
# 게이트가 켜져 있으면 헤더가 필요하다(KBP_FACADE_KEY 는 verify-bundle.sh 가 이미 강제).
log "facade-worker 등록 확인 (/jobs/workers)"
FKEY="$(grep -E '^KBP_FACADE_KEY=' "$BUNDLE_ROOT/.env" | head -1 | cut -d= -f2- | tr -d '[:space:]')"
FACADE_CTR="$(ctr facade)"
# 헤더는 **배열**로 넘긴다. `${FKEY:+-H "X-Facade-Key: $FKEY"}` 는 단어 분리로
# `-H` 와 `X-Facade-Key: <키>` 가 한 인자로 붙어버려, 키가 설정된 배포에서 이 검사가
# 항상 실패한다(= 정상인데 배포 실패로 보고).
HDR=(); [ -n "$FKEY" ] && HDR=(-H "X-Facade-Key: $FKEY")
worker_online=""
if [ -n "$FACADE_CTR" ]; then
  deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
  while :; do
    worker_online="$(podman exec "$FACADE_CTR" curl -fsS "${HDR[@]}" \
        http://localhost:19000/jobs/workers 2>/dev/null || true)"
    # 본문 조건: online 인 worker 가 1개 이상.
    case "$worker_online" in *'"online":true'*|*'"online": true'*) break;; esac
    [ "$(date +%s)" -ge "$deadline" ] && break
    sleep 3
  done
fi
case "$worker_online" in
  *'"online":true'*|*'"online": true'*)
    echo "  facade-worker     ✓ online" ;;
  *)
    echo "  facade-worker     ✗ 등록 안 됨 — /parse·/ingest 접수가 503 이 됩니다."
    echo "                    원인: podman logs \$(podman ps -a --format '{{.Names}}' | grep -m1 facade-worker)"
    FAIL=1 ;;
esac

# ── 5) MinIO 버킷 생성 (멱등, 존재 검증) ──────────────────────────────────────
log "MinIO 버킷 생성"
# ★ grep 매치 0건(exit 1)이면 set -e 아래서 파이프 전체가 실패해 스크립트가 죽는다.
#   `.env.airgap.example` 에 MINIO_BUCKET 키가 우연히 있어서 지금까지 안 터졌을 뿐이라
#   여기서 `|| true` 로 방어한다(v3→v4, 실제 bash 재현으로 확인).
BUCKET="$(grep -E '^MINIO_BUCKET=' "$BUNDLE_ROOT/.env" | cut -d= -f2 | tr -d '[:space:]')" || true
BUCKET="${BUCKET:-document-parser}"
# 같은 함정 — `grep -i minio` 는 다른 스택의 minio 를 집어 **남의 버킷**에 만든다.
MC_CTR="$(ctr minio)"
if [ -z "$MC_CTR" ]; then
  warn "minio 컨테이너를 찾지 못함 — 버킷 생성 건너뜀"
  FAIL=1                                    # ★ die 아님 — 6번 요약까지 도달시킨다
else
  podman exec "$MC_CTR" sh -c \
    'mc alias set local http://localhost:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null 2>&1' \
    || { warn "mc alias 설정 실패 — MinIO 컨테이너 상태 확인"; FAIL=1; }

  # mc mb -p 는 이미 --ignore-existing 이라 존재하는 버킷에서 실패하지 않는다.
  # || true 는 그 외의(권한·용량 등) 진짜 오류에 대한 방어적 안전망일 뿐이다.
  podman exec "$MC_CTR" mc mb -p "local/$BUCKET" >/dev/null 2>&1 || true

  # mc mb 의 성패가 아니라 mc stat 의 실제 존재 여부로 판정한다(전체 버킷 목록을
  # 보는 `mc ls local/` 는 항상 비어있지 않아 실패를 삼켰다).
  if podman exec "$MC_CTR" mc stat "local/$BUCKET" >/dev/null 2>&1; then
    echo "  ✓ 버킷 '$BUCKET' 존재 확인"
  else
    warn "버킷 '$BUCKET' 생성 실패 — mc stat 로 존재 확인 안 됨. MinIO 자격증명·용량 확인"
    FAIL=1                                  # ★ die 아님 — 아래 근거
  fi
fi

# ── 6) 요약 ───────────────────────────────────────────────────────────────────
log "상태"
"${COMPOSE[@]}" ps || true
if [ "$FAIL" -eq 0 ]; then
  # 안내 주소는 **호스트 매핑**이다(위 health 는 컨테이너 내부 포트). 어긋나면 배포자가
  # 접속 못 하는 주소를 받는다. 아래 값은 docker-compose.airgap.yml 의 **published** 포트다
  # (P1 2026-08-10: parse-svc 는 18081→19001. 18081 은 사내 OCR 게이트웨이 것이다).
  printf '\n\033[1;32m✅ 전 서비스 healthy\033[0m\n'
  printf '   facade   http://<서버IP>:3000\n'
  printf '   webui    http://<서버IP>:3002    edgequake API  http://<서버IP>:3001\n'
  printf '   minio 콘솔 http://<서버IP>:3003  parse-svc      http://<서버IP>:19001\n'
  printf '   (doc_guard 는 외부 노출 없음 — facade /gate/* 로만 접근)\n'
else
  die "일부 서비스 unhealthy. 로그 확인: ${COMPOSE[*]} logs <service>  (자세한 진단은 docs/airgap-deploy.md §트러블슈팅)"
fi
