#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# pg-restore.sh — pg-backup.sh 가 만든 덤프를 **새 번들의 fresh postgres** 에 복원한다.
#
# 전제: 새 번들을 이미 로드·기동해 postgres 가 **healthy** 상태여야 한다
# (parse-only-up.sh 또는 load-and-up.sh 로 먼저 띄운다). 이 스크립트는 볼륨을
# 만들지 않는다 — compose 가 이미 fresh 볼륨으로 postgres 를 초기화해둔 뒤에 쓴다.
#
# 정공법 5단계 중 4)에 해당한다(1~3 은 pg-backup.sh 와 새 번들 기동, 5 는 수동):
#   1) 기존 이미지로 기존 볼륨을 그대로 띄운다
#   2) pg_dump(-Fc, 단일 DB) 로 edgequake DB 를 덤프한다   ← pg-backup.sh
#   3) 새 볼륨에 새 이미지(postgres)를 초기화한다          ← parse-only-up.sh/load-and-up.sh
#   4) pg_restore --clean --if-exists 로 덤프를 새 DB 에 넣는다  ← 이 스크립트
#   5) 정상 확인 후에만 옛 볼륨을 지운다            ← 수동, 이 스크립트는 지우지 않는다
#
# 사용:
#   ./scripts/airgap/pg-restore.sh backups/kbp-pg-backup-20260814-170000.sql
#   ./scripts/airgap/pg-restore.sh <파일> --force   # 대상에 이미 데이터가 있어도 강행
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE/.." 2>/dev/null || cd "$HERE"
BUNDLE_ROOT="$(pwd)"

log()  { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m✗ %s\033[0m\n' "$*"; exit 1; }

SQL_FILE=""
FORCE=0
for a in "$@"; do
  case "$a" in
    --force) FORCE=1 ;;
    *) [ -z "$SQL_FILE" ] && SQL_FILE="$a" ;;
  esac
done
[ -n "$SQL_FILE" ] || die "사용법: $0 <백업.sql> [--force]"
[ -f "$SQL_FILE" ] || die "파일 없음: $SQL_FILE"
[ -s "$SQL_FILE" ] || die "빈 파일입니다: $SQL_FILE"

ENGINE="${KBP_ENGINE:-}"
if [ -z "$ENGINE" ]; then
  command -v docker >/dev/null 2>&1 && ENGINE=docker
  [ -z "$ENGINE" ] && command -v podman >/dev/null 2>&1 && ENGINE=podman
fi
[ -n "$ENGINE" ] || die "docker/podman 둘 다 없습니다."

PROJECT="${COMPOSE_PROJECT_NAME:-kbp}"

find_ctr() {
  # 못 찾아도 0으로 반환한다(2026-08-18, pg-backup.sh와 같은 버그) — `set -e` 아래서
  # `CTR="$(find_ctr postgres)"` 대입문에 쓰이면 못 찾는 순간 스크립트가 조용히
  # 죽어, 바로 아래 있는 `[ -n "$CTR" ] || die "..."` 의 명확한 에러 메시지가
  # 절대 안 뜬다.
  local svc="$1" n
  for lbl in com.docker.compose.service io.podman.compose.service; do
    n="$("$ENGINE" ps --filter "label=$lbl=$svc" \
                    --filter "label=com.docker.compose.project=$PROJECT" \
                    --format '{{.Names}}' 2>/dev/null | head -1)"
    [ -n "$n" ] && { echo "$n"; return 0; }
  done
  return 0
}

CTR="$(find_ctr postgres)"
[ -n "$CTR" ] || die "postgres 컨테이너를 찾지 못했습니다 — 새 번들을 먼저 기동하세요
   (parse-only-up.sh 또는 load-and-up.sh)."

st="$("$ENGINE" inspect "$CTR" --format '{{.State.Health.Status}}' 2>/dev/null || echo unknown)"
[ "$st" = "healthy" ] || die "postgres($CTR) 가 healthy 가 아닙니다(현재: $st) — 기동을 먼저 완료하세요."

PW="$(grep -E '^POSTGRES_PASSWORD=' "$BUNDLE_ROOT/.env" 2>/dev/null | head -1 | cut -d= -f2-)"
PW="${PW:-edgequake_secret}"

# PGPASSWORD/-h 필수 + `|| true` 폴백 — pipefail 아래서 인증 실패가 `| tr` 뒤에 숨어
# 메시지 없이 스크립트를 죽인다(pg-backup.sh 에서 실측). 버전 조회는 진단용이라
# 실패해도 복원 자체를 막으면 안 된다.
DST_VERSION="$("$ENGINE" exec -e PGPASSWORD="$PW" "$CTR" \
  psql -U edgequake -h 127.0.0.1 -tAc 'SHOW server_version;' 2>/dev/null | tr -d '[:space:]' || true)"
log "대상: $CTR (postgres $DST_VERSION)"

# ── 안전장치 — 이미 데이터가 있는 인스턴스에 실수로 겹쳐 붓지 않는다 ─────────
# `kbp` 스키마(잡 큐)에 테이블이 하나라도 있으면 "이미 뭔가 있다" 로 본다. fresh
# 볼륨이면 이 스키마 자체가 없거나 비어 있어야 정상이다.
EXISTING="$("$ENGINE" exec -e PGPASSWORD="$PW" "$CTR" \
  psql -U edgequake -h 127.0.0.1 -d edgequake -tAc \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema='kbp';" 2>/dev/null | tr -d '[:space:]' || echo "?")"

if [ "$EXISTING" != "0" ] && [ "$FORCE" -ne 1 ]; then
  die "대상 DB 에 이미 kbp 스키마 테이블이 ${EXISTING}개 있습니다 — fresh 볼륨이 아닌 것
   같습니다. 실수로 데이터를 덮어쓰지 않도록 중단합니다.
   정말 강행하려면(예: 재시도) --force 를 붙이세요: $0 '$SQL_FILE' --force"
fi

# pg-backup.sh 가 pg_dump -Fc(단일 DB, custom format)로 만든 파일이다. `psql -f` 가 아니라
# `pg_restore` 를 쓴다. `--clean --if-exists` 는 대상 DB 안의 기존 객체를 지우고 다시
# 만든다(role/database 자체는 안 건드린다 — 대상은 이미 부트스트랩으로 그 role/db 를
# 갖고 있어야 한다). `--no-owner` 는 원본과 대상의 소유자가 달라도(둘 다 edgequake 라
# 보통 같지만) 실패하지 않게 방어한다.
log "복원 중 — $SQL_FILE → $CTR (DB=edgequake)"
if ! "$ENGINE" exec -i -e PGPASSWORD="$PW" "$CTR" \
     pg_restore -U edgequake -h 127.0.0.1 -d edgequake \
       --clean --if-exists --no-owner --exit-on-error < "$SQL_FILE" \
     > /tmp/pg-restore-$$.log 2>&1; then
  echo "── 복원 로그(끝부분) ──"
  tail -40 /tmp/pg-restore-$$.log
  rm -f /tmp/pg-restore-$$.log
  die "복원 실패 — 위 로그를 확인하세요. 대상 postgres 버전($DST_VERSION)이 원본 버전보다
   낮으면(다운그레이드) 실패할 수 있습니다 — pg_dump 는 상위 버전으로의 복원만 보장합니다."
fi
rm -f /tmp/pg-restore-$$.log

JOBS_COUNT="$("$ENGINE" exec -e PGPASSWORD="$PW" "$CTR" \
  psql -U edgequake -h 127.0.0.1 -d edgequake -tAc \
  "SELECT count(*) FROM kbp.jobs;" 2>/dev/null | tr -d '[:space:]' || echo "?")"

echo
echo "✅ 복원 완료"
echo "  대상 컨테이너: $CTR (postgres $DST_VERSION)"
echo "  kbp.jobs 행 수: $JOBS_COUNT (원본과 대조해 확인하세요)"
echo
echo "다음 단계 — 애플리케이션 레벨로 정상 확인 후에만:"
echo "  옛 볼륨을 수동으로 지우세요:  $ENGINE volume rm <프로젝트>_eq_pg_data"
echo "  (이 스크립트도 pg-backup.sh 도 옛 볼륨을 자동으로 지우지 않습니다.)"
