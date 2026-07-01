# compose 스택 스모크 검증 (Phase 1 도커화)

플랜: `docs/superpowers/plans/2026-07-01-engine-stack-dockerization-phase1.md`
검증일: 2026-07-01 · compose 프로젝트명: `kbp`

## 요약 — ✅ 전 구간 통과

| 구간 | 상태 |
|---|---|
| 전 서비스 빌드 + `docker compose up -d --wait` | ✅ 11/11 healthy |
| 게이트 회귀 (excel-parser + doc_guard) | ✅ 통과 |
| 워커 수 불변식 | ✅ 확인 |
| **E2E: facade ingest→parse→chunk→insert→search** | ✅ **통과** |

## 1. 전 서비스 healthy (`docker compose up -d --wait` → exit 0)

postgres · redis · minio · gotenberg · edgequake · document-parser · excel-parser · doc_guard · adaptive_chunk · parse-svc · facade — **11/11 healthy**.

> redis 는 플랜 원본 서비스 목록에 없었으나 document-parser 가 VL 모델설정을 redis 에 저장(`core/config/model_profiles.py`)하고, redis 부재 시 `/health` 가 hang → parse-svc/facade 데드락. bring-up 중 발견해 compose 에 추가(`46309fb`). document-parser env `REDIS_HOST=redis`.

## 2. 게이트 회귀 — 도커 컨테이너 대상 (Task 9 Step 1)

| 파일 | 기대 | 실제 | 판정 |
|---|---|---|---|
| 신한자산신탁_외부테이터_필요사이트 정리.xlsx | gate ok=False | False | ✅ |
| 251210_..._WBS_v0.1_sys.xlsx | gate ok=False | False | ✅ |
| 2-1. 위임전결기준표(2026.04.17. 개정).xlsx | gate ok=True | True | ✅ |
| doc_guard `/v1/check-excel` (정상 gate_summary) | result=pass | pass | ✅ |

## 3. 워커 수 불변식 (Task 9 Step 3, `docker top`)

| 서비스 | gunicorn 프로세스 | 기대 | 판정 |
|---|---|---|---|
| parse-svc | 5 | 마스터+4 | ✅ |
| facade | 3 | 마스터+2 | ✅ |
| adaptive_chunk | 2 | 마스터+**1** (in-process job store) | ✅ |
| excel-parser | 2 | 마스터+**1** (in-process job store) | ✅ |
| document-parser | 4 | 마스터+3 | ✅ |
| doc_guard | 3 | 마스터+2 | ✅ |

## 4. E2E — facade ingest → search (Task 9 Step 2)

**POST /ingest** (소형 텍스트 문서, workspace_id=smoke-compose-001):
```
status: "indexed"   chunk_count: 1
chunking_selection.method_selected: "recursive_1100"
scores: sc=1.0 icc=0.827 dcc=1.0 rc=1.0 avg=0.957   (adaptive_chunk 스코어링 정상 — litellm 임베딩 동작)
```
→ facade → parse-svc(파싱) → adaptive_chunk(청킹+스코어) → edgequake(insert/index) 전 구간 동작.

**POST /search** ("BGE-M3 임베딩 차원과 edgequake 청커 모드는?"):
```
results[0]: chunk_id=<doc>-chunk-0  score=0.986  (적재한 청크 정확 검색)
+ 그래프 엔티티 추출 확인: EDGEQUAKE("passthrough chunker") 등 (edgequake LLM 그래프 추출 동작)
answer: 생성됨
```
→ 벡터 검색 + 그래프 추출 + RAG 답변 전부 동작. **실 시크릿(OpenRouter qwen LLM, litellm bge-m3 1024d)으로 검증.**

## 5. 운영 메모

- 호스트 포트 충돌(다른 스택: trust-backend :18050, docker-minio :19010/11)은 `docker-compose.override.yml`(로컬 전용, gitignore)로 minio→19020/21, document-parser→18051 재매핑해 공존.
- 기동: `git submodule update --init --recursive edgequake` → `.env`(실 시크릿) → `docker compose up -d --wait`. 상세는 `docs/kbp-docker-startup.md`.
