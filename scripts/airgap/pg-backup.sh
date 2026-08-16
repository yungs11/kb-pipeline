#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# pg-backup.sh — airgap 번들 버전을 올리기 전에 postgres 볼륨을 SQL로 백업한다.
#
# **새 번들을 로드하기 전에** 딱 한 번 실행한다. `docker/podman load` 로 새 이미지를
# 받으면 옛 postgres 이미지 태그(`kbp-postgres:${TAG}`)가 새 이미지로 덮여써져, 옛
# 데이터를 읽을 수 있는 postgres 바이너리가 로컬에서 사라진다 — 그래서 순서가 중요하다.
#
# 계기(2026-08-14 실측): `ghcr.io/raphaelmansuy/edgequake-postgres` 가 버전 태그 없이
# `:latest` 만 나가는데, 번들을 다시 빌드할 때마다 업스트림이 pg16→pg18 처럼 메이저
# 버전이 오를 수 있다. pg18+ 이미지는 pg16 이하 포맷의 데이터 디렉터리를 **거부**한다
# (데이터를 지우지는 않는다 — postgres 자체의 안전장치). 그래도 방치하면 그 볼륨은
# 새 이미지로 영영 못 열리므로, 버전을 올릴 때마다 이 절차를 거친다:
#
#   1) [이 스크립트] 기존 이미지로 기존 볼륨을 그대로 띄운다(볼륨은 건드리지 않는다)
#   2) [이 스크립트] pg_dump(-Fc, 단일 DB) 로 edgequake DB 를 덤프해 호스트 파일로 남긴다.
#      **pg_dumpall 이 아니다** — 대상은 이미 role/db(edgequake/edgequake)를 자체
#      부트스트랩해두므로, pg_dumpall 의 CREATE ROLE/DATABASE 문이 거기서 항상 충돌한다.
#   3) 새 번들을 로드하고 기동한다(fresh 볼륨 — 이름이 같으면 아래 참고)
#   4) pg-restore.sh 로 새 인스턴스에 그 덤프를 복원한다
#   5) 정상 확인 후에만 옛 볼륨을 **수동으로** 지운다(이 스크립트는 절대 지우지 않는다)
#
# 사용:
#   ./scripts/airgap/pg-backup.sh                       # 기본(프로젝트=kbp, ./backups/)
#   ./scripts/airgap/pg-backup.sh --out /path/to/dir
#   COMPOSE_PROJECT_NAME=kbptest ./scripts/airgap/pg-backup.sh
#   KBP_ENGINE=podman ./scripts/airgap/pg-backup.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE/.." 2>/dev/null || cd "$HERE"
BUNDLE_ROOT="$(pwd)"

OUT_DIR="$BUNDLE_ROOT/backups"
for a in "$@"; do
  case "$a" in
    --out) shift_next=1 ;;
  esac
done
# 단순 옵션 파싱(getopts 없이) — 이 스택 스크립트들의 기존 관례를 따른다.
args=("$@")
for i in "${!args[@]}"; do
  if [ "${args[$i]}" = "--out" ] && [ $((i+1)) -lt ${#args[@]} ]; then
    OUT_DIR="${args[$((i+1))]}"
  fi
done
mkdir -p "$OUT_DIR"

log()  { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m✗ %s\033[0m\n' "$*"; exit 1; }

# ── 엔진 자동탐지(parse-only-up.sh 와 동일 관례) ────────────────────────────
ENGINE="${KBP_ENGINE:-}"
if [ -z "$ENGINE" ]; then
  command -v docker >/dev/null 2>&1 && ENGINE=docker
  [ -z "$ENGINE" ] && command -v podman >/dev/null 2>&1 && ENGINE=podman
fi
[ -n "$ENGINE" ] || die "docker/podman 둘 다 없습니다."

PROJECT="${COMPOSE_PROJECT_NAME:-kbp}"          # compose 파일의 `name: kbp` 기본값과 일치시킨다
VOL="${PROJECT}_eq_pg_data"
TAG="${TAG:-airgap}"

log "대상 볼륨: $VOL (engine=$ENGINE)"
if ! "$ENGINE" volume inspect "$VOL" >/dev/null 2>&1; then
  echo "  볼륨이 없습니다 — 백업할 기존 데이터가 없다는 뜻입니다(신규 배포). 종료합니다."
  exit 0
fi

# ── 옛 이미지를 찾는다 — 볼륨을 만든 실제 이미지를 최우선으로 쓴다 ───────────
# `kbp-postgres:${TAG}` 태그는 이미 새 번들 로드로 덮였을 수 있다(그래서 이 스크립트는
# **새 번들을 로드하기 전에** 실행하라고 위에 못박았다). 그래도 만약을 대비해
# 실제 컨테이너의 이미지를 먼저 찾는다 — 태그보다 신뢰도가 높다.
#
# 라벨 기반 조회(load-and-up.sh 의 ctr() 와 같은 이유) — 이름 규칙에 기대면 다른
# 스택의 컨테이너를 잘못 집을 수 있다.
find_ctr() {
  local svc="$1" n
  for lbl in com.docker.compose.service io.podman.compose.service; do
    n="$("$ENGINE" ps -a --filter "label=$lbl=$svc" \
                    --filter "label=com.docker.compose.project=$PROJECT" \
                    --format '{{.Names}}' 2>/dev/null | head -1)"
    [ -n "$n" ] && { echo "$n"; return; }
  done
}

EXIST_CTR="$(find_ctr postgres)"
if [ -n "$EXIST_CTR" ]; then
  OLD_IMG="$("$ENGINE" inspect "$EXIST_CTR" --format '{{.Config.Image}}' 2>/dev/null)"
  log "기존 컨테이너 발견: $EXIST_CTR → 이미지 $OLD_IMG (이걸 신뢰한다)"
else
  OLD_IMG="kbp-postgres:${TAG}"
  warn "postgres 컨테이너를 못 찾음 — 현재 태그 '$OLD_IMG' 로 시도합니다."
  warn "이미 새 번들을 로드했다면 이 태그가 **새 이미지**일 수 있고, 그러면 볼륨을 못 엽니다"
  warn "(데이터가 지워지진 않습니다 — postgres 가 거부만 합니다. 안전하게 실패합니다)."
fi

PW="$(grep -E '^POSTGRES_PASSWORD=' "$BUNDLE_ROOT/.env" 2>/dev/null | head -1 | cut -d= -f2-)"
PW="${PW:-edgequake_secret}"

STAMP="$(date +%Y%m%d-%H%M%S)"
OUT_SQL="$OUT_DIR/kbp-pg-backup-${STAMP}.dump"   # pg_dump custom format(-Fc) — SQL 텍스트 아님
OUT_META="$OUT_DIR/kbp-pg-backup-${STAMP}.meta.txt"

# ── 임시 컨테이너로 볼륨을 그대로 띄운다(지우지 않는다) ─────────────────────
TMP_CTR="kbp-pg-backup-tmp-$$"
"$ENGINE" rm -f "$TMP_CTR" >/dev/null 2>&1 || true
log "옛 이미지로 임시 기동: $OLD_IMG (볼륨 재사용, 초기화 아님)"
"$ENGINE" run -d --name "$TMP_CTR" \
  -v "${VOL}:/var/lib/postgresql" \
  -e POSTGRES_PASSWORD="$PW" \
  "$OLD_IMG" >/dev/null || die "임시 컨테이너 기동 실패 — 이미지/볼륨 궁합을 확인하세요."

cleanup() { "$ENGINE" rm -f "$TMP_CTR" >/dev/null 2>&1 || true; }
trap cleanup EXIT

log "postgres 준비 대기(최대 60초)"
ready=0
for _ in $(seq 1 60); do
  if "$ENGINE" exec "$TMP_CTR" pg_isready -U edgequake >/dev/null 2>&1; then
    ready=1; break
  fi
  # 버전 불일치로 크래시루프하면 여기서 못 뜬다 — 조기 진단
  st="$("$ENGINE" inspect "$TMP_CTR" --format '{{.State.Status}}' 2>/dev/null || echo unknown)"
  if [ "$st" = "restarting" ] || [ "$st" = "exited" ]; then
    if "$ENGINE" logs "$TMP_CTR" 2>&1 | grep -qi "in 18+, these Docker images are configured\|database files are incompatible\|incompatible with this version"; then
      die "옛 이미지($OLD_IMG)가 이 볼륨을 못 엽니다 — 이미 새 번들을 로드해 태그가
     덮여쓰인 것으로 보입니다. 새 번들을 로드하기 **전에** 백업했어야 합니다.
     아직 옛 이미지 tar 를 갖고 있다면 재로드 후 다시 시도하세요:
       $ENGINE load -i <옛 번들>/images/kbp-parse-images-*.tar"
    fi
  fi
  sleep 1
done
[ "$ready" -eq 1 ] || die "postgres 가 60초 안에 준비되지 않았습니다 — $ENGINE logs $TMP_CTR 로 확인하세요."

# `-e PGPASSWORD` + `-h 127.0.0.1` 필수 — 없으면 docker exec 은 root 로 붙어 peer 인증이
# 실패한다. `pipefail`(스크립트 상단)이 켜져 있어 이걸 빠뜨리면 `| tr` 뒤에 숨어
# **아무 메시지 없이 스크립트가 죽는다**(2026-08-14 실측). `|| true` 는 이중 방어 —
# 버전 조회는 진단용일 뿐이라 실패해도 백업 자체를 막으면 안 된다.
SRC_VERSION="$("$ENGINE" exec -e PGPASSWORD="$PW" "$TMP_CTR" \
  psql -U edgequake -h 127.0.0.1 -tAc 'SHOW server_version;' 2>/dev/null | tr -d '[:space:]' || true)"
# pg_dumpall(전 DB+역할) 이 아니라 **pg_dump 단일 DB**(custom format, -Fc) 를 쓴다.
# 이 스택은 role/db 가 언제나 고정값(edgequake/edgequake) 하나뿐이고, 복원 대상은
# `POSTGRES_USER=edgequake POSTGRES_DB=edgequake` 로 **이미 그 role/db 를 부트스트랩해둔
# 상태**라, pg_dumpall 의 `CREATE ROLE`/`CREATE DATABASE` 문이 거기서 항상 충돌한다
# (2026-08-14 실측: `ERROR: role "edgequake" already exists`). -Fc + pg_restore
# `--clean --if-exists` 조합은 role/db 를 새로 안 만들고 **기존 DB 안의 내용만** 교체한다.
log "덤프 중 (postgres $SRC_VERSION, DB=edgequake) → $OUT_SQL"
if ! "$ENGINE" exec -e PGPASSWORD="$PW" "$TMP_CTR" \
     pg_dump -U edgequake -h 127.0.0.1 -d edgequake -Fc > "$OUT_SQL"; then
  rm -f "$OUT_SQL"
  die "pg_dump 실패 — 위 로그를 확인하세요."
fi
[ -s "$OUT_SQL" ] || die "덤프 파일이 비어 있습니다 — 실패로 간주합니다."

# `latest.dump` 포인터 — pg-upgrade.sh 가 파일명(타임스탬프)을 몰라도 체이닝할 수 있게.
cp -f "$OUT_SQL" "$OUT_DIR/latest.dump"

{
  echo "backup_time=$STAMP"
  echo "engine=$ENGINE"
  echo "project=$PROJECT"
  echo "volume=$VOL"
  echo "source_image=$OLD_IMG"
  echo "source_pg_version=$SRC_VERSION"
  echo "sql_file=$(basename "$OUT_SQL")"
  echo "sql_bytes=$(wc -c < "$OUT_SQL" | tr -d '[:space:]')"
} > "$OUT_META"

echo
echo "✅ 백업 완료"
echo "  SQL:      $OUT_SQL ($(wc -c < "$OUT_SQL" | tr -d '[:space:]') bytes)"
echo "  메타:     $OUT_META"
echo "  원본버전: postgres $SRC_VERSION"
echo
echo "다음 단계(한 번에 하려면 ./scripts/airgap/pg-upgrade.sh 를 대신 쓰세요):"
echo "  1) 새 번들을 로드·기동한다(fresh 볼륨으로 — 옛 볼륨 '$VOL' 은 그대로 둔다)"
echo "  2) ./scripts/airgap/pg-restore.sh '$OUT_SQL' 로 새 인스턴스에 복원한다"
echo "  3) 정상 확인 후에만 옛 볼륨을 수동으로 지운다:"
echo "       $ENGINE volume rm $VOL"
