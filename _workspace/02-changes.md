# 02 · 변경내용

> 출처: SoT.md §0/§11, SoT.v1.md, dev_wiki.md, process-definition 개정이력.

---

## 0-A. 문서단위 그래프(관계) 추출 스킵 — UI 라디오 + metadata 게이트 (3레포, 2026-06-30)

엑셀처럼 그래프 추출이 무의미·고비용(청크 2천급)인 문서를 **벡터 적재/검색만** 하고
edgequake 엔티티/관계 추출(qwen 동기 블로킹 단계)을 **문서 단위로 건너뛴다**. provider=kb_pipeline 전용.
계획: `docs/superpowers/plans/2026-06-30-excel-skip-graph-extraction.md` (v3 READY, ultracode 적대적검증 2회).

- **신호 흐름**: UI 라디오(extractGraph) → kb `/documents/ingest`(`extract_graph`) → facade `/insert`(`extract_graph`)
  → edgequake `metadata.skip_graph_extraction`. **엑셀(chunk_strategy=excel_rag_parser)은 UI에서 '미추출' 고정(disabled)**.
- **edgequake(Rust)**:
  - (선행 버그수정) async 업로드 경로가 `request.metadata` 를 버리던 것 수정 — `text_upload.rs` 에서
    base object에 merge(보호키 document_id/title/tenant_id/workspace_id 우선, 그 외 사용자키 추가). 이 수정 없이는 skip 신호 미도달.
  - `processing.rs` 에 `process_with_resilience_cancellable_opts(skip_extraction)` 신규 — 추출 서브블록만
    `&& !skip_extraction` 게이트. `chunk_async`(청킹)·`finish_document_processing`(청크임베딩·lineage)는 게이트 밖이라 유지 → **벡터검색 무영향**. 기존 메서드는 false 위임이라 다른 호출자 무영향.
  - `text_insert.rs`: `skip_graph_extraction`(bool) 읽어 opts 호출. **status 가드** — skip 시 `entity_count==0`을
    `partial_failure` 아닌 `completed` 로(안 고치면 facade `document_phase` 가 `_PHASE_SUCCESS` 제외→/insert가 failed 반환). 진짜 LLM실패(skip=false) 0엔티티는 여전히 partial_failure.
- **facade**: `/insert` 에 `extract_graph: bool=Body(True)` 추가 → `insert_chunks(skip_graph=not extract_graph)`
  → **`submit_document`**(post_document 아님) 본문에 `skip_graph` True일 때만 `metadata.skip_graph_extraction=true` 첨부(기본은 byte-identical).
- **knowledge_base**: UploadPanel 라디오(삼항 바깥 공통 위치, 엑셀 disabled+false 고정), api.ts/스키마/라우터/워커/`KbContext`/
  `_ingest_kb_pipeline_tail`/`kb_pipeline_client` 에 `extract_graph` 배선(+`KbPipelineLike` Protocol). **provider==kb_pipeline 분기 한정**(타 provider None→미첨부, 격리).
- **검증**: edgequake `cargo build` PASS + pipeline lib 215 tests PASS; facade pytest 52 PASS;
  kb tsc PASS + 대상 38 tests PASS(broader 6 실패는 dify/raganything/ragflow gate 관련 **pre-existing**, 본 변경과 무관 확인). 교차계약 일관성 검증 consistent.
- **부작용/이득**: skip 문서는 그래프·커뮤니티/글로벌검색에서 빠짐(의도), 벡터검색은 정상. qwen 추출 생략으로 엑셀 적재 시간·비용 대폭 절감. 같은 KB 혼합 안전.
- **비범위**: 이미 그래프 생성된 문서의 사후 skip 전환(checkpoint-resume가 게이트 우회), 라이브 E2E 스모크.
- **배포순서**: edgequake(재빌드+재기동, `EDGEQUAKE_CHUNKER=passthrough` 유지) → facade(재기동) → knowledge_base(백엔드+프론트). 하위호환(기본 추출 ON)이라 부분배포 중 회귀 0.

---

## 0. Chunk method 선택 passthrough (facade B, 2026-06-29)

knowledge_base plan `23_plan_chunk_method_selection.md` §B 반영. adaptive_chunk(:18060)가
`options.methods`/`skip_scoring`/`llm_regex_pattern`(A 확정 계약) 을 받게 되어, facade 가
이를 **통과만** 시킨다(검증/의미는 adaptive_chunk 소유).

- `service/adaptive_chunk.py AdaptiveChunkClient.chunk()` — kwargs `methods=None`/
  `skip_scoring=False`/`llm_regex_pattern=None` 추가. 비-기본일 때만 `POST /chunk/jobs`
  의 `options` 에 실음. 세 값 모두 기본이면 `options={"atomic_markers":…}` 로 **byte-동일**
  (auto 경로 회귀 0, 하위호환).
- `service/app.py POST /chunk` — Body 필드 `methods`/`skip_scoring`/`llm_regex_pattern`
  (전부 optional, embed) 추가 → `ac.chunk(...)` 전달. 무지정 호출 = auto(현 동작 불변).
- `POST /parse` 변경 없음(이미 `enriched_content` 반환).
- 라이브 probe(:18060): `recursive_600`+`skip_scoring` → `method_selected=recursive_600`,
  `methods_compared=[]`, `scores={}`. `llm_regex`+`제\d+조` → 조문별 4청크(LLM 패턴생성 생략).
- 테스트: `service/tests/test_chunk_endpoint.py` +3 (FakeAdaptiveChunk·auto 회귀가드 갱신).
  full suite 141 passed (기존 138→141, 회귀 0; 무관한 pre-existing 1건
  `test_insert_endpoint` stale assert 제외).
- **C(KB 백엔드) 미구현**: 이 facade 입력 계약을 KB 백엔드가 `KbPipelineClient.chunk()`
  로 호출(plan §C3 예정).

---

## 1. v1 → v2 결정 변천 (해소된 모순)

`SoT.v1.md`(원본)은 방향성 메모였다. 4개 참고 레포(`excel-parser-markitdown`, `adaptive_chunk`, `raganything_svc`, edgequake 본체)를 코드/로그 레벨로 검증한 뒤 모순을 제거하고 "차용 vs 신규"를 확정한 것이 v2(SoT) 다.

| v1 의 문제 | v2 의 해소 |
|------------|------------|
| "adaptive_chunk → edgequake insert" 정면충돌(edgequake 내부 청킹) | edgequake 공개 `ChunkingStrategy` trait 에 꽂아 해소 → 이후 facade 소유로 재확정 |
| "edgequake 스키마 차용" vs "edgequake 에 insert" 혼재 | edgequake 를 **엔진으로 운용**(베이스), 앞단만 커스텀으로 확정 |
| per-KB "schema/RLS" 표현 모호 | edgequake 가 **실제 Postgres RLS 보유**(009/013/022) → 공유테이블+tenant/workspace RLS 로 확정 |
| 파서 분담이 실측과 불일치 | 확장자별 파서 **실측 재확정** |
| raganything 교체 여부 미결 | raganything 는 엔진이 아니라 "모달 LLM 서술" 아이디어만 차용 |
| 그래프 이중생성 위험 | content 를 **단일 스트림**으로 → edgequake 가 추출/그래프 단독 소유 |

**v1 의 미해결 질문**(SoT.v1.md): "raganything 의 content_list 방식이 더 효율적이지 않나?" → **답: 엔진 채택 아님.** raganything `modalprocessors` 의 모달 서술 *구조만* 차용하고, content 는 단일 enriched 스트림으로 만들어 edgequake 추출에 일임(이중생성 제거).

---

## 2. 청킹 소유권 이전 (v1 edgequake-adaptive → v2 facade-passthrough)

가장 큰 아키텍처 변경. **2026-06-24 정정.**

### v1 경로 (구버전, 더 이상 사용 안 함)
- edgequake 내부에 `AdaptiveChunkStrategy`(Rust) 구현: `〈MODAL〉` 경계 분리 → 모달은 단독 atomic 청크, 텍스트 갭만 adaptive_chunk `/chunk` HTTP 위임. `Pipeline::with_chunking_strategy()` 신설 + 팩토리 플래그 `EDGEQUAKE_CHUNKER=adaptive`.
- 당시엔 "adaptive_chunk 의 어느 엔드포인트도 모달 스팬 원자성을 강제하지 않음(codex 확인)" → 원자성을 전적으로 Rust 전략이 소유했다.

### v2 경로 (현행)
- **청킹·모달원자성을 facade `/chunk` 가 소유.** facade 가 adaptive_chunk 를 `atomic_markers=〈MODAL〉…〈/MODAL〉` 로 호출 → adaptive_chunk `service/runner.py` `_segment_atomic`/`DEFAULT_ATOMIC_MARKERS` 가 모달 원자성을 강제. → 구버전의 "atomic 미강제" 메모는 **무효화**됨.
- 전용 edgequake 는 **`EDGEQUAKE_CHUNKER=passthrough`** 로 띄워 facade 청크를 U+001E 경계로 그대로 저장(재청킹 금지).
- ⚠️ `adaptive` 로 띄우면 facade 가 이미 청킹한 내용을 다시 adaptive_chunk 로 재청킹(이중청킹)하다 빈 구분자 조각(``)을 보내 **HTTP 422 → 적재 실패**.
- 기동 스크립트 `service/scripts/start_dedicated_edgequake.sh` passthrough 로 갱신(8.kb-pipeline `456d52a`).

---

## 3. edgequake fork 버그 수정 3건

런타임 중 발견·수정해 fork 에 반영. (실 서버 바이너리 = 루트 패키지 `edgequake`(`cargo build --bin edgequake`), `edgequake-api`(lib) 아님.)

### 3.1 임베딩 차원 미적용
- `OpenAIProvider` 가 미지 모델을 **1536 하드코딩** + `provider_setup` 이 `EDGEQUAKE_EMBEDDING_DIMENSION` 을 로깅만 함.
- 수정: `with_embedding_dimension` 추가 + 적용해 `EDGEQUAKE_EMBEDDING_DIMENSION=1024` 반영.
- `edgequake-llm` 이 crates.io 의존(0.6.23)이라 `vendor/edgequake-llm` + `[patch.crates-io]` 로 패치. → **upstream 동기화 시 vendor/patch 재적용 필요**(핀 고정 + CI).

### 3.2 chat(추출) provider 무시
- `create_openai()` 가 `OPENAI_BASE_URL` 무시 + `gpt-5-mini` 하드코딩.
- 수정: `OpenAIProvider::compatible(key, base_url)` + env 모델 해석 → OpenRouter `qwen/qwen3.5-122b-a10b` 사용.
- **임베딩 BASE_URL 과 chat BASE_URL 분리**(임베딩=litellm, chat=OpenRouter).

### 3.3 KV JSONB 전체 GIN 인덱스 제거 (2026-06-25)
원본 `PostgresKVStorage::create_table()` 가 `eq_*_kv.value` 전체에 `USING GIN (value)` 자동 생성 → write-heavy checkpoint/lineage 저장을 심각하게 지연.

**실측 incident** (dev_wiki.md):
- 문서 `89eb9cd6-...` 의 단건 KV checkpoint upsert 가 **109.116s** 소요. 실제 chunk vector 저장은 그 직후 약 0.8s.
- `eq_eq_default_kv` = live 약 1,601행 / 약 2.4MB JSON 대비, `eq_eq_default_kv_value_gin` = **1,020MB** 비대화(런타임 읽기엔 사실상 미사용).

**결정/규칙**:
- KV primary key 인덱스 유지, `keys_with_suffix` 용 reverse-key suffix 인덱스 유지.
- `value` 전체 GIN 재도입 금지. (현행 경로는 `get_by_id`/`get_by_ids`/`keys_with_prefix`/`keys_with_suffix` key 기반 접근이 주류, community/graph 조회는 `GraphStorage`/AGE 전용 인덱스 사용.)
- JSONB 검색 필요 시 특정 key family/JSON path 의 **partial/expression index 만** 허용.
- fork 에서 신규 자동 생성 제거. 기존 dev DB 는 drop:
  ```sql
  DROP INDEX IF EXISTS public.eq_eq_default_kv_value_gin;
  ```
- 검증:
  ```sql
  SELECT indexrelname, pg_size_pretty(pg_relation_size(indexrelid)) AS size
  FROM pg_stat_user_indexes
  WHERE relname = 'eq_eq_default_kv'
  ORDER BY pg_relation_size(indexrelid) DESC;
  ```

---

## 4. 파서 라우팅 변경 (markitdown → 구조파서 분기)

- v1: PDF/PPTX/DOCX 전부 markitdown. XLS/HWP 계열은 kordoc.
- v2 실측 재확정(W6):
  - **markitdown 은 pptx/DOCX 의 병합(colspan/rowspan)을 파싱 시점에 소실**(blockify 복구 불가) → 병합 중요 pptx/DOCX 는 **OCR·VLM(:18050) structural 라우팅**(과거 kordoc/MinerU 권고와 동일 취지).
  - PDF → OpenDataLoader(`markdown_with_html=True`, `<table>` 70개 실측).
  - HWP/HWPX/HWPML → kordoc 신뢰(실측 생략, 수용된 리스크).
  - 텍스트형은 markitdown 유지.

---

## 5. 정정 사실 (drift 정리)

- **실 서버 바이너리** = 루트 패키지 `edgequake`(`cargo build --bin edgequake`), `edgequake-api`(lib) 아님.
- **§11 E2E 기록은 v1(edgequake=adaptive)** 시점 — 현행 facade 경로는 passthrough(03-dev-progress §2 참조).
- **임베딩 `:7997` 표기 드리프트**: SoT.md §3.5/§5.4 의 로컬 `:7997` 은 현행 런처보다 오래된 표기. 현행 운영 배선은 원격 litellm. (process-definition v1.0 에서 정정 반영.)
- **DSN 포트 정합**: 코드 기본 DSN `port=5432`(community.py `DEFAULT_DSN`) vs 운영 전제 `:5433` → 환경별 명시 필요.
- **edgequake base URL 정합**: search.py 기본 `:8080` vs 전용 edgequake 기동 `:8081` → 배선 일원화 필요.
- **프로세스정의서 개정 이력**(`docs/kb-pipeline-process-definition.md`):
  - v0.1 (2026-06-29): 초안 — 원본 프로세스정의서 구조(1.개요~6.협의사항) 모방, 코드 사실 반영.
  - v1.0 (2026-06-29): 검증 반영 — 임베딩 배선(원격 litellm) 정정, 4장 업무어조 복원·코드 레퍼런스 5장 강등, Search 실노출경로(`/search`) vs 라이브러리 라우터(`unified_search`) 분리, `/communities/build` 추가.
  - 이 정의서가 코드 레퍼런스(함수명/파일경로/env/유니코드)의 권위 출처이며, `_workspace/01-architecture.md 부록 A` 에 색인으로 요약됨.

---

## 6. 엑셀 게이트웨이 검증 재설계 (2026-06-29~30)

> 설계 `docs/superpowers/specs/2026-06-29-excel-gate-postparse-design.md` · 계획 `docs/superpowers/plans/2026-06-29-excel-gate-postparse.md`(v2 READY, ultracode 대립검증으로 codex 대체).

- **게이트 이동**: doc_guard 를 **파서 전(원시바이트 13규칙)** → **파서 후단(파싱 결과 기반)** 으로 이동. 기존 13규칙 전면 비활성·제거(`docguard.check` 호출 0건).
- **목적 전환**: "모든 형식 엑셀을 잘 파싱"이 최우선. 게이트는 추출이 실제로 깨지는 경우만 차단. 핵심 가정 — 파서(기본 kordoc)가 헤더 후보를 의외로 잘 찾으므로 **값이 잘못 뽑히면 헤더 오추출로 간주**.
- **차단 규칙(4 code)**: `ref_error`(#REF!/#VALUE! 등, 값+수식 양쪽 스캔) · `header_leak`(헤더가 값으로 추출) · `empty_header`(보수적·거의 비활성) · `side_by_side`(나란히 놓인 무관한 두 표). side_by_side 는 **인덱스열 중복 OR ≥2 distinct 라벨블록 비겹침 반복**일 때만(매트릭스·동명컬럼 거짓양성 제외).
- **게이트 단위 = 파일 단위**: 한 시트라도 finding 이면 파일 차단. (예: 자산목록은 NAC연계 시트의 진짜 side_by_side 로 파일 차단.)
- **provider 범위 = kb_pipeline 전용**(사용자 결정). dify/edgequake/raganything/ragflow 미적용.
- **2단계 흐름 정합(중요)**: kb_pipeline UI 는 Phase1 `parse_preview_task`(미리보기) → Phase2 `ingest_document`(pre_parsed). 게이트는 **Phase1 에 위치**(실사용 경로), Phase2 는 `pre_parsed is None` 일 때만(직접경로) 재게이트.
- **컴포넌트**: excel-parser `/parse` `stats.gate_summary` 산출(신규 `excel_parser_rag/gate/excel_gate.py`) → doc_guard `POST /v1/check-excel`(CheckReport 재사용, gate_error 합성) → knowledge_base `docguard.check_excel` + `ExcelParseResult.gate_summary`. 프론트 JobList 단계 `파싱→게이트검증→청킹→적재`, UploadPanel 문서가드규칙 패널 제거.
- **구현 브랜치(미머지)**: 7.excel-parser `feat/excel-gate`, doc_guard `feat/excel-gate`(이 세션에 git init), knowledge_base `feat/kb-pipeline-provider`.
- **비범위/후속**: 위임전결 ○매트릭스 고도화, compute_gate_summary canvas 재사용(perf), 라이브 스모크.
