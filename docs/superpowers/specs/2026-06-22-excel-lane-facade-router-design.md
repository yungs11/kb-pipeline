# 엑셀 lane — facade 전략 라우터 설계

**작성일:** 2026-06-22
**대상:** `8.kb-pipeline/service`(facade) + `knowledge_base` kb_pipeline tail (둘 다 additive)

## 1. 배경 / 문제

kb_pipeline provider 의 엑셀(xlsx/xlsm/xls)은 현재 facade `/parse`(parse-svc markitdown) →
`/chunk`(adaptive_chunk) 를 탄다. markitdown 은 엑셀 구조(병합/헤더/region)를 잃고,
adaptive_chunk 는 표 위주 엑셀에 부적합하며 LLM(deepseek/qwen) 의존이라 현재 불안정하다.

별도 서비스 **excel-rag-parser(:18055)** 는 엑셀을 region 단위로 파싱+청킹해 RagChunk 를
낸다(LLM 무관). 엑셀은 이걸 타야 한다.

## 2. 아키텍처 결정 (확정: A = 전략 라우터)

**chunk(=청크 생성) 단계를 "전략 라우터"로 둔다. excel-rag-parser 는 parse+chunk 가 결합된
엑셀 전용 백엔드로 그대로 위임(이식·복붙 없음).**

- 근거: excel-rag-parser 의 region 검출이 곧 청킹이라 parse/chunk 분리 불가. 분리·이식(B)은
  region 스키마를 서비스 경계 너머로 퍼뜨려 응집↓·결합↑. **위임이 가장 유지보수 좋음.**
- excel-rag-parser·parse-svc·adaptive_chunk·edgequake = 동급의 은닉 전용 백엔드.

**두 lane:**
```
일반:  파일 → parse-svc(/parse) → enriched_content → adaptive_chunk(/chunk) → chunks → insert
엑셀:  파일 → excel-rag-parser(:18055, parse+chunk) → chunks → insert   (parse-svc·adaptive 우회)
```

라우팅은 **파일을 가진 facade `/parse` 지점**에 둔다(excel-rag-parser 입력 = 파일 바이트).

## 3. 컴포넌트

### 3.1 `service/excel_parser_client.py` (신규, facade)

`ExcelRagParserClient(base_url=KBP_EXCEL_URL(:18055), timeout, poll_timeout, poll_interval)`:
- `parse_chunks(file_bytes, filename) -> list[dict]`:
  - `POST {base}/parse/jobs/file` (multipart `file`, form `doc_name=filename`) → `{job_id}`.
  - `GET {base}/parse/jobs/{id}` 폴링 → terminal. succeeded → `result.chunks`(RagChunk dict 리스트);
    failed/cancelled/timeout → `RuntimeError`.
  - RagChunk dict 를 **facade 청크 contract** 로 정규화(아래 §5) 후 반환.

### 3.2 facade `/parse` 포맷 라우팅 (`service/app.py`)

`get_excel_client()` 의존성 추가(`ExcelRagParserClient(os.environ.get("KBP_EXCEL_URL",
"http://localhost:18055"), ...)`). `/parse` 핸들러:
- `_is_excel(safe_name)` (ext ∈ {xlsx,xlsm,xls}) 이면 →
  `chunks = excel_client.parse_chunks(data, safe_name)` →
  반환 `{"enriched_content": <display용 join>, "n_blocks": len(chunks), "modal_spans": [],
         "chunks": chunks, "chunk_strategy": "excel_rag_parser"}`.
- 아니면 → 기존 `pc.parse(...)` 그대로(`{enriched_content, n_blocks, modal_spans}`, `chunks` 키 없음).

`_is_excel(name)` = ext 소문자 ∈ {xlsx,xlsm,xls}. facade 로컬 헬퍼(knowledge_base 의
`_is_excel_filename` 과 동일 로직).

### 3.3 `KbPipelineClient.parse` (knowledge_base, additive)

현재 반환 `{enriched_content, n_blocks, modal_spans}` 에 **passthrough 추가**:
`chunks = body.get("chunks")`(있으면 그대로), `chunk_strategy = body.get("chunk_strategy")`.
비엑셀(키 부재)이면 `chunks=None` → 기존 동작 불변.

### 3.4 kb_pipeline tail (`pipeline.py::_ingest_kb_pipeline_tail`, additive 분기)

`on_stage("parse")` 후 parse 결과에 `chunks`(엑셀 strategy) 가 있으면:
- `on_stage("chunk")` 단계에서 **kbp.chunk 호출을 건너뛰고** parse 가 준 chunks 를 청크 결과로 사용.
- `chunking_selection = {"method_selected": "excel_rag_parser", "scores": {}, "methods_compared": []}`.
- 이후 insert 는 동일(chunk text 리스트 → edgequake passthrough).

`chunks` 부재(비엑셀)면 기존 `kbp.chunk(enriched_content)` 그대로. **분기 1개만 추가, 그 외 불변.**

## 4. 데이터 흐름 (엑셀)

```
kb_pipeline tail
  parse(file) ─HTTP→ facade /parse ─(_is_excel)→ ExcelRagParserClient ─HTTP→ :18055 /parse/jobs/file
                                                  ← RagChunks ← poll
              ← {enriched_content(display), chunks:[정규화], chunk_strategy:"excel_rag_parser"}
  chunk 단계: parse.chunks 존재 → kbp.chunk 생략, 그대로 사용
  insert(chunk_texts) → edgequake passthrough (변경 없음)
```

## 5. 정규화 (RagChunk → facade 청크 contract)

facade 청크 contract(adaptive 경로와 동일): `{chunk_index:int, text:str, titles_context, pages:list}`.
RagChunk dict → :
- `text` = `content_text`(비면 `title` 폴백). 빈 텍스트 청크는 제외.
- `titles_context` = `path`(list) 또는 `[title]`(있으면) 또는 `None`.
- `pages` = `[]`(엑셀은 페이지 개념 없음; sheet 는 titles_context 로).
- `chunk_index` = 정규화 후 0-based enumerate.
- `enriched_content`(display) = 정규화된 text 들을 `"\n\n"` join(표시용, 청크 경계와 무관).

## 6. 계약 / 불변식

- **비엑셀 경로 완전 불변**(facade `/parse` 분기, `chunks` 키 비엑셀엔 없음).
- **다른 provider(dify/edgequake/raganything/ragflow) 완전 불변** — kb_pipeline tail 만 분기.
- **LLM 무관** — excel-rag-parser·edgequake insert 어디에도 modal/adaptive LLM 없음.
- adaptive_chunk·parse-svc·modal `enrich` **무변경**.
- KbPipelineClient.parse 추가는 **additive**(비엑셀 None).

## 7. 엣지 케이스

1. 엑셀인데 excel-rag-parser 실패(폴링 failed/timeout) → facade `/parse` 500 또는 `{status:failed}`
   → tail `_fail("kb_pipeline excel parse 실패")`. (문서 전체 실패; 부분 강등 없음.)
2. 엑셀인데 청크 0개 → tail `_fail("...청크 0개")` (기존 chunk-0 분기 재사용).
3. 빈 `content_text` 청크 → 정규화에서 제외.
4. 비엑셀 → 분기 안 탐, 기존과 byte 동일.
5. 파일명에 ext 없음 → `_is_excel`=False → 일반 경로.

## 8. 테스트

- facade: `_is_excel` 판정, `/parse` 엑셀이면 ExcelRagParserClient 호출(mock)+`chunks`/`chunk_strategy`
  반환, 비엑셀이면 parse-svc 호출+`chunks` 키 없음. ExcelRagParserClient 폴링/정규화(빈텍스트 제외,
  titles_context, chunk_index) — :18055 mock(respx/monkeypatch).
- knowledge_base: KbPipelineClient.parse 가 chunks/chunk_strategy passthrough(엑셀)·None(비엑셀);
  tail 이 엑셀이면 kbp.chunk 미호출+chunking_selection=excel_rag_parser, 비엑셀이면 기존대로.

## 9. Non-goals

- excel-rag-parser 청킹 로직 이식/복붙 금지(위임만).
- page→이미지→MinIO + adaptive page 지표(별도 Feature 2, 다른 세션).
- 1200 recursive 재청킹(=옵션 C) 안 함 — excel-rag-parser native 청크 사용.
- LLM 백엔드 복구는 본 작업 범위 밖(엑셀은 LLM 무관이라 독립 동작).
