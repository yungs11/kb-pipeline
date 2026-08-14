---
name: restart-kbp-stack
description: Use when (re)starting kb-pipeline services or preparing localhost:18080 for end-to-end testing — parse-svc (:19001), facade (:3000), its facade-worker job-queue consumer (no HTTP port), kb-backend (:8088), its durable batch worker, EdgeQuake (:3001), and the required EdgeQuake graph WebUI (:3002) — OR the excel-gate stack — doc_guard (:8000), excel-parser (:18055). Also use after editing their code, when batch uploads stay queued, "그래프 보기" fails, when a removed/old doc_guard gate still blocks uploads, when /parse returns empty enriched_content or missing gate_summary, on "Unable to locate a Java Runtime", on kordoc "*.md 를 찾을 수 없습니다", or on facade httpx.ReadTimeout. Always start and verify the facade-worker, the kb batch worker, and edgequake_webui when restoring the test stack — without facade-worker the facade rejects every /parse·/chunk·/insert·/ingest with 503. Each host service has its own launcher script that pins the right PATH/env and kills the old process BY PORT.
---

# Restart the kb-pipeline provider stack

## 필수 완료 조건 — batch worker + 그래프 UI 포함

`localhost:18080` 테스트 환경 또는 전체 KB 스택을 복구할 때
kb-backend의 durable batch worker와 `edgequake_webui`를 선택 서비스로 남겨두지
말고 **반드시 함께 기동**한다.
EdgeQuake API만 `:3001`에서 정상이어도 “그래프 보기”는 동작하지 않는다.
kb-backend API만 `:8088`에서 정상이어도 새 배치 행은 `queued`에 머문다.

```bash
# 기존 이미지를 사용하는 안전한 복구. postgres/minio 볼륨은 보존된다.
docker compose up -d --no-build postgres minio edgequake edgequake_webui
```

이 머신의 그래프 WebUI 호스트 포트는 **`:3002`** 이다 — base `docker-compose.yml` 이
`3002:3000` 으로 발행한다(P1 2026-08-10. 이전엔 `docker-compose.override.yml` 이 13000 으로
리맵했는데 그 파일은 이제 주석만 남았다. **override 를 되살리지 마라** — 정답 포트는 base 에 있다).
`:14000`은 이 프로젝트에서 사용하지 않는다.
KB 프론트의 "그래프 보기" 는 **보고 있는 호스트의 `:3002`** 로 연다
(`window.location` 파생 — 폐쇄망에서는 서버IP:3002. `NEXT_PUBLIC_EDGEQUAKE_WEBUI_URL` 로 덮을 수 있다).

재기동 완료를 보고하기 전에 아래 검증을 모두 통과시킨다.

```bash
docker compose ps edgequake edgequake_webui
curl -fsS http://localhost:3001/health >/dev/null
curl -fsS http://localhost:3002/popup/graph >/dev/null
test -s /tmp/kb_batch_worker.pid
kill -0 "$(cat /tmp/kb_batch_worker.pid)"
curl -fsS http://localhost:18080/kb >/dev/null
```

`scripts/run-kb-backend.sh`는 Alembic migration 후 kb-backend와 batch worker를 함께
재기동한다. worker 로그는 `/tmp/kb_batch_worker.log`, PID는
`/tmp/kb_batch_worker.pid`다. 화면의 “배치 Worker”가 offline이거나 파일이
queued에 머물면 PID만 믿지 말고 worker 로그와 인증된
`GET /kb/{kb_id}/batches/worker` 응답의 `online/capacity/available`을 확인한다.

`edgequake_webui` health가 `starting`이면 `healthy`가 될 때까지 기다린 뒤 HTTP를
검증한다. `:3002`이 실패하면 `docker compose up -d --no-build edgequake_webui`와
`docker compose logs --tail 100 edgequake_webui`로 복구·진단한다.

## 1순위 — Docker Compose (전체 또는 단일 서비스 재빌드/재기동)

`docker-compose.yml` (project name `kbp`) 이 10개 서비스를 함께 관리한다. 코드를 바꾼 뒤 해당 서비스만 재빌드해 올릴 수 있다.

```bash
cd /Users/xxx/workspace/8.kb-pipeline

# 전체 스택 재기동 (이미지 재빌드 포함)
docker compose up -d --build
# 그래프 UI는 필수 후조건이다. 전체 명령 뒤에도 명시적으로 보장한다.
docker compose up -d --no-build edgequake_webui

# 단일 서비스만 재빌드·재기동 (가장 흔한 패턴)
docker compose up -d --build facade
docker compose up -d --build parse-svc
docker compose up -d --build edgequake
docker compose up -d --no-build edgequake_webui
docker compose up -d --build doc_guard
docker compose up -d --build excel-parser
docker compose up -d --build adaptive_chunk
docker compose up -d --build document-parser

# 로그 확인
docker compose logs -f facade
docker compose logs -f parse-svc

# 서비스 상태
docker compose ps
```

> **전제**: `edgequake/` submodule 이 체크아웃돼 있어야 한다(`git submodule update --init --recursive edgequake`). `.env` 도 실값이 채워진 상태여야 한다(`cp -n .env.example .env` 후 편집).

> **kb-backend(:8088) 와 frontend 는 compose 범위 밖** — 아래 호스트 스크립트를 쓴다.

---

## 2순위 — 개별 호스트 런처 스크립트 (단일 서비스 호스트 기동·디버그·fallback)

compose 를 쓰지 않거나, 특정 서비스를 호스트에서 직접 띄울 때 사용한다. 각 스크립트는 **포트 기준 kill**(lsof -ti:\<PORT>) → 재기동 → health 검증 순서로 동작한다(`--reload` 없음, 코드 변경 시 반드시 재기동).

| Service | Port | Script | Gotcha it handles |
|---|---|---|---|
| parse-svc | 19001 | `scripts/run-parse-svc.sh` | needs **openjdk@17** on PATH (OpenDataLoader) + `KBP_OPENAI_API_KEY` (modal LLM) + **kordoc env**(KORDOC_BIN/KORDOC_MD_OUT/EXCEL_PARSER_BACKEND — Phase 2e 로 엑셀 파싱이 in-process 다) |
| facade | 3000 | `scripts/run-facade.sh` | reads `os.environ` directly (no dotenv) → needs `KBP_*` **and `MINIO_*`** from the repo-root `.env` (load-dev-env.sh) |
| facade-worker | — (no port) | `scripts/run-facade-worker.sh` | 잡 큐 소비자. 다운스트림 호출은 **오직 여기 슬롯 안에서만** 일어난다. 살아있는지는 `GET /jobs/workers` 의 `online` 으로 확인 |
| kb-backend + batch worker | 8088 + DB heartbeat | `scripts/run-kb-backend.sh` | runs Alembic first, restarts API and durable worker; worker capacity defaults to 2 |
| doc_guard | 8000 | `scripts/run-doc-guard.sh` | verifies `POST /v1/check-excel` answers (new excel-gate endpoint), not just healthz |
| excel-parser (레거시 코호트) | 18055 | `scripts/run-parse-svc.sh --excel-only` | **run-parse-svc 로 통합**(2026-08-10). kordoc env 를 두 스크립트가 각자 관리하다 어긋나 호스트 엑셀 파싱이 죽었다. kills :18055 **by port** (module `service.main:app` is shared with adaptive_chunk :18060 — never module-pattern kill) |
| edgequake_webui | 3002 | `docker compose up -d --no-build edgequake_webui` | required by KB frontend “그래프 보기”; verify `/popup/graph`, not port 14000 |

```bash
bash scripts/run-parse-svc.sh    # after editing parse_service/ or kb_pipeline/
bash scripts/run-facade.sh          # after editing service/ (facade)
bash scripts/run-facade-worker.sh   # 잡 큐 소비자 — facade 만 띄우면 접수가 전부 503
bash scripts/run-kb-backend.sh   # after editing knowledge_base backend/config
bash scripts/run-doc-guard.sh    # after editing doc_guard app/
bash scripts/run-parse-svc.sh --excel-only   # 레거시 excel-parser(:18055) 만 — provider=excel_parser 코호트
bash scripts/run-parse-svc.sh --with-excel   # parse-svc + excel-parser 둘 다
bash scripts/restart-gate-stack.sh   # all 3 excel-gate services in dep order (doc_guard+excel-parser→kb-backend)

# localhost:18080 테스트 환경을 복구했다면 항상 마지막에 실행·검증
docker compose up -d --no-build edgequake_webui
curl -fsS http://localhost:3002/popup/graph >/dev/null
test -s /tmp/kb_batch_worker.pid
kill -0 "$(cat /tmp/kb_batch_worker.pid)"
```

## Excel gate stack (doc_guard + excel-parser + kb-backend)

The parser-후단 엑셀 게이트 spans 3 services. If an edit doesn't take effect (e.g. an
old/removed doc_guard gate still blocks an upload), it is almost always a **stale
process**, not the code. `restart-gate-stack.sh` restarts all three in dependency order
(doc_guard + excel-parser must be up before kb-backend calls them) and verifies each is
running NEW code (doc_guard `/v1/check-excel`, excel-parser `/parse` returns
`stats.gate_summary`).

**Two traps that bit us (2026-06-30):**
1. **Kill by PORT, not by cmdline pattern.** kb-backend ran as
   `uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8088`, but the old
   launcher's `pkill -f "app.main:app --app-dir backend --port 8088"` didn't match
   (`--host 127.0.0.1` sits between `backend` and `--port`) → old process survived, new
   one failed to bind and died, **old code kept serving :8088**. All launchers now
   `kill $(lsof -nP -iTCP:<port> -sTCP:LISTEN -t)`.
2. **kordoc env 는 parse-svc 도 필요하다.** (2026-08-10) Phase 2e 에서 엑셀 파싱이 parse-svc
   in-process 로 흡수됐는데 env 가 `run-excel-parser.sh` 에만 남아, 컨테이너에서는 되고
   **호스트 dev 에서만** `parse_failed: ... '*.md' 를 찾을 수 없습니다` 로 죽었다. 두 런처를
   `run-parse-svc.sh` 로 합쳐 한 곳에서 세팅한다. 원문: default `EXCEL_PARSER_BACKEND=auto` routes non-전결
   xlsx to the kordoc CLI; without `KORDOC_BIN=kordoc` + node on PATH, `/parse` 500s
   ("*.md 를 찾을 수 없습니다") → kb gets no `gate_summary` → gate silently passes
   everything. `run-excel-parser.sh` discovers kordoc (`command -v kordoc` / nvm glob)
   and exports `KORDOC_BIN`/`KORDOC_MD_OUT`.

**Which backend does the UI hit?** `knowledge_base/frontend/.env.local` →
`BACKEND_ORIGIN` (currently `http://localhost:8088`). If uploads still show old behavior
after a restart, confirm the frontend points at the backend you restarted, and hard-refresh.

## The gotchas, in detail

1. **parse-svc — java**: OpenDataLoader shells out to `java`. macOS `/usr/bin/java` is a
   stub (`Unable to locate a Java Runtime`) → CLI exit 1 → **empty `enriched_content`**.
   The script pins `/usr/local/opt/openjdk@17/bin` (or `/opt/homebrew/...`).
2. **parse-svc / facade — env**: `service/llm.py` reads `os.environ["KBP_OPENAI_API_KEY"]`
   with no default → KeyError when a modal block is described. 값은 **gitignored 리포 루트
   `.env` 하나**가 소유한다 — 런처 3종(parse-svc/facade/facade-worker)이 모두
   `scripts/lib/load-dev-env.sh` 로 그 파일을 읽는다. `scripts/parse-svc.env`·
   `scripts/facade.env` 는 **2026-08-13 폐기**했다(파일이 둘이면 어긋난다 — 루트만 새 OCR
   게이트웨이 주소로 고쳐지고 파생본이 옛 주소로 남아 옛 GW 로 가고 있었다). 로더는 아직
   두 파일을 읽어주지만 **새로 만들지 마라.**
   > Trap: `export FOO=...` and `uvicorn ... &` as **separate** `!` commands run in
   > separate shells — the export never reaches the launched process. Always use the script.
3. **facade — ReadTimeout on big PDFs**: parse-svc calls the modal LLM **once per table,
   sequentially** (a 4-table PDF ≈ 400s+). The facade→parse-svc read timeout is 1800s
   (`KBP_PARSE_SVC_TIMEOUT`) and kb-backend→facade is `kb_pipeline_timeout_seconds=1800`
   so neither gives up early. If you see `httpx.ReadTimeout`, suspect a slow multi-table
   parse (or two parses colliding on the single-worker parse-svc).

## First-time setup (gitignored env file)

`cp -n .env.example .env` 후 값을 채운다. 최소: `KBP_OPENAI_API_KEY`, `MINIO_*`,
`POSTGRES_PASSWORD`(=> `KBP_PG_DSN` 은 여기서 조립된다), `MODEL_NAME`,
`KBP_PADDLE_OCR_GATEWAY_URL`. 주소는 적지 않는다 — 호스트 dev 는 `_dev_env_host_addrs`
가 localhost+포트로 파생한다.

돌고 있는 프로세스에서 값을 회수해야 하면(비밀값 출력 없이):

```bash
ps eww "$(pgrep -f 'service.app:app' | head -1)" | tr ' ' '\n' | grep -E '^KBP_[A-Z_]+='
```

런처가 `env: <경로>` 뒤에 **레거시 파일이나 중복 키 경고**를 찍으면 그게 곧 드리프트
신호다 — 무시하지 말고 `.env` 로 합쳐라(실제로 30개를 경고하는 채로 옛 GW 주소가 살아 있었다).

## Verify a real parse works

`healthz` only proves the process booted. To prove java + modal LLM end-to-end:

```bash
curl -s -m 1800 -F "file=@<some.pdf>;type=application/pdf" -F "filename=t.pdf" \
  http://localhost:19001/parse \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('status'),d.get('n_blocks'),len(d.get('enriched_content','')))"
```

Avoid running this while a user upload is in flight — parse-svc is single-worker, so two
heavy parses serialize and can trip the facade timeout.


## facade-worker (잡 큐 소비자) — 없으면 접수가 전부 503

facade 는 이제 `/parse`·`/chunk`·`/insert`·`/ingest` 를 **직접 처리하지 않는다**. 잡을
`kbp.jobs`(postgres :5433)에 넣고, 별도 프로세스인 facade-worker 가 슬롯 안에서 집어
다운스트림을 호출한다. 유량제어가 여기서 일어난다.

```bash
bash scripts/run-facade-worker.sh
curl -s http://localhost:3000/jobs/workers   # {"online":true,"capacity":4,...}
```

`online:false` 또는 `capacity:0` 이면 worker 가 죽은 것이다. 이 상태에서 접수는
**즉시 503 + Retry-After** 로 거절된다(무한 대기 대신 빨리 실패). 잡이 `queued` 에
머무르면 PID 만 믿지 말고 `GET /jobs/workers` 와 `/tmp/kbp_facade_worker.log` 를 본다.

**프로세스 죽이기 — 포트가 없다.** facade-worker 는 HTTP 포트가 없어 다른 런처들처럼
포트로 스코프할 수 없다. 실측으로 확인한 함정 둘:

* `pkill -f "python -m service.worker"` → **안 맞는다.** Homebrew 파이썬의 실제
  cmdline 은 `/usr/local/Cellar/.../MacOS/Python -m service.worker` 로 **대문자 Python**
  이다. 이것 때문에 옛 worker 가 살아남아 같은 큐를 이중 소비했다.
* `pkill -f "service.worker"` → **너무 넓다.** 정규식에서 `.` 이 아무 문자나 매치해
  VS Code 의 `--service-worker-schemes=...` 까지 잡는다.

런처가 쓰는 정답은 `pgrep -f -- '-m service\.worker'` 이고, PID 파일과 패턴 매치를
**둘 다** 모아 죽인다(고아 정리).

**잡 큐가 사는 곳** — `localhost:5433` → DB `edgequake` → 스키마 `kbp`(`jobs`,
`job_workers`). **kb-backend 는 같은 서버의 다른 DB**(`kb_orchestrator`)라 서로 영향이
없다. 서버만 공유하므로 커넥션 예산은 edgequake 와 나눠 쓴다.

**postgres 는 `docker compose` 가 관리한다는 전제다** — 컨테이너 `kbp-postgres-1`,
named volume `kbp_eq_pg_data`. 컨테이너를 지워도 볼륨에 남으므로 **큐는 재기동을 견딘다**.

`start_dedicated_edgequake.sh` 는 `eq-pg-kbp` 를 볼륨 없이 띄우는 대안 경로라 그쪽에서는
재기동이 곧 큐 소멸이지만, 그 런처는 **"compose 미사용 시에만"** 쓰는 것이고 compose 가
5433 을 잡고 있으면 포트 충돌로 실패한다. 잡 큐는 **compose 전제로만 지원한다**.

진행 중이던 잡 **행**은 어느 경우에도 복구되지 않는다.
