# 인수인계 — knowledge_base (provider = kb_pipeline) 운영 가이드

> 목적: 내가 없어도 **knowledge_base 에서 provider=`kb_pipeline` 로 KB 를 만들어 문서 적재·검색이
> 정확히 동작**하게 만드는 데 필요한 서비스들의 **용도·기동순서·검증·자주 나는 오류**와,
> **소스를 어떻게 정리해 넘길지**를 정리한다.
>
> 작성: 2026-06-30. 권위 출처(코드 사실)는 `_workspace/`(통합문서), `docs/kb-pipeline-process-definition.md`,
> 각 런처 `8.kb-pipeline/scripts/*.sh` + `service/scripts/start_dedicated_edgequake.sh`.

---

## 0. 한눈에 — 무엇이 무엇을 부르나

```
[브라우저] → frontend(:4000) ──BACKEND_ORIGIN──▶ kb-backend(:8088, knowledge_base 오케스트레이터)
                                                  │  provider == "kb_pipeline" 2단계 흐름
  Phase1 parse_preview ─┬─ (xlsx면) 엑셀 게이트: excel-parser(:18055) → doc_guard(:8000, /v1/check-excel)
                        └─ facade(:19000) /parse ─ parse-svc(:19001) [비엑셀+모달LLM] · OCR(:18050) · excel-parser(:18055)
  Phase2 ingest ─ facade(:19000) /chunk ─ adaptive_chunk(:18060)
                  facade(:19000) /insert ─ edgequake(:8081, passthrough) ─ postgres eq-pg-kbp(:5433, pgvector+AGE) + 임베딩 bge-m3(원격 litellm)
                  (적재 성공 후) facade /communities/build ─ edgequake (OpenRouter qwen)
  검색 ─ kb-backend → facade /search ─ edgequake
```

**핵심**: kb_pipeline 의 벡터·그래프는 **edgequake 전용 postgres(:5433)** 에 들어간다(Qdrant 아님 — Qdrant 는
dify/raganything 등 다른 provider 용). 청킹·모달원자성은 **facade `/chunk`** 가 소유하고, 전용 edgequake 는
반드시 `EDGEQUAKE_CHUNKER=passthrough` 로 띄운다(재청킹 금지).

---

## 1. 서비스 인벤토리

| # | 서비스 | 포트 | 레포 / 위치 | 브랜치 | 용도 | 기동 |
|---|---|---|---|---|---|---|
| A | **postgres (eq-pg-kbp)** | 5433 | docker `ghcr.io/raphaelmansuy/edgequake-postgres` | — | kb_pipeline 단일 저장소(pgvector+AGE). named volume `eq_pg_data`(데이터 보존) | compose `kbp` 스택 또는 edgequake 런처 |
| B | **edgequake (전용)** | 8081 | `8.kb-pipeline/edgequake` (submodule) | feat/kb-pipeline-provider | 베이스 엔진: passthrough 적재 + 추출/임베딩/AGE 그래프/커뮤니티/검색 + per-KB RLS | compose `kbp` 스택 (첫 빌드 ~9-10분, Rust 캐시 후 빠름) |
| C | **OCR / document-parser** | 18050 | `8.kb-pipeline` 내 `Dockerfile.aws` 참조 | — | 이미지·스캔 PDF VLM/OCR | compose `kbp` 스택 (`document-parser` 서비스) |
| D | **임베딩 bge-m3** | (원격) | 원격 litellm `https://litellm.ax-demo.com/v1` | — | 1024d 임베딩(청킹 채점·적재·검색 공통) | 원격 — 키만 필요 |
| E | **adaptive_chunk** | 18060 | `99.projects/adaptive_chunk` | feat/adaptive-chunk-metric-weighting | 청킹 허브(atomic_markers 보존, 텍스트 갭 4방법 경쟁) | compose `kbp` 스택 또는 repo `./restart.sh` |
| F | **excel-parser** | 18055 | `7.excel-parser` | feat/excel-gate | 엑셀 파싱(LLM無) + **게이트 요약 `stats.gate_summary`** 산출 | compose `kbp` 스택 또는 `scripts/run-excel-parser.sh` |
| G | **doc_guard** | 8000 (호스트: 8001) | `99.projects/shinhan_trust/doc_guard` | feat/excel-gate | 엑셀 게이트 판정·한국어 메시지(`POST /v1/check-excel`) | compose `kbp` 스택 또는 `scripts/run-doc-guard.sh` |
| H | **parse-svc** | 19001 | `8.kb-pipeline/parse_service` | feat/kb-pipeline-provider | 비엑셀 파싱 + 모달 LLM 서술 → enriched_content + 모달마커 | compose `kbp` 스택 또는 `scripts/run-parse-svc.sh` |
| I | **facade (kb-pipeline)** | 19000 | `8.kb-pipeline/service` | feat/kb-pipeline-provider | 오케스트레이터: `/parse`·`/chunk`·`/insert`·`/search`·`/communities/build` | compose `kbp` 스택 또는 `scripts/run-facade.sh` |
| J | **kb-backend (knowledge_base)** | 8088 | `99.projects/shinhan_trust/knowledge_base/backend` | feat/kb-pipeline-provider | 소비자/집계자: 업로드·잡추적·provider 분기(kb_pipeline tail)·게이트 | `8.kb-pipeline/scripts/run-kb-backend.sh` (**compose 범위 외**) |
| K | **frontend (knowledge_base)** | 4000(prod)/4001(dev) | `99.projects/shinhan_trust/knowledge_base/frontend` | feat/kb-pipeline-provider | UI(업로드·프로세스 단계·게이트 팝업·문서상세) | `next dev`/`next build && start` (**compose 범위 외**) |

### 부수 인프라(별도 기동, knowledge_base 가 의존)
- **kb-backend 자체 postgres** (`DATABASE_URL`, 잡/문서 메타) · **Redis** (`REDIS_URL`, arq 2단계 잡큐) · **MinIO**
  (원본 파일 staging) — knowledge_base 의 `.env` 가 가리킨다. **Qdrant** 는 dify/raganything/ragflow 용이며
  kb_pipeline 적재엔 직접 안 쓰이나 클라이언트가 빌드되므로 URL 은 살아있어야 부팅이 깔끔하다.
- **OpenRouter** (`OPENROUTER_API_KEY`, 모달·추출·질의·커뮤니티 LLM = qwen/qwen3.5-122b-a10b) · **litellm**
  (`LITELLM_API_KEY`, bge-m3 임베딩).

---

## 2. 기동 순서

> 원칙: **부르는 쪽보다 불리는 쪽을 먼저** 띄운다. 저장소·엔진 → 파싱/청킹/게이트 → facade → kb-backend → frontend.

### 2-A. Docker Compose 기동 (1순위 — 권장)

`docker-compose.yml` (project name `kbp`) 이 postgres·edgequake·document-parser·adaptive_chunk·excel-parser·doc_guard·parse-svc·facade·minio·gotenberg 10개 서비스를 의존 순서(healthcheck 기반)로 함께 올린다.

**전제 조건**

```bash
# (a) edgequake submodule 초기화 (클론 직후 또는 submodule 업데이트 시 1회)
cd /Users/xxx/workspace/8.kb-pipeline
git submodule update --init --recursive edgequake
#   edgequake/ 아래 Rust 소스가 채워진다 — 이게 없으면 docker build 실패.

# (b) .env 생성 (실값 채우기)
cp -n .env.example .env
# .env 를 열어 OPENROUTER_API_KEY, LITELLM_API_KEY, KBP_OPENAI_API_KEY 등 실값 입력.
# (파일이 이미 있으면 cp -n 은 덮어쓰지 않는다.)
```

**기동**

```bash
cd /Users/xxx/workspace/8.kb-pipeline

# 전체 스택 기동 (첫 실행 시 edgequake Rust 빌드 ~9-10분, 이후 캐시로 빠름)
docker compose up -d --build

# 상태 확인
docker compose ps
```

**공개 호스트 포트 (localhost → 컨테이너)**

| 서비스 | 호스트 포트 | 비고 |
|---|---|---|
| facade | **19000** | `kb_pipeline_base_url` / `KBP_EDGEQUAKE_URL` 의 기준 포트 |
| parse-svc | 19001 | |
| edgequake | 8081 | |
| excel-parser | 18055 | |
| doc_guard | **8001** → 컨테이너 8000 | kb-backend 설정에서 `DOCGUARD_BASE_URL=http://localhost:8001` |
| adaptive_chunk | 18060 | |
| document-parser (OCR) | 18050 → 컨테이너 8000 | |
| postgres (eq-pg-kbp) | 5433 → 컨테이너 5432 | named volume `eq_pg_data` |
| minio API | 19010 → 컨테이너 9000 | |
| minio Console | 19011 → 컨테이너 9001 | |
| gotenberg | (내부 전용, 3000) | compose 내부 DNS만 사용 |

> **컨테이너 간 DNS**: 서비스명이 곧 호스트명이다(예: facade→parse-svc는 `http://parse-svc:19001`). `localhost`가 아님에 주의.

**frontend / kb-backend 연결 확인**

compose facade 는 호스트 `:19000` 으로 노출된다. 아래 두 곳이 이 포트를 가리키는지 확인한다.

```bash
# knowledge_base backend .env
grep kb_pipeline_base_url 99.projects/shinhan_trust/knowledge_base/backend/.env
# → kb_pipeline_base_url=http://localhost:19000

# knowledge_base frontend .env.local
grep BACKEND_ORIGIN 99.projects/shinhan_trust/knowledge_base/frontend/.env.local
# → BACKEND_ORIGIN=http://localhost:8088  (kb-backend — compose 범위 외, 변경 불필요)
```

**주의: 포트 충돌**

compose 스택이 올리는 포트(특히 18050·18060·19010) 는 다른 프로젝트 스택이 이미 사용 중일 수 있다. 기동 전 점유 여부 확인:

```bash
for p in 5433 8081 8001 18050 18055 18060 19000 19001 19010 19011; do
  printf ":%s " $p; lsof -ti:$p >/dev/null 2>&1 && echo BUSY || echo free
done
```

BUSY 포트가 있으면 해당 서비스를 먼저 내리거나, compose `ports` 매핑을 조정한다.

**단일 서비스만 재빌드/재기동**

```bash
docker compose up -d --build <서비스명>
# 예: docker compose up -d --build facade
# 예: docker compose up -d --build parse-svc edgequake
```

**검증 상태**: gate regression(doc_guard `/v1/check-excel` + excel-parser `stats.gate_summary`) 및 개별 서비스 health 는 확인됨. 전체 E2E(업로드→파싱→청킹→적재→검색) 는 사용자 환경에서 추가 검증 필요.

---

### 2-B. 개별 호스트 스크립트 기동 (2순위 — 단일 서비스 디버그·재기동 시)

compose 를 쓰지 않거나 특정 서비스만 호스트에서 실행할 때 사용한다. **kb-backend 와 frontend 는 항상 이 경로** (compose 범위 외).

```bash
# 0) 인프라 확인 (compose 또는 별도 스택이 떠 있어야 함)
docker ps | grep -E "eq-pg-kbp|document-parser|minio"      # postgres:5433, OCR:18050, minio
#    + kb-backend용 postgres / Redis / Qdrant 가 떠 있는지 (knowledge_base/.env 의 DATABASE_URL/REDIS_URL/QDRANT_URL)

# 1) edgequake(전용) + postgres(:5433)  ── compose 미사용 시에만
bash /Users/xxx/workspace/8.kb-pipeline/service/scripts/start_dedicated_edgequake.sh
#    EDGEQUAKE_CHUNKER=passthrough 고정. 임베딩=litellm bge-m3, LLM=openrouter qwen.

# 2) 청킹 + 게이트 + 파싱 서비스  ── compose 미사용 시에만
bash /Users/xxx/workspace/99.projects/adaptive_chunk/restart.sh          # adaptive_chunk :18060
bash /Users/xxx/workspace/8.kb-pipeline/scripts/run-excel-parser.sh      # excel-parser :18055 (KORDOC_BIN+node 자동)
bash /Users/xxx/workspace/8.kb-pipeline/scripts/run-doc-guard.sh         # doc_guard :8000
bash /Users/xxx/workspace/8.kb-pipeline/scripts/run-parse-svc.sh         # parse-svc :19001 (openjdk@17 필요)

# 3) facade  ── compose 미사용 시에만
bash /Users/xxx/workspace/8.kb-pipeline/scripts/run-facade.sh            # facade :19000

# 4) kb-backend (항상 호스트 — compose 범위 외)
bash /Users/xxx/workspace/8.kb-pipeline/scripts/run-kb-backend.sh        # kb-backend :8088

# 5) frontend (항상 호스트 — compose 범위 외)
cd /Users/xxx/workspace/99.projects/shinhan_trust/knowledge_base/frontend
#    .env.local 의 BACKEND_ORIGIN 이 kb-backend(:8088)를 가리키는지 확인
npm run dev    # 개발(:4001)  또는  npm run build && npm run start  # prod(:4000)
```

> **엑셀 게이트만 다시 띄우려면** (게이트 코드 바꿨거나 옛 차단이 안 풀릴 때):
> `bash /Users/xxx/workspace/8.kb-pipeline/scripts/restart-gate-stack.sh`
> (doc_guard + excel-parser → kb-backend 를 의존순서로, 각자 새 코드 응답까지 검증)

---

## 3. 헬스 체크 (전부 한 번에)

```bash
echo "edgequake   :8081  $(curl -s -m3 localhost:8081/health 2>/dev/null | head -c40)"
echo "adaptive    :18060 $(curl -s -m3 localhost:18060/healthz 2>/dev/null)"
echo "excel-parser:18055 $(curl -s -m3 localhost:18055/healthz 2>/dev/null)"
echo "doc_guard   :8000  $(curl -s -m3 localhost:8000/healthz 2>/dev/null)"
echo "parse-svc   :19001 $(curl -s -m3 localhost:19001/healthz 2>/dev/null)"
echo "facade      :19000 $(curl -s -m3 localhost:19000/healthz 2>/dev/null | head -c60)"
echo "kb-backend  :8088  $(curl -s -m3 -o /dev/null -w '%{http_code}' localhost:8088/openapi.json)"
echo "OCR         :18050 $(curl -s -m3 -o /dev/null -w '%{http_code}' localhost:18050/health 2>/dev/null)"
# postgres(:5433): docker ps | grep eq-pg-kbp
```
포트 점유만 빠르게: `for p in 5433 8081 18060 18055 8000 19001 19000 8088 18050; do printf ":%s " $p; lsof -ti:$p >/dev/null 2>&1 && echo UP || echo DOWN; done`

---

## 4. 엑셀 게이트 (이번에 새로 만든 기능)

**무엇**: provider=`kb_pipeline` + 엑셀(xlsx/xlsm) 업로드 시, **추출이 실제로 깨지는 경우만** 차단하고
사용자에게 위치·사유를 알려 엑셀을 고치게 한다. (기존 doc_guard 13규칙 전면 제거.)

- 위치: kb-backend **Phase1 `parse_preview_task`** (UI 가 실제로 거치는 경로). Phase2 는 `pre_parsed` 있으면 재게이트 안 함.
- 산출: excel-parser `/parse` → `stats.gate_summary{ok, sheets:[{sheet,ok,findings:[{code,cells,detail}]}]}`
- 판정: doc_guard `POST /v1/check-excel` → 기존 CheckReport 스키마(프론트 GatePopup 그대로).
- **차단 코드 4종**: `ref_error`(#REF! 등), `header_leak`(헤더가 값으로), `empty_header`(보수적), `side_by_side`(나란히 놓인 두 표 — 인덱스열 중복 OR ≥2 distinct 라벨블록 반복일 때만; 매트릭스·동명컬럼 거짓양성 제외).
- **파일 단위 차단**: 한 시트라도 finding 이면 파일 차단. (비엑셀/비-kb_pipeline 은 게이트 자체가 안 돈다.)
- 설계/계획: `docs/superpowers/specs/2026-06-29-excel-gate-postparse-design.md`, `docs/superpowers/plans/2026-06-29-excel-gate-postparse.md`.

---

## 5. 자주 나는 오류 & 처방 (이번 세션 실측)

| 증상 | 원인 | 처방 |
|---|---|---|
| 코드 고쳤는데 **옛 차단게이트가 계속 뜸** | 서비스가 **옛 코드 프로세스로 떠 있음**(재기동 안 됨) | `restart-gate-stack.sh`. 특히 kb-backend(:8088). |
| 재기동했는데도 옛 동작 | 옛 런처 `pkill -f` 패턴이 `--host` 끼임으로 **매치 실패 → 옛 프로세스 안 죽음** | 런처들은 이제 **포트 기준 kill**(`lsof -ti:PORT`)로 수정됨. 수동 시 `kill $(lsof -ti:8088)`. |
| 업로드는 됐는데 **여전히 옛 게이트** | frontend 가 **다른 백엔드**를 봄 | `knowledge_base/frontend/.env.local` 의 `BACKEND_ORIGIN`(현재 :8088) 확인 + 브라우저 하드 새로고침. |
| **facade `/chunk` 500** | **adaptive_chunk(:18060) down** | `bash 99.projects/adaptive_chunk/restart.sh`. |
| excel-parser `/parse` 500 `*.md 를 찾을 수 없습니다` | `auto`→kordoc 인데 **KORDOC_BIN/node PATH 없음** → gate_summary 미생성(게이트 무력화) | `run-excel-parser.sh` 가 kordoc 경로 자동 주입. 수동 시 `KORDOC_BIN=kordoc` + node bin PATH. |
| parse-svc enriched_content 비어있음 / "Unable to locate a Java Runtime" | OpenDataLoader java 스텁 | `run-parse-svc.sh`(openjdk@17 PATH 핀). |
| facade `httpx.ReadTimeout` | 모달 LLM 표당 순차호출(다중표 PDF 400s+) | 정상 — 타임아웃 1800s. 두 파싱 충돌 주의(parse-svc 단일워커). |
| **service.main:app 광역 kill 사고**(excel-parser·adaptive_chunk 동시 죽음) | 모듈 패턴 kill | **항상 포트 기준 kill**. excel-parser=:18055, adaptive_chunk=:18060. |

---

## 6. 소스 인수인계 — 어떻게 정리해 넘길까

### 6.1 넘길 레포 + 브랜치 (현재 작업본)
| 레포 | 경로(예) | 브랜치 | 비고 |
|---|---|---|---|
| kb-pipeline (facade·parse-svc·런처·edgequake submodule) | `8.kb-pipeline` | `feat/kb-pipeline-provider` | edgequake 는 git submodule(`edgequake/`, feat/kb-pipeline-provider) — **submodule 포함해 클론**(`git clone --recurse-submodules`) |
| excel-parser | `7.excel-parser` | `feat/excel-gate` | 게이트 요약 산출 |
| adaptive_chunk | `99.projects/adaptive_chunk` | `feat/adaptive-chunk-metric-weighting` | 청킹 |
| knowledge_base (backend+frontend) | `99.projects/shinhan_trust/knowledge_base` | `feat/kb-pipeline-provider` | 오케스트레이터+UI |
| doc_guard | `99.projects/shinhan_trust/doc_guard` | `feat/excel-gate` | **이 세션에 git init 함** — 첫 커밋부터 통째로 |

> 권장: 인수 전 각 브랜치를 **main(또는 default)에 머지하거나 PR** 로 만들어 "현재본 = default" 가 되게 한다.
> 지금은 전부 feature 브랜치라 받는 사람이 어느 브랜치인지 헷갈린다. (이번 작업물은 미머지 상태.)

### 6.2 시크릿/환경 (gitignore 라 레포에 없음 — 별도·안전 채널로 전달)
| 파일 | 들어있는 키(예) | 어떻게 |
|---|---|---|
| `8.kb-pipeline/scripts/facade.env` | KBP_OPENAI_API_KEY, KBP_OPENAI_BASE_URL, KBP_PG_DSN, KBP_EDGEQUAKE_URL, KBP_ADAPTIVE_CHUNK_URL, KBP_PARSE_SVC_URL, KBP_LLM_MODEL | 값 채운 실파일을 안전채널로 |
| `8.kb-pipeline/scripts/parse-svc.env` | KBP_OPENAI_API_KEY, KBP_OCR_URL, KBP_EXCEL_URL, MINIO_* | 〃 |
| `knowledge_base/.env` | DATABASE_URL, REDIS_URL, JWT_SECRET, CREDENTIAL_ENCRYPTION_KEY, DOCGUARD_BASE_URL, QDRANT_*, MINIO_*, OPENROUTER_API_KEY, INGEST/EDGEQUAKE_EMBEDDING_MODEL, kb_pipeline_base_url 등 | 〃 (가장 큼) |
| `knowledge_base/frontend/.env.local` | BACKEND_ORIGIN(=http://localhost:8088) | 〃 |
| `99.projects/adaptive_chunk/.env` | ADAPTIVE_CHUNK_OPENROUTER_*, ADAPTIVE_CHUNK_*_EMBEDDING_*, LITELLM_API_KEY, ... | 〃 |
| edgequake 런처용 | `OPENROUTER_API_KEY`, litellm 임베딩 키 | 런처가 env 에서 읽음 — 받는 사람이 export |
| doc_guard | (없음 — config.py 기본값으로 동작) | — |

> 각 레포에 **`.env.example`** 을 동봉해 "어떤 키가 필요한지" 를 명확히 하고, 실값은 별도로.
> (doc_guard 는 `.env.example` 있음. 나머지도 example 정비 권장.)

### 6.3 외부 의존 (받는 머신에 설치/접속 필요)
- **docker** (postgres eq-pg-kbp :5433, OCR document-parser :18050, MinIO) + kb-backend용 postgres/Redis/Qdrant.
- **openjdk@17** (parse-svc OpenDataLoader) — `brew install openjdk@17`, PATH 핀.
- **node + kordoc CLI** (excel-parser auto 백엔드) — `kordoc` 가 PATH 에 있어야(nvm).
- **Python 3.x + 각 repo 의 `.venv`** (uvicorn). 각 레포에서 venv 생성·`pip install -e .`/requirements.
- **OpenRouter 키**(qwen LLM) + **litellm 키**(bge-m3 임베딩) — 잔액/엔드포인트 살아있어야 적재·검색 동작.
- edgequake **fork 바이너리**: `edgequake/edgequake/target/.../edgequake` (submodule 에서 `cargo build --bin edgequake`).

### 6.4 받는 사람 빠른 시작(체크리스트)
1. 5개 레포 클론. **8.kb-pipeline 은 반드시 `git clone --recurse-submodules`** 또는 클론 후 `git submodule update --init --recursive edgequake`.
2. `8.kb-pipeline/.env.example` → `.env` 복사 후 실값(API 키 등) 입력(§6.2 참고).
3. kb-backend/redis/Qdrant 인프라(compose 범위 외) 기동.
4. **`docker compose up -d --build`** (8.kb-pipeline/) — 10개 서비스 일괄 기동. 첫 빌드는 Rust 컴파일로 ~9-10분.
5. kb-backend/frontend 는 호스트에서 기동(§2-B 4~5번).
6. `knowledge_base/backend/.env` 의 `kb_pipeline_base_url=http://localhost:19000` 확인. `DOCGUARD_BASE_URL=http://localhost:8001` 확인.
7. **§3 헬스 체크** 전부 통과 확인.
8. UI(:4000)에서 KB 생성 시 **provider=`kb_pipeline`** 선택 → 문서 업로드 → 파싱→게이트검증→청킹→적재 확인.
9. 막히면 **§5 표**로 진단(대개 "옛 프로세스", "포트 충돌", 또는 "의존 서비스 down").

---

## 7. 참고 문서
- `_workspace/README.md` (인덱스·ADR·포트·불변식), `_workspace/01-architecture.md`(흐름·컴포넌트·저장소·RLS), `_workspace/02-changes.md`(변천; §6 엑셀게이트), `_workspace/03-dev-progress.md`(진행).
- `docs/kb-pipeline-process-definition.md` (프로세스정의서 v1.0 — 코드 레퍼런스 권위 출처).
- 재기동 스킬: `.claude/skills/restart-kbp-stack`(parse-svc·facade·kb-backend·doc_guard·excel-parser·gate-stack), `adaptive-chunk-restart`(:18060).
