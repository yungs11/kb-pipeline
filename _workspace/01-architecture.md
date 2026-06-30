# 01 · 아키텍처

> 출처: SoT.md §1–5, SoT.v1.md, `docs/kb-pipeline-process-definition.md` §2–5.
> edgequake 실제 Rust 워크스페이스 = `edgequake/edgequake/`(자체 Cargo.toml; crates/·migrations/ 여기). 루트 `edgequake/crates/` 는 별도 crate 세트라 무시.

---

## 1. 전체 파이프라인

```
[1 Parse]        확장자별 최고급 파서 → "markdown + inline HTML 표"
       ↓
[2 Blockify]     hybrid_to_blocks() → 블록 리스트(content_list류)        [parse-svc]
       ↓         (VLM 경로는 서비스 elements[] 를 직접 매핑)
[3 Modal enrich] 표/이미지/수식 블록 → LLM 서술 → content 에 atomic 인라인  [parse-svc]
       ↓         단일 enriched content 스트림 (텍스트 + 서술된 모달, modal_id 마킹)
[4 Chunking]     facade /chunk → adaptive_chunk(atomic_markers) → ChunkResult[]  [facade 소유]
       ↓
[5 Insert]       edgequake: 엔티티/관계 추출 → 임베딩 → 그래프 → 단일 Postgres(pgvector+AGE) 적재(RLS)
       ↓
[6 Community]    edgequake AGE → Louvain detect_communities + LLM 리포트 — 배치(스케줄/임계/온디맨드)
       ↓
[7 Search]       pgvector KNN + AGE 순회 + community 리포트 머지, per-KB RLS
```

**오케스트레이션 경로**
- **표준(단계별)**: 소비자(kb-backend)가 facade `/parse` → `/chunk` → `/insert`(+poll `/insert/status`)를 직접 순차 호출. (one-shot 아님)
- **블로킹 변형 `/ingest`**: parse→chunk→insert 한 호출 → `{document_id, chunk_count, status, chunking_selection, edgequake_workspace_id}`.
- **비동기 변형 `/ingest/submit`**: FRONT(parse→blockify→modal_enrich) 동기 수행 후 edgequake async 제출 → `{document_id, status:"submitted"}`. 소비자는 `GET /ingest/status?workspace_id&doc_id` 폴링.

---

## 2. facade (kb-pipeline, :19000) — 오케스트레이터

`service/app.py`. 외부 소비자(knowledge_base 백엔드)에게 capability 를 노출하고 다운스트림을 숨긴다. **청킹과 모달 원자성을 소유**한다.

**노출 capability**: `/parse`, `/chunk`, `/insert`(+`/insert/status`), `/search`, `/ingest`, `/ingest/submit`, `/communities/build`.

**핵심 책임**
- **워크스페이스 해석 소유**: 모든 핸들러가 `eq.ensure_workspace(workspace_id)` 로 kb id 를 edgequake workspace UUID 로 변환 → 검색·적재를 workspace 스코프로 격리(교차 스코핑 누출 0, 실측).
- **청킹 소유**: `/chunk` 가 adaptive_chunk 허브(:18060)를 모달 마커(`atomic_markers`)와 함께 호출 → 모달 원자성을 facade 에서 강제. (§4 참조)
- **passthrough 묶기**: 적재 시 청크 텍스트를 U+001E(`PASSTHROUGH_SEP`)로 join 해 단일 문서로 만들어 edgequake 에 제출 → edgequake PassthroughStrategy 가 같은 바이트로 분할 → 상류 청커 결과와 1:1 정합.
- **타이밍 passthrough**: `/chunk` 응답에 `timing_details`(adaptive_chunk methods/metrics), `/insert` 응답에 `phases`(edgequake phase 근사) 통과.
- **`edgequake_workspace_id` 반환**: `/insert`·`/ingest` 응답에 포함 → kb-backend 가 `KB.edgequake_workspace_id` 로 영속(그래프 보기 팝업이 X-Workspace-ID 로 edgequake 직접 호출).

---

## 3. parser (parse-svc, :19001) — Parse + Blockify + Modal Enrich

`parse_service/app.py`. 한 번의 `POST /parse` 안에서 FRONT 3단계를 연속 수행해 **enriched_content + 모달 스팬 + 페이지 스팬**을 산출한다. Excel(xlsx/xlsm/xls)은 parse-svc 가 아니라 excel-parser(:18055)가 LLM 없이 처리한다.

### 3.1 Parse — 확장자별 파서 라우팅 (`PARSER_ROUTING`, 기본 `markitdown`)

| 확장자 | 라우팅 | 파서 | `<table>` HTML | 비고(실측) |
|--------|--------|------|----------------|-----------|
| PDF | `structural` | **OpenDataLoader** (`markdown_with_html=True`) | ✅ (06=70개, pipe=0) | JRE 11+ 의존. 문서당 .md 1개 → `<<<ODL_PAGE_BREAK>>>` sentinel 로 페이지 복원. JVM 1회 호출 |
| PPTX / DOCX | `structural` | **OCR·VLM(:18050)** | ✅ | markitdown 은 colspan/rowspan 을 parse 시점에 평탄화 → 병합 중요 office 는 structural 라우팅 |
| XLSX | `markitdown` | MarkItDown / (excel-parser :18055) | — | 페이지 개념 없음 → page=1 강등. `KBP_EXCEL_URL` 로 주입 가능 |
| 단일 이미지 | 강제 | **OCR·VLM(:18050)** | ✅ + elements[] | `_IMAGE_EXTS`={png,jpg,jpeg,gif,bmp,tif,tiff,webp} |
| 스캔 PDF / 스캔 페이지 | 보충 | **OCR(:18050)** | ✅ | 글자 거의 없는 페이지만 렌더 → OCR 보충(혼합 PDF, best-effort 비치명) |

- **표준 중간표현**: "markdown + inline HTML 표". 표는 절대 pipe 로 납작화 금지.
- 문서 ID 폴백 = `sha256(file_bytes).hexdigest()[:16]`(orchestrator 동일 식 → MinIO 키 일치). 파일명 정규화 = `_safe_basename`(경로 탈출 차단). 비표시문자 제거 = PUA(U+E000–U+F8FF).
- OCR·VLM 계약: `POST {ocr_url}/api/v1/ocr`, multipart `file` + `strategy=hybrid`, timeout 600s. `content.{markdown|text}` 우선, 없으면 `elements[*].content`.

### 3.2 Blockify — `hybrid_to_blocks()` / `elements_to_blocks()`

`kb_pipeline/blockify.py`. "markdown + inline HTML 표"를 **블록 리스트**로 변환(`markdown-it-py(html=True).enable("table")`):
- `html_block` + `<table` → `{type:"table", table_body:<HTML 그대로>, page_idx}`
- `heading_open` → `{type:"text", text, text_level:N, page_idx}`
- `table_open`(markitdown pipe표) → HTML 렌더 → table 블록
- `<img>`/`![]()` → image, `$$`/math → equation, 그 외 → text
- **VLM 경로 예외**: 서비스 `elements[]`(category=text/table/image/equation/title)를 직접 블록으로 매핑(가장 충실).

규칙: ① 블록 1개 = 구조 단위 1개(문서 순서 유지), ② 표는 HTML 보존, ③ `page_idx` 부여, ④ `text_level` 로 섹션 계층 유지.

**page_idx / 페이지 이미지**: canonical 1-based. OCR elements 보통 0-based → `page_number = page_idx + 1` 정규화. 페이지 이미지는 `parse_service/pdf_pages.py:render_pdf_pages`(PyMuPDF, dpi=300, jpg q=90)로 JPEG 래스터화 → MinIO 키 `{docs_id}/{docs_id}_{p}.jpeg`. MinIO 미설정 시 업로드만 skip.

### 3.3 Modal Enrich — 표/그림/수식 LLM 서술

`kb_pipeline/modal.py:enrich_with_spans`. raganything `modalprocessors` 의 **구조만** 차용(엔진 채택 아님). 모달 블록마다:
- **table/equation**: HTML/LaTeX → **텍스트 LLM** 서술(비전 불요)
- **image/figure**: `img_path` → **비전 LLM** 서술

**합류 방식(고급안: 독립 모달청크 + 엔티티)**: 서술을 atomic 마킹 블록으로 인라인.
```
〈MODAL id="T1" type="table"〉[제목]\n{LLM 서술}\n{table_body HTML}\n[각주]〈/MODAL〉
```
- 마커 괄호 = U+3008/U+3009, **W1 Rust 소비자와 byte-identical**.
- 창 크기 `BEFORE_WINDOW=3`/`AFTER_WINDOW=6`, 병렬 워커 `KBP_MODAL_MAX_WORKERS`(기본 3), id 카운터(표 `T`/수식 `E`/그림 `I`).
- 두 모달이 같은 사이 블록을 다투면 **문서순 앞 모달이 선점**. LLM 실패 시 해당 모달만 흡수 0·요약 생략으로 강등(폴백, 재시도 없음).
- 산출: `enriched_content` + `modal_spans`(`[{id,type,char_range:[start,end]}]` 반열림) + `page_spans`(`[{page_number,char_start,char_end}]`). 세그먼트 join 은 `\n\n`(2자), 이 길이를 running offset 에 반영해 page_spans 계산.

> **기본 동작(중요)**: 모달 LLM 보강은 `KBP_MODAL_ENRICH` 로 토글하며 **기본 off("0")**. off 일 때 LLM 0회로 각 모달을 `summary="", tc=fc=0` 으로 강등해 원본 payload 를 마커로 통과. 모달 원자성·page_spans 는 유지되어 청킹/페이지 지표 무영향, 손실은 표/그림 검색용 의미요약뿐. 현재 `/parse` 는 `vision_llm=None` 이라 그림은 LLM 미호출.

→ 표/그림이 **검색가능 텍스트 + 그래프 노드**로 승격되며, 그래프는 edgequake 추출이 단독 생성(이중생성 없음).

---

## 4. chunker (adaptive_chunk, :18060) — facade 가 소유

> **현행(v2):** 청킹은 **facade `/chunk` 가 소유**한다. facade 가 `adaptive_chunk /chunk` 를 `atomic_markers=〈MODAL〉…〈/MODAL〉` 로 호출해 **모달 원자성까지 facade 에서 강제**한다(adaptive_chunk `service/runner.py` `_segment_atomic`/`DEFAULT_ATOMIC_MARKERS`). 따라서 전용 edgequake 는 `EDGEQUAKE_CHUNKER=passthrough`. (구버전 SoT 의 "adaptive_chunk 엔드포인트가 모달 atomic 미강제" 메모는 atomic_markers 추가로 무효화됨. v1 의 edgequake 내부 `AdaptiveChunkStrategy` 경로 → §변경 02 참조.)

**facade `/chunk` 처리**
- facade 가 모달 마커 `MODAL_ATOMIC_MARKERS=[["〈MODAL","〈/MODAL〉"]]` 를 잡 본문 `options.atomic_markers`(최상위 필드 아님)로 전달. 허브는 모달 *의미*를 모른 채 "이 스팬은 원자적"이라는 사실만 받아 단일 atomic 청크로 유지(marker-aware chunking).
- 나머지 텍스트 갭만 **4방법 경쟁**으로 청킹.
- 대형 입력·느린 방법 대응으로 **비동기 잡**: `POST /chunk/jobs` → `GET /chunk/jobs/{id}` 폴링(간격 3s, 폴링 타임아웃 1800s, 클라 timeout 600s). terminal: `succeeded`/`failed`/`cancelled`.
- 정규화: 허브 R1 `chunk_text`→`text`, `chunk_pages`→`pages`. 산출 `chunks[]`(`chunk_index/text/titles_context/pages`) + `chunking_selection`(`method_selected`, `scores`, `methods_compared`).

**4방법 경쟁(완화는 비범위, 사용자 결정으로 현상 유지)**
- `recursive` (recursive_1100/600), `llm_regex`(reasoning LLM 단일콜 ~339s 실측), `semantic`(문장쌍 N-1 reranker), `coref`(RC LLM). **승자 1개 선택에 전부 지불** → 청커 ~10분의 원인(03-dev-progress 참조).
- 토큰 타깃 = 허브 기본 1100/600(KB 정책 고정).

**청크 메타 계약**: `{doc_id, kb_id, chunk_order_index, page_idx, titles_context, block_type, modal_id?}`. `modal_id`=모달 청크 식별자, `source_id`=edgequake 부여 chunk id 와 정합.

---

## 5. edgequake (:8081) — 베이스 엔진 (차용)

Rust, `crates/edgequake-pipeline`. passthrough 로 facade 청크를 받아 추출·임베딩·그래프 적재·검색 수행.

### 5.1 Insert 파이프라인
`service/edgequake.py:EdgequakeClient` → edgequake `Pipeline`. 제출 `POST /api/v1/documents`(`async_processing:true`):
1. `chunk_async`(**PassthroughStrategy**, U+001E 분할) — 상류와 1:1 복원
2. `extract_parallel` — 엔티티/관계 추출 (LLM=OpenRouter `qwen/qwen3.5-122b-a10b`)
3. `finish_document_processing` — link_extractions_to_chunks → `generate_all_embeddings`(bge-m3 1024d) → `build_lineage`

폴링 `GET /api/v1/tasks/{track_id}`, 단계 `pending→chunking→extracting→embedding→indexing/storing→completed`, poll_timeout 1200s/간격 3s. 산출 `{document_id, chunk_count, status}`(성공=`indexed`).

- **격리**: `set_config('app.current_tenant_id', <kb>)` + `X-Workspace-ID`/`X-Tenant-ID`. tenant 기본 `00000000-0000-0000-0000-000000000002`, workspace `kb-<kbid>` 슬러그 `ensure_workspace`(멱등). 테이블 `eq_eq_default_ws_<short8>_vectors`, 그래프 `eq_eq_default_graph`.
- **재청킹 금지**: 반드시 `EDGEQUAKE_CHUNKER=passthrough`. `adaptive` 로 띄우면 이중청킹 → 빈 구분자 조각 → HTTP 422 적재 실패.
- **문서단위 그래프 추출 스킵(2026-06-30)**: 2단계 `extract_parallel`(엔티티/관계)은 문서 단위로 끌 수 있다. facade `/insert` 의 `extract_graph=false`(UI 라디오, **엑셀은 고정 미추출**) → submit 본문 `metadata.skip_graph_extraction=true` → edgequake `process_with_resilience_cancellable_opts(skip_extraction)` 가 추출 서브블록만 건너뛴다. 1·3단계(청킹·임베딩·lineage)는 그대로라 벡터검색 무영향. 0엔티티여도 status=completed(skip 가드). 상세 02-changes §0-A.

### 5.2 Community — Louvain + 리포트 (검색과 분리된 오프라인 배치)
`kb_pipeline/community.py:build_workspace_communities`(순수 Python, edgequake Rust 불변). 두 경로 기동 — (1) facade `POST /communities/build` 온디맨드(202 + 백그라운드, 예외 swallow), (2) global 검색 시 build-if-missing.
1. `fetch_graph`(`eq_eq_default_graph` Node/EDGE, workspace_id 스코프, `properties::text::jsonb`)
2. `build_communities`(networkx + python-louvain `best_partition`, weight, `random_state=42`)
3. `generate_report`(Entities/Relationships CSV → GraphRAG `COMMUNITY_REPORT_PROMPT`, `/no_think` 접두 → JSON)
4. `store_reports`(`public.community_reports` upsert, `ON CONFLICT (workspace_id, level, community_id)`)

산출 행 = `title, summary, findings(jsonb), rank, entity_ids[]`. 라이브 실측: 커뮤니티 60 / 리포트 15.

### 5.3 Search — 두 경로
- **실노출 경로** — facade `POST /search`: edgequake hybrid 질의(`POST /api/v1/query`, 벡터 KNN + 그래프 순회 서버측 머지)를 직접 호출 → `{answer, results}`(`results[]`={chunk_id,text,score,document_id}). local/global 라우팅 없음.
- **라이브러리 경로(미배선)** — `kb_pipeline/search.py:unified_search`: `route()`(GLOBAL_CUES 단서어 + tiny LLM 타이브레이크 → local/global) → local=edgequake hybrid, global=커뮤니티 map-reduce(`community.global_query`). app.py 가 import 하지 않음 → 향후/계획.

---

## 6. 저장소 / 멀티테넌시 (단일 Postgres, :5433)

- **단일 Postgres**: pgvector(벡터) + Apache AGE(그래프)를 **한 DB**에. 단일 트랜잭션·단일 RLS = 관리포인트 1개. (Qdrant/Memgraph 분리 금지 — 비범위)
- **per-KB 격리 = 공유테이블 + tenant/workspace 컬럼 + RLS**(별도 스키마 아님). edgequake migrations: `009_add_rls_policies`(documents/entities/relationships/chunks), `013_add_age_graph`(graph_nodes/edges), `022_add_pdf_documents_table`(workspace 정책).
- 세션 진입 시 `set_config('app.current_tenant_id'/'app.current_workspace_id')` → 정책이 행 자동 필터. workspace 헤더(`x-workspace-id`/`x-tenant-id`)로 `TenantContext::from_headers` 가 워크스페이스 벡터 테이블 격리.
- 모든 검색 모드가 application-level 에서 workspace_id 로 제약 — 교차 스코핑 누출 0(실측).
- 재처리: 문서 추가=증분 upsert. 전체 재처리는 workspace 단위 `rebuild_knowledge_graph`/`rebuild_embeddings`/ReprocessAll(SPEC-032). `TaskType::Reindex` 단건은 미구현·불요.
- ⚠️ **W4 RLS 한계**: 앱이 superuser 롤(`edgequake`, rolbypassrls=t)로 접속 → FORCE RLS 도 무조건 우회. 앱레벨 격리는 검증됨, DB레벨은 프로덕션 하드닝 과제(02/03 참조).

---

## 7. 데이터 계약

### 7.1 블록 스키마 (Blockify 출력)
```jsonc
{"type":"text",     "text":"...", "text_level":1, "page_idx":0}
{"type":"table",    "table_body":"<table>…</table>", "table_caption":[], "table_footnote":[], "page_idx":0}
{"type":"image",    "img_path":"…", "image_caption":[], "page_idx":0}
{"type":"equation", "latex":"…", "text_format":"latex", "page_idx":0}
```

### 7.2 청크 메타데이터 (chunk → 적재)
`{doc_id, kb_id, chunk_order_index, page_idx, titles_context, block_type, modal_id?}` — `modal_id`=모달청크 식별, `source_id`=edgequake chunk id 와 정합.

### 7.3 RLS 세션 계약
모든 적재/조회 경로는 시작 시 `set_config('app.current_tenant_id', kb_id)`(+workspace) 호출 필수. 누락 시 정책 0건 → "조용한 빈 결과" 버그 주의.

### 7.4 임베딩 일관성 (BGE-M3 1024d)
- 청킹·적재·검색 세 구간을 `bge-m3` 1024차원으로 단일화.
- **현행 운영 배선 = 원격 litellm**: `EDGEQUAKE_EMBEDDING_PROVIDER=openai`, `EDGEQUAKE_EMBEDDING_BASE_URL=https://litellm.ax-demo.com/v1`, `EDGEQUAKE_EMBEDDING_MODEL=bge-m3`, `EDGEQUAKE_EMBEDDING_DIMENSION=1024`(`service/scripts/start_dedicated_edgequake.sh`). 로컬 `:7997` 은 과거 스모크 기록의 대체 구성.
- 임베딩 BASE_URL 은 chat(추출) LLM 과 분리(임베딩=litellm, chat=OpenRouter qwen). (KURE-v1 도 1024d 호환.)
- 운영 메모: bge-m3 main 리비전은 safetensors 부재(torch 2.2.2 CVE) → safetensors revision 심링크 필요.

### 7.5 차용할 edgequake 마이그레이션
(경로 `edgequake/edgequake/migrations/`) `001_init_database`, `008_add_multi_tenancy_tables`, `009_add_rls_policies`, `011_tenant_performance_indexes`, `013_add_age_graph`, `022_add_pdf_documents_table`, `028_add_vector_materialized_columns`, `029_add_vector_btree_indexes`, `038_*`(tenant/workspace 백필). + `docker/init-extensions.sql`(vector/AGE 확장).

---

## 8. excel-parser (:18055) — Excel 전용 경로

Excel(xlsx/xlsm/xls)은 parse-svc 의 FRONT 경로를 타지 않고 excel-parser 가 **LLM 없이** parse+chunk 를 함께 수행해 native 청크를 반환한다(`chunk_strategy=excel_rag_parser`). `KBP_EXCEL_URL` 로 주입. 현행 parse-svc 의 xlsx 라우팅은 markitdown(page=1 강등)이며, excel-parser 자동 기동·라우팅 정식화는 협의사항(03-dev-progress §4.2).

---

## 부록 A — 코드 레퍼런스 색인

> `docs/kb-pipeline-process-definition.md`(프로세스정의서 v1.0) §5.7 의 코드 사실을 단계별로 요약. 운영 배선의 권위 출처는 기동 런처 `service/scripts/start_dedicated_edgequake.sh` 와 facade `service/app.py`, parse-svc `parse_service/app.py`, `kb_pipeline/*` 모듈이다.

| 단계 | 진입점 / 함수 | 핵심 식별자 |
|------|---------------|-------------|
| **Parse** | `POST /parse` → `run_parse()` → `parse_to_pages()` | doc_id 폴백 `sha256(bytes)[:16]`, 파일명 `_safe_basename`, 비표시문자 `_PUA_RE`(U+E000–F8FF). PDF sentinel `_PAGE_SEP`=`<<<ODL_PAGE_BREAK>>>`. 산출 `list[PageDoc]`(`{page_number(1-based), blocks}`) |
| **Blockify** | `kb_pipeline.blockify`: `hybrid_to_blocks(md, page_idx)`, `elements_to_blocks(elements)` | math `$$..$$`→equation, `<img>`/`![]()`→image, heading→`text_level`. OCR category 매핑. page_idx 1-based |
| **페이지 이미지** | `parse_service.pdf_pages.render_pdf_pages` | PyMuPDF/`fitz` lazy, dpi=300, jpg q=90. MinIO 키 `{docs_id}/{docs_id}_{p}.jpeg`(`page_uuid="{docs_id}_{p}"`) |
| **Modal Enrich** | `parse_service/app.py:run_parse` → `kb_pipeline.modal.enrich_with_spans`, LLM=`service/llm.py:get_text_llm` | `BEFORE_WINDOW=3`/`AFTER_WINDOW=6`, `KBP_MODAL_MAX_WORKERS`(기본 3), 마커 `〈MODAL id="X" type="..."〉…〈/MODAL〉`(U+3008/U+3009). 산출 `enriched_content`+`modal_spans`(반열림)+`page_spans`. 토글 `KBP_MODAL_ENRICH`(기본 off) |
| **Chunking** | facade `/chunk` → `service/adaptive_chunk.py:AdaptiveChunkClient` | 마커 `MODAL_ATOMIC_MARKERS=[["〈MODAL","〈/MODAL〉"]]` → 잡 본문 `options.atomic_markers`. 비동기 `POST /chunk/jobs`→`GET /chunk/jobs/{id}`(간격 3s, 폴타임아웃 1800s, 클라 600s). 정규화 `chunk_text`→`text`, `chunk_pages`→`pages`. join `chr(0x1E)`(`PASSTHROUGH_SEP`) |
| **Insert** | `service/edgequake.py:EdgequakeClient` + edgequake `Pipeline` | `POST /api/v1/documents`(`async_processing:true`). `chunk_async`(PassthroughStrategy)→`extract_parallel`(qwen)→`finish_document_processing`(link→`generate_all_embeddings` bge-m3 1024d→`build_lineage`). 폴링 `GET /api/v1/tasks/{track_id}`(또는 `document_phase`), poll_timeout 1200s. tenant 기본 `00000000-0000-0000-0000-000000000002`, ws `kb-<kbid>` `ensure_workspace`. 테이블 `eq_eq_default_ws_<short8>_vectors`, 그래프 `eq_eq_default_graph` |
| **Community** | `kb_pipeline/community.py:build_workspace_communities`; 트리거 `service/app.py:communities_build`(202)→`_build_communities_job`(BackgroundTask, 예외 swallow) | `fetch_graph`(`properties::text::jsonb`)→`build_communities`(networkx+python-louvain `best_partition`, `random_state=42`)→`generate_report`(`COMMUNITY_REPORT_PROMPT`, `/no_think`)→`store_reports`(`public.community_reports` upsert, `ON CONFLICT (workspace_id, level, community_id)`) |
| **Search(실)** | `service/app.py:search` → `eq.ensure_workspace` → `eq.search(workspace_id, query, top_k)` | edgequake `POST /api/v1/query`(hybrid), top_k→max_results. 산출 `{answer, results[{chunk_id,text,score,document_id}]}` |
| **Search(미배선)** | `kb_pipeline/search.py:unified_search`, `route()` | `GLOBAL_CUES`(요약/전체/핵심/개요/summary…)+tiny LLM 타이브레이크. local=edgequake `POST /api/v1/query`(`mode="hybrid"`, `include_references:true`, 180s), global=`community.global_query` map-reduce. app.py 미import |
