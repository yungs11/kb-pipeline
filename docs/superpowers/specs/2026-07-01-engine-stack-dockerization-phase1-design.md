# kb_pipeline 엔진 스택 도커화 + compose 통일 (Phase 1) — 설계 스펙

작성 2026-07-01 · 상태: 설계(브레인스토밍 완료) → 구현계획 단계

## 0. 한 줄 요약
kb_pipeline provider 엔진측 서비스 전부를 **도커 이미지 + 단일 `docker-compose.yml`** 로 통일하고, 파이썬 서비스는 **gunicorn(uvicorn worker) 다중워커**로 띄운다. **코드 병합은 없다**(파서 통합은 Phase 2). "띄울 게 너무 많다"를 배포 통일로 해결한다.

## 1. 목표 / 비범위

### 목표
- 엔진측 서비스(+의존 인프라)를 **compose 한 방(up/down)** 으로 기동. 각자 흩어진 호스트 uvicorn 프로세스 관리 부담 제거.
- 파이썬 FastAPI 서비스 **uvicorn → gunicorn**(`-k uvicorn.workers.UvicornWorker -w N`) — 단일워커 병목 해소(특히 parse-svc 모달 LLM 순차호출).
- 오늘 만든 **엑셀 게이트가 그대로 동작**(회귀 0).

### 비범위 (Phase 2 이후)
- **파서 코드 통합**(excel-parser → parse-svc, document-parser → parse-svc). 파서는 커서 별도 심층분석 phase.
- **kb-backend(knowledge_base :8088) / frontend** — 범위 밖(다른 provider도 쓰는 테스트 소비자; facade가 provider로 제공).
- Gotenberg/원격 VL/litellm/OpenRouter 자체 — 외부 유지(compose는 URL·또는 gotenberg만 포함).

## 2. compose 대상 서비스

| 서비스 | 포트 | 이미지 | Dockerfile |
|---|---|---|---|
| postgres (eq-pg-kbp) | 5433 | `ghcr.io/raphaelmansuy/edgequake-postgres` | 공식 |
| **edgequake** | 8081 | 신규 빌드 | **신규**(멀티스테이지 Rust) |
| adaptive_chunk | 18060 | 신규 빌드 | **신규**(python) |
| excel-parser | 18055 | 신규 빌드 | **신규**(python + node/kordoc) |
| doc_guard | 8000 | 신규 빌드 | **기존** `doc_guard/Dockerfile` |
| parse-svc | 19001 | 신규 빌드 | **신규**(python + **java17**/OpenDataLoader) |
| facade | 19000 | 신규 빌드 | **신규**(python) |
| document-parser | 18050 | 신규 빌드 | **기존** `.../document-parser-backend-src/Dockerfile` |
| gotenberg | 3000 | `gotenberg/gotenberg:8` | 공식 |
| minio | 19010/19011 | `minio/minio`(현행 pgsty/minio) | 공식 |

- **런타임 주의**: parse-svc = python+**JRE17**(OpenDataLoader), excel-parser = python+**node/kordoc**. facade/doc_guard/adaptive_chunk = python only. document-parser = python+fitz(+gotenberg 호출). GPU 없음.
- **위치**: `8.kb-pipeline/docker-compose.yml` + `8.kb-pipeline/docker/Dockerfile.<svc>`. 타 레포 서비스는 compose `build.context` 를 상대경로(`../../7.excel-parser` 등)로 가리키거나, 각 레포에 Dockerfile 배치 후 context 지정.

## 3. gunicorn 배선
파이썬 6종은 `gunicorn -k uvicorn.workers.UvicornWorker -w <N> -b 0.0.0.0:<port> <app>` 로 기동. 워커 수 기준(전부 I/O 바운드 — LLM/VL/HTTP 대기):

| 서비스 | 워커 | 근거 |
|---|---|---|
| parse-svc | 4 | 모달 LLM 표당 순차 → 문서 동시성 확보(단일워커 폐기) |
| adaptive_chunk | 3 | 청킹+임베딩/리랭크 대기 |
| document-parser | 3 | VL API 대기 |
| facade | 2 | 오케스트레이션(대부분 하위서비스 대기) |
| excel-parser | 2 | CPU 파싱(openpyxl) — 과다워커시 메모리↑ 주의 |
| doc_guard | 2 | 순수 판정(가벼움) |

- ⚠️ 워커별 메모리 배수 주의(16GB 개발환경). timeout(gunicorn `--timeout`)은 모달/VL 장시간(≥1800s) 허용.
- 잡큐/타이밍 등 startup 1회 상태는 워커별 중복 초기화 점검(대개 무상태라 안전).

## 4. 네트워킹 / env
- compose 네트워크에서 **서비스명 DNS**로 상호참조. 호스트 `localhost:PORT` → `http://<svc>:PORT` 로 교체:
  - facade: `KBP_PARSE_SVC_URL=http://parse-svc:19001`, `KBP_ADAPTIVE_CHUNK_URL=http://adaptive_chunk:18060`, `KBP_EDGEQUAKE_URL=http://edgequake:8081`, `KBP_PG_DSN=...@postgres:5432/...`(컨테이너 내부 5432)
  - parse-svc: `KBP_OCR_URL=http://document-parser:8000`(컨테이너 내부 포트), `KBP_EXCEL_URL=http://excel-parser:18055`
  - document-parser: `GOTENBERG_URL=http://gotenberg:3000`, `MINIO_ENDPOINT=minio:9000`, `MODEL_API_URL=<원격 VL 유지>`
  - edgequake: `DATABASE_URL=postgres://edgequake:...@postgres:5432/edgequake`, 임베딩=원격 litellm 유지, LLM=OpenRouter 유지
- **외부 유지 URL**(교체 안 함): 원격 litellm(bge-m3), OpenRouter(qwen), 원격 VL(`MODEL_API_URL`).
- **env 파일**: compose 루트 `.env`(시크릿, **gitignore**) + `.env.example`(커밋). 서비스별 `env_file:` 로 주입. 기존 `scripts/facade.env`·`parse-svc.env`·각 레포 `.env` 키를 이관·정리.
- **포트 노출**: 호스트 디버깅 위해 필요한 것만 `ports:`(facade 19000, edgequake 8081 등). 내부 전용은 노출 생략 가능.

## 5. 기동 순서 (depends_on + healthcheck)
```
postgres(healthy) ─▶ edgequake(healthy) ─┐
gotenberg, minio ─▶ document-parser ─────┤
adaptive_chunk, excel-parser, doc_guard ─┼─▶ parse-svc ─▶ facade
```
- 각 서비스 `healthcheck:`(위 §3 헬스 엔드포인트: `/healthz`, edgequake `/health`, kb-backend는 범위밖).
- `depends_on: condition: service_healthy` 로 순서 보장. facade 는 parse-svc·doc_guard·adaptive_chunk·edgequake 가 healthy 후.

## 6. edgequake 도커화 (신규 Dockerfile)
- 멀티스테이지: `rust:<ver>` 빌더에서 `cargo build --release --bin edgequake`(소스=submodule `edgequake/edgequake/`) → 슬림 런타임(`debian:slim`)에 바이너리+마이그레이션 복사.
- env: `EDGEQUAKE_CHUNKER=passthrough`(불변식), `EDGEQUAKE_PORT=8081`, `DATABASE_URL=...@postgres:5432/edgequake`, LLM=openrouter qwen, 임베딩=litellm bge-m3 1024d.
- **⚠️ postgres 데이터 영속**: 현재 launcher 는 컨테이너 재생성으로 **DB 소거**([[edgequake-launcher-wipes-pg]] 메모리). compose 는 **named volume**(`eq_pg_data:/var/lib/postgresql/data`)로 영속화하고, edgequake 기동은 **바이너리-온리**(마이그레이션 idempotent 적용, DB 초기화 금지).

## 7. 마이그레이션 (호스트 런처 → compose)
- 기본 기동 = `docker compose up -d`. 개별 재기동 = `docker compose up -d --build <svc>`.
- 기존 `scripts/run-*.sh`·`restart-gate-stack.sh`·`adaptive_chunk/restart.sh` 는 **보조로 유지**(호스트 단일 서비스 빠른 재기동/디버깅용). `restart-kbp-stack` 스킬에 compose 경로를 1순위로 문서화.
- frontend/kb-backend 는 여전히 호스트(범위 밖) — `BACKEND_ORIGIN`·`kb_pipeline_base_url` 이 compose 의 facade(:19000 노출)를 가리키게 확인.

## 8. 데이터 흐름/불변식 (Phase 1 에서 보존)
- 청킹·모달원자성 = facade `/chunk` 소유, edgequake `EDGEQUAKE_CHUNKER=passthrough`(재청킹 금지).
- 모달 마커 U+3008/U+3009 byte-identical · 표 `<table>` HTML 보존 · 단일 Postgres+per-KB RLS · BGE-M3 1024d.
- 엑셀 게이트(Phase1 parse_preview / kb-backend 측)는 compose 의 excel-parser·doc_guard 를 호출 — **동작 불변**.

## 9. 테스트 / 검증
1. `docker compose up -d` → 전 서비스 healthy(§5 헬스).
2. **게이트 회귀**: excel-parser `/parse`(compose 내) → 법령리스트 `gate_summary.ok=False`, aws `ok=True`. doc_guard `/v1/check-excel` pass/fail.
3. **E2E 스모크**: facade `/ingest`(소형 문서) → parse→chunk→insert 성공, edgequake 조회. facade `/search` 응답.
4. **격리**: kb-backend/frontend(호스트)에서 provider=kb_pipeline KB 업로드 → compose facade 경유 적재·검색 정상.
5. gunicorn 다중워커에서 동시 2건 업로드 → 직렬화 없이 병렬 진행(parse-svc 단일워커 병목 해소 확인).

## 10. 리스크
- **parse-svc 멀티런타임 이미지**(python+JRE17): 이미지 큼·빌드 김 → 멀티스테이지·레이어 캐시. java 스텁 아닌 openjdk@17 확인(현행 함정).
- **edgequake Rust 빌드**: 최초 빌드 오래(캐시 활용). postgres named volume 영속 필수(소거 방지).
- **워커×메모리**(16GB): 워커 수 보수적 시작 후 조정.
- **네트워킹 전환**(localhost→서비스명): 하드코딩된 localhost URL 누락 점검(특히 document-parser 내부 MinIO/gotenberg).
- **시크릿 이관**: 여러 `.env` → compose `.env` 통합 시 키 누락 점검.

## 11. Phase 2 예고 (비범위, 참고)
- excel-parser → parse-svc `excel` 패키지 + dify 엑셀레인도 parse-svc 호출로.
- document-parser 24k LOC 중 **kb_pipeline 실사용 표면만**(스캔/이미지 VL) parse-svc `ocr` 패키지로. pptx/docx 미전송 결정으로 표면 축소.
- 각 파서 계약·의존(java/node/fitz)·테스트 이식은 별도 스펙.
