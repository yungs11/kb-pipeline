# PDF Triage (PyMuPDF 기반 페이지 분류) 설계

**목표**: parse-svc 의 PDF 파싱에서, OCR/VL 같은 비싼 처리를 쓰기 전에 **PyMuPDF 로 저비용 페이지 시그널을 뽑아 페이지별로 처리 경로를 결정(triage)** 한다. 디지털 텍스트 페이지는 무료 추출(ODL), 스캔+단순 페이지는 OCR, 복잡/혼합/차트 페이지는 VL 로 보내 **비용·품질을 동시에 최적화**한다.

참고: PyTorchKR "PyMuPDF 기반 초가성비 문서 분류(Triage)" 아이디어 및 사용자 제공 레퍼런스 `triage.py`.

## 배경 / 현재 상태

- 현재 PDF 파서(`parse_service/parsers/pdf/__init__.py::parse`)는 페이지별로 OpenDataLoader(ODL, Apache-2.0) markdown 을 뽑고, `_digital_text_len(md) >= _DIGITAL_MIN_CHARS(1)` 로 **digital vs scanned** 만 2분기한다(2026-07-07 추가). scanned 페이지는 `render_pdf_pages`(PyMuPDF) 로 JPEG 렌더 → in-process VL(`ocr_elements_sync`, 기본 `qwen/qwen3-vl-235b-a22b-instruct`).
- PyMuPDF(`fitz`)는 이미 의존성으로 설치·사용 중이다(`pdf_pages.py`, `parsers/ocr/pdf_converter.py`). **AGPL 라이선스는 책임자 승인 아래 사용 결정됨** — 본 설계는 fitz 를 triage 신호 추출에도 사용한다.
- 텍스트 추출기 OpenDataLoader = Apache-2.0(안전), pdfminer.six = MIT(설치됨, 본 설계 미사용).

이 triage 는 기존 2분기(`_digital_text_len`)를 **4-버킷 다중신호 분류**로 대체한다.

## 아키텍처

두 부분:

1. **`parsers/pdf/triage.py`** (신규) — 순수 분류 로직. 입력 `pdf bytes` → 페이지별 `PageSignals`(버킷+사유+신호). 외부 서비스 호출 0, fitz 로컬 연산만.
2. **`parsers/pdf/__init__.py::parse`** (수정) — triage 결과로 페이지별 핸들러 라우팅. ODL/VL/OCR-seam 은 기존 자산 재사용.

경계: triage 모듈은 **판정만** 한다(렌더·OCR·VL 호출 없음, 부수효과 없음). 실제 처리는 `parse()` 가 route 를 보고 수행한다. 이렇게 분리해 triage 를 독립적으로 테스트한다.

## PageSignals / 신호 추출

`extract_signals(page: fitz.Page) -> PageSignals` (레퍼런스 이식, parse-svc 는 bytes 로 open):

| 신호 | 산출 | 용도 |
|---|---|---|
| `char_count` | `sum(len(w[4]) for w in page.get_text("words"))` | 텍스트 유무/양 |
| `word_count` | `len(page.get_text("words"))` | 텍스트 양 |
| `has_native_text` | `char_count > 20` (워터마크/푸터 무시) | 디지털 여부 |
| `text_coverage` | text block 면적합 / page 면적 (`get_text("blocks")`, block_type==0) | 텍스트 밀도 |
| `image_count`, `image_coverage` | `get_image_info()` bbox 면적합 / page 면적 | 이미지 지배도 |
| `has_tables`, `table_count` | `len(page.find_tables().tables)` | 표 유무/개수 |
| `has_forms` | widget annotation 존재(`page.annots()`, `PDF_ANNOT_WIDGET`) | 양식 |
| `block_count` | `len(get_text("blocks"))` | 레이아웃 밀집도 |
| `vector_drawing`, `drawing_count` | `len(page.get_drawings())` | 순서도/차트/도형 근사 |

`page_area`는 0 나누기 방어(`or 1.0`). 모든 커버리지는 `min(.., 1.0)` 클램프.

## 분류 규칙 (`classify(sig, **thresholds)`)

우선순위 순서로 판정하고 `sig.bucket` + `sig.reason` 기입. 임계값은 **키워드 인자(모듈 상수 기본값)** 로 튜닝 가능.

기본 임계: `blank_char=10`, `blank_image_cov=0.02`, `ocr_image_cov=0.25`, `ocr_text_max=30`, `simple_table_max=5`, `dense_block=30`, `vector_min=40`(순서도/차트 근사 — 표 경계선 수준을 넘는 벡터 드로잉 개수), `llm_complexity_score=2`.

1. **SKIP** — `char_count < blank_char AND image_coverage < blank_image_cov` → 빈 페이지(출력 제외).
2. **OCR_NEEDED** — 스캔형(`image_coverage >= ocr_image_cov AND char_count < ocr_text_max`) **이면서 단순**: `table_count <= simple_table_max` AND **not** 복잡신호(아래 LLM 신호 없음). 스캔+텍스트+간단한 표(≤5).
3. **LLM_NEEDED** — 복잡/구조/혼합. 다음 중 하나라도:
   - `table_count > simple_table_max` (표 많음)
   - `has_forms`
   - `image_count > 0 AND has_native_text` (혼합 콘텐츠)
   - `drawing_count >= vector_min` (표 경계선 수준 초과 벡터 드로잉) → **순서도/차트 근사**
   - `block_count > dense_block` (밀집 레이아웃)
   - `text_coverage < 0.10 AND char_count > 50` (양식형 성긴 배치)
   - 복잡도 점수 `>= llm_complexity_score(기본 2)` 로 종합.
4. **TEXT_ONLY** — 그 외(native 텍스트 추출 가능한 일반 본문) → 기존 확장자별 parser(ODL).

**OCR↔LLM 경계 요지**(사용자 기준): 스캔이어도 *단순(텍스트+간단한 표 ≤5)*이면 OCR, *순서도·차트·혼합·표>5·고복잡도*면 LLM(VL). ※ 순서도/차트 정밀판별은 어려워 **벡터 드로잉 밀도** 휴리스틱으로 근사하며, 실문서로 임계 튜닝 전제.

## 라우팅 / 핸들러 (`parse()` 통합)

`parse(file_bytes, filename, *, ocr_url)`:

1. `report = triage_document(file_bytes)` — 페이지별 버킷.
2. `md_texts = _page_markdowns(file_bytes, filename)` — ODL(text 페이지용). ODL 실패(ToolError)면 기존대로 `ParserError`.
3. 페이지별 route:
   - **text** → `hybrid_to_blocks(md_texts[i], page_idx=n)` (기존 ODL 경로)
   - **ocr** → `_ocr_page(jpeg)` **seam** = 현재 **VL fallback**(render(fitz)→`ocr_elements_sync`→`elements_to_blocks`). 후일 `PARSE_OCR_ENGINE=tesseract` 로 로컬 OCR 교체.
   - **llm** → render(fitz)→`ocr_elements_sync`(VL)→`elements_to_blocks` (기존 scanned 경로)
   - **skip** → 페이지 제외(빈 blocks; 또는 pages 에서 누락)
4. 각 페이지 dict 에 `route`, `triage_reason`, 주요 signals 를 메타로 태깅(관측/비용추적, 소비자 비파괴 additive).
5. `RouteResult(kind="pages", chunk_needed=True, pages=pages)` (기존과 동일 계약).

기존 `_digital_text_len`/`_DIGITAL_MIN_CHARS` 는 triage 로 대체·제거.

## 데이터 흐름

```
file_bytes ─┬─ triage_document(fitz)  → [PageSignals(bucket, reason, signals)]
            └─ _page_markdowns(ODL)   → [md per page]
                     │
        per page ────┤ route=text → ODL md → hybrid_to_blocks
                     │ route=ocr  → render(fitz) → VL(seam) → elements_to_blocks
                     │ route=llm  → render(fitz) → VL → elements_to_blocks
                     │ route=skip → []
                     ▼
        pages[] (+route/signals meta) → RouteResult(kind="pages")
```

## 에러 처리

- **fitz open 실패**(비-PDF/손상): triage 가 빈 리포트 반환 → `parse()` 는 **기존 동작으로 폴백**(전 페이지 ODL md 기반, 텍스트 있으면 text 처리). triage 실패가 파싱을 죽이지 않음.
- **페이지 단위 처리 실패**(VL/OCR 예외): 기존과 동일하게 per-page 비치명(`blocks=[]` + 로그).
- **find_tables()/get_drawings() 예외**: 해당 신호를 보수적 기본값(False/0)으로 두고 계속(triage 견고성).

## 테스트

- **단위** `test_triage.py`: 합성 `PageSignals` → `classify` 버킷 검증.
  - 디지털 본문(char↑, table 0) → TEXT_ONLY
  - 스캔+간단한 표 3개(image_cov↑, char 소, table 3) → OCR_NEEDED
  - 스캔+표 8개 → LLM_NEEDED
  - 순서도(벡터 드로잉 다수, 표 0, 텍스트 소) → LLM_NEEDED
  - 혼합(이미지+native 텍스트) → LLM_NEEDED
  - 빈 페이지(char<10, image_cov<0.02) → SKIP
- **통합** `test_parser_pdf.py`(확장): 실제 신탁 PDF(3p) → 표 페이지 LLM_NEEDED → VL 로 내용 채워짐(회귀). 기존 `_digital_text_len` 테스트는 triage 로 치환.
- extract_signals 는 fitz 실호출이라 소형 합성 PDF(테스트 픽스처)로 스모크.

## 비범위 / 향후

- **로컬 OCR 엔진(Tesseract kor 등) 실제 연결**은 후속. 지금은 OCR_NEEDED → VL fallback(seam 만 마련).
- 임계값 정밀 튜닝(순서도/차트 판별 등)은 실문서 코퍼스로 후속 조정.
- 비-PDF(엑셀/docx/pptx) 라우팅은 기존대로(본 triage 는 PDF 도메인 파서 내부 한정).

## 라이선스 노트

PyMuPDF(AGPL-3.0)는 **책임자 승인 아래** 사용. 본 설계는 fitz 를 triage 신호·렌더에 사용한다. OpenDataLoader(Apache-2.0)·VL(외부 API)는 별개. 최종 납품은 고객사 OSS/법무 기준을 따른다.
