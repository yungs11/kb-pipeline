#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# deploy-both.sh — [Phase B / 오프라인] kbp + kb 두 번들을 순서대로 기동
#
# 두 번들(kbp-airgap-bundle, kb-airgap-bundle)을 각각 압축 해제한 디렉터리가
# 같은 서버에 있다는 전제로, 올바른 순서(kbp 먼저 → kb 나중)와 두 .env 사이의
# 필수 일치값(KBP_FACADE_KEY, KBP_NETWORK)을 자동으로 맞춰 기동한다.
#
# 이 스크립트가 하는 일:
#   1) kbp 번들 디렉터리(이 스크립트가 속한 번들의 루트)에서 load-and-up.sh 실행
#   2) kbp 가 만든 실제 podman 네트워크 이름 확인(compose project name 기반)
#   3) kb 번들 .env 에 KBP_NETWORK 를 그 이름으로, KBP_FACADE_KEY 를 kbp 와
#      동일 값으로 맞춰 씀(불일치 시 401/403·네트워크 없음 오류로 이어지는
#      실측된 실패 패턴 — 수동 배포에서 가장 자주 놓치는 지점)
#   4) kb 번들 디렉터리에서 load-and-up.sh 실행
#   5) 두 스택 헬스체크 요약
#
# 사용:
#   ./scripts/airgap/deploy-both.sh /path/to/kb-bundle-dir
#
# 전제: 이 스크립트가 속한 kbp 번들(현재 디렉터리 트리)이 이미 압축 해제돼 있고,
#       인자로 준 kb 번들 디렉터리도 이미 압축 해제(tar xzf)돼 있어야 한다.
#       두 디렉터리 모두 .env.airgap.example → .env 준비는 각자 알아서 한다
#       (이 스크립트는 KBP_NETWORK/KBP_FACADE_KEY 두 값만 자동으로 맞춘다).
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE/.." 2>/dev/null || cd "$HERE"
KBP_DIR="$(pwd)"
KB_DIR="${1:?사용법: deploy-both.sh /path/to/kb-bundle-dir}"
KB_DIR="$(cd "$KB_DIR" && pwd)"

log()  { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m✗ %s\033[0m\n' "$*"; exit 1; }

[ -f "$KBP_DIR/docker-compose.airgap.yml" ] || die "kbp 번들 루트가 아님: $KBP_DIR (docker-compose.airgap.yml 없음)"
[ -f "$KB_DIR/docker-compose.airgap.yml" ]  || die "kb 번들 루트가 아님: $KB_DIR (docker-compose.airgap.yml 없음)"
[ -f "$KBP_DIR/.env" ] || die "kbp/.env 가 없습니다 — 먼저 cp .env.airgap.example .env 후 【A】 블록을 채우세요."
[ -f "$KB_DIR/.env" ]  || die "kb/.env 가 없습니다 — 먼저 cp .env.airgap.example .env 후 【A】 블록을 채우세요."

# ── 1) kbp 먼저 ────────────────────────────────────────────────────────────────
log "1/5 kbp 스택 기동 (${KBP_DIR})"
( cd "$KBP_DIR" && ./scripts/airgap/load-and-up.sh ) \
  || die "kbp 기동 실패 — 위 로그 확인 후 재실행(멱등)"

# ── 2) kbp 가 만든 실제 네트워크 이름 확인 ──────────────────────────────────────
# compose `name: kbp` 이면 실제 네트워크는 `kbp_kbp` 가 기본이다. project name 을
# 바꿔 기동했을 수도 있으니 실제로 존재하는 걸 조회해서 쓴다(추측하지 않는다).
log "2/5 kbp 네트워크 확인"
KBP_NET="$(podman network ls --format '{{.Name}}' 2>/dev/null | grep -m1 '_kbp$' || true)"
[ -n "$KBP_NET" ] || die "kbp 네트워크를 못 찾음(podman network ls 에 *_kbp 없음) — kbp 가 정말 떴는지 확인"
echo "  → $KBP_NET"

# ── 3) kb/.env 에 KBP_NETWORK·KBP_FACADE_KEY 동기화 ────────────────────────────
log "3/5 kb/.env ↔ kbp/.env 값 동기화 (KBP_NETWORK, KBP_FACADE_KEY)"
KBP_FACADE_KEY_VAL="$(grep -E '^KBP_FACADE_KEY=' "$KBP_DIR/.env" | head -1 | cut -d= -f2-)" || true
if [ -z "${KBP_FACADE_KEY_VAL:-}" ]; then
  warn "kbp/.env 의 KBP_FACADE_KEY 가 비어있음 — 게이트 꺼진 상태(dev 동작). 운영이면 채우는 걸 권장."
fi

sync_env_kv() {
  local file="$1" key="$2" val="$3"
  if grep -qE "^${key}=" "$file"; then
    # macOS/BSD sed 와 GNU sed 양쪽 호환(백업 확장자 빈 문자열).
    sed -i.bak "s#^${key}=.*#${key}=${val}#" "$file" && rm -f "${file}.bak"
  else
    printf '%s=%s\n' "$key" "$val" >> "$file"
  fi
}
sync_env_kv "$KB_DIR/.env" "KBP_NETWORK" "$KBP_NET"
[ -n "${KBP_FACADE_KEY_VAL:-}" ] && sync_env_kv "$KB_DIR/.env" "KBP_FACADE_KEY" "$KBP_FACADE_KEY_VAL"
echo "  kb/.env: KBP_NETWORK=$KBP_NET"
[ -n "${KBP_FACADE_KEY_VAL:-}" ] && echo "  kb/.env: KBP_FACADE_KEY = (kbp 와 동일 값으로 맞춤)"

# ── 4) kb 기동 ──────────────────────────────────────────────────────────────────
log "4/5 kb 스택 기동 (${KB_DIR})"
( cd "$KB_DIR" && ./scripts/airgap/load-and-up.sh ) \
  || die "kb 기동 실패 — 위 로그 확인 후 재실행(멱등). 자주 나는 원인은 docs/airgap-deploy.md §5 트러블슈팅 참고"

# ── 5) 요약 ──────────────────────────────────────────────────────────────────────
log "5/5 완료 — 헬스체크 요약"
FRONT_PORT="$(grep -E '^FRONTEND_PORT=' "$KB_DIR/.env" | head -1 | cut -d= -f2-)"
FRONT_PORT="${FRONT_PORT:-18080}"
echo "  facade   : curl -fsS http://localhost:3000/healthz"
echo "  edgequake: curl -fsS http://localhost:3001/health"
echo "  parse-svc: curl -fsS http://localhost:19001/healthz"   # 18081 은 OCR 게이트웨이
echo "  kb api   : curl -fsS http://localhost:8080/readyz"
echo "  웹앱     : http://<서버IP>:${FRONT_PORT}"
echo
echo "  둘 중 하나라도 이상하면 각 리포의 docs/airgap-deploy.md §5(트러블슈팅)를 먼저 본다."
