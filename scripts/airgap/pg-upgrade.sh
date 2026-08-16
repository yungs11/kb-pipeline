#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# pg-upgrade.sh — 폐쇄망 번들 버전을 올릴 때 postgres 데이터를 지키는 **정공법 한 번에**.
#
# pg-backup.sh · parse-only-up.sh · pg-restore.sh 를 순서대로 엮는다:
#   1) [pg-backup.sh]    옛 이미지로 옛 볼륨을 그대로 띄워 pg_dump(-Fc) 로 백업
#   2) [이 스크립트]      옛 postgres 컨테이너 정리 + 옛 볼륨 삭제 ── **여기부터 되돌릴 수 없다**
#   3) [parse-only-up.sh] 새 번들 이미지 로드 + fresh 볼륨으로 postgres 재초기화 + 전체 기동
#   4) [pg-restore.sh]   백업을 새 인스턴스에 복원
#
# **볼륨 삭제는 기본적으로 확인을 받는다.** 스크립트로 자동화됐다고 안전해진 게
# 아니다 — 백업이 실제로 유효한지 사람이 한 번은 봐야 한다. 무인 실행이 필요하면
# `--yes` 를 명시한다(그때도 백업 자체는 항상 먼저 실행된다 — 건너뛸 수 없다).
#
# 사용:
#   ./scripts/airgap/pg-upgrade.sh              # 확인 프롬프트 있음
#   ./scripts/airgap/pg-upgrade.sh --yes         # 무인 실행(현장 자동화용)
#   COMPOSE_PROJECT_NAME=kbp KBP_ENGINE=docker ./scripts/airgap/pg-upgrade.sh
#
# 전제: 새 번들 이미지 tar 가 이 번들 디렉터리의 `images/` 밑에 이미 있어야 한다
# (즉 **이 스크립트는 새 번들을 압축 해제한 디렉터리 안에서 실행한다** — 그래야
# 3단계의 parse-only-up.sh 가 새 이미지를 로드한다). 옛 볼륨은 엔진(docker/podman)
# 전역이라 어느 디렉터리에서 만들어졌든 상관없이 이름으로 찾는다.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE/.." 2>/dev/null || cd "$HERE"
BUNDLE_ROOT="$(pwd)"

log()  { printf '\n\033[1;35m━━━ %s ━━━\033[0m\n' "$*"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m✗ %s\033[0m\n' "$*"; exit 1; }

YES=0
OUT_DIR="$BUNDLE_ROOT/backups"
for a in "$@"; do
  case "$a" in
    --yes|-y) YES=1 ;;
  esac
done
args=("$@")
for i in "${!args[@]}"; do
  if [ "${args[$i]}" = "--out" ] && [ $((i+1)) -lt ${#args[@]} ]; then
    OUT_DIR="${args[$((i+1))]}"
  fi
done

ENGINE="${KBP_ENGINE:-}"
if [ -z "$ENGINE" ]; then
  command -v docker >/dev/null 2>&1 && ENGINE=docker
  [ -z "$ENGINE" ] && command -v podman >/dev/null 2>&1 && ENGINE=podman
fi
[ -n "$ENGINE" ] || die "docker/podman 둘 다 없습니다."
PROJECT="${COMPOSE_PROJECT_NAME:-kbp}"
VOL="${PROJECT}_eq_pg_data"

[ -f "$BUNDLE_ROOT/scripts/airgap/pg-backup.sh" ]  || die "pg-backup.sh 가 없습니다 — 새 번들 디렉터리에서 실행하세요."
[ -f "$BUNDLE_ROOT/scripts/airgap/pg-restore.sh" ] || die "pg-restore.sh 가 없습니다 — 새 번들 디렉터리에서 실행하세요."
[ -f "$BUNDLE_ROOT/scripts/airgap/parse-only-up.sh" ] || die "parse-only-up.sh 가 없습니다 — 새 번들 디렉터리에서 실행하세요."

# ── 옛 볼륨이 없으면 백업/복원 자체가 무의미 — 그냥 기동만 하면 된다 ──────────
if ! "$ENGINE" volume inspect "$VOL" >/dev/null 2>&1; then
  log "옛 볼륨 없음 — 신규 배포로 판단"
  echo "  백업/복원이 필요 없습니다. 그냥 실행하세요:"
  echo "    ./scripts/airgap/parse-only-up.sh"
  exit 0
fi

# ── 1) 백업 ───────────────────────────────────────────────────────────────────
log "1/4 — 옛 데이터 백업"
KBP_ENGINE="$ENGINE" COMPOSE_PROJECT_NAME="$PROJECT" \
  bash "$BUNDLE_ROOT/scripts/airgap/pg-backup.sh" --out "$OUT_DIR"

DUMP_FILE="$OUT_DIR/latest.dump"
[ -s "$DUMP_FILE" ] || die "백업 파일이 안 보입니다($DUMP_FILE) — 위 로그를 확인하세요."
DUMP_BYTES="$(wc -c < "$DUMP_FILE" | tr -d '[:space:]')"

# ── 2) 확인 + 옛 컨테이너/볼륨 정리 (되돌릴 수 없는 지점) ────────────────────
log "2/4 — 옛 postgres 정리(볼륨 삭제 — 되돌릴 수 없습니다)"
echo "  백업 파일: $DUMP_FILE ($DUMP_BYTES bytes)"
echo "  지울 볼륨: $VOL"
if [ "$YES" -ne 1 ]; then
  printf "  계속할까요? 백업을 반드시 확인한 뒤에 답하세요. [y/N] "
  read -r ans
  case "$ans" in
    y|Y|yes|YES) ;;
    *) die "취소했습니다. 옛 볼륨은 그대로입니다. 준비되면 다시 실행하거나 --yes 를 쓰세요." ;;
  esac
fi

find_ctr() {
  local svc="$1" n
  for lbl in com.docker.compose.service io.podman.compose.service; do
    n="$("$ENGINE" ps -a --filter "label=$lbl=$svc" \
                    --filter "label=com.docker.compose.project=$PROJECT" \
                    --format '{{.Names}}' 2>/dev/null | head -1)"
    [ -n "$n" ] && { echo "$n"; return; }
  done
}
OLD_PG_CTR="$(find_ctr postgres)"
if [ -n "$OLD_PG_CTR" ]; then
  echo "  옛 컨테이너 정리: $OLD_PG_CTR"
  "$ENGINE" rm -f "$OLD_PG_CTR" >/dev/null
fi
echo "  볼륨 삭제: $VOL"
"$ENGINE" volume rm "$VOL" >/dev/null || die "볼륨 삭제 실패 — 다른 컨테이너가 아직 물고 있을 수 있습니다
   ($ENGINE ps -a --filter volume=$VOL 로 확인하세요)."

# ── 3) 새 번들 기동(fresh 볼륨으로 postgres 재초기화) ─────────────────────────
log "3/4 — 새 번들 기동"
COMPOSE_PROJECT_NAME="$PROJECT" KBP_ENGINE="$ENGINE" \
  bash "$BUNDLE_ROOT/scripts/airgap/parse-only-up.sh" \
  || die "새 번들 기동 실패 — 위 로그를 확인하세요. 백업은 $DUMP_FILE 에 안전하게 남아 있습니다."

# ── 4) 복원 ───────────────────────────────────────────────────────────────────
log "4/4 — 복원"
COMPOSE_PROJECT_NAME="$PROJECT" KBP_ENGINE="$ENGINE" \
  bash "$BUNDLE_ROOT/scripts/airgap/pg-restore.sh" "$DUMP_FILE" \
  || die "복원 실패 — 위 로그를 확인하세요. 백업 파일은 그대로 있습니다: $DUMP_FILE"

echo
echo "✅ 업그레이드 완료"
echo "  백업 파일(유일한 옛 데이터 사본입니다 — 안전한 곳에 옮겨두는 것을 권합니다):"
echo "    $DUMP_FILE"
echo "  애플리케이션 레벨로 정상 동작을 확인하세요(예: 실제 /parse 잡 제출)."
