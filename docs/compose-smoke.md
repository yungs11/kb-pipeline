# compose 스택 스모크 검증 (Phase 1 도커화)

플랜: `docs/superpowers/plans/2026-07-01-engine-stack-dockerization-phase1.md`
검증일: 2026-07-01 · compose 프로젝트명: `kbp`

## 요약

| 구간 | 상태 | 비고 |
|---|---|---|
| 이미지 빌드 (전 10서비스) | ✅ 완료 | Task 1~7 각 `docker build` 성공; Task 8 `docker compose config` OK |
| 게이트 회귀 (excel-parser + doc_guard, **도커라이즈된** 서비스) | ✅ 통과 | 아래 상세 |
| 워커 수 불변식 (excel-parser=-w1, doc_guard=-w2) | ✅ 확인 | `docker top` 기준 |
| E2E (facade ingest→chunk→insert→search) | ⏳ 보류 | 포트 충돌 + 실 시크릿 필요(아래 "남은 검증") |
| parse-svc/adaptive_chunk/edgequake/document-parser 라이브 | ⏳ 보류 | 동일 |

## 1. 게이트 회귀 — 도커 컨테이너 대상 (Task 9 Step 1)

컨테이너: `kbp-excel-parser-1`(:18055, healthy), `kbp-doc_guard-1`(:8001, healthy) — 둘 다 compose 로 기동된 이미지.

| 파일 | 기대 | 실제 | 판정 |
|---|---|---|---|
| 신한자산신탁_외부테이터_필요사이트 정리.xlsx | gate ok=False | **False** | ✅ |
| 251210_..._WBS_v0.1_sys.xlsx | gate ok=False | **False** | ✅ |
| 2-1. 위임전결기준표(2026.04.17. 개정).xlsx | gate ok=True | **True** | ✅ |
| doc_guard `/v1/check-excel` (정상 gate_summary) | result=pass | **pass** | ✅ |

→ 도커라이즈된 excel-parser 의 kordoc 백엔드 + 게이트 로직, doc_guard 판정이 compose 네트워크에서 정상 동작. 오늘 만든 엑셀 게이트 **회귀 0**.

## 2. 워커 수 불변식 (Task 9 Step 3)

`docker top` 기준 (slim 이미지에 `ps` 미포함 → 컨테이너 내 grep 대신 호스트에서 확인):

| 서비스 | gunicorn 프로세스 | 기대(마스터+워커) | 판정 |
|---|---|---|---|
| excel-parser | 2 | 2 (마스터+ **1**) — in-process job store 불변식 | ✅ |
| doc_guard | 3 | 3 (마스터+2) | ✅ |

## 3. 인프라 헬스 (Task 8 검증 시 확인)

`kbp-postgres-1`, `kbp-gotenberg-1`, `kbp-excel-parser-1`, `kbp-doc_guard-1` 모두 `healthy`.
gotenberg/minio 이미지에 `curl` 존재 확인 → 헬스체크 probe 유효.

## 남은 검증 (⏳ 사용자 환경 필요)

전체 스택 `docker compose up -d` 로 E2E(facade `/ingest`→parse→chunk→insert→edgequake 조회, `/search`) 를 돌리려면 두 가지가 선행되어야 한다:

1. **포트 충돌 해소** — 호스트에서 다른 스택이 다음 포트를 이미 점유:
   - `:18050` ← `trust-backend-document-parser-1` (별도 프로젝트)
   - `:19010/:19011` ← `docker-minio-1` (pgsty/minio, 별도 프로젝트)
   - `:18060` ← 호스트 `adaptive_chunk` Python 프로세스
   → 해당 스택/프로세스를 내리거나, compose 의 published 포트를 비충돌 포트로 재매핑해야 함.
2. **실 시크릿** — `.env` 에 OpenRouter/litellm/VL/MinIO 실제 키. E2E 의 LLM/VL 호출에 필요(헬스체크 자체는 불필요).

> 참고: 개별 이미지의 부팅/헬스는 각 Task 에서 검증 완료. 위 E2E 는 "라이브 통합" 확인 단계로, 사용자의 실행 환경에서 수행한다.
