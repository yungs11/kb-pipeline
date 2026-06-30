<!-- plan-version: v3 -->
<!-- codex-validation: READY v3 (ultracode adversarial) at 2026-06-30 -->
<!-- v1→v2: ultracode 적대적검증 반영 — (1)/insert 는 post_document 아닌 submit_document 경유[BLOCKER], (2)metadata 는 skip=True 때만 첨부(기본 byte-identical), (3)프론트 라디오는 isExcelNative 삼항 바깥, (4)A3 근거 정정, +minor. -->
<!-- v2→v3: 2차 적대적검증 READY(blocker/major 0). minor 반영 — A3 기제를 _PHASE_SUCCESS 제외(edgequake.py:216/271)로 정밀화, A0 merge는 Some&&as_object 일 때만, C3 KbPipelineLike Protocol(pipeline.py:197-199)에 extract_graph 추가(mypy), C2 라인정정(kb.py:415-422/340-342), 사후-skip 전환 비범위 명시. -->

# 엑셀 그래프(관계) 추출 스킵 — UI 라디오 + 문서단위 metadata 게이트 (구현계획)

작성 2026-06-30 · provider=kb_pipeline 전용 · 3레포(knowledge_base / 8.kb-pipeline facade / edgequake)

## 0. 한 줄 요약
업로드 UI에 **"관계(그래프) 추출 / 미추출" 라디오**를 추가하고, **엑셀(chunk_strategy=excel_rag_parser)이면 '미추출'로 고정(disabled)** 한다. 이 불리언을 knowledge_base → facade `/insert` → edgequake `metadata.skip_graph_extraction` 로 흘려, edgequake가 **그 문서만 엔티티/관계 추출 서브스텝을 건너뛴다**. 청킹·청크임베딩·벡터검색은 그대로 유지(분리 경로). 추출 0건이어도 `partial_failure`가 아니라 `completed`로 처리한다.

## 1. 목표 / 비범위

### 목표
- 엑셀처럼 그래프 추출이 무의미·고비용인 문서를 **벡터 적재/검색만** 하고 그래프 추출(qwen 호출)을 건너뛴다.
- 사용자가 비-엑셀 문서에 대해서도 라디오로 선택 가능. **엑셀은 강제 미추출(변경 불가)**.
- 다른 provider(dify/edgequake/raganything/ragflow) 및 비-kb_pipeline 경로 **완전 불변**.

### 비범위
- workspace 전역 추출 토글(config.enable_entity_extraction env화) — 채택 안 함(문서단위가 더 국소적).
- edgequake **동기 업로드 경로**(async_processing:false) skip — facade는 항상 async라 불필요. (sync 경로 metadata-merge는 후속.)
- 이미 적재된 문서의 그래프 사후 삭제/재처리 — 별도. **이미 그래프가 생성된 문서를 사후에 skip 으로 전환**도 비범위(checkpoint-resume(text_insert.rs:349)는 skip 게이트를 우회 — 항상-skip 엑셀엔 무해하나, 전환 시나리오는 보장 안 함).

## 2. 아키텍처 (신호 흐름)

```
[UI UploadPanel 라디오] extractGraph(bool)  ── 엑셀이면 false 고정(disabled)
   │ ingestWithMethod(body.extractGraph)
[knowledge_base 백엔드]
   POST /kb/{id}/documents/ingest  IngestWithMethodRequest.extract_graph
   → 큐 enqueue(extract_graph)  → ingest_document_task → KbContext.extract_graph
   → _ingest_kb_pipeline_tail: kbp.insert(..., extract_graph=kb.extract_graph)   ※ kb_pipeline 분기에서만
[facade /insert]  body.extract_graph → EdgequakeClient.insert_chunks(skip_graph=not extract_graph)
   → POST edgequake /api/v1/documents  metadata={"skip_graph_extraction": <bool>}
[facade /insert]  insert_chunks(skip_graph) → submit_document(skip_graph)  ※ post_document 아님
[edgequake]
   text_upload(async): request.metadata 병합 → TextInsertData.metadata
   text_insert: skip_graph_extraction 읽음 → process_with_resilience_cancellable_opts(skip_extraction=true)
   processing: 추출 블록만 스킵(청킹·임베딩 유지) → extractions=[] → 그래프 upsert 빈 배치(무해)
   status: entity_count==0 이어도 skip이면 completed  ※ 안 고치면 partial_failure→facade document_phase 맵에서 failed(edgequake.py:210)
```

**핵심 불변식**: skip은 **추출 서브스텝만** 끈다. `chunk_async`(청킹)와 `finish_document_processing`(청크 임베딩·lineage)은 추출 게이트 **바깥**이라 그대로 실행됨 → 벡터검색 무영향.

## 3. 컴포넌트 A — edgequake (Rust). 권위 출처: 코드.

### A0. (선행 버그수정) async 경로가 request.metadata 를 버림
`crates/edgequake-api/src/handlers/documents/upload/text_upload.rs` (async 분기, 현재 ~226–236):
`TextInsertData.metadata` 를 `{document_id,title,tenant_id,workspace_id}` 로만 만들고 **사용자 `request.metadata` 를 무시**한다. → `request.metadata`가 **`Some` 이고 `.as_object()` 일 때만** 이 기본 object에 **merge**(객체 아니면 무시, 에러 아님). **키충돌 정책: 4개 보호키(document_id, title, tenant_id, workspace_id) 모두 기본값 우선**(사용자 metadata가 이들을 덮어쓰지 못하게) — text_insert 가 metadata에서 tenant_id(테넌트 스코프)·document_id 도 읽으므로. 사용자 키(skip_graph_extraction 등 그 외)는 추가. 이 수정 없이는 skip 신호가 edgequake 내부까지 도달 못 함.
- 검증 필요(구현 시): 정확한 라인/필드명은 현재 코드로 재확인(스펙의 라인번호는 참고치). `TextInsertData` 정의(필드 `metadata: Option<serde_json::Value>`)와 일치하는지 확인.

### A1. 추출 게이트에 per-document 스킵 주입 (신규 opts 메서드, 호출자 무영향)
`crates/edgequake-pipeline/src/pipeline/processing.rs`:
- 현재 `process_with_resilience_cancellable(&self, document_id, content, progress_callback, cancel_token, embed_progress)` 내부에 추출 게이트:
  `if self.config.enable_entity_extraction || self.config.enable_relationship_extraction { if let Some(extractor)= ... { resilient_extract_parallel(...) } }`.
- **방법**: `process_with_resilience_cancellable` 본문을 `..._opts(..., skip_extraction: bool)` 로 추출하고, 기존 메서드는 `skip_extraction=false` 로 위임(시그니처 불변 → batch_upload.rs:160 / file_upload.rs:277 / text_upload.rs sync:300 / process_with_resilience 래퍼 전부 무영향). 추출 게이트 조건을 `(... || ...) && !skip_extraction` 로 변경.
- 청킹/`finish_document_processing` 는 그대로 호출 → chunks·청크임베딩·lineage 유지. extractions 는 빈 Vec.
- 참고: `finish_document_processing`(processing.rs:37–42)도 같은 config 게이트로 link/aggregate 를 감싸지만 **빈 extractions 에 무해(no-op)** — `generate_all_embeddings`(청크임베딩)는 게이트 밖이라 항상 실행. skip=true 면 stats.entity_count 는 0 유지.
- (`ProcessingResult` 수동 구성 금지 — 스펙 초안의 "result.chunks 재사용/수동 ProcessingResult" 접근은 청크·임베딩 유실 위험이라 **폐기**.)

### A2. text_insert: 플래그 읽기 + opts 호출
`crates/edgequake-api/src/processor/text_insert.rs`:
- source_type 읽는 패턴(현재 ~52–59) 옆에 `skip_graph_extraction = data.metadata…get("skip_graph_extraction").as_bool().unwrap_or(false)` 추가.
- ~417 의 `process_with_resilience_cancellable(...)` 호출을 `_opts(..., skip_extraction=skip_graph_extraction)` 로 교체.

### A3. status: 의도된 추출 0건을 partial_failure 로 강등하지 않기
`text_insert.rs` final_status 분기(현재 ~1031–1042 `entity_count==0 && chunk_count>0 → partial_failure`):
- 같은 `skip_graph_extraction` 플래그가 true면 이 분기에서 `"completed"` 로. (그 외 분기 불변: all_chunks_failed→failed, chunk_count==0→failed, storage_errors→partial_failure.)
- **근거(정정)**: `/insert` 경로의 성공판정은 facade `insert_chunks` 가 `document_phase` 를 폴링해 얻는 `succeeded` 인데, 그 load-bearing 게이트는 **`_PHASE_SUCCESS={completed,indexed}` 포함 여부**(edgequake.py:216, 판정 edgequake.py:271)다. `partial_failure` 는 이 집합에 없으므로 → 가드 없으면 skip 문서(0엔티티)가 partial_failure 가 되어 `succeeded=False` → **/insert 가 status="failed" 반환** → knowledge_base 적재 실패. (`_PHASE_MAP` 의 `partial_failure→failed`(edgequake.py:210)는 coarse 라벨일 뿐 판정 기제 아님.) 이전 v1 의 "PG completed→indexed / `_POLL_OK`" 서술도 이 경로 기제 아님 — 정정. 추가로 partial_failure 회피는 UI/대시보드·`costs.rs`(non-completed 필터)에도 정합.

### A4. 안전성(조사로 확인됨, 재확인 대상)
- 그래프 upsert: `if !nodes_batch.is_empty()` / `if !edges_batch.is_empty()` 가드(**text_insert.rs:897/979**) → 빈 추출 안전.
- 엔티티 임베딩 루프: 빈 Vec면 0회 — 파이프라인측 `pipeline/helpers/embeddings.rs:329` **및** storage측 store_entity_embedding 루프(text_insert.rs:920) 둘 다 0-iter.
- lineage: `Option`, 엔티티 없는 lineage도 `build_lineage` 안전(빈 그래프).
- checkpoint(처리 재개): skip 시 추출이 없어 checkpoint 비용 없음 → 재개 시 chunk/embed 경로만 저렴하게 재실행. 강제 스킵 불필요. (구현 시 1회 확인.)
- 표 전처리(`table_preprocessor`, text_insert.rs:312)는 skip 과 무관하게 그대로 — 엑셀 표 텍스트화에 영향 없음.

### A5. 빌드/배포
- `cargo build` (워크스페이스 `edgequake/edgequake/`) → 전용 edgequake 재기동(`service/scripts/start_dedicated_edgequake.sh`, `EDGEQUAKE_CHUNKER=passthrough` 유지).

## 4. 컴포넌트 B — facade (8.kb-pipeline, Python)

### B1. `/insert` 에 옵션 필드 추가
`service/app.py` `insert(...)`: `extract_graph: bool = Body(True, embed=True)` 추가. `eq.insert_chunks(..., skip_graph=not extract_graph)` 로 전달. 기본 True(=기존 동작: 추출 수행) → 비-엑셀/미지정 회귀 0.
- (선택) `/ingest`·`/ingest/submit` 에도 동일 옵션 추가하되 기본 True. 엑셀 자동라우팅 경로가 아니므로 이번엔 `/insert` 만 필수, 나머지는 additive 옵션으로만.

### B2. EdgequakeClient 에 skip 전달 — **submit_document 경유**(정정)
> v1 정정[BLOCKER]: `/insert` 는 `insert_chunks`(edgequake.py:356) → **`submit_document`**(218) → `document_phase` 폴링이다. `post_document`(110)는 `service/ingest.py` 전용이라 /insert 신호와 무관. metadata 주입 대상은 **submit_document**.
`service/edgequake.py`:
- `insert_chunks(..., skip_graph: bool=False)` → `submit_document(content, ..., skip_graph=skip_graph)` (356행 호출에 인자 추가).
- `submit_document(..., skip_graph: bool=False)`: 제출 본문(234행 `json={"content","title","async_processing":True}`)에 **`skip_graph` 가 True일 때만** `"metadata": {"skip_graph_extraction": True}` 를 더한다. **기본(False)이면 metadata 키를 넣지 않아 오늘과 byte-identical**(MAJOR#2). content/title/async_processing 불변.
- (선택) `post_document` 에도 동일 옵션을 추가해 `/ingest`(ingest.py) 일관성 확보 가능하나 이번 必修 아님 — additive.

### B3. 테스트(facade)
- `/insert` body `extract_graph=false` → submit_document POST 본문에 `metadata.skip_graph_extraction=true` 포함(httpx mock/respx). `extract_graph` 미지정/true → **metadata 키 부재**(오늘과 byte-identical) — 명시 단언.
- `insert_chunks(skip_graph=True/False)` → submit_document 본문 단위.

## 5. 컴포넌트 C — knowledge_base (소비자, /99.projects/shinhan_trust/knowledge_base)

> 모든 변경은 **provider==kb_pipeline 분기 한정**. 타 provider tail 무변경.

### C1. 프론트
- `frontend/components/UploadPanel.tsx`:
  - 상태 `const [extractGraph,setExtractGraph]=useState(true)` 추가.
  - 엑셀 판정 기존값 `isExcelNative = preview?.chunk_strategy === "excel_rag_parser"`(현재 ~189) 재사용.
  - 그래프추출 라디오 필드셋을 **`isExcelNative ? (배너) : (method fieldset)` 삼항 바깥**(공통 위치, ingest 버튼 위)에 추가 — method 라디오는 비엑셀 else 분기에만 렌더되므로 그 안에 두면 엑셀에서 안 보인다(MAJOR#3). 공통 위치라야 엑셀에서도 렌더되어 **disabled + 강제 false** 표시 가능. 라벨: "엑셀은 그래프 미추출 고정".
  - `ingestWithMethod(...)` 호출 body에 `extractGraph: isExcelNative ? false : extractGraph` 추가(현재 ~235–239).
- `frontend/lib/api.ts` `ingestWithMethod`(현재 ~306–329): body 타입 `extractGraph?:boolean`, payload `extract_graph?:boolean`, `if(body.extractGraph!==undefined) payload.extract_graph=body.extractGraph`.

### C2. 백엔드 스키마/라우터
- `backend/app/schemas/parse_preview.py` `IngestWithMethodRequest`: `extract_graph: bool = True` 추가.
- `backend/app/routers/kb.py` `ingest_with_method`(enqueue 블록 kb.py:415–422): `extract_graph=(body.extract_graph if (kb.provider or "dify")=="kb_pipeline" else None)` 를 큐 kwargs 로. (이 핸들러엔 지역 `provider` 변수 없음 — `kb` 객체에서 직접 판정, kb.py:340–342.)

### C3. 워커/컨텍스트/tail
- `backend/app/workers/tasks.py` `ingest_document_task` 시그니처에 `extract_graph: bool|None=None` 추가, `KbContext(... extract_graph=extract_graph)`.
- `backend/app/core/pipeline.py` `KbContext` 에 `extract_graph: bool|None=None` 필드.
- `_ingest_kb_pipeline_tail` 의 `kbp.insert(...)`(현재 ~2092–2097): `extract_graph=kb.extract_graph` 전달. None이면 클라이언트가 미첨부(기존 동작).
- **mypy 가드**: `KbPipelineLike.insert` Protocol(pipeline.py:197–199)과 테스트 더블에도 `extract_graph: bool|None=None` 추가 — 안 하면 pipeline.py:2092 호출에서 새 kwarg 가 타입 에러.

### C4. 클라이언트
- `backend/app/clients/kb_pipeline_client.py` `insert(..., extract_graph: bool|None=None)`: 현재 인라인 리터럴 json(kb_pipeline_client.py:237–242)을 `body={...}` 로 호이스트한 뒤 `if extract_graph is not None: body["extract_graph"]=extract_graph` → `json=body`.

### C5. provider 격리 검증
- dify/edgequake/raganything/ragflow tail 호출부 무변경. 라우터에서 비-kb_pipeline 이면 extract_graph=None → 큐~클라이언트 전 구간 미첨부 → facade 기본 True 동작과 동일.

## 6. 검색/부작용 분석 (왜 안전한가)
- **벡터검색 무영향**: edgequake `engine.rs` `if mode.uses_vector_search()` 분기는 그래프와 독립. skip 문서의 청크는 임베딩되어 그대로 검색됨(추출 게이트 바깥에서 임베딩 생성).
- **그래프/커뮤니티**: skip 문서는 엔티티/관계·community 리포트에서 빠짐(의도). 같은 KB의 비-skip 문서 그래프는 정상.
- **혼합 KB 안전**: 그래프 검색은 workspace 단위 popular labels라 skip 문서가 0 엔티티여도 크래시/빈결과 없음.
- **전부-엑셀 KB**: 그래프가 비면 글로벌/커뮤니티 검색은 그래프 기반 답을 못 내지만 벡터 검색은 정상. `build_workspace_communities` 가 빈 `fetch_graph`(노드 0)에서 안전히 빈 결과로 끝나는지 구현 시 1회 확인(예외 swallow 경로라 치명 아님).
- **이득**: qwen 추출(동기 블로킹 단계) 생략 → 2천청크급 엑셀 적재 시간·비용 대폭 절감.

## 7. 테스트 / 수용 기준
- **edgequake(Rust)**: skip=true 문서 → status completed, entity_count=0, chunk_count>0, 청크 임베딩 존재(벡터검색으로 청크 히트). skip=false 동일문서 → 엔티티 추출 정상. 의도적 skip의 0엔티티가 partial_failure 로 안 빠지는지(회귀: 진짜 LLM 실패 0엔티티는 여전히 partial_failure).
- **facade**: §B3.
- **knowledge_base**: kb_pipeline+엑셀 업로드 → extract_graph=false 가 facade까지 도달(프론트 disabled 고정). 비엑셀 라디오 false 선택도 전달. 타 provider 미첨부(격리).
- **E2E 스모크**: 엑셀 1건 적재 → /search 로 엑셀 청크 검색됨 + 그래프 보기에 엑셀 엔티티 없음 확인.

## 8. 리스크 / 오픈이슈
1. edgequake 구조체/라인번호는 스펙의 조사 기반 — 구현 시 `ProcessingResult`/`ProcessingStats`/`TextInsertData` 실제 정의로 재확인(컴파일러가 강제).
2. A0 metadata merge 시 키 충돌 정책(기본키 우선) 명시 — 사용자 metadata 가 document_id 등 덮어쓰지 않게.
3. text_upload sync 경로 metadata-merge 는 이번 비범위(facade async 전용). 추후 일반화.
4. 프론트 라디오 disabled 로직 방향(엑셀일 때 disabled) 정확히 — `disabled = isExcelNative || phase!=="ready"`.
5. 3레포 배포 순서: edgequake(재빌드/재기동) → facade(재기동) → knowledge_base(백엔드/프론트). 하위호환(기본 추출 ON)이라 부분배포 중에도 회귀 없음.

## 9. 변경 파일 요약
| repo | 파일 | 종류 |
|---|---|---|
| edgequake | `crates/edgequake-api/src/handlers/documents/upload/text_upload.rs` (async metadata merge) | 수정 |
| edgequake | `crates/edgequake-pipeline/src/pipeline/processing.rs` (opts 메서드 + 게이트) | 수정 |
| edgequake | `crates/edgequake-api/src/processor/text_insert.rs` (플래그 읽기·opts 호출·status guard) | 수정 |
| facade | `service/app.py` (/insert extract_graph) | 수정 |
| facade | `service/edgequake.py` (insert_chunks→**submit_document** skip_graph→metadata, skip=True 때만) | 수정 |
| facade | `service/tests/...` | 신규/수정 |
| knowledge_base | `frontend/components/UploadPanel.tsx`, `frontend/lib/api.ts` | 수정 |
| knowledge_base | `backend/app/schemas/parse_preview.py`, `routers/kb.py`, `workers/tasks.py`, `core/pipeline.py`, `clients/kb_pipeline_client.py` | 수정 |
