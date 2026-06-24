<!-- plan-version: v3 -->
<!-- codex-validation: READY v2 at 2026-06-23T00:56:40Z; v3 live-verified (parse-svc e2e green), codex re-validation PENDING -->

# kb-pipeline 페이지 이미지 + 청크별 page_number (chunks_meta 점등)

## 변경 이력
- **v3** — 라이브 e2e(parse-svc)에서 발견한 2건 반영:
  1. **OpenDataLoader는 문서당 .md 1개**를 낸다(per-page .md 아님 — v2 §5.1.3 가정 오류, 라이브에서 page_spans가 page 1로 붕괴). 수정: `convert(..., markdown_page_separator=_PAGE_SEP)` 로 각 페이지 앞에 구분자 삽입 → 단일 .md를 SEP로 split(선두 빈 부분 1개 제거) → 페이지 복원. (`parse_service/parsing.py:_parse_pdf_to_pages`)
  2. **minio `_ensure_bucket` 제거** — `bucket_exists`/`make_bucket` 은 버킷-관리 권한을 요구해 업로드-전용/제한 자격증명에서 AccessDenied. 버킷은 인프라가 미리 만들므로 곧장 `put_object`. (`parse_service/minio_client.py`)
  - 배포 메모: parse-svc venv에 `minio`+`PyMuPDF` 설치 필요. parse-svc는 document-parser 버킷에 **write 권한 있는** minio 자격증명 필요(knowledge_base/.env의 `minio` secret은 read-only). 라이브 검증: 3페이지 PDF → page_spans 3 + minio 객체 3개(JPEG) 실제 업로드 확인.
- **v2** — codex v1 NEEDS_REVISION 3건 반영:
  1. page_spans를 기존 `parse→blockify→enrich` 평탄화 경로에서 "공짜로" 얻을 수 있다는 가정 제거. parse-svc가 **페이지 단위를 평탄화 전에 보존**하고(`parse_to_pages`/OCR elements), **명시적 오프셋 추적**(`enrich_with_spans`)으로 page_spans를 만든다. (codex 1, 3)
  2. enrich는 char offset을 추적하지 않고, modal_spans는 `parse_service/app.py:_modal_spans` 가 최종 문자열에 regex로 사후 계산함을 정확히 반영. page_spans는 modal_spans regex에 **piggyback하지 않고** enrich 조립 단계에서 별도 추적. (codex 3)
  3. 검색 근거 이미지 URL은 presigned가 아니라 **same-origin `/obj/{key}`**(`deps.minio.public_url`, knowledge_base `core/chat.py:379`, `clients/minio_client.py`). 표현 정정. (codex 2)
- **v1** — 초안.

## 0. 한 줄 목표

kb-pipeline 프로바이더로 적재한 PDF/이미지 문서가, knowledge_base 화면(청크 목록 썸네일 + 검색 근거문서 이미지)에서 **dify 경로와 동일하게** 페이지 이미지를 보이도록 만든다. = orchestrator의 기존 `chunks_meta`(page_uuid, page_number, minio_image_object)를 kb-pipeline 경로에서도 채운다.

## 1. 배경 / 문제

knowledge_base 앱(멀티 프로바이더 RAG 오케스트레이터)에는 페이지 이미지 인프라가 **이미 전부 구현되어 동작**한다 — 단 dify/edgequake/excel 경로에서만:

- `chunks_meta` 테이블 (orchestrator postgres `kb_orchestrator`): 청크마다 `page_number`, `page_uuid`, `minio_image_object`(`{docs_id}/{page_uuid}.jpeg`), `text`, `quality_score`, 리뷰 상태.
- 청크 목록 화면 `/kb/[kbId]/documents/[docId]`: "원문" 컬럼 = `minio_image_object` 썸네일, "청크 본문" = `text`. 썸네일은 same-origin `/obj/{minio_object}` 로 연다(Next 프록시 → minio).
- 검색 근거: `backend/app/core/chat.py:_attach_page_images()` 가 chunk_id → chunks_meta → `deps.minio.public_url(minio_object)`(= same-origin `/obj/{key}`) 를 `image_url` 로 부착 → 프론트 `CitationCard` 가 썸네일 렌더. (presigned GET URL 아님.)
- MinIO: 버킷 `document-parser`, 키 `{docs_id}/{page_uuid}.jpeg`, `page_uuid = "{docs_id}_{page_number}"`, `docs_id = content_hash(file_bytes)[:16]`.
- 페이지 렌더: `backend/app/core/pdf_pages.py:render_pdf_pages`(PyMuPDF) + `_render_and_upload_pages`(edgequake 경로가 사용).

**구멍**: kb-pipeline 프로바이더 경로(`backend/app/core/pipeline.py` kb_pipeline tail, ~2026-2058)는 `page_uuid=None`, `minio_image_object=None` 으로 둔다. 페이지 이미지를 렌더/업로드하지 않고, 청크별 page_number도 정확하지 않다(어댑티브가 페이지 경계를 못 받아 `chunk_pages`가 비어 `pages[0]`이 None).

## 2. 등장인물 (3개 레포)

| 이름 | 정체 | 위치 | 포트 |
|---|---|---|---|
| **orchestrator** | knowledge_base 백엔드 (ingest 지휘, chunks_meta write, docs_id 계산) | `99.projects/shinhan_trust/knowledge_base/backend` | :8080 |
| **facade** | kb-pipeline 정문 | `8.kb-pipeline/service` | :19000 |
| **parse-svc** | kb-pipeline 파서 (이번에 렌더+업로드+page_spans 추가) | `8.kb-pipeline/parse_service` | :19001 |
| **adaptive** | 청커 | `99.projects/adaptive_chunk` | :18060 |
| **edgequake** | 검색 엔진 (rdb+vector+graph) | `8.kb-pipeline/edgequake` | :8081 |

데이터 경로(적재): orchestrator → facade `/parse` → parse-svc → (orchestrator) → facade `/chunk` → adaptive → (orchestrator) → facade `/insert` → edgequake. 그 뒤 orchestrator가 chunks_meta write.

## 3. 확정된 설계 결정 (잠금)

- **D-저장 = A (chunks_meta 통일)**: 페이지 메타는 orchestrator의 `chunks_meta`(앱 DB)에 적재. kb-pipeline 응답에서 materialize. edgequake postgres에 넣지 않는다(엔진/앱 분리, 멀티프로바이더 통일성).
- **D-렌더링 = parse-svc 소유**: 페이지→JPEG 래스터화 + MinIO 업로드를 parse-svc가 수행(자기완결 프로바이더). orchestrator는 키만 조립.
- **D-키 규칙 = 기존 그대로**: 버킷 `document-parser`, object key `{docs_id}/{docs_id}_{page}.jpeg`, `page_uuid="{docs_id}_{page_number}"`. (uuid4/tenant_id 안 씀 — 기존 UI가 이 규칙으로 키를 조립하므로 반드시 일치해야 함.)
- **D-docs_id 출처 = orchestrator**: orchestrator가 `content_hash(file_bytes)[:16]` 로 계산해 `/parse` 호출 시 동봉. parse-svc는 받아서 키에만 사용(자체 생성 금지 — 양쪽 키 일치 보장). docs_id 미전달 시 parse-svc가 동일 식으로 폴백.
- **D-스코프 = 페이지 이미지 + 청크별 page_number**. 대상 포맷: **디지털 PDF, 스캔 PDF(다중 페이지), 단일 이미지**. Excel 제외(F1이 전용 파서로 우회 + 페이지 개념 없음).
- **D-후속(out of scope)**: ① 청크 본문 편집 → edgequake 재인덱스, ② chunks_meta.text 사본 제거(단일화), ③ facade 이미지 서빙 프록시 엔드포인트.

## 4. 포맷별 라우팅

| 포맷 | 텍스트(블록·경계) 출처 | 이미지 렌더 |
|---|---|---|
| 디지털 PDF | OpenDataLoader 페이지별 .md → `hybrid_to_blocks(md, page_idx)` | PyMuPDF 래스터 |
| 스캔 PDF (다중) | **페이지별 OCR(:18050) → `elements_to_blocks`**(page 보존) | PyMuPDF 래스터 |
| 단일 이미지 (.png/.jpg) | OCR(:18050) → `elements_to_blocks` (page=1) | 원본을 JPEG로 정규화 |
| Excel (xlsx) | **제외** (F1) | 제외 |

- **이미지 렌더는 디지털/스캔 공통**(PyMuPDF 래스터는 텍스트 레이어 불필요). 포맷 분기는 **블록(텍스트)** 출처에만.
- **스캔 판별(페이지 단위, mixed PDF 대응)**: OpenDataLoader 1회 변환으로 페이지별 .md 확보 + PyMuPDF 1회 렌더로 페이지별 (이미지, `get_text`) 확보. 페이지 i에 대해 OpenDataLoader .md_i 또는 `get_text(i)` 가 유의미하면 디지털 → `hybrid_to_blocks(md_i, page_idx=i)`. 둘 다 (거의) 비면 스캔 → 그 페이지의 렌더 이미지를 OCR(:18050)에 POST → `elements_to_blocks(elements)` 의 page_idx를 i로 리맵. JVM 호출은 OpenDataLoader 1회뿐(페이지당 호출 아님).

## 5. 작업 영역별 상세

### 5.1 parse-svc (`8.kb-pipeline/parse_service/`) — 신규의 핵심

#### 5.1.1 신규: MinIO 클라이언트 `parse_service/minio_client.py`
knowledge_base `clients/minio_client.py` 미러. env: `MINIO_ENDPOINT`(기본 `localhost:9000`), `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY`, `MINIO_BUCKET`(기본 `document-parser`), `MINIO_SECURE`(기본 false).
- `page_image_object_key(docs_id, page_uuid) -> "{docs_id}/{page_uuid}.jpeg"`
- `put_page_image(docs_id, page_uuid, jpeg_bytes) -> key` (content_type `image/jpeg`, 버킷 없으면 생성). 개별 페이지 업로드 실패는 비치명(로그 후 계속).

#### 5.1.2 신규: 페이지 렌더 `parse_service/pdf_pages.py`
knowledge_base `core/pdf_pages.py:render_pdf_pages` 미러. PyMuPDF(`fitz`), 기본 `dpi=300, jpg_quality=90`(뷰어 화질 우선, D3). 페이지마다 `RenderedPage(page_number(1-based), jpeg_bytes, text=get_text("text"))`. 손상/렌더 오류는 빈 리스트(비치명).

#### 5.1.3 신규: 페이지 보존 파서 `parse_service/parsing.py:parse_to_pages`
**핵심 — 평탄화하지 않는 새 경로.** 기존 `parse_to_markdown`(평탄화)은 그대로 두고(하위호환), 페이지 보존용 함수를 추가:
```
parse_to_pages(file_bytes, filename, *, ocr_url, excel_url) -> list[PageDoc]
# PageDoc = {page_number:int(1-based), blocks:list[dict]}  # blocks는 page_idx 채워짐
```
- **디지털 PDF**: OpenDataLoader convert 1회 (`markdown_page_separator=_PAGE_SEP`, JVM 1회). OpenDataLoader는 **문서당 .md 1개**를 내고 각 페이지 **앞**에 SEP를 삽입한다 → 단일 .md를 SEP로 split(선두 빈 프리앰블 1개 제거)하여 페이지 복원 → 각 페이지 → `hybrid_to_blocks(md, page_idx=page_number)`. (v2의 "per-page .md" 가정은 라이브 e2e에서 오류로 확인됨 → 정정.)
- **스캔 PDF/단일 이미지**: OCR(:18050) 호출 결과의 **raw `elements[]` 보존**(현재 `_ocr_markdown` 가 버리는 것) → `elements_to_blocks(elements)`(blockify.py:311, page 보존). 다중 페이지 스캔은 §4의 페이지 단위 판별로 페이지별 OCR.
- **페이지 번호 canonical = PyMuPDF page_count**(렌더와 정렬). OpenDataLoader/OCR 페이지가 어긋나면 로깅, 매핑 불가 페이지는 page_span 없이 진행.

> 근거: `hybrid_to_blocks(doc, page_idx=0)`(blockify.py:161)·`elements_to_blocks`(blockify.py:311, `item.get("page_idx", item.get("page", 0))`)가 **이미 page_idx를 채운다**. 새로 만드는 건 "평탄화 대신 페이지별로 blockify해 page_idx를 살리는" 호출 경로뿐.

#### 5.1.4 신규: page_spans 산출 — 명시적 오프셋 추적 `kb_pipeline/modal.py:enrich_with_spans`
enrich는 `(enriched, modal_ids)` 만 반환하고 **char offset을 추적하지 않는다**(modal.py:172-292). modal_spans는 `parse_service/app.py:_modal_spans`(app.py:63) 가 최종 문자열에 regex로 **사후** 계산한다. → page_spans는 이 regex에 piggyback 불가. 명시적 추적을 추가한다:

- enrich의 Phase A–D 내부 조립 로직을 헬퍼 `_assemble(blocks, decisions, consumed) -> (segments, seg_page_idx)` 로 추출. `seg_page_idx[k]` = 세그먼트 k를 만든 블록의 `page_idx`(모달 세그먼트는 모달 블록의 page_idx).
- **기존 `enrich`는 시그니처/반환 불변**(`(enriched, modal_ids)`) — `_assemble` 결과를 `"\n\n".join(segments)` 로 감싸 그대로 반환. → **facade `service/ingest.py:run_front` 등 기존 호출자·F1 안 깨짐.**
- **신규 `enrich_with_spans(blocks, ...) -> (enriched, modal_ids, page_spans)`**: `_assemble` 후 `"\n\n".join` 하면서 running offset을 누적("\n\n" 2자 포함), `seg_page_idx` 로 페이지별 [min char_start, max char_end) 를 모아 `page_spans=[{page_number, char_start, char_end}]`(page_number = page_idx, enriched_content 기준 char 오프셋) 생성.
- 블록에 page_idx가 비면(전부 0) page_spans는 전체를 page 1로 덮는 단일 span으로 강등(안전).

#### 5.1.5 변경: `parse_service/app.py:run_parse` / `/parse` 엔드포인트
- `/parse` 폼에 **optional `docs_id`** 추가(없으면 `content_hash(file_bytes)[:16]` 폴백).
- run_parse 흐름:
  1. `pages = parse_to_pages(...)` → 페이지별 blocks(page_idx 채움). `_strip_pua` 는 블록 텍스트 단계로 이동(기존 PUA 제거 유지).
  2. 모든 페이지 blocks를 문서순 concat → `enriched, modal_ids, page_spans = enrich_with_spans(blocks, ...)`.
  3. `modal_spans = _modal_spans(enriched)`(기존 regex, 불변).
  4. PDF면 `render_pdf_pages(file_bytes)` → 각 페이지 `put_page_image(docs_id, "{docs_id}_{p}", jpeg)`. 이미지면 원본 1장 JPEG 정규화 업로드.
- `/parse` 응답 (additive — 기존 enriched_content/n_blocks/modal_spans 그대로):
  ```json
  {
    "enriched_content": "...", "n_blocks": 0, "modal_spans": [...],
    "docs_id": "ab12...", "page_count": 7,
    "pages": [{"page_number": 1, "page_uuid": "ab12..._1", "minio_object": "ab12.../ab12..._1.jpeg"}, ...],
    "page_spans": [{"page_number": 1, "char_start": 0, "char_end": 1820}, ...]
  }
  ```

### 5.2 facade (`8.kb-pipeline/service/`) — additive plumbing

- `parse_client.py:parse()`: `docs_id` 인자 추가(폼 전달), 응답의 `docs_id/page_count/pages/page_spans` passthrough.
- `service/app.py:/parse`(72): `docs_id` optional 폼 → parse_client 전달, page 필드 반환. **Excel 분기(81-89) 손대지 않음**(F1). 비-Excel 경로만 page 필드 부착.
- `service/adaptive_chunk.py`: `chunk()` 에 `page_spans`(+선택 `pages`) 인자 추가 → adaptive `/chunk` 바디에 동봉.
- `service/app.py:/chunk`(94): `page_spans`(+선택 `pages`) optional 바디 필드 추가 → adaptive로 forward. 응답 정규화(`chunk_pages`→`pages`)는 기존(115) 그대로.
- `service/app.py:/ingest`(195): orchestrator는 이 엔드포인트를 쓰지 않음(별도 /parse+/chunk+/insert). **F1이 /ingest를 건드리므로 이번 변경에서 /ingest는 손대지 않음**(충돌 회피). /parse·/chunk만 확정.

> F1 코디네이션: 본 작업의 facade 변경은 전부 **additive**(optional 필드), 대상 `/parse`·/chunk`. F1은 `/ingest`+excel. `enrich`도 시그니처 불변. 머지 충돌 없음.

### 5.3 adaptive (`99.projects/adaptive_chunk/`) — 경계 수용 + attribution post-pass

근거(조사): `Chunk`(types.py:16-36)에 char offset **없음**, `chunk_pages`는 PageSplitter만 채움. 기본 `overlap_tokens=0`(schemas.py:39), `coref_fn=None`(main.py) 에서 `chunk_text`는 입력의 **verbatim 부분문자열** → 순차 커서 substring 매핑 안전. 파이프라인 순서: split → score → select → postprocess → overlap → serialize(runner.py).

- **schemas.py `ChunkRequest`(87)**: additive
  ```python
  pages: Optional[list[dict]] = None        # [{page_number, markdown}] → ParsedDoc 구성(page 방법 합류)
  page_spans: Optional[list[dict]] = None   # [{page_number, char_start, char_end}] → 전 청크 attribution
  ```
- **runner.py `run_chunk`(259)**: `page_spans` 파라미터 추가. `parsed`는 이미 인자로 있음. `pages`가 오면 `ParsedDoc(doc_name, markdown=text, pages=[ParsedPage(...)], blocks=[], metadata={})` 직접 구성(OcrParser 우회) → page 방법 합류.
- **신규 post-pass `_attribute_page_numbers(chunks, text, page_spans)`**: `select()` 반환 직후·`postprocess`/`apply_overlap` **이전**(runner.py ~340). 각 청크의 `chunk_text`를 `text`(= adaptive 입력 = enriched_content)에서 **순차 커서 find** → char 범위 → `page_spans` 와 겹치는 page_number(들)을 `chunk_pages` 에 set(PageSplitter가 이미 채운 경우 보존). 부분문자열 미발견은 `chunk_pages=[]` 유지(비치명).
- **main.py `/chunk`(134)·`/chunk/jobs`(텍스트 경로)**: `page_spans`·`pages` passthrough.
- 출력 `chunk_pages` 는 이미 응답 노출(runner `_chunk_to_dict`) → 출력 스키마 변경 불필요.
- **불변식**: page_spans char 오프셋은 enriched_content(= adaptive 입력 `text`) 기준 — 동일 문자열이라 매핑 정합. overlap 켜면 chunk_text 변형되므로 attribution은 overlap **이전** 수행.

### 5.4 orchestrator (`knowledge_base/backend`) — chunks_meta 점등

- `clients/kb_pipeline_client.py:parse()`: `docs_id` 동봉 전송, 응답 `pages/page_spans/docs_id` 수신·반환.
- `clients/kb_pipeline_client.py:chunk()`: `/chunk` 요청에 `page_spans` 동봉(parse 응답값).
- `core/pipeline.py` kb_pipeline tail(~1910 docs_id, ~2026-2058 chunks_meta):
  - `new_docs_id = (rec.content_hash or content_hash(file_bytes))[:16]`(기존) → `kb_pipeline_client.parse(docs_id=new_docs_id, ...)`.
  - 청크 row(현재 None):
    ```python
    pages = (c or {}).get("pages") or []
    page_number = pages[0] if pages else None
    page_uuid = f"{new_docs_id}_{page_number}" if page_number is not None else None
    minio_image_object = deps.minio.page_image_object_key(new_docs_id, page_uuid) if page_uuid else None
    ```
  - **이미지 업로드는 parse-svc가 이미 수행** → orchestrator는 `_render_and_upload_pages` 호출 안 함, 키만 조립.
- `replace_chunks_meta`(repositories.py:190) 기존 그대로 영속화.

### 5.5 읽기 경로 (전부 기존 — chunks_meta 차면 자동 점등)

- **청크 목록**: `GET /kb/{kb}/documents/{doc}` → documents+chunks_meta → "원문"=썸네일(`/obj/{minio_object}`), "청크 본문"=text. **코드 변경 0**.
- **검색 근거**: `chat.py:_attach_page_images`(355) → chunk_id→chunks_meta→`public_url`(same-origin `/obj/{key}`)→`image_url`→`CitationCard`. **코드 변경 0**. (edgequake가 돌려준 `{doc}-chunk-N` 가 chunks_meta.chunk_id 와 일치 — kb_pipeline tail이 `f"{doc_id}-chunk-{i}"` 로 저장하므로 조인 성립.)

## 6. 일관성 모델 (이번 스코프 범위)

- 적재 시 **fan-out**: 어댑티브 출력을 ① facade `/insert`→edgequake(검색) ② orchestrator chunks_meta(이미지/UI) 에 **같은 출처에서 동시 기록** → 태생적 일치.
- edgequake↔chunks_meta 편집 동기화(재인덱스)는 **후속 과제**(scope 밖).

## 7. 테스트

- parse-svc: ① `parse_to_pages` 가 페이지별 blocks에 page_idx를 정확히 채우는지(디지털 PDF 다중 페이지 + 스캔 페이지 OCR) ② `enrich_with_spans` page_spans char 범위가 enriched_content 슬라이스와 정합 ③ `enrich` 2-튜플 반환 불변(회귀) ④ render+upload 키 `{docs_id}/{docs_id}_{p}.jpeg`(minio mock) ⑤ Excel/비대상은 page 필드 없이 기존 응답.
- adaptive: ① `_attribute_page_numbers` char-range 매핑(페이지 경계 걸친 청크→복수 page) ② `pages` 입력 시 page 방법 합류 ③ page_spans 없을 때 무변경(회귀) ④ overlap on일 때 attribution이 overlap 이전 수행.
- facade: `/parse` docs_id 전달+page 필드 반환, `/chunk` page_spans forward, Excel 분기 무변경.
- orchestrator: kb-pipeline 경로 chunks_meta에 page_uuid/minio_image_object/page_number 채워짐(None 회귀 방지).
- e2e: 다중 페이지 PDF를 kb-pipeline 프로바이더로 적재 → 청크 목록 썸네일 + 검색 근거 이미지.

## 8. 리스크 / 미해결

1. **페이지 정렬**: PyMuPDF(canonical)/OpenDataLoader/OCR 페이지 1:1 가정. 어긋나면 로깅 + 매핑 불가 페이지 graceful(page_number=None → 썸네일만 없음, 본문/검색 정상).
2. **per-page blockify vs 기존 join 차이**: 현재는 페이지 .md를 join 후 1회 blockify. 신규는 페이지별 blockify → concat. 페이지 경계의 단락/heading 파싱이 미세하게 달라질 수 있음(enriched_content가 기존과 약간 다를 수 있음). PDF kb-pipeline 경로 한정 변경이며 의도된 것. 회귀 테스트로 확인.
3. **페이지 경계 흡수 edge**: 모달이 직전 페이지의 제목 text 블록을 흡수하면 그 세그먼트 page_idx가 모달 페이지로 귀속 → 경계에서 attribution이 1페이지 어긋날 수 있음(드묾). 허용.
4. **스캔 판별 휴리스틱**: `get_text`/OpenDataLoader .md 공백 임계값 → 오탐 가능. 페이지 단위 판별로 완화.
5. **attribution verbatim 의존**: facade가 overlap/coref 미사용(기본값) 유지해야 함 — 명시적 기본 옵션 전송.
6. **3개 레포 동시 변경**. 배포 순서: adaptive(additive) → parse-svc/facade(additive, enrich 시그니처 불변) → orchestrator(소비). 각 단계 하위호환.

## 9. 구현 순서

0. parse-svc `minio_client` + `pdf_pages` 신규 (단위 테스트)
1. `kb_pipeline/modal.py` `_assemble` 추출 + `enrich_with_spans` 신규(`enrich` 불변 회귀 테스트)
2. parse-svc `parse_to_pages` + `run_parse` 개편(page_idx 보존 + render/upload + page_spans) + `/parse` additive
3. adaptive `pages/page_spans` 수용 + `_attribute_page_numbers` post-pass
4. facade `/parse`·/chunk` additive plumbing (Excel/`/ingest` 무변경)
5. orchestrator kb_pipeline_client + pipeline tail chunks_meta 점등
6. e2e 검증(다중 페이지 PDF → 화면 썸네일 + 검색 근거)
