# kb-pipeline v2 — 중앙 오케스트레이션 + 전담 백엔드 분해 설계 스펙

작성 2026-06-22 · 상태: 설계 승인됨(대화) → 스펙 리뷰 → 구현계획
최종 개정: knowledge_base = 중앙 오케스트레이터(각 백엔드 API 호출만), parse 전담 서비스, UI 단계 = API 호출 단위.

## 1. 동기 / 목표

v1 kb_pipeline은 `POST /ingest` **하나의 불투명 호출**에 parse→modal→청킹→임베딩→추출→적재를 통째로 넣었다 → (a) 진행 단계 안 보임, (b) 청킹 선택근거(method_selected/scores) 폐기(UI엔 `kb_pipeline_internal` placeholder).

**목표(v2)**: knowledge_base를 **중앙 오케스트레이터**로 두고, 각 단계를 **전담 백엔드의 API 호출**로 분해한다. 각 단계의 관심사가 자기 백엔드 한 곳에 응집되고, UI는 **API 호출 직전 단계 시작 → 응답 도착 시 완료(✓)** 로 정확히 표시된다.

**비목표**: 검색 UI(W5), W4 DB-RLS.

## 2. 아키텍처 — knowledge_base 중앙 오케스트레이션

knowledge_base는 **로직을 직접 수행하지 않고**, 순서대로 각 백엔드를 API로 호출만 한다. 각 호출 직전 `set_state(stage=...)`, 응답 도착 시 다음 단계로(=프론트가 그 단계 ✓).

```
[knowledge_base 워커 = 중앙 오케스트레이터]

  stage "gate"   → doc_guard (:8000, 기존)                    호출→응답
  stage "parse"  → kb-pipeline svc  POST /parse (:19000)      호출→응답: enriched content(〈MODAL〉)
  stage "chunk"  → adaptive_chunk   POST /chunk (:18060)      호출→응답: { chunks[], method_selected, scores, methods_compared }
  stage "insert" → edgequake        POST /documents (:8081)   호출→응답: { document_id, chunk_count }   (passthrough; 임베딩+추출+적재)
  → chunks_meta 저장(adaptive_chunk 메타 + edgequake chunk_id) + chunking_selection 저장 → status=ready
  stage "community"(W3) → kb-pipeline svc POST /communities/build  (적재 후 비차단 잡)
  검색: knowledge_base → edgequake POST /search (:8081)
```

**전담 백엔드 4곳**:
| 백엔드 | 포트 | 책임 | 호출 주체 |
|--|--|--|--|
| doc_guard | :8000 | 가드(기존) | knowledge_base |
| **kb-pipeline parse svc** | :19000 | **parse + modal enrich (고유)** | knowledge_base |
| **adaptive_chunk** | :18060 | **청킹: modal 원자 + 방법선택 + 점수** | knowledge_base **직접** |
| **edgequake (dedicated)** | :8081 | **임베딩 + 엔티티추출 + 그래프적재 + 검색** | knowledge_base **직접** |

## 3. UI 단계 모델 (API 호출 = 단계)

- 워커가 각 백엔드 호출 **직전** `jobs.set_state(job_id, stage=X)` → 프론트는 stage X를 "진행중 ⏳".
- 그 호출이 **응답을 반환**하면 다음 stage로 진행 → 프론트는 X를 "완료 ✓".
- 프론트 `JobList.tsx` `STAGE_ORDER = [ gate(게이트), parse(파싱), chunk(청킹), insert(적재), persist_meta(메타저장) ]`. 레거시 `select/dify` 제거(다른 provider용 `dify→적재` 라벨 fallback만 유지).
- **"청킹 ✓"는 adaptive_chunk 응답 도착 시** = 청킹(방법선택 포함)이 실제로 끝난 시점. (사용자 요구 충족.)

## 4. Component A — adaptive_chunk 백엔드를 modal-aware로

위치: `/Users/xxx/workspace/99.projects/adaptive_chunk`(로컬, git 아님 — 수정 가능). `service/main.py`(POST /chunk) → `service/runner.py::run_chunk` → `adaptive_chunk/selector` 방법선택+점수.

### 4.1 modal-aware `run_chunk`
1. **segment**: 입력을 `〈MODAL …〉…〈/MODAL〉`(`〈`=U+3008,`〉`=U+3009) 경계로 분할. 미종결 마커는 일반 텍스트로 취급(유실 금지).
2. **gap 청킹 + 방법선택**: modal 사이 gap 텍스트를 **현행 방법선택·점수 로직 그대로** 청킹. 채점/선택은 gap 텍스트 기준.
3. **modal 원자 청크**: 각 〈MODAL〉 스팬 = 청크 1개(쪼개지 않음). titles_context는 인접 상속(없으면 None).
4. **조립**: gap 청크 + modal 청크를 **원래 순서**로 합쳐 `chunks[]`, `chunk_index` 재부여.
5. **반환**: 현행 R1 형식 `{ method_selected, scores, methods_compared, chunks, timing_ms }` 불변.
- 옵션 `modal_aware: bool=true`. **마커 0개면 동작 = 기존과 동일(회귀 0)**.
- 동기 `/chunk` + 비동기 `/chunk/jobs` 동일 적용.
- 테스트: split 동치성(edgequake `split_segments` 규칙), 원자성, 조립 순서, malformed=텍스트, 마커0=기존, 점수형식 불변.

## 5. Component B — edgequake fork: passthrough ChunkingStrategy

`crates/edgequake-pipeline/src/chunker/`에 **`PassthroughStrategy`**:
- `async fn chunk(&self, content, _config) -> Vec<ChunkResult>`: `content`를 `\u{001e}`(RECORD SEPARATOR)로 split → 각 조각 `ChunkResult{content, tokens, chunk_order_index}`(빈 조각 제외). 외부 호출 없음.
- `workspace_pipeline_factory.rs`: `EDGEQUAKE_CHUNKER=passthrough` → `PassthroughStrategy`(기존 `adaptive` 분기 보존).
- 임베딩·엔티티추출·그래프적재 **불변** → W3 엔티티추출 청크별 정상.
- **재빌드** `cargo build --bin edgequake`, `EDGEQUAKE_CHUNKER=passthrough` 재기동(`search_path=public` 금지 — AGE 깨짐).
- 단위테스트: split/empty/순서.

## 6. Component C — kb-pipeline parse 서비스 (축소: parse + community)

위치: `8.kb-pipeline/service`. **insert/chunk 엔드포인트 제거**(insert는 knowledge_base가 edgequake 직접, chunk는 adaptive_chunk 직접). 남는 것:
| 메서드 | 경로 | 입력 | 출력 |
|--|--|--|--|
| POST | `/parse` | multipart `file`, `filename`, `content_type?` | `{ enriched_content, n_blocks, modal_ids }` (W6 parse→blockify→modal enrich; v1 `run_front` 재사용) |
| POST | `/communities/build` | `workspace_id` (202) | W3 커뮤니티 빌드(기존 community 로직 + edgequake DSN) |
| GET | `/healthz` | — | `{status:"ok"}` |
- v1 `/ingest`·`/ingest/submit`·`/ingest/status`·`/chunks`·`/doc`는 **호환 위해 보존**(테스트 green)하되 v2 경로 미사용(추후 제거 가능).

## 7. Component D — knowledge_base 중앙 오케스트레이션

### 7.1 클라이언트 (각 백엔드 = 얇은 HTTP 래퍼)
- `KbPipelineParseClient`: `parse(file) -> {enriched_content, ...}` (kb-pipeline svc /parse). `build_communities(workspace_id)`.
- `AdaptiveChunkClient`(**기존 재사용**): `chunk(text=enriched, doc_name) -> AdaptiveChunkResult{chunks, method_selected, scores, methods_compared}`.
- `EdgequakeKbpClient`: **dedicated edgequake(:8081) 전용** — `ensure_workspace(kb_id)`, `insert(workspace, doc_id, title, chunk_texts) -> {document_id, chunk_count, status}`(청크를 `\u{001e}` join → passthrough 적재 + task 폴링), `delete_doc`, `search(...)`. 기존 `:8080` edgequake_client과 별 인스턴스(config `edgequake_kbp_base_url=:8081`).

### 7.2 `_ingest_kb_pipeline_tail` (오케스트레이션)
1. `on_stage("parse")` → `parse_client.parse(file)` → enriched
2. `on_stage("chunk")` → `adaptive_chunk.chunk(text=enriched, doc_name)` → `result`(chunks + 선택근거)
3. `on_stage("insert")` → `eq_kbp.ensure_workspace(kb_id)` → `eq_kbp.insert(ws, doc_id, title, [c.chunk_text for c in result.chunks])` → document_id, chunk_count
4. **chunks_meta** = `result.chunks`(text/titles_context/pages) + edgequake chunk_id `{document_id}-chunk-{i}` 병합 → `replace_chunks_meta`. **chunking_selection** = `{result.method_selected, result.scores, result.methods_compared}` → `set_chunking_selection`(placeholder 제거). status=ready.
   - 실패 시 `eq_kbp.delete_doc` + status=failed(성공위장 금지). 다른 provider tail 불변.
5. 적재 성공 후 community 빌드 잡 enqueue(기존).

### 7.3 검색 / 프론트
- 검색: kb_pipeline KB는 `eq_kbp.search` (edgequake :8081 직접). (v1 비범위였으나 적재처가 edgequake라 검색도 edgequake.)
- 프론트 `JobList.tsx`: §3 STAGE_ORDER. 문서상세 `chunking_selection` 카드 = **진짜 method/scores/methods_compared** 표시.

## 8. 데이터 계약 / 리스크

- **계약**: `/parse`→enriched str; adaptive_chunk→`{chunks,method_selected,scores,methods_compared}`(현행); edgequake insert→`{document_id,chunk_count,status}`. chunk_id = `{document_id}-chunk-{i}`(edgequake passthrough가 순서대로 부여 — chunks_meta와 일치). workspace_id=kb_id, doc_id=content_hash[:16]. 성공=terminal-ok & chunk_count>0.
- **R1 passthrough 정확성**: edgequake passthrough가 `\u{001e}` 경계 정확 보존 + 청크별 임베딩/추출 수행 + chunk_id `{doc}-chunk-{i}` 순서 일치 라이브 검증.
- **R2 modal split 정확성**: adaptive_chunk modal-split이 edgequake `split_segments`와 동치(원자성·malformed=텍스트·마커0=기존) 단위테스트.
- **R3 선택근거 범위**: adaptive_chunk가 gap 텍스트 기준 채점 → modal 대부분 문서는 근거 빈약. UI "(본문 gap 기준)" 주석.
- **R4 청크 메타 출처**: 표시용 chunks_meta(titles_context/pages)는 **adaptive_chunk** 응답에서, 검색은 **edgequake**. chunk_id로 정렬 일치.
- **R5 재빌드/재기동**: edgequake passthrough 재빌드+재기동, adaptive_chunk 재기동. v1 코드(AdaptiveChunkStrategy/`/ingest`) 보존(롤백).
- **R6 호환**: adaptive_chunk 기존 호출자(마커 없는 입력) 불변; v1 `/ingest`·다른 provider 무영향; 테스트 green.
- **R7 멀티 repo(4곳)**: adaptive_chunk / edgequake(fork) / 8.kb-pipeline(svc) / knowledge_base. 빌드·테스트·재기동 조율.

## 9. 테스트
- adaptive_chunk: modal-split 단위(동치·원자·순서·마커0 회귀) + 방법선택 회귀.
- edgequake: PassthroughStrategy split 단위.
- kb-pipeline svc: `/parse` 계약 테스트(파싱→modal).
- knowledge_base: tail 오케스트레이션(parse→chunk→insert) 단위 + chunks_meta 병합 + chunking_selection 저장 + 검색 + 다른 provider 회귀.
- 라이브 스모크: 실 PDF → 단계 시퀀스(gate→parse→chunk→insert) **각 API 응답마다 ✓** + 문서상세 **진짜 선택근거** + modal 원자성(표=1청크) + 검색.
