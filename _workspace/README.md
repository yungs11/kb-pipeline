# kb-pipeline _workspace — 통합 문서

> 이 폴더는 흩어져 있던 4개 문서를 **주제별로 합쳐 정리**한 최종본이다.
> 합쳐진 원본: `SoT.v1.md`(방향성 메모), `SoT.md`(SoT v2 실행 스펙), `kb-stage-monitoring.md`(타이밍 모니터링 plan), `dev_wiki.md`(KV GIN 인덱스 결정).
> 권위 출처(코드 사실)는 `docs/kb-pipeline-process-definition.md`(프로세스정의서 v1.0)와 기동 런처 `service/scripts/start_dedicated_edgequake.sh`, facade `service/app.py`, parse-svc `parse_service/app.py`, `kb_pipeline/*` 모듈이다.

## 한 줄 정의

업로드 문서 1건을 **Parse → Blockify → Modal Enrich → Chunking → Insert → Community → Search** 단계로 흘려, 단일 Postgres(pgvector+AGE)에 적재·검색 가능 상태로 만드는 지식베이스 적재 파이프라인.

## 문서 구성

| 파일 | 주제 | 합쳐진 원본 |
|------|------|-------------|
| [`01-architecture.md`](01-architecture.md) | **아키텍처** — 전체 흐름 + 컴포넌트별(facade / parser / chunker / edgequake) 설계, 저장소·RLS, 데이터 계약 | SoT.md §1–5, SoT.v1.md, process-definition §2–5 |
| [`02-changes.md`](02-changes.md) | **변경내용** — v1→v2 결정 변천, edgequake fork 버그수정 3건, KV GIN 인덱스 결정, 정정 사실 | SoT.md §0/§11, dev_wiki.md, SoT.v1.md |
| [`03-dev-progress.md`](03-dev-progress.md) | **개발진행사항** — W0~W6 작업항목 상태, E2E 검증, 타이밍 모니터링 plan, 리스크/협의사항 | SoT.md §6/§8/§10/§11, kb-stage-monitoring.md, process-definition §6 |

## 핵심 아키텍처 결정 (ADR)

**경로 (나′): edgequake 를 베이스 엔진으로 두고, 앞단(파싱·블록화·모달서술·청킹)만 커스텀으로 만들어 edgequake 에 청크를 주입한다.**

- edgequake 는 단일 Postgres(pgvector+AGE)에 **추출·임베딩·그래프·커뮤니티·검색 + 진짜 RLS 멀티테넌시**를 이미 갖춘 완결 엔진이다. 재구현하지 않는다.
- 청킹과 모달 원자성은 **facade `/chunk` 가 소유**한다. 전용 edgequake 는 `EDGEQUAKE_CHUNKER=passthrough` 로 띄워 facade 청크를 경계 그대로 저장한다(재청킹 금지).

**기각된 대안**: (가) markdown 만 edgequake API 에 POST → 재청킹으로 adaptive_chunk 사장(MVP 폴백으로만 보존), (다) 순수 자체 스택 → 비용 과다, (B) LightRAG + Qdrant/Memgraph → RLS 상실 + 커뮤니티 리포트 기본 미제공.

## 시스템 구성 (포트)

| 구성요소 | 포트 | 한 줄 정의 |
|----------|------|-----------|
| facade (kb-pipeline) | 19000 | 오케스트레이터. parse→chunk→insert→search 노출, 청킹·모달원자성 소유 (`service/app.py`) |
| parse-svc | 19001 | 비-Excel 문서 파싱 + modal LLM 서술 → enriched_content + 모달 마커 (`parse_service/app.py`) |
| adaptive_chunk | 18060 | 청킹 허브. atomic_markers 받아 모달 원자 보존, 텍스트 갭만 4방법 경쟁 |
| edgequake | 8081 | 베이스 엔진. passthrough 적재 + 추출/임베딩/AGE 그래프/검색 |
| postgres (eq-pg-kbp) | 5433 | 단일 저장소. pgvector + Apache AGE |
| 임베딩(bge-m3) | — | OpenAI-호환, 1024d. 현행 배선=원격 litellm(`https://litellm.ax-demo.com/v1`) |
| ocr | 18050 | 이미지/스캔 PDF VLM/OCR. content + elements[] |
| excel-parser | 18055 | Excel 전용. LLM 없이 parse+chunk (native chunks) |
| kb-backend (knowledge_base) | 8088 | 소비자/집계자. facade 호출 + IngestionJob 추적·영속 |

## 불변식

- **청크는 KB당 단일 우주**: 하나의 enriched content → 하나의 청크 집합 → 그 위에서 벡터·그래프가 동일 `source_id` 로 묶인다. 벡터용/그래프용 청크를 분리하지 않는다.
- **마크다운 + inline HTML 중간표현**: 표는 절대 pipe 로 납작화하지 않고 `<table>` HTML(colspan/rowspan) 보존.
- **단일 Postgres + per-KB RLS**: 별도 스키마가 아니라 공유테이블 + tenant_id/workspace_id 컬럼 + RLS.
- **BGE-M3 1024d 통일**: 청킹·적재·검색 세 구간 단일 모델/차원.
