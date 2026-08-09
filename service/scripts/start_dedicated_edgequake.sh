#!/usr/bin/env bash
# ⚠️ **보조 경로다.** dev edgequake 의 정본은 compose 다:
#      docker compose up -d postgres edgequake     # → edgequake 호스트 3001, pg 5433
#    compose 는 명명 볼륨 eq_pg_data 를 쓰므로 데이터가 남는다.
#
# 이 스크립트를 쓰면 실제로 생기는 문제는 둘이다(P1 2026-08-10 실측으로 정정):
#   (i)  compose postgres 가 5433 을 점유 중이면 아래 `docker run -p 5433:5432` 가
#        **바인드 충돌**로 실패한다.
#   (ii) compose postgres 를 내려서 우회하면, 이 컨테이너의 **빈 PG 가 라이브 볼륨을 가려**
#        facade 가 빈 DB 를 본다(데이터는 eq_pg_data 에 남아 있지만 그 세션에서는 안 보인다).
#   그리고 이 컨테이너 자신의 PG 데이터는 **볼륨이 없어 매 기동 소거**된다.
#   ※ compose 의 명명 볼륨 eq_pg_data 는 이 스크립트가 **건드리지 못한다** —
#     `docker rm -f eq-pg-kbp` 는 자기 컨테이너만 지운다.
#
# 이 런처로 띄우면 edgequake 는 **8081** 이다(compose 는 3001). 그래서 facade 에
#   KBP_EDGEQUAKE_URL=http://localhost:8081
# 을 줘야 한다(코드 기본값은 3001 — service/app.py).
set -euo pipefail
KEY=$(grep -E '^OPENAI_API_KEY=' /Users/xxx/workspace/99.projects/rag-edgequake-benchmark/docker/.env | head -1 | cut -d= -f2-)
# litellm embedding key: never hardcode in this git-tracked file. Read from env, else
# fall back to the gitignored adaptive_chunk/.env (LITELLM_API_KEY=...).
LITELLM_KEY="${LITELLM_API_KEY:-$(grep -E '^LITELLM_API_KEY=' /Users/xxx/workspace/99.projects/adaptive_chunk/.env 2>/dev/null | head -1 | cut -d= -f2-)}"
: "${LITELLM_KEY:?LITELLM_API_KEY not set and not found in adaptive_chunk/.env}"
docker rm -f eq-pg-kbp 2>/dev/null || true
docker run -d --name eq-pg-kbp -p 5433:5432 \
  -e POSTGRES_USER=edgequake -e POSTGRES_PASSWORD=edgequake_secret -e POSTGRES_DB=edgequake \
  ghcr.io/raphaelmansuy/edgequake-postgres:latest
# The edgequake-postgres image runs an init pass that restarts the server mid-startup,
# so a single pg_isready can pass against the transient init server. Require the DB to
# accept a real connection N times in a row before launching edgequake.
ok=0
until [ "$ok" -ge 5 ]; do
  if docker exec eq-pg-kbp psql -U edgequake -d edgequake -c 'SELECT 1' >/dev/null 2>&1; then
    ok=$((ok+1))
  else
    ok=0
  fi
  sleep 1
done
EQ=/Users/xxx/workspace/8.kb-pipeline/edgequake/edgequake
# NOTE: DATABASE_URL must NOT pin `?options=-c search_path=public` — that drops
# ag_catalog from the search_path and breaks AGE graph operators (graphid =),
# making GET /api/v1/chunks/{id} return 500 on its entity/relationship edge query.
nohup env \
  HOST=0.0.0.0 PORT=8081 \
  EDGEQUAKE_HOST=0.0.0.0 EDGEQUAKE_PORT=8081 EDGEQUAKE_CHUNKER=passthrough \
  ADAPTIVE_CHUNK_URL=http://localhost:18060 \
  DATABASE_URL='postgres://edgequake:edgequake_secret@localhost:5433/edgequake' \
  EDGEQUAKE_LLM_PROVIDER=openrouter OPENROUTER_API_KEY="$KEY" \
  OPENAI_BASE_URL=https://openrouter.ai/api/v1 OPENAI_API_KEY="$KEY" \
  EDGEQUAKE_DEFAULT_LLM_MODEL=qwen/qwen3.5-122b-a10b EDGEQUAKE_LLM_MODEL=qwen/qwen3.5-122b-a10b EDGEQUAKE_LLM_DISABLE_REASONING=1 EDGEQUAKE_EXTRACTION_LANGUAGE=Korean EDGEQUAKE_RERANK_BASE_URL=https://litellm.ax-demo.com/v1/rerank EDGEQUAKE_RERANK_MODEL=Qwen3-Reranker-0.6B EDGEQUAKE_RERANK_API_KEY="$LITELLM_KEY" EDGEQUAKE_MIN_RERANK_SCORE=0.0 \
  EDGEQUAKE_EMBEDDING_PROVIDER=openai EDGEQUAKE_EMBEDDING_BASE_URL=https://litellm.ax-demo.com/v1 \
  EDGEQUAKE_EMBEDDING_API_KEY="$LITELLM_KEY" EDGEQUAKE_EMBEDDING_MODEL=bge-m3 EDGEQUAKE_EMBEDDING_DIMENSION=1024 \
  PDFIUM_AUTO_CACHE_DIR=/tmp/eqkbp-pdfium RUST_LOG=info \
  "$EQ/target/debug/edgequake" > /tmp/edgequake_kbp.log 2>&1 &
disown
