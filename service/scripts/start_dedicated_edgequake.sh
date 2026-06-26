#!/usr/bin/env bash
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
  EDGEQUAKE_DEFAULT_LLM_MODEL=qwen/qwen3.5-122b-a10b EDGEQUAKE_LLM_MODEL=qwen/qwen3.5-122b-a10b \
  EDGEQUAKE_EMBEDDING_PROVIDER=openai EDGEQUAKE_EMBEDDING_BASE_URL=https://litellm.ax-demo.com/v1 \
  EDGEQUAKE_EMBEDDING_API_KEY="$LITELLM_KEY" EDGEQUAKE_EMBEDDING_MODEL=bge-m3 EDGEQUAKE_EMBEDDING_DIMENSION=1024 \
  PDFIUM_AUTO_CACHE_DIR=/tmp/eqkbp-pdfium RUST_LOG=info \
  "$EQ/target/debug/edgequake" > /tmp/edgequake_kbp.log 2>&1 &
disown
