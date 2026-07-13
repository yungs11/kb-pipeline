# MinerU PDF 레인 통합 — 설계(Design Spec)

> parse-svc PDF 파서에 MinerU 를 붙여, **문서수준 게이트**로 텍스트 PDF 는 기존 OpenDataLoader(ODL) 레인,
> 스캔/혼합/복잡 PDF 는 MinerU(hybrid, VLM 원격 + PaddleOCR 로컬) 레인으로 분기한다.

- 상태: DRAFT (v1)
- 작성일: 2026-07-13
- 관련 코드(권위 출처): `parse_service/parsers/pdf/__init__.py`, `parse_service/parsers/__init__.py`(RouteResult),
  `parse_service/pdf_pages.py`, `kb_pipeline/blockify.py`, `parse_service/parsers/ocr.py`
- 참조 설계: `docs/superpowers/specs/2026-07-08-pdf-triage-design.md`(native_text 신호 로직 재사용)
- 검증 스파이크: MinerU `hybrid_analyze.py`/`pdf_classify.py`/`common.py` 직독 결과(본문 §2, §3)

---

## 1. 목표와 동기

### 1.1 목표
- 스캔·혼합·복잡 PDF 의 **표/레이아웃 추출 품질**을 MinerU hybrid 로 끌어올린다.
  (현행: 스캔 페이지 = in-process VL(qwen3-vl) 단독 → 표 구조/reading-order 약함. 2026-07-07 "스캔 빈 표" 버그의 근본 대응.)
- 순수 텍스트 PDF 는 **기존 ODL 레인 그대로**(더 싸고 빠름, 원격 VLM 호출 없음).
- 라우팅은 **문서수준 1회 판정**(사용자 확정: "문서수준분기가 좋음").

### 1.2 비목표(Out of scope)
- ODL 레인 내부 로직 변경(현행 유지). Excel/docx/pptx/이미지 파서 무관.
- MinerU VLM 서버(원격 GPU) 구축 자체 — 별도 서비스로 이미 존재/제공된다고 가정(엔드포인트만 소비).
- 청킹/적재/검색 파이프라인 변경. 본 설계는 **parse-svc 산출물(RouteResult pages/blocks)** 까지만.

---

## 2. 검증된 사실 (스파이크 결과 — 설계 전제)

`hybrid-http-client` 백엔드를 `do_parse` 로 태웠을 때의 내부 동작을 소스로 확인했다.

1. **문서수준 게이트 bool** — `hybrid_analyze.py:912` `_ocr_enable = ocr_classify(pdf_bytes, parse_method)`.
   `ocr_classify`(line 140–148): `parse_method=='auto'` 면 `pdf_classify.classify(pdf_bytes)=='ocr'` 일 때 True,
   `parse_method=='ocr'` 면 강제 True, `'txt'` 면 False. **문서당 1개 bool**(페이지별 아님). → 사용자의 "문서수준분기" 이해 확인됨.

2. **VLM 은 항상 주 추출자, PaddleOCR 은 layout(bbox+type) 담당** — `batch_extract_with_layout(..., not_extract_list=None if _ocr_enable else not_extract_list)`
   (line 983–992). `not_extract_list`(line 73) = VLM 이 **추출을 건너뛸 네이티브-텍스트 블록 타입**.
   - `_ocr_enable=True`(스캔): `not_extract_list=None` → **VLM 이 모든 블록**(텍스트+표+그림) 추출 + PaddleOCR OCR-det 보충(`_apply_vlm_ocr_det_sidecars_for_window`, line 989).
   - `_ocr_enable=False`(디지털): VLM 은 **표/그림/수식 블록만** 추출, 텍스트는 PDF 네이티브 사용.
   주석 근거(line 671–673): `OCR 模式下文本由 VLM 抽取，pipeline 侧只需要 layout`.

3. **layout = bbox + type(+reading order)**, 내용 아님. PaddleOCR(로컬)이 "지도"(각 영역의 위치·종류·순서)를 만들고,
   VLM(원격)이 그 영역의 실제 내용을 채운다.

**설계 함의(정정 — ultracode 검증 반영)**: `_ocr_enable` 은 **문서당 1개 bool**이고 이게 `not_extract_list`(VLM 이 스킵할 텍스트 블록)를
문서 전체에 적용한다. 따라서 **스캔 페이지(네이티브 텍스트 없음)가 하나라도 있으면 `parse_method='ocr'` 강제**해야 한다 —
`'auto'` 로 두면 텍스트 다수 문서를 MinerU 문서수준 classify 가 `'txt'` 로 판정 → `_ocr_enable=False` →
그 스캔 페이지의 텍스트 블록을 VLM 이 스킵하고 네이티브 텍스트(없음)로 채움 → **스캔 텍스트 유실**(= 이 기능이 잡으려던 2026-07-07 버그 재발).
`'auto'` 는 **스캔 페이지가 전혀 없는 경우에만**(네이티브 텍스트 + 래스터 이미지만 = `LLM_NEEDED` 페이지들) 안전 —
모든 페이지가 네이티브 텍스트를 보유하므로 유실 없이 VLM 호출만 이미지 블록으로 최소화된다. (§4.3)

---

## 3. 아키텍처 — 문서수준 게이트 + 2 레인

```
POST /parse (pdf)
   │
   ▼
[게이트] triage_document(pdf_bytes)  ← PyMuPDF 저비용 신호(§3.1), 부수효과 없음
   │   페이지별 Bucket 집계
   ├── 모든 비어있지 않은 페이지가 TEXT_ONLY ───────────► [ODL 레인] 기존 __init__.parse (변경 없음)
   │
   └── 하나라도 OCR_NEEDED 또는 LLM_NEEDED ────────────► [MinerU 레인] (§4)
                                                            VLM=원격 GPU, PaddleOCR=로컬
   │
   ▼
RouteResult(kind="pages", chunk_needed=True, pages=[{page_number, blocks}])   ← 두 레인 동일 계약
```

### 3.1 게이트 로직 (triage native_text 재사용 — 사용자 확정)

`feat/pdf-triage` 의 `parse_service/parsers/pdf/triage.py` 를 **본 브랜치로 가져와** 게이트로 쓴다.
`triage_document(pdf_bytes)` → `list[PageSignals]`(페이지별 `Bucket`). 판정만 하고 부수효과 없음.
비싼 신호(`get_drawings`/`find_tables`) 미사용 — content-stream 크기로 빈페이지 판별.

버킷 정의(triage.py `classify`):
- `TEXT_ONLY` — native text 있음(char>20) & 큰 래스터 이미지 없음 → ODL 로 충분
- `LLM_NEEDED` — native text 있음 + 래스터 이미지 coverage ≥ 0.25 (혼합 페이지)
- `OCR_NEEDED` — native text 없음 + 내용 있음(이미지 or content-stream>300B; 스캔·아웃라인·벡터표)
- `SKIP` — 진짜 빈 페이지

**문서수준 집계 규칙**(사용자 확정 "혼합이면 문서 전체 MinerU"):

```
buckets = {sig.bucket for sig in triage_document(pdf_bytes) if sig.bucket != Bucket.SKIP}
if not buckets:                       # 전부 빈 페이지(또는 열기 실패 폴백)
    → ODL 레인 (기존 동작, best-effort)
elif buckets == {Bucket.TEXT_ONLY}:   # 비어있지 않은 페이지가 전부 순수 텍스트
    → ODL 레인
else:                                  # OCR_NEEDED 또는 LLM_NEEDED 하나라도 포함
    → MinerU 레인
```

- 게이트 판정에 쓴 신호(`has_native_text` 집계)로 **MinerU parse_method 도 결정**(§4.3):
  `OCR_NEEDED`만 있고 `TEXT_ONLY`/`LLM_NEEDED` 없음 = **순수 스캔** → `'ocr'` 강제;
  그 외(네이티브 텍스트 페이지가 섞임) = **혼합** → `'auto'`.
- `triage_document` 가 `[]`(PDF 열기 실패) → 폴백으로 ODL 레인(기존 동작 보존).

---

## 4. MinerU 레인

### 4.1 백엔드 선택
- `hybrid-http-client`: **PaddleOCR(PP-OCR) 로컬** + **VLM 원격**(`server_url`).
  사용자 확정: "paddle=PP-OCR 로컬(이 서버에 존재), VLM 만 GPU 서버로 호출".
- pipeline(로컬 전량)·vlm-*(VLM 전량) 은 비채택 — 각각 품질/원격의존 트레이드오프가 목표와 안 맞음.

### 4.2 in-process 호출
- parse-svc `.venv-kb` 에 MinerU 를 **라이브러리로 import** 해 in-process 호출(별도 HTTP 서비스 안 띄움 — 기존
  "Phase 2 파서 일원화 = in-process" 원칙 유지).
- 진입점: MinerU `do_parse`(파일/배치 기반) 대신 가능하면 lower-level(`aio_doc_analyze`/`doc_analyze`) 직호출로
  bytes→결과를 얻는다. `do_parse` 만 실용적이면 **스크래치패드 임시파일**(session scratchpad)에 bytes 를 써서 호출하고
  즉시 정리(부수효과 격리). *정확한 in-process 진입 시그니처는 구현 플랜 Task 에서 MinerU 소스로 확정.*

### 4.3 parse_method (문서수준, §3.1 집계 결과로 결정 — 정정됨)
| 문서 유형(게이트 버킷) | parse_method | 이유 |
|---|---|---|
| 스캔 페이지 존재(`OCR_NEEDED` 하나라도 포함) | **`'ocr'` 강제** | 스캔 페이지엔 네이티브 텍스트가 없다. `'ocr'` → `_ocr_enable=True` → VLM 전량추출 + PaddleOCR det 로 그 텍스트를 읽는다. `'auto'` 로 두면 문서수준 classify 가 'txt' 판정 시 스캔 텍스트 유실(§2 함의). |
| 스캔 없음 + 혼합(`LLM_NEEDED` 만, `OCR_NEEDED` 없음) | `'auto'` | 모든 페이지가 네이티브 텍스트 보유 → MinerU 가 텍스트=네이티브, 이미지=VLM 으로 처리해 **원격 VLM 호출 최소화**(유실 위험 없음). |

> **정정 근거(ultracode 검증)**: 초안은 "혼합=auto"였으나, `_ocr_enable` 이 문서수준 단일 bool 이라 `{TEXT_ONLY + OCR_NEEDED}`(텍스트 다수 + 스캔 소수) 문서를
> 'auto' 로 보내면 classify='txt' → 스캔 페이지 텍스트가 유실됨(2렌즈 독립 지적, spec §2 와 모순). 그래서 **스캔 페이지가 하나라도 있으면 'ocr' 강제**로 바꾼다.
> 비용 최소화 목적의 'auto' 는 **스캔이 전혀 없는 텍스트+이미지 혼합**에만 안전하게 유지.
> 순수 스캔(`{OCR_NEEDED}` 단독)도 'ocr' 로 가므로 MinerU 재-classify(pdfium 패스)까지 생략된다.

### 4.4 VLM 원격 엔드포인트 설정 (확정)
- **MinerU 전용 VLM 서버가 별도로 존재**(사용자 확정). 기존 in-process VL 의 `MODEL_API_URL` 을 재사용하지 않고
  **신규 env `MINERU_VLM_SERVER_URL`**(+필요 시 `MINERU_VLM_API_KEY`)로 MinerU `server_url` 에 주입한다.
  gitignored `scripts/parse-svc.env`(런처 `scripts/run-parse-svc.sh` 가 실제 로드하고 `.gitignore` `scripts/*.env` 로 무시됨)에 둔다(비밀은 커밋 금지).
- PP-OCR 모델 경로/버전(PP-OCRv5) env 로 명시(서버에 존재 가정).

---

## 5. 출력 매핑 (MinerU → RouteResult)

MinerU 산출 `content_list.json`(블록 리스트: `type`, `page_idx`, `bbox`, 그리고 타입별 `text`/`table_body`(HTML)/`img_path`/`text`(equation)) 을
parse-svc **blocks** 로 변환한다. 두 레인은 동일한 `RouteResult(kind="pages", chunk_needed=True, pages=[{page_number, blocks}])` 을 반환.

매핑 규칙(불변식 준수):
- `type=='text'|'title'` → text 블록(`page_idx` → `page_number = page_idx + 1` 정규화, `_workspace/01-architecture.md:80`).
- `type=='table'` → **`table_body` 의 `<table>` HTML 그대로 보존**(pipe 평탄화 금지 — 불변식). blockify 표 경로에 태운다.
- `type=='image'` → image 블록(필요 시 모달 마커 U+3008/U+3009 — W1 Rust 소비자 byte-identical, blockify 담당).
- `type=='equation'` → 기존 blockify 규칙에 맞춰 text/모달 처리.
- reading order 는 content_list 순서 유지.
- *정확한 content_list 필드명/타입 enum 은 구현 Task 에서 MinerU 소스(`content_list` 생성부)로 대조 확정.*

**단일 청크 우주 불변식**: MinerU 레인도 `blocks` 만 만들고 청킹은 facade `/chunk` 가 소유(`chunk_needed=True`). 별도 청크 생성 금지.

---

## 6. 폴백 / 에러 처리

- `triage_document([])`(PDF 열기 실패) → ODL 레인(기존 동작).
- **게이트 자체 실패(pymupdf 부재/triage 페이지 반복 중 예외 — 암호화·손상 PDF)** → ODL 레인 폴백. 게이트 호출은 반드시
  try/except 로 감싸 새 500 을 만들지 않는다(triage 는 `pymupdf.open` 만 try/except, 페이지 반복은 try/finally 라 반복 중 예외가 전파됨).
- MinerU 호출 실패(원격 VLM 다운/타임아웃/import 실패) **또는 성공했으나 빈 결과(blocks 전무)** → **로그 + 폴백**:
  1차 폴백 = 기존 in-process VL 경로(현행 스캔 처리)로 문서 처리. VL 도 실패하는 페이지는 빈 blocks(현행처럼 비치명).
  → MinerU 도입이 **가용성 회귀를 만들지 않는다**(스캔 PDF 가 최소 현행 수준은 보장).
- 페이지 단위 부분 실패는 비치명(빈 blocks) — 현행 계약 유지.

---

## 7. 불변식 체크 (CLAUDE.md)

- ✅ 청크는 KB당 단일 우주 — MinerU 레인은 blocks 만, 청킹은 facade `/chunk`.
- ✅ 표 `<table>` HTML 보존 — content_list `table_body` 원형 유지.
- ✅ 모달 마커 U+3008/U+3009 — blockify 경유(변경 없음).
- ✅ BGE-M3 1024d — 파싱 구간 무관(임베딩은 하류).
- ✅ page_idx 1-based canonical — MinerU 0-based `page_idx` → `+1` 정규화.
- ✅ in-process 일원화 — MinerU 라이브러리 import(외부 HTTP 서비스 신설 안 함). 단 VLM 은 원격(설계 의도).

---

## 8. 테스트 전략

1. **게이트 단위 테스트**(triage 재사용) — 순수텍스트/순수스캔/혼합/빈문서 4종 fixture 로 레인 라우팅 + parse_method 결정 검증.
   MinerU 는 fake(monkeypatch)로 호출 인자(parse_method/server_url)만 검증.
2. **출력 매핑 테스트** — 대표 content_list.json(text/title/table/image/equation) → blocks 변환:
   표 `<table>` 보존, page_number 정규화, reading order.
3. **폴백 테스트** — MinerU import/호출 예외 시 in-process VL 폴백 경로 진입.
4. **회귀** — 기존 `parse_service/tests/test_parser_pdf.py`(디지털/스캔/태그무시) 그린 유지.
   기존 순수텍스트 PDF 는 여전히 ODL 레인(라우팅 불변) 확인.
5. **스택검증**(수동, 실환경) — 실제 스캔 PDF 1건: MinerU 레인 → 표 구조 비어있지 않게 추출(2026-07-07 버그 재발 없음).

---

## 9. 전제조건 / 리스크

- **[리스크·차단후보] MinerU+torch+PaddleOCR 설치 가능성** — 현재 dev = Intel Mac(GPU/CUDA/torch 로컬 검증 한계).
  parse-svc **배포 서버**에 PP-OCRv5 + MinerU 런타임이 실제 설치·구동 가능한지 **구현 착수 전 별도 확인 필요**.
  로컬에서는 게이트/매핑/폴백을 fake MinerU 로 단위검증하고, 실 MinerU 경로는 배포서버 스택검증으로 분리.
- **[리스크] content_list.json 스키마 드리프트** — MinerU 버전에 필드명/enum 이 바뀔 수 있음 → 버전 핀 + 매핑 Task 에서 소스 대조.
- **[리스크] 원격 VLM 지연/비용** — 혼합 문서 `'auto'` 로 완화하되, 대형 스캔 문서는 여전히 느릴 수 있음(현행 파서도 표당 20–40s). 모니터링 대상.

---

## 10. 열린 결정 (구현 플랜에서 확정)

1. MinerU in-process 진입점(`do_parse` 임시파일 vs `doc_analyze` bytes 직호출) — 소스 확인 후.
2. content_list.json 정확한 필드/enum 매핑.
3. MinerU/torch/paddle 의존성 설치 방식과 `.venv-kb` 반영(배포서버 전제조건 확인 후).
