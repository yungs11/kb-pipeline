# shellcheck shell=bash
# 호스트 dev 런처 공용 env 로더 (2026-08-10) — `source` 해서 쓴다.
#
# ── 왜 만들었나 ──────────────────────────────────────────────────────────────
# env 파일이 셋이었고(`.env` / `scripts/facade.env` / `scripts/parse-svc.env`)
# **API 키와 MinIO 자격증명 6개가 3중 중복**이었다. 한 곳만 고치면 조용히 어긋난다.
# 게다가 두 런처가 각자 `set -a; . <파일>` 로 읽어서 **CLI 로 준 값을 덮어썼다** —
# 같은 버그를 하루에 두 번 고쳤다(OCR 게이트웨이 주소 검증이 옛 주소로 갔다).
#
# ── 설계 ────────────────────────────────────────────────────────────────────
# 1) **비밀값·선택값은 리포 루트 `.env` 하나**가 소유한다(compose 와 같은 파일).
#    충돌 걱정이 없다 — compose 는 서비스 DNS 주소를 `docker-compose.yml` 에 직접 박고
#    `.env` 에서는 `${VAR}` 로 비밀값·선택값만 읽는다(실측: `.env` 에 주소 키 0개).
# 2) **호스트 주소는 설정하지 않고 파생**한다. 호스트 프로세스라는 사실에서 localhost +
#    포트가 결정되므로 사람이 적어둘 이유가 없다(적어두면 포트 재배치 때 또 어긋난다).
# 3) **CLI 가 가장 세다** → `.env` → 레거시 파일 → 파생 기본값. dotenv 관례다.
#
# 레거시 `scripts/facade.env`·`scripts/parse-svc.env` 는 **있으면 계속 읽는다**(하위호환).
# 다만 `.env` 와 겹치는 키는 `.env` 가 이긴다 — 중복을 지우라고 경고한다.

# _dev_env_source_nonempty <파일> — `KEY=값` 중 **값이 비지 않은 것만** export 한다.
#   주석·빈 줄·`export ` 접두어를 허용한다. 값 안의 `#` 는 자르지 않는다(비밀값에 있을 수 있다).
_dev_env_source_nonempty () {
  local line key val
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue;; esac
    line="${line#export }"
    case "$line" in *=*) ;; *) continue;; esac
    key="${line%%=*}"; val="${line#*=}"
    case "$key" in [A-Za-z_]*) ;; *) continue;; esac
    # 양쪽 따옴표 제거(dotenv 관례)
    case "$val" in \"*\") val="${val#\"}"; val="${val%\"}";; \'*\') val="${val#\'}"; val="${val%\'}";; esac
    [ -n "$val" ] || continue          # ← 빈 값은 무시(덮어쓰지 않는다)
    export "$key=$val"
  done < "$1"
}

# _dev_env_load <추가로 읽을 레거시 파일…>
_dev_env_load () {
  local root="$1"; shift

  # ① CLI 로 들어온 값을 먼저 스냅샷한다(파일 source 가 덮어쓰지 못하게).
  local _snap="" _k _v
  for _k in $(env | grep -oE '^(KBP_|MINIO_|MODEL_|VL_|EXCEL_PARSER_|KORDOC_)[A-Z0-9_]*' || true); do
    eval "_v=\${$_k:-}"
    [ -n "$_v" ] && _snap="$_snap $_k"
  done
  local _snapfile; _snapfile="$(mktemp)"
  for _k in $_snap; do eval "printf '%s=%s\n' \"\$_k\" \"\${$_k}\"" >> "$_snapfile"; done

  # ② 레거시 파일 → ③ `.env` 순서로 읽는다(뒤가 이긴다 = `.env` 우선).
  #
  # ★ **빈 값은 "없음" 으로 취급한다.** 그냥 `. <파일>` 로 읽으면 `.env` 의 `KEY=`(빈 값)이
  #   레거시 파일의 실제 값을 **덮어써서** 기동이 죽는다 — 실측 2026-08-10:
  #   `.env` 의 `KBP_OPENAI_API_KEY=` 가 `facade.env` 의 실 키를 지워 facade 가 안 떴다.
  #   템플릿에서 복사한 `.env` 는 채우지 않은 키가 빈 값으로 남아 있는 것이 정상이므로
  #   이걸 안 다루면 통합 자체가 사고가 된다.
  local f
  for f in "$@"; do
    [ -f "$f" ] || continue
    _dev_env_source_nonempty "$f"
    _DEV_ENV_LEGACY="$_DEV_ENV_LEGACY $f"
  done
  if [ -f "$root/.env" ]; then
    _dev_env_source_nonempty "$root/.env"
    _DEV_ENV_MAIN="$root/.env"
  fi

  # ④ CLI 값을 되돌린다(가장 셈).
  if [ -s "$_snapfile" ]; then
    set -a; . "$_snapfile"; set +a
  fi
  rm -f "$_snapfile"
}

# _dev_env_host_addrs — 호스트 프로세스가 쓰는 주소를 **파생**한다(미설정일 때만).
# 포트는 P1(2026-08-10) 배치다. compose 로 띄울 때는 컨테이너 DNS 라 여기 값이 쓰이지 않는다.
_dev_env_host_addrs () {
  : "${KBP_PARSE_SVC_URL:=http://localhost:19001}"
  : "${KBP_EDGEQUAKE_URL:=http://localhost:3001}"
  : "${KBP_ADAPTIVE_CHUNK_URL:=http://localhost:18060}"
  : "${KBP_DOC_GUARD_URL:=http://localhost:8001}"
  : "${MINIO_ENDPOINT:=localhost:9000}"
  : "${MINIO_SECURE:=false}"
  # postgres 는 자격증명이 들어가므로 `.env` 의 POSTGRES_* 로 조립한다(비밀값 중복 방지).
  if [ -z "${KBP_PG_DSN:-}" ]; then
    KBP_PG_DSN="postgres://${POSTGRES_USER:-edgequake}:${POSTGRES_PASSWORD:-edgequake_secret}@localhost:5433/${POSTGRES_DB:-edgequake}"
  fi
  export KBP_PARSE_SVC_URL KBP_EDGEQUAKE_URL KBP_ADAPTIVE_CHUNK_URL KBP_DOC_GUARD_URL \
         MINIO_ENDPOINT MINIO_SECURE KBP_PG_DSN
}

# _dev_env_report — 무엇을 어디서 읽었는지 한 줄로 남긴다(디버깅용).
#   중복 키가 있으면 경고한다 — 지우지 않으면 또 어긋난다.
_dev_env_report () {
  local root="$1"
  echo "env: ${_DEV_ENV_MAIN:-(.env 없음)}${_DEV_ENV_LEGACY:+  + 레거시:$_DEV_ENV_LEGACY}"
  local f dup
  for f in $_DEV_ENV_LEGACY; do
    [ -f "$root/.env" ] || continue
    dup="$(comm -12 <(grep -oE '^[A-Z_]+' "$f" | sort -u) \
                    <(grep -oE '^[A-Z_]+' "$root/.env" | sort -u) | tr '\n' ' ')"
    [ -n "$dup" ] && {
      echo "  ⚠️  $f 에 .env 와 중복된 키가 있다(.env 가 이긴다). 지우면 어긋날 일이 없다:" >&2
      echo "      $dup" >&2
    }
  done
}
