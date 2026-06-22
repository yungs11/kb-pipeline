# kb-pipeline v2 — 단계 분해 + modal-aware chunk 백엔드 설계 스펙

작성 2026-06-22 · 상태: 설계 승인됨(대화) → 스펙 리뷰 → 구현계획
개정: v2′ — modal 분리/조립을 **adaptive_chunk 백엔드 내부**로 이동(사용자 결정).

## 1. 동기 / 목표

v1 kb_pipeline은 `POST /ingest` **하나의 불투명 호출**에 parse→(modal)→adaptive 청킹→임베딩→엔티티추출→적재를 통째로 넣었다. 결과:
- **진행 단계(phase)가 안 보임** — "청킹이 끝났다"를 정확히 표시 못 함.
- **청킹 선택근거(method_selected/scores)가 버려짐** — 청킹을 edgequake `AdaptiveChunkStrategy`가 내부에서 수행하고 그 선택근거를 폐기 → UI엔 placeholder `kb_pipeline_internal`.

**목표(v2′)**: 청킹 오케스트레이션을 단계로 분해하고, **청킹의 모든 관심사(modal 원자성 + 방법선택 + 점수)를 adaptive_chunk 백엔드 한 곳에 모은다.** 각 단계를 명시적 API로 호출해 (a) phase별 진행표시 정확, (b) 선택근거 자연 캡처·표시, (c) edgequake는 "임베딩+엔티티추출+그래프적재+검색" 저장엔진으로 단순화.

**비목표**: 검색 UI(W5), W4 DB-RLS.

## 2. 아키텍처 (오케스트레이션 = knowledge_base 워커)

```
knowledge_base 워커 (오케스트레이터)
  Stage 0: doc_guard                 (기존, 변경 없음)
  phase "parse"  → kb-pipeline svc  POST /parse        → enriched content (W6 parse → blockify → modal enrich → 〈MODAL〉 마커)
  phase "chunk"  → adaptive_chunk    POST /chunk (직접) → { chunks[], method_selected, scores, methods_compared }
                       (adaptive_chunk 가 〈MODAL〉 내부 분리·원자 유지 + gap 청킹 + 조립 — Component A)
  phase "insert" → kb-pipeline svc  POST /insert        → edgequake(passthrough chunker): 임베딩+엔티티추출+그래프적재
                       → { document_id, chunk_count, status }   (insert 동안 edgequake doc status 폴링 = embed/extract/store 세부 phase)
  → repo.replace_chunks_meta(chunks_meta) + set_chunking_selection(real) → status=ready
  phase "community"(W3) → 적재 성공 후 비차단 잡 (기존)
  검색: knowledge_base → edgequake (기존)
```

각 phase 전후로 `jobs.set_state(job_id, stage=phase)` → 프론트가 실제 단계 체크.

## 3. Component A — adaptive_chunk 백엔드를 modal-aware로 (핵심)

위치: `/Users/xxx/workspace/99.projects/adaptive_chunk`(로컬 서비스, git 아님 — 수정 가능). 구조: `service/main.py`(POST /chunk) → `service/runner.py::run_chunk` → `adaptive_chunk/selector` 방법선택+점수.

### 3.1 modal-aware run_chunk
`run_chunk`(또는 그 앞단 래퍼)에 다음을 추가:
1. **segment**: 입력 텍스트를 `〈MODAL …〉…〈/MODAL〉` 경계로 분할(`〈`=U+3008, `〉`=U+3009; 여는 마커는 `〈MODAL`로 시작해 `〉`로 닫힘; 닫는 마커 `〈/MODAL〉`). 미종결 마커는 일반 텍스트로 취급(내용 유실 금지).
2. **gap 청킹 + 방법선택**: modal 사이 텍스트 gap을 **현행 방법선택·점수 로직 그대로** 청킹(여러 gap은 합쳐 한 번 채점 → 일관된 method_selected/scores; 경계는 gap별 유지). 즉 채점/선택은 **gap 텍스트 기준**.
3. **modal 원자 청크**: 각 〈MODAL〉 스팬 = 청크 1개(쪼개지 않음). titles_context/pages는 인접 컨텍스트 상속(없으면 None).
4. **조립**: gap 청크 + modal 원자 청크를 **원래 순서대로** 합쳐 `chunks[]`, `chunk_index` 재부여.
5. **반환**: 현행 R1 형식 그대로 `{ method_selected, scores, methods_compared, chunks, timing_ms }` — 선택근거는 gap 기준(§6 R3).

### 3.2 입력 옵션
- `POST /chunk` body에 `modal_aware: bool=true`(기본) 옵션 추가(미지정 시 켜짐; 기존 호출자 호환 위해 마커 없으면 자연히 일반 청킹과 동일). 마커 0개면 동작 = 기존과 동일(전체가 gap 1개).
- 비동기 `POST /chunk/jobs` 동일 적용(실문서).
- **외부 호환**: 〈MODAL〉 마커가 없는 기존 입력은 동작 불변(회귀 0).

### 3.3 테스트
modal split 동치성(edgequake `split_segments`와 동일 규칙), 원자성(스팬 안 쪼갬), 조립 순서, malformed 마커=텍스트, 마커 0개=기존 동작. method_selected/scores/methods_compared 형식 불변.

## 4. Component B — edgequake fork: passthrough ChunkingStrategy

`crates/edgequake-pipeline/src/chunker/`에 **`PassthroughStrategy`** 추가:
- `async fn chunk(&self, content, _config) -> Vec<ChunkResult>`: `content`를 구분자 `\u{001e}`(RECORD SEPARATOR, 본문 미출현 제어문자)로 split, 각 조각을 `ChunkResult{content, tokens, chunk_order_index}`로 그대로 반환(빈 조각 제외). 외부 호출 없음.
- `workspace_pipeline_factory.rs`: `EDGEQUAKE_CHUNKER=passthrough` → `PassthroughStrategy` 주입(기존 `adaptive` 분기 옆, 기존 코드 보존).
- 임베딩·엔티티추출·그래프적재 파이프라인 **불변** → W3 GraphRAG 엔티티추출은 인서트 시 청크별 정상 수행.
- **재빌드**: `cargo build --bin edgequake`. 전용 edgequake를 `EDGEQUAKE_CHUNKER=passthrough`로 재기동(`search_path=public` 금지 — AGE 깨짐).
- 단위테스트: split/empty/순서.

## 5. Component C — kb-pipeline 서비스 (단순화: parse + insert)

위치: `8.kb-pipeline/service`. v1 monolith `/ingest`는 호환 유지(테스트). 신규 2엔드포인트:
| 메서드 | 경로 | 입력 | 출력 |
|--|--|--|--|
| POST | `/parse` | multipart `file`, `filename`, `content_type?` | `{ enriched_content, n_blocks, modal_ids }` (W6 parse→blockify→modal enrich; v1 `run_front` 재사용) |
| POST | `/insert` | json `{ chunks, workspace_id, doc_id, title }` | `{ document_id, chunk_count, status }` (청크를 `\u{001e}` join → passthrough edgequake 적재; `ensure_workspace`·task 폴링 v1 재사용) |
| GET | `/insert/status` | `workspace_id, document_id` | `{ phase, chunk_count, terminal, succeeded }` (edgequake doc status 중계, v1 재사용) |
| POST | `/communities/build` | `workspace_id` (202) | 기존 |
| GET | `/healthz` | — | `{status:"ok"}` |

청킹은 **우리 서비스에 없음** — knowledge_base가 adaptive_chunk 직접 호출.

## 6. Component D — knowledge_base 통합 (tail 오케스트레이션)

- **KbPipelineClient**: `parse()`, `insert()`, `insert_status()` 추가(v1 submit/ingest 호환 유지).
- **deps**: `adaptive_chunk`(기존 `AdaptiveChunkClient`/`AdaptiveChunkLike`)를 **kb_pipeline 경로에서도 사용**(기존엔 adaptive provider 전용 — 주입 조건만 확장).
- **`_ingest_kb_pipeline_tail`** 재작성(오케스트레이션):
  1. `on_stage("parse")` → `kb_pipeline.parse(file)` → enriched
  2. `on_stage("chunk")` → `deps.adaptive_chunk.chunk(text=enriched, doc_name=...)` → `AdaptiveChunkResult`(chunks + method_selected/scores/methods_compared)
  3. `on_stage("insert")` → `kb_pipeline.insert(chunks, workspace, doc_id, title)` → document_id; insert 동안 `insert_status` 폴링하며 `on_stage(edgequake phase)`
  4. `fetch_chunk_meta`→`replace_chunks_meta` + **`set_chunking_selection({method_selected,scores,methods_compared})`**(placeholder 제거) → ready
  - 실패 시 `delete_doc`+status=failed(성공위장 금지). 다른 provider tail 불변.
- **워커**(`tasks.py`): kb_pipeline 경로 `on_stage` 콜백으로 phase별 `set_state`.
- **프론트**(`JobList.tsx`): `STAGE_ORDER = gate→parse(파싱)→chunk(청킹)→extracting(추출)→embedding(임베딩)→storing(적재)→persist_meta(메타저장)`.
- **문서상세 카드**: 기존 `chunking_selection` 렌더 그대로 — 이제 **진짜 method_selected/scores/methods_compared** 표시.

## 7. 데이터 계약 / 리스크

- **계약**: `/parse`→enriched str; adaptive_chunk `/chunk`→`{chunks,method_selected,scores,methods_compared}`(현행); `/insert`→`{document_id,chunk_count,status}`. workspace_id=kb_id, doc_id=content_hash[:16]. 성공=status terminal-ok & chunk_count>0.
- **R1 passthrough 정확성**: edgequake passthrough가 `\u{001e}` 경계를 정확 보존하고 청크별 임베딩/엔티티추출을 수행하는지 라이브 검증.
- **R2 modal split 정확성**: adaptive_chunk modal-split이 edgequake `split_segments`와 동치(원자성·malformed=텍스트·마커0=기존) 단위테스트.
- **R3 선택근거 범위**: adaptive_chunk가 **gap 텍스트 기준**으로 선택 → modal 대부분/gap 희소 문서는 근거 빈약. UI에 "(본문 gap 기준)" 주석. 대표값임을 문서화.
- **R4 이중 청킹 제거**: adaptive_chunk 1회만, edgequake passthrough(청킹 안 함) → 중복 없음.
- **R5 재빌드/재기동**: edgequake passthrough 재빌드 + 재기동. adaptive_chunk 서비스 재기동(modal-aware). v1 코드(AdaptiveChunkStrategy/`/ingest`) 보존(롤백 가능).
- **R6 호환**: adaptive_chunk 기존 호출자(마커 없는 입력) 동작 불변; v1 `/ingest`·다른 provider 무영향; 테스트 green 유지.
- **R7 멀티 repo**: 변경 4곳 — adaptive_chunk(서비스) / edgequake(fork) / 8.kb-pipeline(service) / knowledge_base(tail+front). 빌드·테스트·재기동 조율 필요.

## 8. 테스트
- adaptive_chunk: modal-split 단위테스트(동치성·원자성·순서·마커0 회귀) + 기존 방법선택 회귀.
- edgequake: PassthroughStrategy split 단위테스트.
- kb-pipeline svc: `/parse`·`/insert`(passthrough edgequake mock) 계약 테스트.
- knowledge_base: tail 오케스트레이션(parse→adaptive_chunk→insert) 단위테스트 + chunking_selection 저장 검증 + 다른 provider 회귀.
- 라이브 스모크: 실 PDF → phase 시퀀스(parse→chunk→insert/embed/extract/store) + **문서상세에 진짜 선택근거(method/scores) 표시** + modal 원자성(표가 한 청크) + 청크 격리.
