#!/usr/bin/env bash
# facade (:3000, service.app:app) launcher / restarter.
#
# 포트: 기본 3000(P1 2026-08-10 — 폐쇄망 published 포트와 통일. 이전 19000).
#   경합 시 `KBP_FACADE_PORT=<포트> bash scripts/run-facade.sh` 로 옮긴다.
#   ⚠️ 옮기면 kb 쪽 짝 env 도 같이 줘야 한다 — kb .env 의
#      `KB_PIPELINE_BASE_URL=http://localhost:<포트>` (없으면 kb→facade 적재·게이트가 끊긴다).
#   ⚠️ 이건 **호스트 프로세스 전용**이다. compose facade 는 3000:19000 으로 발행하므로
#      compose 쪽 충돌은 docker-compose.yml 을 직접 고친다.
#
# KBP_REQUIRE_EDGEQUAKE=1 이면 edgequake 도달 불가 시 기동을 **중단**한다(기본: 경고만).
#
# The facade reads its config straight from os.environ (no dotenv), so it needs the
# KBP_* vars exported. They live in the gitignored scripts/facade.env (captured from
# the running process). Unlike parse-svc, the facade does NOT need java.
#
# DOCKER-SHADOW gotcha (2026-07-07): facade + parse-svc are HOST dev processes; the
# docker-compose stack is BACKING services only. `docker compose up -d` also starts the
# facade/parse-svc CONTAINERS (stale image code) which SHADOW these host processes and
# serve old code (you see "옛날 파싱"/old behavior). This script stops the shadow facade
# container so the host source serves. Do NOT `docker compose up` facade/parse-svc for
# dev — use run-facade.sh / run-parse-svc.sh (or `docker compose build` to rebuild).
#
# Usage:  bash scripts/run-facade.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# 0) Guard against the docker-shadow gotcha: stop any compose facade container so this
#    host process (and kb-backend's localhost:3000 calls) hit the live source.
for _cid in $(docker ps -q --filter "label=com.docker.compose.service=facade" 2>/dev/null); do
  echo "guard: stopping shadow docker facade container ($_cid) — host source must serve"
  docker stop "$_cid" >/dev/null 2>&1 || true
done

# env + secrets (gitignored). set -a auto-exports every KEY=value.
ENV_FILE="$ROOT/scripts/facade.env"
# ★ 호출자가 준 값이 facade.env 보다 **우선**한다(dotenv 관례).
#   `set -a; . "$ENV_FILE"` 만 하면 파일 값이 CLI 값을 덮어써서
#   `KBP_EDGEQUAKE_URL=... bash scripts/run-facade.sh` 가 조용히 무시된다.
#   (실측 2026-08-10: 그래서 KBP_REQUIRE_EDGEQUAKE=1 가드 테스트가 통과해버렸다.)
#   덮어쓰기를 허용할 키만 명시적으로 스냅샷한다 — `export -p` 재적용은 PWD 같은 것까지 건드린다.
_CALLER_EQ_URL="${KBP_EDGEQUAKE_URL:-}"
_CALLER_PORT="${KBP_FACADE_PORT:-}"
if [ -f "$ENV_FILE" ]; then set -a; . "$ENV_FILE"; set +a; fi
[ -n "$_CALLER_EQ_URL" ] && export KBP_EDGEQUAKE_URL="$_CALLER_EQ_URL"
[ -n "$_CALLER_PORT" ]   && export KBP_FACADE_PORT="$_CALLER_PORT"
true   # 위 두 [ ] 가 거짓일 때 set -e 로 죽지 않게 한다
: "${KBP_OPENAI_API_KEY:?missing — scripts/facade.env must set KBP_OPENAI_API_KEY}"
: "${KBP_PG_DSN:?missing — scripts/facade.env must set KBP_PG_DSN}"
# 잡 staging 이 여기 없으면 /parse·/ingest 접수가 런타임에 전면 실패한다.
: "${MINIO_ENDPOINT:?missing — scripts/facade.env must set MINIO_ENDPOINT (호스트 dev 는 localhost:9000)}"
# Raised parse read-timeout (multi-table PDFs take ~400s+). Code default is 1800.
export KBP_PARSE_SVC_TIMEOUT="${KBP_PARSE_SVC_TIMEOUT:-1800}"

PORT="${KBP_FACADE_PORT:-3000}"

# edgequake 도달성 — **경고만** 하고 계속 진행한다.
# facade 는 edgequake 에 기동 의존이 없다(get_edgequake 는 요청별 Depends) — /parse·/chunk·
# /healthz·/gate/*·/objects/* 는 edgequake 없이 동작한다. 그래서 여기서 중단하면 parse 전용
# 작업이나 edgequake 재빌드 중 재기동을 막는다(탈출구를 없애는 것).
EQ_URL="${KBP_EDGEQUAKE_URL:-http://localhost:3001}"
if ! curl -fsS -m 3 "$EQ_URL/health" >/dev/null 2>&1; then
  echo "⚠️  edgequake 에 닿지 않는다: $EQ_URL/health"
  echo "    compose 로 띄웠으면 http://localhost:3001, dedicated 런처면 http://localhost:8081 이다."
  echo "    (KBP_EDGEQUAKE_URL 로 지정. insert/search 만 영향 — parse/chunk/gate 는 정상 동작한다.)"
  if [ "${KBP_REQUIRE_EDGEQUAKE:-0}" = "1" ]; then
    echo "    KBP_REQUIRE_EDGEQUAKE=1 이라 기동을 중단한다." >&2; exit 1
  fi
fi

# restart — wait for :$PORT to free (sleep 1 races uvicorn graceful shutdown).
pkill -f "uvicorn service.app:app" 2>/dev/null || true
for _ in $(seq 1 20); do
  if ! lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then break; fi
  sleep 0.5
done
lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 && { pkill -9 -f "uvicorn service.app:app" 2>/dev/null || true; sleep 1; }
# 남이 그 포트를 잡고 있으면 uvicorn 이 바인드 실패한다. 아래 health 판정이 **본문**을 보므로
# 남의 HTML 응답을 성공으로 오인하지 않는다(3000 은 dev 에서 경합이 가장 심한 번호다).

LOG="${FACADE_LOG:-/tmp/facade-kbp.log}"
nohup "$ROOT/.venv-kb/bin/python" -m uvicorn service.app:app \
  --host 127.0.0.1 --port "$PORT" > "$LOG" 2>&1 &
UPID=$!
echo "facade launched (pid $UPID) on :$PORT — log: $LOG (parse timeout ${KBP_PARSE_SVC_TIMEOUT}s)"
# 폴링 여유 30초. 콜드 스타트(파이썬 import + ensure_schema + 스케줄러 스레드)가 10초를
# 넘는 것을 실측했다 — 정상 서버에 오경보하는 가드는 곧 무시당한다(그게 더 위험하다).
for i in $(seq 1 30); do
  # ★ 본문을 검증한다. `[ -n "$r" ]` 로 두면 남이 이 포트를 잡았을 때 그쪽 HTML 을
  #   성공으로 읽어 "런처 성공, 실제로는 엉뚱한 서비스" 가 된다(실측: next dev 가 7533B HTML).
  r="$(curl -s -m 3 "http://localhost:$PORT/healthz" 2>/dev/null || true)"
  case "$r" in *'"status"'*'"ok"'*) echo "healthz: $r"; exit 0;; esac
  # ★ 바인드 실패를 실패로 보고한다 — uvicorn 이 죽었으면 더 기다릴 이유가 없다.
  if ! kill -0 "$UPID" 2>/dev/null; then
    echo "✗ uvicorn 이 죽었다(:$PORT 바인드 실패?). 로그 마지막 줄:" >&2
    tail -5 "$LOG" >&2
    echo "  다른 프로세스가 :$PORT 를 잡고 있으면 KBP_FACADE_PORT 로 옮긴다." >&2
    exit 1
  fi
  if [ -n "$r" ]; then
    echo "✗ :$PORT 가 facade 가 아니다 — /healthz 응답이 {\"status\":\"ok\"} 가 아니다:" >&2
    printf '  %.120s\n' "$r" >&2
    echo "  다른 서비스가 그 포트를 점유했다. KBP_FACADE_PORT 로 옮기거나 그 서비스를 내린다." >&2
    exit 1
  fi
  sleep 1
done
echo "✗ facade healthz 가 30초 안에 준비되지 않았다 — $LOG 확인" >&2
exit 1
