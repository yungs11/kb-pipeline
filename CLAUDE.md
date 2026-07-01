# CLAUDE.md — kb-pipeline

## 프로젝트 맥락 (필수 선행 단계)

**사용자 지시가 들어오면 코드를 만지기 전에 반드시 `_workspace/` 에서 관련 맥락을 먼저 검색한다.**

`_workspace/` 는 이 프로젝트의 단일 진실 출처(SoT)를 주제별로 정리한 통합 문서다. 어떤 작업이든(기능 추가·버그 수정·질문·리팩터링) 시작 전에 해당 주제 문서를 읽어 아키텍처 결정·불변식·진행상황을 파악한 뒤 진행한다.

### 워크플로

1. **검색**: 사용자 지시의 키워드(facade / parse / chunk / modal / edgequake / community / search / RLS / 임베딩 / 타이밍 등)로 `_workspace/` 를 검색한다.
   - 우선 `_workspace/README.md`(인덱스 + ADR + 포트/불변식)를 보고 어느 문서가 관련 있는지 판단한다.
2. **정독**: 관련 주제 문서를 읽는다.
   - [`_workspace/01-architecture.md`](_workspace/01-architecture.md) — 전체 흐름 + 컴포넌트별(facade/parser/chunker/edgequake) 설계, 저장소·RLS, 데이터 계약
   - [`_workspace/02-changes.md`](_workspace/02-changes.md) — v1→v2 결정 변천, 청킹 소유권 이전, edgequake fork 버그수정, KV GIN 결정, drift 정정
   - [`_workspace/03-dev-progress.md`](_workspace/03-dev-progress.md) — W0~W6 작업항목 상태, E2E 검증, 타이밍 모니터링 plan, 리스크/협의사항
3. **대조**: 작업이 기존 아키텍처 결정(ADR)·불변식·비범위와 충돌하지 않는지 확인한다. 충돌하면 진행 전에 사용자에게 알린다.
4. **반영**: 작업 완료 후 아키텍처 결정·진행상황·중요한 정정이 바뀌었다면 해당 `_workspace/` 문서를 갱신한다. (코드만 바꾸고 문서를 방치하지 않는다.)

### 절대 어기면 안 되는 불변식 (상세는 01-architecture.md)

- **청크는 KB당 단일 우주** — 하나의 enriched content → 하나의 청크 집합 → 동일 `source_id` 로 벡터·그래프가 묶인다. 벡터용/그래프용 청크 분리 금지.
- **청킹·모달원자성은 facade `/chunk` 가 소유** — 전용 edgequake 는 반드시 `EDGEQUAKE_CHUNKER=passthrough`(재청킹 금지, `adaptive` 로 띄우면 HTTP 422 적재 실패).
- **표는 `<table>` HTML 보존** — pipe 평탄화 금지(colspan/rowspan 손실).
- **단일 Postgres + per-KB RLS** — 별도 스키마 아님(공유테이블 + tenant_id/workspace_id + RLS). Qdrant/Memgraph 분리 금지.
- **BGE-M3 1024d 통일** — 청킹·적재·검색 세 구간 단일 모델/차원.
- **모달 마커 괄호 U+3008/U+3009** — W1 Rust 소비자와 byte-identical.

## todo_list.md 워크플로 (세션 시작 시 필수 점검)

**모든 세션을 시작할 때 `todo_list.md` 를 먼저 읽는다.** 사용자는 여기에 변경/구현을 지시한다. 항목이 있으면 아래 순서로 처리하고, 비어 있으면(미체크 항목 0개) 평소대로 진행한다.

1. **분석**: `todo_list.md` 의 미완료 항목(`[ ]`)을 읽는다. 각 항목이 어떤 컴포넌트(facade/parser/chunker/edgequake/community/search/RLS/모니터링)에 해당하는지 식별하고, 위의 "프로젝트 맥락" 워크플로대로 `_workspace/` 에서 관련 맥락을 검색·정독한다.
2. **구현계획 수립**: 항목별 구현계획을 세운다. 글로벌 룰의 `/plan` 워크플로(plan 파일 버전 헤더 + codex 검증)를 따른다.
3. **codex 검증 (미해결 0건까지 반복)**: `Agent(subagent_type='codex:codex-rescue')` 로 read-only 검증을 의뢰한다. `NEEDS_REVISION` 이면 계획을 고도화하고 버전을 올려 재검증한다. **`READY`(미해결 0건)가 나올 때까지 반복**한 뒤에만 구현에 착수한다.
4. **구현**: 검증 통과한 계획대로 구현한다. 불변식(아래)과 기존 ADR·비범위를 어기지 않는다. TDD/검증 스킬을 활용하고, 완료 주장 전 실제 검증 명령으로 증거를 확인한다.
5. **완료 후 정리**:
   - 완료된 항목을 `todo_list.md` 에서 **삭제**한다(체크만 하지 말고 제거).
   - 변경된 아키텍처 결정·진행상황·정정을 `_workspace/` 의 해당 문서에 반영한다 (아키텍처→`01-architecture.md`, 변경/버그수정/결정→`02-changes.md`, 작업항목 상태/진행→`03-dev-progress.md`, 필요 시 `README.md` 인덱스).
6. **phase 단위 진행 시 중간 반영**: 작업이 여러 phase 로 나뉘면, 각 phase 가 끝날 때마다 `_workspace/03-dev-progress.md` 에 진행상황(완료 phase, 남은 phase, 발견된 리스크)을 **중간중간 갱신**한다. 마지막 phase 까지 미룬 채 문서를 방치하지 않는다.

> 요약 순서: **todo_list 분석 → _workspace 맥락 검색 → 구현계획 → codex 검증(미해결 0건까지) → 구현 → todo_list 삭제 + _workspace 문서 반영 (phase 진행 시 중간 반영)**.

### 권위 출처

`_workspace/` 가 정리본이라면, 코드 사실의 권위 출처는: 기동 런처 `service/scripts/start_dedicated_edgequake.sh`, facade `service/app.py`, parse-svc `parse_service/app.py`, `kb_pipeline/*` 모듈, 프로세스정의서 `docs/kb-pipeline-process-definition.md`. `_workspace/` 와 코드가 어긋나면 코드를 신뢰하고 `_workspace/` 를 갱신한다.
