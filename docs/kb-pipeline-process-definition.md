# KB 파이프라인 프로세스정의서

**업무명: 문서 지식베이스 적재 자동화 (Document Knowledge-Base Ingestion)**
**Ver 1.0**

## 개정 이력

| 버전 | 일자 | 작성 | 내용 |
|------|------|------|------|
| 0.1 | 2026-06-29 | KB 파이프라인 분석팀 | 초안 — 원본 프로세스정의서 구조(1.개요~6.협의사항) 모방, 코드 사실 반영 |
| 1.0 | 2026-06-29 | KB 파이프라인 분석팀 | 검증 반영 — 임베딩 배선(원격 litellm) 정정, 4장 업무어조 복원·코드 레퍼런스 5장 강등, Search 실노출경로(/search) vs 라이브러리 라우터 분리, /communities/build 추가 |

---

## 1. 개요

### 1.1 목적
본 정의서는 kb-pipeline 문서 적재 파이프라인의 오케스트레이션 원칙과 단계별 책임을 확정한다. 하나의 업로드 문서를 Parse → Blockify → Modal Enrich → Chunking → Insert → Community → Search 의 단계로 흘려 단일 Postgres(pgvector+AGE)에 적재·검색 가능 상태로 만드는 과정을 기술한다. 단계 경계·소유권·데이터 계약을 명문화하여 이중처리(이중청킹/이중그래프생성)를 제거하고, 단계별 추적·격리·정합성을 보장하는 것이 목적이다.

### 1.2 적용 범위
- facade(:19000)가 외부 소비자(knowledge_base 백엔드)에게 노출하는 capability(`/parse`, `/chunk`, `/insert`, `/search`, `/ingest`, `/ingest/submit`, `/communities/build`, 상태 폴링)와 그 뒤의 다운스트림 서비스 오케스트레이션 전 구간.
- 비범위: 청킹 4방법 경쟁의 완화/최적화(사용자 결정으로 현상 유지), edgequake Rust 엔진 내부 알고리즘 변경, 벡터/그래프 store 분리(단일 Postgres 원칙 위배).

### 1.3 기본 원칙
- **단일 Postgres + per-KB RLS**: 벡터(pgvector)·그래프(Apache AGE)를 한 DB에 둔다. KB별 별도 스키마가 아니라 공유테이블 + tenant_id/workspace_id 컬럼 + Row Level Security 로 격리한다(edgequake migrations 009/013/022). 단일 트랜잭션·단일 관리포인트를 유지한다.
- **facade 가 청킹을 소유한다**: 청킹과 모달 원자성은 facade `/chunk` 가 소유한다. facade 가 청킹 허브를 모달 원자 마커와 함께 호출해 모달 영역을 단일 원자 청크로 강제한다. 따라서 전용 edgequake 는 청킹을 끄고(passthrough) facade 청크를 경계 그대로 저장한다(재청킹 금지).
- **BGE-M3 1024d 통일**: 임베딩은 `bge-m3` 1024차원으로 청킹·적재·검색 세 구간을 단일화한다. edgequake 의 모델 미지 시 1536 하드코딩 버그는 fork 의 차원 지정으로 교정했다(세부 §5.4).
- **모달 원자성**: 표/그림/수식 블록은 LLM 서술과 함께 모달 마커로 봉인되어, 분할되지 않고 단독 atomic 청크로 적재된다. 저장 단계까지 metadata 에 모달 마킹이 보존된다.
- **마크다운 + inline HTML 중간표현**: 모든 파서의 표준 출력은 "markdown + inline HTML 표"다. 표는 절대 pipe 로 납작화하지 않고 `<table>` HTML(colspan/rowspan)을 보존한다.
- **불변식 — 청크는 KB당 단일 우주**: 하나의 enriched content → 하나의 청크 집합 → 그 위에서 벡터·그래프가 동일 source_id 로 묶인다. 벡터용/그래프용 청크를 분리하지 않는다.

---

## 2. 시스템 구성 요약

| 구성요소 | 포트 | 한 줄 정의 |
|----------|------|-----------|
| facade (kb-pipeline) | 19000 | 오케스트레이터. parse→chunk→insert→search capability 를 노출하고 청킹·모달원자성을 소유하며 다운스트림을 숨긴다(`service/app.py`). |
| parse-svc | 19001 | 비-Excel 문서를 "markdown + inline HTML"로 파싱하고 표/그림을 modal LLM 으로 서술해 enriched_content + 모달 마커를 반환한다. |
| adaptive_chunk | 18060 | 청킹 허브. atomic_markers 를 받아 모달 스팬을 원자 보존하고, 텍스트 갭만 4방법 경쟁으로 청킹해 선택 근거(method_selected/scores)를 반환한다. |
| edgequake | 8081 | 베이스 엔진. passthrough 로 facade 청크를 받아 엔티티/관계 추출 → 임베딩 → AGE 그래프 → 단일 Postgres 적재(RLS)와 검색을 수행한다. |
| postgres (eq-pg-kbp) | 5433 | 단일 저장소. pgvector + Apache AGE 를 한 DB에. docker, POSTGRES_PASSWORD=edgequake_secret. |
| 임베딩(bge-m3) | — | OpenAI-호환 임베딩 엔드포인트. 현행 운영 배선은 원격 litellm(`https://litellm.ax-demo.com/v1`, model `bge-m3`, 1024d). 환경별 가변(로컬 OpenAI-호환 서버 `:7997` 도 대체 구성으로 가능).¹ |
| ocr | 18050 | 이미지/스캔 PDF 의 VLM/OCR 경로. content + elements[] 구조배열을 제공한다. |
| excel-parser | 18055 | Excel(xlsx/xlsm/xls) 전용. parse+chunk 를 LLM 없이 수행해 native chunks(chunk_strategy=excel_rag_parser)를 반환한다. |
| kb-backend (knowledge_base) | 8088 | 소비자/집계자. facade(:19000)를 호출하고 IngestionJob stage 를 추적·영속한다(kb_pipeline_base_url=http://localhost:19000). |

> ¹ 현행 기동 런처(`service/scripts/start_dedicated_edgequake.sh`)는 임베딩을 원격 litellm 으로 라우팅한다(`EDGEQUAKE_EMBEDDING_PROVIDER=openai`, `EDGEQUAKE_EMBEDDING_BASE_URL=https://litellm.ax-demo.com/v1`, `EDGEQUAKE_EMBEDDING_MODEL=bge-m3`, `EDGEQUAKE_EMBEDDING_DIMENSION=1024`). 로컬 `:7997` 은 과거 스모크 기록(`service/tests/test_e2e_smoke.md`)에만 등장하는 대체/과거 구성이며 운영 배선에는 없다. 채팅/추출 LLM(OpenRouter qwen)과 임베딩 BASE_URL 은 분리되어 있다. (SoT.md §3.5/§5.4 의 `:7997` 표기는 현행 런처보다 오래된 드리프트로, 함께 갱신 권고.)

---

## 3. 전체 프로세스 흐름

### 3.1 단계 흐름 (한 줄)

```
Parse → Blockify → Modal Enrich → Chunking → Insert → Community → Search
```

### 3.2 흐름 설명
- **Parse / Blockify / Modal Enrich** 는 parse-svc(:19001)가 한 번의 `POST /parse` 안에서 연속 수행하는 FRONT 단계다. 비-Excel 문서를 "markdown + inline HTML"로 파싱하고(Parse), 구조 단위로 블록화한 뒤(Blockify), 표/그림/수식을 검색 가능한 의미로 보강하고 모달 원자 마커로 봉인(Modal Enrich)하여 enriched_content + 모달 스팬 + 페이지 스팬을 산출한다. Excel(xlsx/xlsm/xls)은 parse-svc 가 아니라 excel-parser(:18055)가 LLM 없이 parse+chunk 를 수행한다.
- **Chunking** 은 facade `/chunk`(:19000)가 소유한다. facade 가 청킹 허브(:18060)에 모달 마커를 넘겨 모달 스팬을 단일 원자 청크로 강제하고, 텍스트 갭만 4방법 경쟁으로 청킹한 뒤 선택 근거를 노출한다.
- **Insert** 는 facade 가 청크 텍스트를 단일 passthrough 문서로 묶어 edgequake(:8081)에 제출하면, edgequake 가 엔티티/관계 추출 → 임베딩 → 그래프 적재를 수행해 단일 Postgres 에 upsert 한다.
- **Community** 는 검색과 분리된 오프라인 배치다. AGE 그래프를 Louvain 으로 커뮤니티로 나누고 커뮤니티별 GraphRAG 리포트를 생성·저장한다. 온디맨드 트리거(`POST /communities/build`)와 검색 시 build-if-missing 두 경로로 기동된다.
- **Search** 는 두 갈래로 구분된다. (1) **실노출 경로**: facade `POST /search` 는 edgequake 의 hybrid 질의(`POST /api/v1/query`)를 직접 호출하고 결과를 정규화해 `{answer, results}` 로 반환한다(현재 외부에 노출된 실제 검색 능력). (2) **라이브러리 경로(미배선)**: `kb_pipeline/search.py` 의 `unified_search` local/global 라우터는 모듈로만 존재하며 facade `/search` 에 연결되어 있지 않다(현재 미배선/계획 메커니즘).

### 3.3 오케스트레이션 경로 / 반려·회귀 규칙
- **표준 단계별 경로**: 소비자(kb-backend)가 facade `/parse` → `/chunk` → `/insert`(+poll `/insert/status`)를 직접 순차 호출한다. 이 단계별 경로가 표준 데이터 경로다(one-shot `/ingest` 아님).
- **블로킹 변형 `/ingest`**: parse→chunk→insert 를 한 호출로 순차 구동해 `{document_id, chunk_count, status, chunking_selection, edgequake_workspace_id}` 를 반환한다.
- **비동기 변형 `/ingest/submit`**: FRONT(parse→blockify→modal_enrich)를 동기 수행한 뒤 edgequake 에 async 제출하고 즉시 `{document_id, status:"submitted"}` 를 반환한다. 소비자는 `GET /ingest/status?workspace_id&doc_id` 를 폴링한다.
- **반려/회귀**: 파싱 실패는 parse-svc 가 `{"status":"failed","detail":...}` 로 반환하며, 단계 회귀(재시도)는 호출측(facade/orchestrator)이 결정한다. render/upload 실패와 modal LLM 실패는 best-effort/비치명으로 본문은 살린다(아래 4·5장).

---

## 4. 단계별 상세 정의

### 4.1 Parse(문서 파싱)

| 항목 | 내용 |
|------|------|
| 수행 주체 | parse-svc(`:19001`)가 업로드된 문서를 내려받아 외부 파서로 위임해 파싱한다. |
| 입력 | 업로드 파일과 파일명(선택적으로 문서 ID·콘텐츠 타입). 문서 ID 가 없으면 파일 내용 해시로 자동 부여하고, 파일명은 안전한 형태로 정규화한다(경로 탈출 차단). |
| 처리 | 문서 종류(확장자)에 따라 적절한 파서로 보낸다. 일반 PDF 는 구조 파서로 페이지별로 파싱하고, 글자가 거의 없는 스캔 페이지는 그 페이지만 이미지로 렌더해 OCR 로 보충한다(혼합 PDF 대응). 이미지·PPTX·DOCX·스캔 문서는 OCR·VLM 경로로 보낸다. 파싱 결과의 비표시 특수문자는 정리한다. |
| 산출물 | 페이지 경계가 보존된 페이지별 블록 묶음(페이지 번호 1-based 부여). |

> 코드 레퍼런스(§5.7): `POST /parse` → `run_parse()` → `parse_to_pages()`. 파서 위임 대상은 OpenDataLoader / OCR·VLM(`:18050`) / markitdown. 문서 ID 폴백 = `sha256(file_bytes).hexdigest()[:16]`(orchestrator 동일 식 → MinIO 키 일치), 파일명 정규화 = `_safe_basename`(`[A-Za-z0-9._-]` 외 `_`). 산출 타입 `list[PageDoc]`, `PageDoc = {"page_number": int(1-based), "blocks": list[dict]}`, 각 블록 `page_idx` 1-based. 비표시 문자 제거 = PUA(U+E000–U+F8FF, `_PUA_RE`).

#### 4.1.1 입력 포맷별 파서 라우팅 (`PARSER_ROUTING`, 기본 `markitdown`)
- **PDF** → `structural`: OpenDataLoader(`opendataloader_pdf.convert`, `markdown_with_html=True`, `markdown_page_separator`). 문서당 .md 1개, 페이지 앞에 sentinel `<<<ODL_PAGE_BREAK>>>`(`_PAGE_SEP`) 삽입 → SEP로 split해 페이지 복원. JVM 호출 1회.
- **PPTX / DOCX** → `structural`: OCR·VLM(`:18050`)로 라우팅. (markitdown은 표의 colspan/rowspan을 parse 단계에서 평탄화하므로 병합셀이 중요한 office 포맷은 structural로 보낸다.)
- **XLSX(Excel)** → `markitdown`: `MarkItDown().convert_stream`. 페이지 개념 없음. (Excel 전용 백엔드 excel-parser `:18055`는 `KBP_EXCEL_URL`로 주입 가능.)
- **단일 이미지(`_IMAGE_EXTS`={png,jpg,jpeg,gif,bmp,tif,tiff,webp})** → 페이지 보존 경로에서 OCR·VLM(`:18050`)로 강제 라우팅.
- **스캔 PDF / 스캔 페이지** → 해당 페이지를 렌더해 OCR(`:18050`)로 보충(best-effort, 비치명).

OCR·VLM 계약: `POST {ocr_url}/api/v1/ocr`, multipart `file` + form `strategy=hybrid`, timeout 600s. 응답에서 `content.{markdown|text}` 우선, 없으면 `elements[*].content` 로 재구성. 페이지 보존 경로는 raw `elements[]` 를 `elements_to_blocks` 로 넘긴다.

### 4.2 Blockify(블록화)

| 항목 | 내용 |
|------|------|
| 수행 주체 | parse-svc 가 파서 출력을 문서 구조 단위(블록)로 잘라 정리한다. |
| 입력 | 페이지별 markdown+HTML 문자열 또는 OCR·VLM 구조요소 배열. |
| 처리 | 구조 단위 1개당 블록 1개를 만들고 문서 순서를 보존한다. 표는 항상 `<table>` HTML 로 유지하고(pipe 평탄화 금지), 수식·이미지·제목을 종류별 블록으로 분리한다. OCR 구조요소는 종류에 따라 표/이미지/수식/제목/본문 블록으로 매핑한다. |
| 산출물 | 페이지 번호가 부여된 블록 리스트(§4.2.1 스키마). |

> 코드 레퍼런스(§5.7): `kb_pipeline.blockify` — `hybrid_to_blocks(md, page_idx)`(markdown+HTML), `elements_to_blocks(elements)`(OCR·VLM). block math `$$...$$`→equation, `<img>`/`![]()`→image, heading→`text_level`. OCR `category` 매핑(`table`→table, `image`/`figure`→image, `equation`→equation, `title`/`heading`→text+text_level, 그 외→text). 각 블록 `page_idx` 1-based.

#### 4.2.1 블록 스키마 (SoT §5.1)
- `{"type":"text", "text":"...", "text_level":1, "page_idx":0}`
- `{"type":"table", "table_body":"<table>…</table>", "table_caption":[], "page_idx":0}`
- `{"type":"image", "img_path":"…", "image_caption":[], "page_idx":0}`
- `{"type":"equation", "latex":"…", "text_format":"latex", "page_idx":0}`

#### 4.2.2 page_idx / 페이지 이미지
- **page_idx**: canonical 1-based. PDF는 .md 인덱스(1-based), OCR elements는 보통 0-based → `page_number = page_idx + 1` 로 정규화. 스캔 보충 페이지의 블록은 해당 페이지 번호로 리맵.
- **페이지 이미지(render+upload)**: `parse_service.pdf_pages.render_pdf_pages`(PyMuPDF/`fitz`, lazy import, `dpi=300`, `jpg_quality=90`)로 페이지별 JPEG(알파 없음) 래스터화. PDF가 아니거나 렌더 실패 시 빈 리스트(비치명). MinIO 키 규칙(잠금): `page_uuid="{docs_id}_{p}"`, `minio_object="{docs_id}/{docs_id}_{p}.jpeg"`. 단일 이미지는 page 1로 JPEG 정규화. MinIO 미설정 시 업로드만 skip하고 page 메타는 그대로 조립.

### 4.3 Modal Enrich(표/그림 LLM 보강)

표·수식·그림 같은 비텍스트 "모달" 블록을 검색 가능한 한국어 의미로 보강하고, 각 모달을 하나의 원자 마커로 봉인해 다운스트림 청커가 표/제목/각주를 쪼개지 못하게 하는 단계이다.

| 항목 | 내용 |
|------|------|
| 수행 주체 | parse-svc 가 모달 블록마다 LLM(OpenRouter qwen)을 호출해 의미요약을 만들고 마커로 봉인한다. |
| 입력 | Blockify 가 만든 문서순 블록 리스트(표/수식/그림 모달 블록과 주변 본문 블록). |
| 처리 | 모달마다 LLM 으로 ① 검색용 한국어 요약을 생성하고, ② 바로 앞·뒤 본문 중 제목·각주 줄을 판정해 같은 모달 영역에 원문 그대로 흡수한다. 호출은 병렬로 수행한다. 두 모달이 같은 사이 블록을 다투면 문서순 앞 모달이 선점한다. |
| 산출물 | enriched_content 와 모달 스팬·페이지 스팬. 각 모달은 단독 원자 영역으로 표시된다. |

> 코드 레퍼런스(§5.2/§5.7): `parse_service/app.py:run_parse` → `kb_pipeline.modal.enrich_with_spans`, 모달 LLM=`service/llm.py:get_text_llm`. 창 크기 `BEFORE_WINDOW=3`/`AFTER_WINDOW=6`, 병렬 워커 `KBP_MODAL_MAX_WORKERS`(기본 3), id 카운터(표 `T`/수식 `E`/그림 `I`). 마커 형식 `〈MODAL id="X" type="table|image|equation"〉…〈/MODAL〉`(괄호 U+3008/U+3009, W1 Rust 소비자와 byte-identical). 산출물 = `enriched_content` + `modal_spans`(`[{id,type,char_range:[start,end]}]` 반열림) + `page_spans`(`[{page_number,char_start,char_end}]`).

> **기본 동작(중요)**: 모달 LLM 보강은 환경변수 `KBP_MODAL_ENRICH`로 토글하며 **기본값은 off("0")** 다. off일 때 LLM 0회로 각 모달을 요약 없음·흡수 0(`summary="", tc=fc=0`)으로 강등해 OpenDataLoader 원본 payload를 그대로 모달 마커로 통과시킨다. 이때도 모달 원자성과 page_spans는 유지되므로 청킹/페이지 지표엔 무영향이고, 손실되는 것은 표/그림의 검색용 의미요약뿐이다. `KBP_MODAL_ENRICH=1` 로 재활성. (현재 `/parse`는 `vision_llm=None`이라 그림은 LLM 미호출·원본 통과.)

### 4.4 Chunking(청킹)

| 항목 | 내용 |
|------|------|
| 수행 주체 | facade `/chunk`(:19000)가 청킹을 **소유**하고, 실제 분할 연산은 청킹 허브(:18060)에 위임한다. edgequake 는 청킹에 관여하지 않는다. |
| 입력 | Modal Enrich 가 생성한 enriched_content(모달 마커 인라인), 문서명, 선택적 페이지 스팬·페이지 본문. |
| 처리 | facade 가 모달 마커를 청킹 잡에 넘겨 각 모달을 단일 원자 청크로 강제하고, 나머지 텍스트만 4방법 경쟁으로 청킹한다. 대형 입력·느린 방법 대응으로 비동기 잡을 제출해 완료까지 폴링한다. 허브가 여러 방법을 비교·채점해 최적 방법을 고르면 facade 가 응답을 외부 계약으로 정규화한다. |
| 산출물 | 정규화된 청크 리스트와 선택 근거(어떤 방법이 왜 선택됐는지). |

> 코드 레퍼런스(§5.3/§5.7): 마커 `MODAL_ATOMIC_MARKERS=[["〈MODAL","〈/MODAL〉"]]` 를 잡 본문 `options.atomic_markers`(최상위 필드 아님)로 전달. 비동기 잡 = `POST /chunk/jobs` → `GET /chunk/jobs/{id}` 폴링(간격 3s, 폴링 타임아웃 1800s, 클라이언트 timeout 600s). 정규화 = 허브 R1 의 `chunk_text`→`text`, `chunk_pages`→`pages`. 산출 `chunks[]`(`chunk_index/text/titles_context/pages`) + `chunking_selection`(`method_selected`, `scores`, `methods_compared`). 적재 시 청크 텍스트를 U+001E(`PASSTHROUGH_SEP`)로 join.

### 4.5 Insert(엔티티/관계 추출 + 임베딩 + 그래프 적재)

| 항목 | 내용 |
|------|------|
| 수행 주체 | facade 가 청크를 단일 passthrough 문서로 묶어 edgequake 엔진에 제출하고, edgequake 가 추출·임베딩·그래프 적재를 수행한다. |
| 입력 | facade 청크 텍스트 리스트, 워크스페이스/테넌트 식별자, 문서 제목. |
| 처리 | edgequake 가 ① passthrough 경계로 청크를 상류와 1:1 복원하고 ② 엔티티/관계를 추출한 뒤 ③ 임베딩(bge-m3 1024d) 생성과 그래프·계보 구축을 수행한다. facade 는 진행 단계를 폴링해 완료까지 추적한다. |
| 산출물 | `{document_id, chunk_count, status}`. 성공 시 `status="indexed"`, 실패/타임아웃 시 `status="failed"`. 데이터는 단일 Postgres(pgvector + AGE, RLS)에 upsert. |

> 코드 레퍼런스(§5.5/§5.7): `service/edgequake.py:EdgequakeClient` + edgequake `Pipeline`(Rust, `crates/edgequake-pipeline`). 제출 `POST /api/v1/documents`(`async_processing:true`). 파이프라인 = `chunk_async`(PassthroughStrategy, U+001E 분할) → `extract_parallel`(LLM=OpenRouter `qwen/qwen3.5-122b-a10b`) → `finish_document_processing`(link_extractions_to_chunks → `generate_all_embeddings` bge-m3 1024d → `build_lineage`). 폴링 `GET /api/v1/tasks/{track_id}`(또는 `document_phase`), 단계 `pending→chunking→extracting→embedding→indexing/storing→completed`, poll_timeout 1200s/간격 3s. 격리 = `set_config('app.current_tenant_id', <kb>)` + `X-Workspace-ID`/`X-Tenant-ID`, tenant 기본 `00000000-0000-0000-0000-000000000002`, workspace `kb-<kbid>` 슬러그 `ensure_workspace`(멱등). 테이블 = `eq_eq_default_ws_<short8>_vectors`, 그래프 `eq_eq_default_graph`.

### 4.6 Community(Louvain + 리포트)

검색 경로와 분리된 오프라인 배치. 두 가지로 기동된다 — (1) facade `POST /communities/build` 온디맨드 트리거, (2) global 검색 시 리포트가 없으면 build-if-missing 선빌드.

| 항목 | 내용 |
|------|------|
| 수행 주체 | facade 가 커뮤니티 빌드를 백그라운드 작업으로 돌리고, 빌드 본체는 순수 Python 으로 그래프를 군집화하고 커뮤니티별 리포트를 생성한다(edgequake Rust 불변). |
| 입력 | 워크스페이스, 같은 edgequake Postgres DSN, LLM(qwen via OpenRouter), resolution·level. |
| 처리 | ① AGE 그래프에서 해당 워크스페이스 범위의 노드·엣지를 로드하고 ② Louvain 으로 커뮤니티를 나눈 뒤 ③ 커뮤니티별 GraphRAG 리포트(한국어)를 LLM 으로 생성하고 ④ 리포트 테이블에 upsert 한다. **온디맨드 트리거** `POST /communities/build` 는 kb id 를 edgequake 워크스페이스 UUID 로 해석해 빌드를 백그라운드 작업으로 시작하고 즉시 `202 {status:"started", workspace_id}` 를 반환한다(작업 중 예외는 삼키고 호출자에게 올리지 않음). |
| 산출물 | 커뮤니티/리포트 카운트와 `public.community_reports` 행. 라이브 실측: 커뮤니티 60 / 리포트 15. |

> 코드 레퍼런스(§5.7): `kb_pipeline/community.py:build_workspace_communities` 가 오케스트레이션. ① `fetch_graph`(`eq_eq_default_graph` `Node`/`EDGE`, workspace_id 스코프, `properties::text::jsonb`) ② `build_communities`(networkx + python-louvain `best_partition`, weight, `random_state=42`, 최대크기 우선) ③ `generate_report`(Entities/Relationships CSV → GraphRAG `COMMUNITY_REPORT_PROMPT`, `/no_think` 접두, JSON 키·`[Data:...]` 인용 영어 유지 → JSON 파싱) ④ `store_reports`(`public.community_reports` upsert, `ON CONFLICT (workspace_id, level, community_id)`). 트리거 핸들러 = `service/app.py:communities_build`(`status_code=202`) → `_build_communities_job`(FastAPI BackgroundTask, 예외 swallow). 산출 행 = `title, summary, findings(jsonb), rank, entity_ids[]`.

### 4.7 Search(검색)

검색은 두 경로로 구분된다.

#### 4.7.1 실노출 경로 — facade `POST /search` (현행 외부 검색 능력)

| 항목 | 내용 |
|------|------|
| 수행 주체 | facade `POST /search` 가 edgequake 의 hybrid 질의를 직접 호출하고 결과를 정규화한다. (local/global 라우팅 없음.) |
| 입력 | 워크스페이스 식별자, 질문 텍스트, top_k(기본 10). |
| 처리 | facade 가 kb id 를 edgequake 워크스페이스 UUID 로 해석해 검색을 워크스페이스 범위로 격리하고, edgequake `POST /api/v1/query`(hybrid: 벡터 KNN + 그래프 순회 서버측 머지)를 호출한다. top_k 를 edgequake 의 max_results 로 매핑하고, edgequake 의 `sources` 를 안정적 결과 형태로 정규화한다. |
| 산출물 | `{answer, results}`. `results[]` = `{chunk_id, text, score, document_id}`(edgequake `sources` 의 `id/snippet/score/document_id` 정규화). |

> 코드 레퍼런스(§5.7): `service/app.py:search` → `eq.ensure_workspace` → `eq.search(workspace_id, query, top_k)`. local/global envelope(`{mode, sources, workspace_id}`)는 반환하지 않는다.

#### 4.7.2 라이브러리 경로 — `unified_search` local/global 라우터 (현재 미배선)

| 항목 | 내용 |
|------|------|
| 상태 | `kb_pipeline/search.py:unified_search` 는 모듈로만 존재하며 facade `/search` 에 연결되어 있지 않다(app.py 가 import 하지 않음 — grep 확인). 향후/계획 메커니즘으로 분류. |
| 처리(설계) | `route()` 가 local/global 판정 → local 이면 edgequake hybrid 질의, global 이면 커뮤니티 리포트 map-reduce(`community.global_query`, 리포트 없으면 build-if-missing). |
| 산출물(설계) | `{mode, answer, sources, workspace_id}`. local 의 `sources` 는 인용 청크/엔티티, global 의 `sources` 는 기여 커뮤니티 id. |

> 코드 레퍼런스(§5.7): `route()` 판정 = ① 글로벌 단서어 `GLOBAL_CUES`(요약/전체/핵심/개요/summary…) → global, ② tiny `llm` 한 단어 타이브레이크, ③ 기본 local. local 위임 = edgequake `POST /api/v1/query`(`mode="hybrid"`, `include_references:true`, 타임아웃 180s).

---

## 5. 주요 프로세스 세부사항

### 5.1 오케스트레이션 — 단계 추적·타이밍
- **워크스페이스 해석은 facade 가 소유**: 모든 핸들러가 `eq.ensure_workspace(workspace_id)` 로 kb id 를 edgequake workspace UUID 로 변환해 검색·적재를 workspace 스코프로 격리한다(교차 스코핑 누출 0, 실측).
- **단계 추적**: kb-backend `core/pipeline.py`(집계자)가 facade `/parse`→`/chunk`→`/insert` 를 순차 호출하며 각 stage 를 IngestionJob 으로 추적한다. facade `/insert` 응답의 `edgequake_workspace_id` 를 `KB.edgequake_workspace_id` 에 영속해 "그래프 보기" 팝업이 X-Workspace-ID 로 edgequake 를 직접 호출한다.
- **타이밍 트리**: 통일 타이밍 트리 계약(단위 ms·float)으로 parse/blockify/modal_enrich/adaptive_chunk/edgequake sub-stage 를 수집·병합. facade `/chunk` 응답은 `timing_details`(adaptive_chunk methods/metrics)를, `/insert` 응답은 `phases`(edgequake 내부 phase 체류시간 근사)를 passthrough 한다. 실측: 12페이지 문서가 파서 약 5분(표/그림당 vision LLM) / 청커 약 10분(4방법 경쟁; `llm_regex` split 단일콜 약 339s). edgequake 는 per-phase 타임스탬프가 없어 phase 소요는 `/insert/status` 폴링 관측 전이 시각으로 도출하는 근사값이다(해상도=폴 간격).

### 5.2 모달 원자성 (atomic_markers, U+3008/U+3009)
- 모달의 원자성은 **parser 가 소유**한다(Philosophy A). Modal Enrich 가 모달 span 안에 요약·제목·각주를 흡수해 `〈MODAL id="X" type="Y"〉[제목]\n{요약}\n{원본 payload}\n[각주]〈/MODAL〉` 형태로 조립한다. 세그먼트 join 은 `\n\n`(2자)이며 이 길이를 running offset 에 반영해 `page_spans` 를 계산한다.
- 두 모달이 같은 사이 블록을 다투면 문서순 앞 모달이 선점한다(사후 충돌 해소). LLM 이 유효 JSON 을 주지 않거나 호출이 실패하면 해당 모달만 흡수 0·요약 생략으로 강등하고 문서 전체는 살린다(폴백, 재시도 없음).
- 청킹 단계에서 facade 가 마커 `[["〈MODAL", "〈/MODAL〉"]]` 를 잡 본문 `options.atomic_markers`(최상위 필드 아님)로 전달한다. 허브는 모달 *의미*를 모른 채 "이 스팬은 원자적"이라는 사실만 받아 해당 영역을 단일 atomic 청크로 유지한다(marker-aware chunking). 마커 괄호 U+3008/U+3009 는 W1 Rust 소비자와 byte-identical 해야 한다.

### 5.3 청킹 선택 (chunking_selection)과 비동기 잡
- 동기 `POST /chunk` 는 토큰 상한(대형 입력 413)이 있고 `llm_regex`(LLM)·`semantic`(리랭커) 활성 시 분 단위가 걸린다. 따라서 `AdaptiveChunkClient` 는 비동기 잡(`POST /chunk/jobs`)을 제출하고 `GET /chunk/jobs/{id}` 를 폴링해 terminal 상태까지 블록한다. terminal: `succeeded` / `failed`·`cancelled`.
- 허브 R1 응답의 `method_selected`·`scores`·`methods_compared` 를 facade 가 그대로 노출하며, UI 의 "왜 이 청커인가" 카드와 `/chunk`·`/ingest` 응답(`chunking_selection`)에 실린다. 모니터링용 `timing_details` 도 passthrough 한다.
- 청크 메타 계약(SoT §5.2): `{doc_id, kb_id, chunk_order_index, page_idx, titles_context, block_type, modal_id?}`. `modal_id` 는 모달 청크 식별자, `source_id` 는 edgequake 가 부여하는 chunk id 와 정합한다.

### 5.4 임베딩 일관성 (BGE-M3 1024d)
- 임베딩은 `bge-m3` 1024차원으로 청킹·적재·검색 세 구간을 단일화한다. **현행 운영 배선은 원격 litellm 엔드포인트**다(`EDGEQUAKE_EMBEDDING_PROVIDER=openai`, `EDGEQUAKE_EMBEDDING_BASE_URL=https://litellm.ax-demo.com/v1`, `EDGEQUAKE_EMBEDDING_MODEL=bge-m3`, `EDGEQUAKE_EMBEDDING_DIMENSION=1024`; `service/scripts/start_dedicated_edgequake.sh`). 로컬 OpenAI-호환 서버 `:7997` 은 과거 스모크 기록에만 등장하는 대체/과거 구성이며 운영 전제는 환경별 가변(local `:7997` 또는 remote litellm)으로 본다.
- edgequake `OpenAIProvider` 가 미지 모델을 1536 하드코딩하던 버그는 fork 의 `with_embedding_dimension`+`EDGEQUAKE_EMBEDDING_DIMENSION=1024` 로 교정했고, **임베딩 BASE_URL 은 chat(추출) LLM 과 분리**되어 있다(임베딩=litellm, chat=OpenRouter). 추출 LLM 은 `create_openai()` 가 `OPENAI_BASE_URL` 무시+모델 하드코딩하던 버그를 `OpenAIProvider::compatible(key, base_url)`+env 모델로 fork 수정해 OpenRouter `qwen/qwen3.5-122b-a10b` 를 사용한다.
- **불변식**: 하나의 enriched content → 하나의 청크 집합 → 동일 source_id 로 벡터·그래프가 묶인다. 벡터용/그래프용 청크를 분리하지 않는다.
- (SoT.md §3.5/§5.4 의 `:7997` 표기는 현행 런처보다 오래된 드리프트로, 본 정의서와 함께 갱신 권고.)

### 5.5 passthrough 경계 (U+001E)와 edgequake 재청킹 금지
- 적재 시 facade 가 청크 텍스트들을 U+001E(`PASSTHROUGH_SEP`, `chr(0x1E)`)로 join 해 단일 문서 본문으로 만들고, edgequake 의 PassthroughStrategy 가 같은 바이트로 분할한다 → 상류 청커 결과와 1:1 정합.
- **전용 edgequake 는 반드시 `EDGEQUAKE_CHUNKER=passthrough`** 로 띄운다. `adaptive` 로 띄우면 facade 가 이미 청킹한 내용을 다시 adaptive_chunk 로 재청킹(이중청킹)하다 빈 구분자 조각을 보내 **HTTP 422 → 적재 실패**한다.

### 5.6 RLS 멀티테넌시
- per-KB 격리는 별도 스키마가 아니라 공유테이블 + tenant_id/workspace_id 컬럼 + Row Level Security(edgequake migrations 009/013/022)로 구현한다. 세션 진입 시 `set_config('app.current_tenant_id'/'app.current_workspace_id')` 를 호출하고, workspace 헤더(`x-workspace-id`/`x-tenant-id`)로 `TenantContext::from_headers` 가 워크스페이스 벡터 테이블에 격리한다.
- 모든 검색 모드가 application-level 에서 workspace_id 로 제약된다 — ws-A 사실을 ws-B 스코프로 물으면 노출되지 않는다(교차 스코핑 누출 0, 실측).

### 5.7 코드 레퍼런스 색인
4장 각 단계의 함수명·파일경로·환경변수·유니코드 코드포인트는 해당 단계 표 하단의 "코드 레퍼런스" 주석에 정리했다(§4.1~§4.7). 운영 배선의 권위 출처는 기동 런처 `service/scripts/start_dedicated_edgequake.sh` 와 facade `service/app.py`, parse-svc `parse_service/app.py`, `kb_pipeline/*` 모듈이다.

---

## 6. 주요 협의사항

### 6.1 RLS / 격리
- **W4 RLS FORCE 미적용(superuser 우회)**: RLS 정책은 documents/entities/relationships/chunks/graph 를 모두 덮지만, 앱이 Postgres superuser 롤(`edgequake`, rolbypassrls=t)로 접속해 FORCE RLS 도 무조건 우회된다(롤백 tx 로 실증). 앱레벨 격리는 검증되었으나 DB레벨 RLS 는 현재 무력. 활성화는 (비-superuser 롤+GRANT)+FORCE RLS+요청당 tx GUC+NULL-tenant 폴백 정리의 all-or-nothing 하드닝 과제.

### 6.2 파서 / 입력 포맷
- **W6 파서 라우팅 colspan 손실**: markitdown 은 pptx/DOCX 의 병합(colspan/rowspan)을 파싱 시점에 소실한다(blockify 복구 불가). 병합이 중요한 pptx/DOCX 는 kordoc/MinerU 로 라우팅 권고. HWP 계열은 kordoc 신뢰(수용된 리스크).
- OCR·VLM(`:18050`) `strategy=hybrid` 외 전략, timeout 600s 적정성.
- markitdown 경로(xlsx)의 페이지 개념 부재 → 단일 페이지(page=1) 강등 정책 확정.
- `_DIGITAL_MIN_CHARS=1`(스캔/디지털 판정 임계) 보수값 적정성.
- Excel 백엔드(excel-parser, `:18055`) 자동 기동·라우팅 정식화(현재 markitdown 사용).

### 6.3 Modal Enrich
- 그림(image) 모달의 vision LLM 연결: 현재 `/parse`는 `vision_llm=None`이라 그림은 보강되지 않고 원본 통과 — vision 백엔드 확정 필요.
- `KBP_MODAL_ENRICH` 기본 off 운영 정책: 검색 품질(표 의미요약) vs 파싱 속도/프록시 부하의 트레이드오프 확정 필요.

### 6.4 Chunking
- 토큰 타깃(허브 기본 1100/600)을 KB 정책으로 고정할지·문서군별로 분기할지.
- `semantic` 방법은 리랭커 호출 폭증으로 대형 문서에서 타임아웃 위험 → 활성 정책/폴백 기준 확정 필요.
- 모달 원자성 시 토큰수·순서 정합 유지 검증(전략의 모달 분리 로직).

### 6.5 Insert / Community / Search 배선
- **Search 라우터 배선**: 현재 facade `/search` 는 bare edgequake hybrid 만 노출하고 `unified_search` local/global 라우터는 미배선(app.py 미import). global(커뮤니티 map-reduce) 능력을 외부에 노출할지·언제 라우터를 facade 에 연결할지 확정 필요.
- **커뮤니티 트리거/가드**: `POST /communities/build`(202 + 백그라운드, 예외 swallow) 온디맨드 경로와 global 검색의 build-if-missing 경로가 공존. KB 규모별 admission 임계(대형 그래프 리소스 가드)가 운영 가능한 형태인지 확정 필요.
- **DSN 포트 정합**: 코드 기본 DSN 은 `port=5432`(community.py `DEFAULT_DSN`)이고 운영 전제는 `:5433` — 환경별 명시 필요.
- **edgequake base URL 정합**: search.py 기본 `:8080` vs 전용 edgequake 기동 `:8081` — 배선 일원화 필요.

### 6.6 모니터링 / 인덱스
- **타이밍 모니터링 미구현**: 통일 타이밍 트리는 plan(v2 READY) 단계. facade `/chunk`·`/insert` passthrough 훅은 코드에 존재하나, parse-svc 파서 단계 타이머(P2), edgequake phase 도출(P3), kb-backend 집계·영속·프론트 카드(P5/P6)는 미구현. parse-svc 내부 실행 지점 확정이 하드 게이트.
- **KV GIN 인덱스 결정(2026-06-25 확정)**: `eq_*_kv.value` 전체에 `USING GIN (value)` 인덱스를 만들지 않는다. 실측에서 live JSON 약 2.4MB/1,601행 대비 1,020MB 로 비대화되어 checkpoint KV upsert 1건이 109s 소요. JSONB 검색이 필요하면 전체 value GIN 이 아니라 특정 key family/JSON path 의 partial/expression index 만 허용한다.
