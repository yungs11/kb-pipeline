# 파서 일원화 (Phase 2) — 설계 스펙

작성 2026-07-02 · 상태: 설계(브레인스토밍 완료) → 구현계획 단계

## 0. 한 줄 요약
흩어진 3개 파서(parse-svc + 외부 excel-parser:18055 + 외부 document-parser:18050)를
**parse-svc 하나로 in-process 통일**한다. facade 는 파싱 로직을 전부 버리고 순수
오케스트레이션만 남긴다. 청킹 진행 여부는 확장자가 아닌 **`chunk_needed` flag** 로
parse-svc 가 결정해 facade 에 알린다. **markitdown 은 완전 제거**한다.

## 1. 목표 / 비범위

### 목표
- **parsing 은 parse-svc 가 단독 소유(위임).** facade 는 파일을 parse-svc `/parse` 에
  넘기고, 응답의 `chunk_needed` 에 따라 `/chunk`(adaptive) 또는 바로 `/insert` 로 간다.
- excel 파서(`excel_parser_rag`)와 document-parser OCR(pptx+이미지)을 **parse-svc 에
  in-process 흡수** → 외부 서비스 excel-parser·document-parser 제거.
- 파서를 **확장자(도메인)별 서브패키지 + 재사용 도구** 2층 구조로 정리.
- **markitdown 패키지/코드 완전 제거** (표 붕괴 실증).

### 비범위
- **PDF 파서 교체 안 함** — OpenDataLoader 유지(본문+표 완벽, 실측).
- kb-backend/frontend 변경 — facade 계약(`/parse` 응답에 `chunk_needed` 추가)은
  additive 라 소비자 무변경(응답 필드 추가만).
- adaptive_chunk / edgequake 내부 — 무변경.
- 엑셀 내 차트/삽입이미지 OCR — **처리 안 함**(사용자 결정). excel_vl_processor 흡수 제외.

## 2. 파서 라우팅 (실측 기반, 확정)

| 확장자 | 파서(도메인) | 도구/엔진 | chunk_needed | 근거 |
|---|---|---|---|---|
| pdf | `parsers/pdf` | OpenDataLoader(JRE) | true | 본문순서+표병합 완벽(재실행 검증) |
| xlsx/xls/xlsm | `parsers/excel` | excel_parser_rag(kordoc) | **false**(자체청킹) | region=청킹, LLM무관 |
| docx | `parsers/docx` | kordoc(node, 네이티브) | true | 병합표 보존, PDF변환 없음 |
| pptx | `parsers/ocr` | gotenberg→PDF→fitz→VL | true | soffice/PDF경유 파편화 회피 위해 OCR |
| 이미지(png/jpg/jpeg/gif/bmp/tif/tiff/webp)/스캔 | `parsers/ocr` | fitz/base64→VL | true | VL 필요 |
| 그 외/미지 (폴백) | `parsers/docx`(kordoc) | kordoc | true | markitdown 폐기 대체 |

- **markitdown 라우팅/코드/패키지 전부 삭제.** `PARSER_ROUTING` 의 "markitdown" 라벨
  제거, 폴백 default 를 kordoc(docx 파서)로.

## 3. 아키텍처 — parse-svc 패키지 구조

```
parse_service/
  app.py                 # POST /parse 진입점: router 호출 → {..., chunk_needed} 반환. /healthz.
  router.py              # 확장자 → parsers/<도메인> 디스패치 (얇은 계층). 폴백=docx(kordoc).
  parsers/
    pdf/                 # parse(file_bytes, filename) → (PageDoc[], chunk_needed=True)
    excel/               # parse(...) → (chunks[], chunk_needed=False)  ← excel_parser_rag 이식
    docx/                # parse(...) → (PageDoc[], chunk_needed=True)  ← tools/kordoc 호출
    ocr/                 # parse(...) → (PageDoc[]/elements, chunk_needed=True)  ← VL OCR 흡수
      vl_api.py          #   VL 호출 + OCR_JSON_SCHEMA (document-parser vision_language_model 이식)
      elements_parser.py #   VL 응답 → elements[] (document_processor 이식)
      image_utils.py     #   base64/멀티페이지/포맷 (utils/image 이식)
      pdf_converter.py   #   pptx→PDF (gotenberg) + fitz 페이지 렌더 (pdf_utilities+safe_fitz 이식)
      prompts.py         #   SYSTEM/USER 프롬프트 (core/config/prompts 이식)
      router 내부(pptx/이미지 경로만) — file_converter_router 발췌
  tools/                 # 재사용 도구(외부 바이너리/라이브러리 래퍼)
    opendataloader.py    #   PDF → md (JRE subprocess)
    kordoc.py            #   docx → <table>md (node CLI)
  (기존 유지) pdf_pages.py, minio_client.py, modal enrich 경로(kb_pipeline.modal)
```

**단위 인터페이스(불변):** 각 `parsers/<도메인>` 은 동일 시그니처
`parse(file_bytes: bytes, filename: str, *, ...) -> ParseResult` 를 노출.
`ParseResult = {enriched_content? , chunks?, chunk_needed: bool, n_blocks, modal_spans,
docs_id, page_count, pages, page_spans, timing_metrics}`.
- chunk_needed=False 파서(excel)는 `chunks[]` 를 채우고 `enriched_content` 는 join 문자열(호환).
- chunk_needed=True 파서는 `enriched_content`(+modal 〈MODAL〉 원자마킹) 를 채운다.

**router.py:** 확장자 → 도메인 매핑만. 파싱 로직 없음.

## 4. facade 변경 (파싱 로직 제거)

### 제거
- `service/parsing.py` — 삭제(parse-svc 소유).
- `service/ingest.py` `run_front`/`FrontError`(파싱 경로) — 삭제.
- `service/excel_parser_client.py` — 삭제(excel 위임이 parse-svc 로 이동).
- `service/app.py` `_is_excel`/`get_excel_client`/excel 분기 — 삭제.
- 엔드포인트 **`/ingest/submit`, `/ingest/status` 제거** (kb-backend 실코드 미사용 확인:
  `backend/app` 호출 0건, 실사용 = `/parse,/chunk,/insert,/ingest,/search`).

### 유지/변경
- `/parse` — parse-svc `/parse` 로 위임(HTTP), 응답 그대로 반환(+`chunk_needed` passthrough).
- `/ingest`(one-shot) — parse-svc `/parse` → **chunk_needed 분기** → adaptive `/chunk`(true)
  또는 바로 insert(false) → edgequake insert. (excel 은 chunk_needed=false 라 /chunk 스킵.)
- `/chunk`, `/insert`, `/search`, `/communities/build`, `/chunks`, `/doc` — 무변경.

### facade `/ingest` 청킹 분기 (핵심)
```
parsed = parse_svc.parse(file, filename)          # parse-svc 위임
if parsed["chunk_needed"]:
    chunks = adaptive.chunk(parsed["enriched_content"], atomic_markers=MODAL)
else:
    chunks = parsed["chunks"]                       # excel: parse-svc 가 이미 청킹
insert(chunks) → edgequake
```
**chunks 스키마 통일**: parse-svc 가 두 경로 모두 facade insert 가 기대하는 동일 청크
스키마(`{chunk_index, text, titles_context, pages}`)로 정규화해 반환한다. 즉 excel 의
자체청킹 결과(`content_text`/`title`/`path`)를 parse-svc 안에서 이 스키마로 매핑한다
(현재 facade `excel_parser_client.normalize_chunks` 가 하던 정규화를 parse-svc 로 이동).
facade 는 chunk_needed 만 보고 분기할 뿐 청크 스키마 변환은 하지 않는다.

## 5. 흡수 대상 (원본 → parse_service/)

### excel (excel_parser_rag, ~8k LOC → parsers/excel/)
- `/Users/xxx/workspace/7.excel-parser/excel_parser_rag/**` 통째 이식(서브패키지 보존).
  backends(kordoc/openpyxl/auto)·canvas·chunking·classification·detection 등.
- 진입: `parsers/excel/__init__.py:parse()` — 기존 excel-parser `/parse` 의 동기 로직
  (`get_backend(config.backend).parse` → chunks) 를 in-process 로. `_ALLOWED_SUFFIXES` 유지.
- **버림**: excel-parser 의 FastAPI service/jobs(비동기 잡스토어) — parse-svc 가 동기 호출.

### ocr (document-parser, ~1.4k LOC → parsers/ocr/) — pptx + 이미지만
| 원본 | → parse_service/parsers/ocr/ | 역할 |
|---|---|---|
| model/vision_language_model.py | vl_api.py | call_vl_api_with_base64, OCR_JSON_SCHEMA |
| pipeline/document_processor.py | elements_parser.py | parse_vision_language_response_to_elements, normalize_all_elements |
| pipeline/file_converter_router.py(pptx/이미지 경로) | (router 내부) | route → base64 |
| pipeline/handlers/image_handler.py | image_utils.py 내 | image_file_to_base64_list |
| utils/image.py | image_utils.py | image_to_base64, multipage, get_image_page_count |
| converter/pdf_utilities.py | pdf_converter.py | convert_to_pdf_bytes(gotenberg), is_convertible_to_pdf |
| pipeline/handlers/pdf_handler.py | pdf_converter.py | pdf_bytes_to_base64_list |
| converter/safe_fitz.py | pdf_converter.py | safe_validate_pages(fitz 렌더) |
| core/config/prompts.py | prompts.py | SYSTEM/USER 프롬프트 |

- **HTTP→in-process 진입 함수**: `parsers/ocr/__init__.py:ocr_file_to_elements(file_bytes,
  filename) -> {"elements":[{category,content:{html,markdown,text},page}], "metadata":{...}}`.
  기존 `parse_service/parsing.py:_ocr_page`(HTTP POST /api/v1/ocr) 를 이 호출로 대체.
  elements[] 는 `kb_pipeline.blockify.elements_to_blocks` 가 그대로 소비(스키마 일치).
- **버림**: redis(distributed_semaphore→asyncio.Semaphore), minio storage(compose 재사용은
  기존 minio_client 유지), transactions 오케스트레이션 과다분, auto_chunk_processor,
  json/txt/html/pdf텍스트/excel/docx텍스트 핸들러, call_vl_api_multimodal.

### 15개 document-parser 처리경로 중 흡수는 2개(pptx OCR, 이미지 OCR)뿐. 나머지 13개 제외.

## 6. 런타임 / compose 변경

### parse-svc 이미지 (Dockerfile.parse-svc)
`python:3.12-slim` + 기존 `openjdk-21-jre`(OpenDataLoader) + **node/kordoc 추가** +
**PyMuPDF(fitz)/Pillow 추가**(ocr). markitdown 제거.
- **soffice 불필요** — pptx→PDF 변환은 gotenberg(compose 서비스)가 담당.

### compose
- **제거**: `excel-parser`, `document-parser` 서비스, `redis` 서비스(document-parser 전용).
- **유지(재사용)**: gotenberg(pptx변환+OCR), minio(이미지 저장), postgres, edgequake,
  adaptive_chunk, facade, doc_guard.
- parse-svc env: `GOTENBERG_URL=http://gotenberg:3000`, `MODEL_API_URL`(원격 VL),
  `MINIO_*`(compose minio), `MODEL_API_KEY`. `KBP_OCR_URL`/`KBP_EXCEL_URL` 제거(내부화).
- facade env: `KBP_EXCEL_URL`/`KBP_OCR_URL` 제거(더는 facade 가 파서 호출 안 함).

## 7. 불변식 (보존)
- 청킹·모달원자성 = facade `/chunk` 소유(chunk_needed=true 경로), edgequake passthrough.
- 모달 마커 U+3008/U+3009 byte-identical · 표 `<table>` HTML 보존(pipe 금지) ·
  단일 Postgres+per-KB RLS · BGE-M3 1024d.
- excel 은 chunk_needed=false 로 facade `/chunk` 스킵(자체청킹). 이는 기존
  excel lane ADR(위임=최선)과 정합 — 다만 위임 지점이 facade→parse-svc 로 이동.

## 8. 구현 단계 (Phase 2a~2e) — 각 단계 끝 테스트+스택 green 확인

- **2a 재구조화(동작 보존)**: `parsers/`·`tools/` 구조 + router + `chunk_needed` flag.
  excel/ocr 는 **아직 HTTP 위임 유지**(기존 외부 서비스 호출). 구조만 잡고 동작 불변.
- **2b excel 흡수**: `parsers/excel/` 이식, HTTP 제거. excel-parser 서비스 의존 끊기.
- **2c ocr 흡수**: `parsers/ocr/` 이식(pptx+이미지), HTTP 제거. document-parser 의존 끊기.
- **2d markitdown 완전제거 + docx=kordoc + 폴백=kordoc + facade 파싱제거**
  (parsing.py/run_front/excel_parser_client 삭제, /ingest/submit·status 제거).
- **2e compose/Dockerfile 정리**: excel-parser·document-parser·redis 제거, parse-svc
  Dockerfile 에 node/kordoc+fitz, E2E 재검증.
- 구현은 **phase 별 ultracode** 로 진행. 각 phase 후 `_workspace/03-dev-progress.md` 갱신.

## 9. 테스트 / 검증
- 각 parser 단위 테스트(파일 바이트 → ParseResult, chunk_needed 정확).
- router 라우팅 테스트(확장자→도메인, 폴백=kordoc).
- facade `/ingest` chunk_needed 분기 테스트(excel→스킵, 그외→/chunk).
- 회귀: 별표1(휴가규정 PDF) 표 `<table>`+rowspan 보존, 05 PPTX OCR, docx kordoc 병합.
- E2E: facade `/ingest`(pdf/docx/pptx/xlsx/이미지) → 적재 → `/search`. 격리 유지.
- markitdown import 0 확인(grep), excel-parser/document-parser/redis 컨테이너 부재 확인.

## 10. 리스크
- **excel_parser_rag 8k LOC 이식** — 의존/import 경로 재작성 규모 큼. 2a 에서 구조만 잡고
  2b 에서 이식 격리.
- **fitz(PyMuPDF) 네이티브 크래시** — safe_fitz subprocess 격리 유지.
- **VL API(MODEL_API_URL) 원격 의존** — pptx/이미지 OCR 은 원격 VL 필요(현행과 동일).
- **kordoc docx 순서** — docx 는 네이티브라 PDF 경유 없음(pptx 파편화 리스크 회피됨). 단
  실 docx 회귀 스모크로 확인.
- **이미지 크기** — node+JRE+fitz 동거로 parse-svc 이미지 비대. 멀티스테이지/캐시 활용.
