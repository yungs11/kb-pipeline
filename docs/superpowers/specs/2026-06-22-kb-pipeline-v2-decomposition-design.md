# kb-pipeline v2 — 단계 분해(orchestrated phases) 설계 스펙

작성 2026-06-22 · 상태: 설계 승인됨(대화) → 스펙 리뷰 → 구현계획

## 1. 동기 / 목표

v1 kb_pipeline은 `POST /ingest` **하나의 불투명 호출**에 parse→(modal)→adaptive 청킹→임베딩→엔티티추출→적재를 통째로 넣었다. 그 결과:
- **진행 단계(phase)가 안 보임** — "청킹이 끝났다"를 정확히 표시 못 함.
- **청킹 선택근거(method_selected/scores)가 버려짐** — 청킹을 edgequake의 `AdaptiveChunkStrategy`가 내부에서 adaptive_chunk(:18060)를 호출해 수행하고 그 선택근거를 폐기. UI엔 placeholder `kb_pipeline_internal`만 표시됨.

**목표(v2)**: 청킹 오케스트레이션을 edgequake **안에서 → knowledge_base/kb-pipeline 서비스로 끌어낸다.** 각 단계를 명시적 API로 분해해 (a) phase별 진행표시가 정확하고, (b) adaptive 청킹 선택근거가 자연히 캡처·표시되며, (c) edgequake는 "임베딩+엔티티추출+그래프적재+검색" 저장엔진으로 단순해진다.

**비목표**: 검색 UI(W5), W4 DB-RLS. modal 청킹의 **아이디어**는 유지하되 구현 위치만 Rust→Python으로 이동.

## 2. 아키텍처 (오케스트레이션 = knowledge_base 워커)

```
knowledge_base 워커 (오케스트레이터)
  Stage 0: doc_guard            (기존, 변경 없음)
  phase "parse"   → kb-pipeline svc  POST /parse   → enriched content (W6 parse → blockify → modal 서술)
  phase "chunk"   → kb-pipeline svc  POST /chunk    → { chunks[], chunking_selection{method_selected,scores,methods_compared} }
                       (〈MODAL〉 스팬 원자 유지 + 텍스트 gap → adaptive_chunk(:18060) → 청크 조립)
  phase "insert"  → kb-pipeline svc  POST /insert   → edgequake(passthrough chunker): 임베딩+엔티티추출+그래프적재
                       → { document_id, chunk_count, status }   (insert 동안 edgequake doc status 폴링 = embed/extract/store 세부 phase)
  → repo.replace_chunks_meta(chunks_meta) + set_chunking_selection(real) → status=ready
  phase "community"(W3) → 적재 성공 후 비차단 잡 (기존)
  검색: knowledge_base → edgequake (기존)
```

각 phase 호출 전후로 워커가 `jobs.set_state(job_id, stage=phase)` → 프론트가 실제 단계를 체크.

## 3. Component A — kb-pipeline 서비스 (8.kb-pipeline, 분해)

기존 monolith `run_ingest`/`/ingest`는 **호환용으로 유지**(테스트·단독 사용)하되, 내부를 아래 3단계 함수로 재구성하고 각각을 **독립 엔드포인트**로 노출한다. `kb_pipeline` 패키지(blockify/modal) 재사용.

### 3.1 엔드포인트
| 메서드 | 경로 | 입력 | 출력 |
|--|--|--|--|
| POST | `/parse` | multipart: `file`, `filename`, `content_type?` | `{ enriched_content, n_blocks, modal_ids }` |
| POST | `/chunk` | json: `{ enriched_content, doc_name }` | `{ chunks:[{chunk_index,text,titles_context,pages}], chunking_selection:{method_selected,scores,methods_compared} }` |
| POST | `/insert` | json: `{ chunks, workspace_id, doc_id, title }` | `{ document_id, chunk_count, status }` |
| GET | `/insert/status` | `workspace_id, document_id` | `{ phase, chunk_count, terminal, succeeded }` (edgequake doc status 중계, v1과 동일) |
| POST | `/communities/build` | `workspace_id` (async 202) | `{status}` (기존) |
| GET | `/healthz` | — | `{status:"ok"}` |

### 3.2 `/parse`
W6 `recommended_parser` 라우팅으로 실파일 파싱 → "markdown+inline HTML 표" → `hybrid_to_blocks` → `modal.enrich`(표/수식 qwen 서술; 이미지 vision 설정 시) → **〈MODAL〉 atomic 인라인 enriched content**. (v1 `run_front` 재사용.)

### 3.3 `/chunk` — modal-aware 청킹 + 선택근거 (핵심 신규)
1. **modal split**: enriched content를 `〈MODAL …〉…〈/MODAL〉` 경계로 분할(edgequake `adaptive_strategy.rs::split_segments` 로직을 Python으로 포팅 → `kb_pipeline.chunking.split_modal_segments`). modal 스팬 = 원자 청크 1개.
2. **gap 청킹**: modal 사이 텍스트 gap들을 합쳐(또는 gap별) adaptive_chunk `POST /chunk`(body `{text|markdown, doc_name}`)에 보내 청크 + `{method_selected, scores, methods_compared}` 수신.
3. **조립**: modal 원자 청크 + gap 청크를 **원래 순서대로** 합쳐 최종 `chunks[]`. `chunk_index` 재부여.
4. **선택근거**: adaptive_chunk가 준 `{method_selected, scores, methods_compared}`를 그대로 반환(gap이 여러 번이면 대표=최대 gap 기준 또는 집계; §6 R3).
   - 서비스에 신규 `adaptive_chunk` HTTP 클라이언트(`service/adaptive_chunk.py`) 추가(knowledge_base `AdaptiveChunkClient` 계약과 동형).

### 3.4 `/insert` — edgequake passthrough 적재
- 입력 `chunks[]`를 **유니크 구분자**(`` RECORD SEPARATOR)로 join한 `content` 생성.
- edgequake `POST /api/v1/documents`(`async_processing:true`, `X-Workspace-ID/X-Tenant-ID`)로 적재 — 단 **전용 edgequake는 `EDGEQUAKE_CHUNKER=passthrough`**(Component B)로 떠 있어 청킹 없이 구분자 split → 그 청크들을 임베딩+엔티티추출+그래프적재.
- task 폴링(v1 `post_document`/`document_phase` 재사용) → `{document_id, chunk_count, status}`.
- `ensure_workspace`(v1) 재사용 — kb_id → edgequake 워크스페이스 UUID.

## 4. Component B — edgequake fork: passthrough ChunkingStrategy

`crates/edgequake-pipeline/src/chunker/`에 **`PassthroughStrategy`** 추가:
- `async fn chunk(&self, content, _config) -> Vec<ChunkResult>`: `content`를 `\u{001e}`(RECORD SEPARATOR)로 split, 각 조각을 `ChunkResult{content, tokens, chunk_order_index}`로 그대로 반환(빈 조각 제외). adaptive_chunk 호출 없음.
- `workspace_pipeline_factory.rs`: `EDGEQUAKE_CHUNKER=passthrough` → `PassthroughStrategy` 주입(기존 `adaptive` 분기 옆).
- 임베딩·엔티티추출·그래프적재 파이프라인은 **그대로** (청킹 단계만 교체). → W3 GraphRAG 엔티티추출은 인서트 시 청크별로 정상 수행.
- **재빌드**: `cargo build --bin edgequake`. 전용 edgequake를 `EDGEQUAKE_CHUNKER=passthrough`로 재기동.
- 단위테스트: split/empty-handling/순서.

## 5. Component C — knowledge_base 통합 (tail 재구성)

- **KbPipelineClient**(`clients/kb_pipeline_client.py`): `parse()`, `chunk()`, `insert()`, `insert_status()` 추가. v1 `submit/poll_status/ingest`는 호환 유지하되 tail은 신규 3단계 사용.
- **`_ingest_kb_pipeline_tail`**: 오케스트레이션으로 재작성 —
  1. `on_stage("parse")` → `parse(file)` → enriched
  2. `on_stage("chunk")` → `chunk(enriched)` → chunks + **chunking_selection(real)**
  3. `on_stage("insert")` → `insert(chunks, workspace, doc_id)` → document_id; insert 동안 `insert_status` 폴링하며 `on_stage(edgequake phase)` (embed/extract/store)
  4. `fetch_chunk_meta` → `replace_chunks_meta` + **`set_chunking_selection(real)`**(placeholder 제거) → ready
  - 실패 시 `delete_doc` + status=failed(성공위장 금지). 다른 provider tail 불변.
- **워커**(`tasks.py`): kb_pipeline 경로가 `on_stage` 콜백으로 phase별 `set_state`(기존 v-phase 작업 확장).
- **프론트**(`JobList.tsx`): `STAGE_ORDER = gate→parse(파싱)→chunk(청킹)→extracting(추출)→embedding(임베딩)→storing(적재)→persist_meta(메타저장)`. "청킹" 체크는 `/chunk`(adaptive 선택 포함) 완료 시 ✓.
- **문서상세 카드**: 기존 `chunking_selection` 렌더 그대로 — 이제 **진짜 method_selected/scores/methods_compared** 표시.

## 6. 데이터 계약 / 리스크

- **계약**: `/parse`→enriched str; `/chunk`→`{chunks[],chunking_selection}`; `/insert`→`{document_id,chunk_count,status}`. workspace_id=kb_id, doc_id=content_hash[:16](기존). 성공=status terminal-ok & chunk_count>0.
- **R1 passthrough 정확성**: edgequake passthrough가 구분자 경계를 정확히 보존하고 청크별 임베딩/추출을 수행하는지 라이브 검증(엔티티추출이 청크별로 도는지 확인).
- **R2 modal split 포팅**: Rust `split_segments` → Python 포팅의 동치성 단위테스트(modal 원자성·malformed marker는 텍스트 취급).
- **R3 선택근거 표현**: modal이 많고 gap이 적은 문서는 adaptive_chunk가 gap만 보므로 선택근거가 gap 텍스트 기준 — UI에 "(gap 기준)" 주석 또는 집계. 대표값임을 문서화.
- **R4 이중 청킹 제거**: adaptive_chunk는 `/chunk`에서 1회만, edgequake는 passthrough(청킹 안 함) → 중복 없음.
- **R5 재빌드**: edgequake 바이너리 재빌드 필요(passthrough). 전용 edgequake `EDGEQUAKE_CHUNKER=passthrough` 재기동. 기존 v1 adaptive 청킹 코드(AdaptiveChunkStrategy)는 보존(롤백 가능).
- **R6 호환**: v1 `/ingest`·`submit`·기존 tail 동작 보존(테스트 green 유지), 다른 provider 무영향.

## 7. 테스트
- 서비스: `/parse`·`/chunk`(modal split + adaptive_chunk mock)·`/insert`(passthrough edgequake mock) 계약 단위테스트 + modal-split 동치성 테스트.
- edgequake: PassthroughStrategy split 단위테스트.
- knowledge_base: tail 오케스트레이션(parse→chunk→insert) 단위테스트 + chunking_selection 저장 검증 + 다른 provider 회귀.
- 라이브 스모크: 실 PDF → phase 시퀀스(parse→chunk→insert/embed/extract/store) + **문서상세에 진짜 선택근거(method/scores) 표시** + 청크 격리.
