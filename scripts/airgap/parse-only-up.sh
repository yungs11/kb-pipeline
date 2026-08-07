#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# parse-only-up.sh — [Phase B / 오프라인] **파싱 배치 전용** 축소 구성 기동
#
# 청킹·적재·검색 없이 **대량 파싱만** 돌리는 배포. 전체 스택(9개) 대신 5개만 띄운다:
#
#   parse-svc      실제 파싱 엔진
#   facade         잡 접수 API (POST /parse → job_id)
#   facade-worker  ★ 잡을 실제로 실행한다(facade 와 같은 이미지, 명령만 다름).
#                  이게 없으면 healthz 는 전부 통과하는데 /parse 접수가 503
#                  ("no live facade-worker") 로 거절돼 **한 건도 처리되지 않는다.**
#   postgres       잡 큐(kbp.jobs). 기동 시 스키마 자동 생성 — 빈 DB 로 충분하다.
#   minio          ★ 잡 staging + 큰 payload/result 오프로딩.
#                  **선택이 아니다** — 없으면 잡 접수 자체가 전면 실패한다
#                  (실측: 버킷 미생성 시 /parse 가 NoSuchBucket 으로 500).
#
# 빠지는 것: edgequake / adaptive_chunk / doc_guard / edgequake_webui
#            (각각 적재·검색 / 청킹 / 엑셀게이트 / 그래프UI 용).
#
# **compose 파일을 따로 두지 않는다.** 같은 docker-compose.airgap.yml 에서 서비스
# 부분집합만 `--no-deps` 로 띄운다 — 별도 파일을 만들면 env·이미지 태그가 조용히
# 어긋나기 때문이다(드리프트 원천 차단). facade 의 depends_on 에 edgequake·
# adaptive_chunk 가 있어 --no-deps 가 반드시 필요하다.
#
# 사용:  ./scripts/airgap/parse-only-up.sh
# 전체 스택이 필요하면 load-and-up.sh 를 쓴다.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE/.." 2>/dev/null || cd "$HERE"
BUNDLE_ROOT="$(pwd)"
COMPOSE_FILE="$BUNDLE_ROOT/docker-compose.airgap.yml"
IMAGES_GLOB="$BUNDLE_ROOT/images"/kbp-images-*.tar.gz
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-300}"

#: 파싱 배치에 필요한 서비스만. 순서는 의존 순(--no-deps 라 우리가 직접 지킨다).
SERVICES=(postgres minio parse-svc facade facade-worker)

log()  { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m✗ %s\033[0m\n' "$*"; exit 1; }

command -v podman >/dev/null || die "podman 이 없습니다."
if command -v podman-compose >/dev/null; then
  COMPOSE=(podman-compose -f "$COMPOSE_FILE")
elif podman compose version >/dev/null 2>&1; then
  COMPOSE=(podman compose -f "$COMPOSE_FILE")
else
  die "podman-compose(또는 'podman compose') 가 없습니다: dnf install podman-compose"
fi

# ── 1) 이미지 로드 ────────────────────────────────────────────────────────────
log "podman load — 이미지 로드"
shopt -s nullglob
tars=( $IMAGES_GLOB )
[ ${#tars[@]} -gt 0 ] || die "이미지 tar 를 찾지 못함: $IMAGES_GLOB"
for t in "${tars[@]}"; do echo "  load $t"; podman load -i "$t"; done

# ── 2) .env 확인 ──────────────────────────────────────────────────────────────
[ -f "$BUNDLE_ROOT/.env" ] || {
  cp "$BUNDLE_ROOT/.env.airgap.example" "$BUNDLE_ROOT/.env"
  die ".env 를 생성했습니다. 파싱에 필요한 값을 채운 뒤 다시 실행하세요:
     - KBP_FILECONVERT_URL/TOKEN  (docx·hwp·ppt·html 파싱에 필수)
     - MODEL_API_URL/KEY          (이미지·스캔 VL-OCR)
     - KBP_OPENAI_*               (모달 보강 LLM)
     - MINIO_ACCESS_KEY/SECRET_KEY, POSTGRES_PASSWORD, KBP_FACADE_KEY"
}
# 축소 구성에서는 임베딩·리랭커·edgequake 키가 필요 없으므로 --env 전체 검증은 쓰지 않는다.
for k in MINIO_ACCESS_KEY MINIO_SECRET_KEY KBP_FACADE_KEY; do
  v="$(grep -E "^${k}=" "$BUNDLE_ROOT/.env" | head -1 | cut -d= -f2- | tr -d '[:space:]')" || true
  [ -n "${v:-}" ] || warn "$k 가 비어 있습니다(파싱 배치에도 권장: 특히 KBP_FACADE_KEY 는 무인증 노출 방지)."
done

# ── 3) 기동 (부분집합 + --no-deps) ────────────────────────────────────────────
log "compose up -d --no-deps  [${SERVICES[*]}]"
"${COMPOSE[@]}" --env-file "$BUNDLE_ROOT/.env" up -d --no-deps "${SERVICES[@]}"

# ── 4) health 폴링 ────────────────────────────────────────────────────────────
log "health 폴링 (상한 ${HEALTH_TIMEOUT}s)"
deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
for svc in postgres minio parse-svc facade; do
  printf '  %-12s ' "$svc"
  while :; do
    cid="$(podman ps -a --filter "label=com.docker.compose.service=${svc}" --format '{{.ID}}' | head -1)"
    st="$(podman inspect "$cid" --format '{{.State.Health.Status}}' 2>/dev/null || echo unknown)"
    [ "$st" = "healthy" ] && { echo "✓ healthy"; break; }
    [ "$(date +%s)" -ge "$deadline" ] && { echo "✗ TIMEOUT(last=$st)"; die "기동 실패 — podman logs 로 확인"; }
    sleep 3
  done
done

# ── 5) MinIO 버킷 생성 (필수 — 없으면 잡 접수가 전면 실패) ────────────────────
log "MinIO 버킷 생성/확인"
BUCKET="$(grep -E '^MINIO_BUCKET=' "$BUNDLE_ROOT/.env" | cut -d= -f2 | tr -d '[:space:]')" || true
BUCKET="${BUCKET:-document-parser}"
MC_CTR="$(podman ps -a --filter 'label=com.docker.compose.service=minio' --format '{{.Names}}' | head -1)"
FAIL=0
if [ -z "$MC_CTR" ]; then
  warn "minio 컨테이너를 찾지 못함 — 버킷 생성 생략"; FAIL=1
else
  podman exec "$MC_CTR" sh -c \
    'mc alias set local http://localhost:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null 2>&1' \
    || { warn "mc alias 설정 실패"; FAIL=1; }
  podman exec "$MC_CTR" mc mb -p "local/$BUCKET" >/dev/null 2>&1 || true
  if podman exec "$MC_CTR" mc stat "local/$BUCKET" >/dev/null 2>&1; then
    echo "  ✓ 버킷 '$BUCKET' 확인"
  else
    warn "버킷 '$BUCKET' 생성 실패 — 이 상태로는 /parse 접수가 NoSuchBucket 으로 500 이 된다."; FAIL=1
  fi
fi

# ── 6) facade-worker 등록 확인 (가장 흔한 함정) ───────────────────────────────
log "facade-worker 등록 확인"
KEY="$(grep -E '^KBP_FACADE_KEY=' "$BUNDLE_ROOT/.env" | cut -d= -f2- | tr -d '[:space:]')" || true
if out="$(curl -fsS -H "X-Facade-Key: ${KEY}" http://localhost:3000/jobs/workers 2>/dev/null)"; then
  echo "  $out"
  case "$out" in
    *'"online":true'*|*'"online": true'*) echo "  ✓ worker 온라인" ;;
    *) warn "worker 가 온라인이 아니다 — /parse 접수가 503 으로 거절된다(podman logs facade-worker)"; FAIL=1 ;;
  esac
else
  warn "/jobs/workers 조회 실패(KBP_FACADE_KEY 불일치 또는 facade 미기동)"; FAIL=1
fi

# ── 7) 요약 ───────────────────────────────────────────────────────────────────
log "요약"
"${COMPOSE[@]}" ps 2>/dev/null | tail -n +1 || true
cat <<EOF

파싱 배치 전용 구성(5개)으로 기동했습니다.
  잡 제출:   curl -sS -H "X-Facade-Key: \$KBP_FACADE_KEY" -F file=@문서.pdf -F filename=문서.pdf \\
               http://localhost:3000/parse
  ※ 응답은 파싱 결과(동기 대기) 또는 잡 참조다 — facade 계약은 docs/facade-api.md 참고.
  parse-svc 직접 호출(잡 큐 없이 동기 파싱)이 필요하면 http://localhost:18081/parse 도 열려 있다.

빠진 서비스: edgequake / adaptive_chunk / doc_guard / edgequake_webui
  → 청킹(/chunk)·적재(/insert)·검색(/search)·엑셀게이트(/gate)는 이 구성에서 동작하지 않는다.
    필요해지면 load-and-up.sh 로 전체 스택을 띄운다.
EOF
[ "$FAIL" -eq 0 ] || die "위 경고 항목을 해결한 뒤 다시 실행하세요(스크립트는 멱등)."
echo "✅ 완료"
