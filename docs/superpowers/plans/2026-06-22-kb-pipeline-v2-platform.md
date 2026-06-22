<!-- plan-version: v4 -->
<!-- codex-validation: PENDING -->

# kb-pipeline v2 — RAG 수집 플랫폼 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development 또는 executing-plans 로 task 단위 구현. 단계는 `- [ ]` 체크박스.

**Goal:** v1 monolith kb_pipeline을 **수집 플랫폼**으로 재구성 — kb-pipeline = 가벼운 facade(은닉·정책·변환·오케스트레이션), 그 뒤에 parse-svc / adaptive_chunk(청킹 허브, marker-aware) / edgequake(passthrough 저장·검색) 은닉. knowledge_base는 facade capability를 단계별 호출해 phase 가시 + 진짜 청킹 선택근거 표시.

**Architecture:** spec `docs/superpowers/specs/2026-06-22-kb-pipeline-v2-decomposition-design.md` 참조(4계층: 소비자 → facade → parse-svc/adaptive_chunk/edgequake).

**Tech Stack:** Python 3.x/FastAPI/httpx/pytest(facade·parse-svc·adaptive_chunk), Rust(edgequake fork), Next.js/React(knowledge_base 프론트), Postgres(pgvector+AGE).

## Global Constraints
- repos/브랜치: `8.kb-pipeline`(facade+parse-svc+edgequake fork, `feat/kb-pipeline-provider`), `99.projects/adaptive_chunk`(git 아님), `99.projects/shinhan_trust/knowledge_base`(`feat/kb-pipeline-provider`).
- 계약: workspace_id=kb_id, doc_id=content_hash[:16], chunk_id=`{document_id}-chunk-{i}`, passthrough 구분자 `\u{001e}`(U+001E RECORD SEPARATOR), modal 마커 `〈MODAL`…`〈/MODAL〉`(U+3008/U+3009).
- 성공 판정: `status terminal-ok(completed/indexed) AND chunk_count>0` — 성공위장 금지.
- 임베딩 1024d `BAAI/bge-m3`(:7997), LLM `qwen/qwen3.5-122b-a10b`(OpenRouter, 키는 `/Users/xxx/workspace/99.projects/rag-edgequake-benchmark/docker/.env`의 OPENAI_API_KEY — **절대 출력 금지**).
- edgequake DATABASE_URL에 `search_path=public` **금지**(AGE 연산자 깨짐).
- facade 규율: 각 엔드포인트는 은닉/정책/변환 가치 추가(단순 forward 금지).
- **knowledge_base = RAG 비교 플랫폼**(provider×chunker 선택→적재·검색 비교). v2 변경은 **ADDITIVE only**: 기존 provider(dify/edgequake/raganything/ragflow)의 적재·검색·**진행단계 표시**·문서상세 동작을 **100% 보존**. kb_pipeline 전용 분기만 추가. kb_pipeline은 dropdown의 **한 peer provider**(특별취급 금지) — provider=kb_pipeline일 때만 tail이 facade를 단계별 호출.
- 호환: adaptive_chunk 기존 호출자(마커 없는 입력)·v1 `/ingest`·다른 provider(dify/edgequake/raganything/ragflow) 동작 보존, 기존 테스트 green.
- 포트: facade :19000, parse-svc :19001(신규), adaptive_chunk :18060, edgequake(passthrough) :8081, bge-m3 :7997, OCR :18050, doc_guard :8000, knowledge_base :8088.

---

## Phase 1 — 백엔드 capability (소비자 변경 전, 플랫폼 조각)

### Task 1.1: adaptive_chunk marker-aware (atomic-region)
**Files:** Modify `99.projects/adaptive_chunk/service/runner.py`(run_chunk), `service/schemas.py`(옵션 `atomic_markers`); Create `99.projects/adaptive_chunk/tests/test_marker_aware.py`. Test venv: adaptive_chunk의 venv.
**Interfaces:** Produces `run_chunk(..., atomic_markers: list[tuple[str,str]] | None)` — 마커 영역 원자 보존 + gap만 방법선택, 조립; 반환 `{method_selected,scores,methods_compared,chunks,timing_ms}` 불변.
- [ ] Step1 실패테스트: `〈MODAL x〉TBL〈/MODAL〉` 포함 입력 → 그 스팬이 **정확히 한 청크**(쪼개짐 0), gap 텍스트는 정상 청킹, 순서 보존; 마커 0개 입력은 기존 결과와 동일(회귀). malformed(미종결) 마커=텍스트.
- [ ] Step2 run: `python -m pytest tests/test_marker_aware.py -q` → FAIL.
- [ ] Step3 구현: `run_chunk` 앞단에 segment(마커 경계 분할) → modal=원자 Chunk, gap→기존 select 경로 → 원래 순서 조립, chunk_index 재부여. 기본 `atomic_markers=[("〈MODAL","〈/MODAL〉")]`.
- [ ] Step4 run → PASS + 기존 스위트 회귀 green.
- [ ] Step5 commit (adaptive_chunk repo는 git 아님 → 변경 파일 목록만 보고, 백업 tar 생략).

### Task 1.2: edgequake PassthroughStrategy + 재빌드
**Files:** Create `8.kb-pipeline/edgequake/edgequake/crates/edgequake-pipeline/src/chunker/passthrough.rs`; Modify `chunker/mod.rs`(export), `crates/edgequake-api/src/workspace_pipeline_factory.rs`(EDGEQUAKE_CHUNKER=passthrough 분기). Test: 해당 crate `#[cfg(test)]`.
**Interfaces:** `PassthroughStrategy: ChunkingStrategy` — `chunk(content,_cfg)`가 `\u{001e}` split → `Vec<ChunkResult>`(빈 제외, chunk_order_index 순서).
- [ ] Step1 실패테스트(Rust): `"a\u{1e}b\u{1e}c"` → 3 ChunkResult(content a/b/c, 순서 0/1/2); 빈 조각 제외; 단일(구분자 없음)=1 청크.
- [ ] Step2 `cargo test -p edgequake-pipeline passthrough` → FAIL.
- [ ] Step3 구현 PassthroughStrategy + factory에 `"passthrough" => Arc::new(PassthroughStrategy)`.
- [ ] Step4 `cargo test -p edgequake-pipeline passthrough` → PASS. `cargo build --bin edgequake` 성공.
- [ ] Step5 commit.

### Task 1.3: facade `/chunk` (→adaptive_chunk, marker 전달 + 선택근거 정규화)
**Files:** Modify `8.kb-pipeline/service/app.py`(POST /chunk); Create `service/adaptive_chunk.py`(httpx 클라이언트), `service/tests/test_chunk_endpoint.py`.
**Interfaces:** `POST /chunk {enriched_content, doc_name}` → `{chunks:[{chunk_index,text,titles_context,pages}], method_selected, scores, methods_compared}`. 내부: adaptive_chunk `/chunk`(atomic_markers=modal) 호출.
- [ ] Step1 실패테스트: monkeypatch adaptive_chunk 클라이언트(고정 응답) → facade `/chunk`가 청크+선택근거를 정규화 반환; modal 마커가 atomic_markers로 전달됨 검증.
- [ ] Step2 `.venv-kb/bin/python -m pytest service/tests/test_chunk_endpoint.py -q` → FAIL.
- [ ] Step3 구현 `service/adaptive_chunk.py`(AdaptiveChunkClient: POST /chunk, 응답 파싱) + app `/chunk`.
- [ ] Step4 → PASS.
- [ ] Step5 commit.

### Task 1.4: facade `/insert` + `/insert/status` (→edgequake passthrough, 정책 일체)
**Files:** Modify `8.kb-pipeline/service/app.py`(POST /insert, GET /insert/status), `service/edgequake.py`(passthrough insert: `insert_chunks(workspace,tenant,title,chunk_texts)`); Create `service/tests/test_insert_endpoint.py`.
**Interfaces:** `POST /insert {workspace_id, doc_id, title, chunks}` → `{document_id, chunk_count, status}`. 내부: ensure_workspace → `\u{001e}` join → submit_document(passthrough) → 폴링. `GET /insert/status` → `{phase, terminal, succeeded, chunk_count}`(document_phase 재사용).
- [ ] Step1 실패테스트: FakeEq(ensure_workspace/submit/document_phase) → `/insert`가 청크를 `\u{001e}` join해 보냈는지 + 성공 반환; `/insert/status`가 phase 중계.
- [ ] Step2 run → FAIL.
- [ ] Step3 구현: edgequake.py에 `insert_chunks`(join+submit), app `/insert`(ensure_workspace→insert_chunks→폴링), `/insert/status`.
- [ ] Step4 → PASS.
- [ ] Step5 commit.

### Task 1.5: facade `/search` (→edgequake `/query`)
**Files:** Modify `service/app.py`(POST /search), `service/edgequake.py`(search); Create `service/tests/test_search_endpoint.py`.
**Interfaces:** `POST /search {workspace_id, query, top_k?}` → `{results:[...]}`. 내부: ensure_workspace → edgequake POST `/api/v1/query`(워크스페이스 헤더).
- [ ] Step1 실패테스트: FakeEq.search 고정 → facade `/search`가 워크스페이스 스코프로 호출 + 결과 정규화.
- [ ] Step2 → FAIL. Step3 구현. Step4 → PASS. Step5 commit.

---

## Phase 2 — parse-svc 분리 + facade 라우팅/오케스트레이션

### Task 2.1: parse-svc 신규 서비스 (run_front 이관)
**Files:** Create `8.kb-pipeline/parse_service/app.py`(POST /parse, /healthz), `parse_service/__init__.py`; Move parse 로직 from `service/ingest.py::run_front`+`service/parsing.py` → parse_service. Create `parse_service/tests/test_parse.py`.
**Interfaces:** `POST /parse (multipart file,filename,content_type?)` → `{enriched_content, n_blocks, modal_spans}`.
- [ ] Step1 실패테스트: monkeypatch 파서/모달 → `/parse`가 enriched_content + modal_spans 반환(보안: `_safe_basename` 유지).
- [ ] Step2 `.venv-kb/bin/python -m pytest parse_service/tests/test_parse.py -q` → FAIL.
- [ ] Step3 구현: parsing.py 이관 + modal enrich + FastAPI app(:19001).
- [ ] Step4 → PASS.
- [ ] Step5 commit.

### Task 2.2: facade `/parse` (→parse-svc)
**Files:** Modify `service/app.py`(POST /parse → parse-svc httpx); Create `service/parse_client.py`, `service/tests/test_parse_endpoint.py`.
- [ ] Step1 실패테스트: monkeypatch parse_client → facade `/parse`가 멀티파트 전달 + enriched 반환.
- [ ] Step2 → FAIL. Step3 구현. Step4 → PASS. Step5 commit.

### Task 2.3: facade `/ingest` (end-to-end 오케스트레이션)
**Files:** Modify `service/app.py`(POST /ingest → parse→chunk→insert 순차); Create `service/tests/test_ingest_orchestration.py`.
**Interfaces:** `POST /ingest (multipart file, workspace_id, doc_id)` → `{document_id, chunk_count, status, chunking_selection}`.
- [ ] Step1 실패테스트: 세 단계 mock → 순서 호출 + 최종 결과(선택근거 포함).
- [ ] Step2 → FAIL. Step3 구현. Step4 → PASS + 전체 facade 스위트 green. Step5 commit.

---

## Phase 3 — knowledge_base 소비자 전환 + 프론트

### Task 3.1: KbPipelineClient (facade capability 래퍼)
**Files:** Modify `knowledge_base/backend/app/clients/kb_pipeline_client.py`(parse/chunk/insert/insert_status/search/build_communities); Modify `backend/tests/test_kb_pipeline_client.py`. venv: `knowledge_base/.venv`.
- [ ] Step1 실패테스트(mock httpx): 각 메서드가 facade 경로 호출 + 응답 매핑(insert outcome.succeeded 등).
- [ ] Step2 `.venv/bin/python -m pytest backend/tests/test_kb_pipeline_client.py -q` → FAIL. Step3 구현. Step4 → PASS. Step5 commit.

### Task 3.2: `_ingest_kb_pipeline_tail` 단계별 오케스트레이션
**Files:** Modify `backend/app/core/pipeline.py`(tail: parse→chunk→insert + on_stage + chunks_meta 병합 + chunking_selection); Modify `backend/tests/test_pipeline_kb_pipeline.py`.
**Interfaces:** tail이 §8 단계 수행 — chunks_meta = chunk 응답(text/titles/pages)+chunk_id; chunking_selection = method/scores/methods_compared.
- [ ] Step1 실패테스트: fake deps(parse/chunk/insert) → 순서 + `on_stage("parse")→on_stage("chunk")→on_stage("insert")` **3개 top-level stage만** 호출(insert 동안 insert_status 폴링은 로깅/detail용 — top-level stage는 `"insert"` 유지, edgequake 세부 extracting/embedding/storing을 top-level on_stage로 올리지 않음 → KB_PIPELINE_STAGE_ORDER 어휘[parse/chunk/insert] 일치) + `set_chunking_selection(real)` + `replace_chunks_meta`(병합) + ready; 실패경로 delete_doc+failed.
- [ ] Step2 → FAIL. Step3 구현(placeholder `kb_pipeline_internal` 제거). Step4 → PASS + raganything/edgequake 회귀 green. Step5 commit.

### Task 3.3: 워커 on_stage + config
**Files:** Modify `backend/app/workers/tasks.py`(kb_pipeline 경로 on_stage→set_state), `backend/app/config.py`(`kb_pipeline_base_url=:19000`), `backend/app/dependencies.py`(KbPipelineClient 주입). Test `backend/tests/test_kb_provider_accept.py`.
- [ ] Step1 실패테스트: kb_pipeline ingest가 phase별 set_state. Step2 → FAIL. Step3 구현. Step4 → PASS. Step5 commit.

### Task 3.4: 프론트 단계 + 선택근거 카드 (두 detail view 일관)
**상태 확인(코드 실측):** 문서상세는 **두 곳**에서 렌더된다 —
- (a) app-router 풀페이지 `frontend/app/kb/[kbId]/documents/[docId]/page.tsx` — **`ChunkingSelectionCard`(line ~382) 이미 완비**(method_selected + methods_compared 비교표 SC/ICC/DCC/BI/RC/BA/avg; `detail.chunking_selection` 있을 때만, line ~690). 이게 사용자가 본 화면.
- (b) 모달 `frontend/components/DocumentDetailModal.tsx`(DocumentList에서 사용) — **chunking_selection 렌더 없음**(메타/gate/chunks_meta만, line 85-91·195-369).

**Files:** Modify `frontend/components/JobList.tsx`(**provider-aware 단계**: 기존 STAGE_ORDER 유지 + kb_pipeline 전용 order 추가, 절대 전역 교체 ✗); Modify `frontend/app/kb/[kbId]/documents/[docId]/page.tsx`(ChunkingSelectionCard에 R3 캐비엇 주석); Create `frontend/components/ChunkingSelectionCard.tsx`(page.tsx의 카드를 공유 컴포넌트로 추출); Modify `frontend/components/DocumentDetailModal.tsx`(공유 카드 렌더 추가); Modify `frontend/lib/types.ts`/문서상세 스키마(모달의 detail 타입에 `chunking_selection` 포함 확인).
**Interfaces:** `ChunkingSelectionCard({selection: ChunkingSelection})` 공유 컴포넌트(page.tsx·모달 양쪽 import).
- [ ] Step1: **provider-aware 단계표시** (stage 어휘 기반 분기 — `provider`는 JobStatus/jobs API에 **미노출**[codex 실측], 어휘로 분기). **현재 상태(실측)**: `JobList.tsx` line 34-42의 단일 전역 `STAGE_ORDER`는 이미 `gate→parsing→chunking→extracting→embedding→storing→persist_meta`(v1 phase 작업이 전역 교체 → 누수). 이를 **두 order로 분리**:
  - `KB_PIPELINE_STAGE_ORDER = [gate"게이트검증", parse"파싱", chunk"청킹", insert"적재", persist_meta"메타저장"]` — kb_pipeline 잡 전용.
  - `DEFAULT_STAGE_ORDER = [gate"게이트검증", parsing"파싱", dify"적재", persist_meta"메타저장", graph_rebuild"그래프재구성"]` — 그 외 provider용(워커 `tasks.py`가 비-kb_pipeline 경로에서 emit하는 어휘 gate/parsing/dify/persist_meta/graph_rebuild과 일치 — 실측 확인).
  - `StageSteps`: 잡이 도달한 stage가 kb_pipeline 어휘(`parse`/`chunk`/`insert` 중 하나)면 `KB_PIPELINE_STAGE_ORDER`, 아니면 `DEFAULT_STAGE_ORDER`. tick 로직 공통.
  → kb_pipeline 잡만 parse/chunk/insert 단계 렌더, **다른 provider는 자기 어휘(parsing/dify)로 정상 렌더 → kb_pipeline phase가 안 새어나감.** 비교도구 본질 보존.
- [ ] Step2: page.tsx의 `ChunkingSelectionCard`를 `frontend/components/ChunkingSelectionCard.tsx`로 **추출**(동일 렌더), page.tsx는 import해 사용(동작 불변). 카드 헤더/설명에 **R3 캐비엇 주석** 추가: kb_pipeline 문서는 "본문 gap 기준 선택(modal 영역 제외)" 한 줄.
- [ ] Step3: `DocumentDetailModal.tsx`에 `detail.chunking_selection && <ChunkingSelectionCard selection={detail.chunking_selection} />` 추가(chunks_meta 섹션 근처). 모달의 detail 타입/응답에 `chunking_selection`이 포함되는지 확인(미포함이면 백엔드 DocumentDetail 응답·`lib/types.ts`에 추가 — page.tsx와 동일 스키마라 이미 있을 가능성 높음, 실측).
- [ ] Step4: `cd frontend && npx tsc --noEmit` → clean(exit 0). 두 view 모두 `chunking_selection` 있을 때 카드 렌더(없으면 미표시 — dify/다른 provider 화면 불변).
- [ ] Step5: commit.

### Task 3.5: 검색 배선
**Files:** Modify `backend/app/...`(kb_pipeline KB 검색 → KbPipelineClient.search) + 테스트.
- [ ] Step1 실패테스트 → FAIL. Step2 구현. Step3 → PASS. Step4 commit.

---

## Phase 4 — 통합 스모크

### Task 4.1: 5서비스 기동 + e2e (anti-loop, 메모리 관리)
**Files:** Create `8.kb-pipeline/docs/runbook-v2-smoke.md`.
- [ ] Step1 기동: edgequake(:8081 passthrough, search_path 옵션 없음), bge-m3 :7997, adaptive_chunk :18060, parse-svc :19001, facade :19000, doc_guard :8000, knowledge_base :8088. (메모리 부족 시 불필요 정지; Docker 불안정 시 2회 시도 후 정지·보고, 루프 금지. 키 출력 금지.)
- [ ] Step2 실 PDF 업로드(knowledge_base UI/API) → 단계 시퀀스 `gate→parse→chunk→insert` **각 응답마다 ✓** 확인.
- [ ] Step3 문서상세에 **진짜 method_selected/scores/methods_compared** 표시 + modal 원자성(표=1청크) 확인.
- [ ] Step4 검색 1건 + 워크스페이스 격리 SQL 확인.
- [ ] Step5 runbook + 결과 commit.

---

## Self-Review
- **Spec 커버리지**: parse-svc(2.1) / adaptive_chunk marker(1.1) / edgequake passthrough(1.2) / facade capability(1.3-1.5,2.2-2.3) / knowledge_base 소비자(3.1-3.5) / 스모크(4.1). spec §3-§10 매핑.
- **타입 일관**: chunk 응답 키(chunks/method_selected/scores/methods_compared) 1.3↔3.2; insert 키(document_id/chunk_count/status) 1.4↔3.1↔3.2; chunk_id `{document_id}-chunk-{i}` 1.4↔3.2; 구분자 `\u{001e}` 1.2↔1.4.
- **호환/비교도구 보존**: adaptive_chunk 마커0 회귀(1.1), 다른 provider tail 회귀(3.2), v1 `/ingest` 보존, **프론트 진행표시 provider-aware(기존 provider STAGE_ORDER 불변, kb_pipeline만 추가)**(3.4), ChunkingSelectionCard는 chunking_selection 있을 때만(dify 화면 불변). knowledge_base는 비교 플랫폼 본질 유지 — kb_pipeline은 peer provider.
- **구현시 확인(flag)**: edgequake `/query` 검색 응답 형식(1.5), parse-svc OCR 계약(2.1) — 실코드 확인.
