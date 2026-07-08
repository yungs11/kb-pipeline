<!-- plan-version: v3 -->
<!-- ultracode-validation: READY v3 at 2026-07-08T02:29:53Z (4-lens 경쟁검증 3라운드: 4→3(+minors)→0 must-fix. codebase-grounding/logic/completeness/adversarial 전부 READY, 종합 READY) -->

# PDF Triage (PyMuPDF 페이지 분류) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** parse-svc PDF 파서에 PyMuPDF 기반 페이지 triage 프리패스를 추가해, 페이지별로 TEXT_ONLY(ODL)/OCR_NEEDED(VL fallback)/LLM_NEEDED(VL)/SKIP 로 라우팅한다.

**Architecture:** 순수 분류 모듈 `parsers/pdf/triage.py`(fitz 신호 추출 + 하드트리거 분류)를 신설하고, `parsers/pdf/__init__.py::parse` 가 그 결과로 페이지별 핸들러(ODL md / render+VL / skip)로 분기한다. triage 불가(비-PDF/손상) 시 기존 `_digital_text_len` 폴백.

**Tech Stack:** Python 3.14, PyMuPDF(`fitz`, 이미 의존성), pytest. 기존 `kb_pipeline.blockify`(hybrid_to_blocks/elements_to_blocks), parse_service ODL/VL 자산 재사용.

## Global Constraints

- venv/실행: `.venv-kb/bin/python` (parse-svc 전용). 테스트는 `.venv-kb/bin/python -m pytest`.
- PyMuPDF import: 신규 triage 모듈은 `import pymupdf` 사용(기존 코드는 `import fitz`). 둘은 별개 모듈 객체지만 동일 PyMuPDF 배포로 API 동일(둘 다 import 성공).
- triage 모듈은 **부수효과 0**(렌더/OCR/VL/HTTP 호출 금지) — 판정만. 실제 처리는 `parse()`.
- 기존 계약 유지: `parse()` 는 `RouteResult(kind="pages", chunk_needed=True, pages=[...])` 반환. `pages[i]` 는 최소 `{"page_number": int, "blocks": list}` 를 포함(추가 키는 additive 허용).
- 임계값은 `classify()` 키워드 인자(모듈 상수 기본값)로만 — 하드코딩 분기 금지.
- 라이선스: PyMuPDF(AGPL) 책임자 승인 사용. 설계: `docs/superpowers/specs/2026-07-08-pdf-triage-design.md`.

---

### Task 1: 분류 로직 (Bucket / PageSignals / classify)

fitz 없이 순수 분류. 합성 `PageSignals` 로 단위 테스트.

**Files:**
- Create: `parse_service/parsers/pdf/triage.py`
- Test: `parse_service/tests/test_triage.py`

**Interfaces:**
- Produces:
  - `class Bucket(Enum)` — `SKIP, TEXT_ONLY, OCR_NEEDED, LLM_NEEDED`
  - `@dataclass PageSignals` — 필드(아래 코드), `bucket: Bucket|None`, `reason: str`
  - `def classify(sig: PageSignals, *, blank_char=10, ocr_image_cov=0.25, ocr_text_max=30, simple_table_max=5, vector_min=40, mixed_image_cov=0.25) -> PageSignals` — `sig.bucket`/`sig.reason` 기입 후 sig 반환.

- [ ] **Step 1: 실패 테스트 작성** — `parse_service/tests/test_triage.py`

```python
"""triage 분류 규칙 — 합성 PageSignals 로 버킷 검증."""
from parse_service.parsers.pdf.triage import PageSignals, Bucket, classify


def _sig(**kw) -> PageSignals:
    s = PageSignals(page_number=0, width=595.0, height=842.0)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def test_blank_page_skip():
    assert classify(_sig(char_count=2, image_coverage=0.0)).bucket is Bucket.SKIP


def test_digital_text_only():
    s = classify(_sig(char_count=1362, word_count=93, has_native_text=True,
                      text_coverage=0.34, image_coverage=0.0))
    assert s.bucket is Bucket.TEXT_ONLY


def test_scanned_simple_ocr():
    # 스캔(이미지 지배 + 글자 극소) + 단순(표 3개) → OCR
    s = classify(_sig(char_count=14, image_coverage=1.0, has_tables=True, table_count=3))
    assert s.bucket is Bucket.OCR_NEEDED


def test_many_tables_llm():
    # 표 >5 → LLM (디지털이어도)
    s = classify(_sig(char_count=1000, has_native_text=True, has_tables=True,
                      table_count=8, image_coverage=0.0))
    assert s.bucket is Bucket.LLM_NEEDED


def test_flowchart_vectors_llm():
    # 순수 벡터 순서도/차트(raster image 없음 → image_coverage=0, native 텍스트 없음) → LLM
    s = classify(_sig(char_count=5, image_coverage=0.0, has_native_text=False,
                      vector_drawing=True, drawing_count=120))
    assert s.bucket is Bucket.LLM_NEEDED


def test_vector_flowchart_not_skipped():
    # 텍스트 극소 + 이미지 0 이지만 벡터 다수 → SKIP 아님 → LLM (v2 SKIP 가드 회귀)
    s = classify(_sig(char_count=3, image_coverage=0.0, drawing_count=90, vector_drawing=True))
    assert s.bucket is Bucket.LLM_NEEDED


def test_digital_table_with_vector_borders_stays_text():
    # 디지털 표(경계선=벡터 다수)지만 native 텍스트 많음 → 벡터 트리거 미발동 → TEXT_ONLY
    s = classify(_sig(char_count=800, word_count=120, has_native_text=True, text_coverage=0.3,
                      drawing_count=200, vector_drawing=True, has_tables=True, table_count=2,
                      image_coverage=0.0))
    assert s.bucket is Bucket.TEXT_ONLY


def test_mixed_content_llm():
    # 이미지 비중 + native 텍스트 혼합 → LLM
    s = classify(_sig(char_count=500, has_native_text=True, image_count=2, image_coverage=0.4))
    assert s.bucket is Bucket.LLM_NEEDED


def test_form_widgets_llm():
    s = classify(_sig(char_count=80, has_native_text=True, has_forms=True, image_coverage=0.0))
    assert s.bucket is Bucket.LLM_NEEDED
```

- [ ] **Step 2: 실패 확인**

Run: `.venv-kb/bin/python -m pytest parse_service/tests/test_triage.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'parse_service.parsers.pdf.triage'`

- [ ] **Step 3: 최소 구현** — `parse_service/parsers/pdf/triage.py`

```python
"""PyMuPDF 페이지 분류(Triage) — OCR/VL 비용 전 저비용 시그널로 페이지별 처리경로 결정.

설계: docs/superpowers/specs/2026-07-08-pdf-triage-design.md
버킷: SKIP(빈 페이지) / TEXT_ONLY(디지털→ODL) / OCR_NEEDED(스캔·단순→VL fallback) /
      LLM_NEEDED(순서도·차트·혼합·표>5·양식→VL). 부수효과 없음(판정만).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

log = logging.getLogger("kb_pipeline.parse_service.parsers.pdf.triage")


class Bucket(Enum):
    SKIP = auto()
    TEXT_ONLY = auto()
    OCR_NEEDED = auto()
    LLM_NEEDED = auto()


@dataclass
class PageSignals:
    page_number: int
    width: float
    height: float
    # text
    char_count: int = 0
    word_count: int = 0
    has_native_text: bool = False
    text_coverage: float = 0.0
    # images
    image_count: int = 0
    image_coverage: float = 0.0
    # structure
    has_tables: bool = False
    table_count: int = 0
    has_forms: bool = False
    block_count: int = 0
    vector_drawing: bool = False
    drawing_count: int = 0
    # derived
    bucket: Optional[Bucket] = field(default=None, init=False)
    reason: str = field(default="", init=False)


def classify(
    sig: PageSignals,
    *,
    blank_char: int = 10,
    ocr_image_cov: float = 0.25,
    ocr_text_max: int = 30,
    simple_table_max: int = 5,
    vector_min: int = 40,
    mixed_image_cov: float = 0.25,
) -> PageSignals:
    """우선순위 SKIP → LLM_NEEDED → OCR_NEEDED → TEXT_ONLY. 첫 매치에서 확정."""
    chars = sig.char_count
    imgcov = sig.image_coverage

    # 1) SKIP — 진짜 빈 페이지(텍스트/이미지/벡터/양식 모두 없음).
    #    ⚠️ 아래 3개 배제조건 필수(v2/v3): 이 조건들이 없으면 SKIP 이 너무 공격적이라
    #    - 순수 벡터 순서도(image_count 0 이지만 drawing_count↑) → 벡터 LLM 트리거를 못 탐
    #    - 이미지(스캔) 페이지(char 0 이지만 image_count>0) → OCR 못 타고 드롭(콘텐츠 유실)
    #    - 양식 페이지(char 0 이지만 has_forms) → 양식 LLM 트리거를 못 탐(드롭)
    #    ⇒ 이런 페이지는 SKIP 하지 않고 아래 규칙으로 넘긴다.
    if (chars < blank_char and sig.image_count == 0
            and sig.drawing_count < vector_min and not sig.has_forms):
        sig.bucket = Bucket.SKIP
        sig.reason = f"빈 페이지 (글자={chars}, 이미지={sig.image_count}, 벡터={sig.drawing_count})"
        return sig

    # 2) LLM_NEEDED — 하드 트리거(하나라도 참이면 즉시 VL)
    llm: list[str] = []
    if sig.table_count > simple_table_max:
        llm.append(f"표 {sig.table_count}개(>{simple_table_max})")
    # 벡터 다수 + native 텍스트 적음 = 순서도/차트(디지털 표의 경계선 오탐 방지: 표는 native
    # 텍스트가 많아 has_native_text=True → 여기 안 걸리고 ODL/표>5 규칙으로 처리).
    if sig.drawing_count >= vector_min and not sig.has_native_text:
        llm.append(f"벡터드로잉 {sig.drawing_count}(순서도/차트 근사)")
    if sig.has_forms:
        llm.append("양식(Form) 위젯")
    if sig.image_count > 0 and sig.has_native_text and imgcov >= mixed_image_cov:
        llm.append(f"혼합 콘텐츠(이미지={imgcov:.2f}+텍스트)")
    if llm:
        sig.bucket = Bucket.LLM_NEEDED
        sig.reason = "복잡 레이아웃: " + ", ".join(llm)
        return sig

    # 3) OCR_NEEDED — 스캔/이미지 중심 + 단순. `or image_count>0` 로 near-textless 페이지가
    #    작은 figure(이미지 비율 0.02~0.25)를 달고 TEXT_ONLY(빈 ODL)로 새는 gap-band 방지(v3).
    if (imgcov >= ocr_image_cov or sig.image_count > 0) and chars < ocr_text_max:
        sig.bucket = Bucket.OCR_NEEDED
        sig.reason = f"스캔/이미지 중심 단순 (이미지={imgcov:.2f}, 글자={chars}, 표={sig.table_count})"
        return sig

    # 4) TEXT_ONLY — 디지털 본문
    sig.bucket = Bucket.TEXT_ONLY
    sig.reason = (
        f"디지털 텍스트 (글자={chars}, 단어={sig.word_count}, 텍스트비율={sig.text_coverage:.2f})"
    )
    return sig
```

- [ ] **Step 4: 통과 확인**

Run: `.venv-kb/bin/python -m pytest parse_service/tests/test_triage.py -q`
Expected: PASS (9 passed)

- [ ] **Step 5: 커밋**

```bash
git add parse_service/parsers/pdf/triage.py parse_service/tests/test_triage.py
git commit -m "feat(parse-svc): PDF triage 분류 로직(Bucket/PageSignals/classify)"
```

---

### Task 2: fitz 신호 추출 + 문서 분류 (extract_signals / triage_document)

fitz 로 실제 PDF 페이지에서 신호를 뽑고 문서 전체를 분류. 손상/비-PDF 는 빈 리스트.

**Files:**
- Modify: `parse_service/parsers/pdf/triage.py` (extract_signals, triage_document 추가)
- Test: `parse_service/tests/test_triage.py` (fitz 스모크 추가)

**Interfaces:**
- Consumes: Task 1 의 `PageSignals`, `classify`, `Bucket`
- Produces:
  - `def extract_signals(page) -> PageSignals` — 단일 fitz Page → 신호(부수효과 없음)
  - `def triage_document(pdf_bytes: bytes, **classify_kwargs) -> list[PageSignals]` — 페이지 순서대로 분류된 리스트. 열기 실패 시 `[]`.

- [ ] **Step 1: 실패 테스트 작성** — `parse_service/tests/test_triage.py` 하단에 추가

```python
def test_triage_document_digital_text_page():
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "This is a digital text page with enough words to classify.")
    data = doc.tobytes()
    doc.close()

    from parse_service.parsers.pdf.triage import triage_document, Bucket
    sigs = triage_document(data)
    assert len(sigs) == 1
    assert sigs[0].page_number == 1          # 1-based(page.number+1)
    assert sigs[0].has_native_text is True
    assert sigs[0].bucket is Bucket.TEXT_ONLY


def test_triage_document_bad_bytes_returns_empty():
    from parse_service.parsers.pdf.triage import triage_document
    assert triage_document(b"not a pdf at all") == []


def test_triage_document_real_form_widget_llm():
    """실제 form widget PDF → has_forms=True(widgets() 경로) → LLM_NEEDED.

    annots() 는 위젯을 못 잡으므로 이 테스트가 없으면 has_forms 는 dead code 로 남는다.
    """
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page()
    w = pymupdf.Widget()
    w.field_name = "f1"
    w.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
    w.rect = pymupdf.Rect(72, 72, 220, 96)
    page.add_widget(w)
    data = doc.tobytes()
    doc.close()

    from parse_service.parsers.pdf.triage import triage_document, Bucket
    sigs = triage_document(data)
    assert sigs[0].has_forms is True
    assert sigs[0].bucket is Bucket.LLM_NEEDED
```


- [ ] **Step 2: 실패 확인**

Run: `.venv-kb/bin/python -m pytest parse_service/tests/test_triage.py::test_triage_document_digital_text_page -q`
Expected: FAIL — `AttributeError: module 'parse_service.parsers.pdf.triage' has no attribute 'triage_document'`

- [ ] **Step 3: 구현** — `triage.py` 하단에 추가 (상단에 `import pymupdf` 추가)

파일 상단 import 에 추가:
```python
import pymupdf
```

함수 추가:
모듈 상단(상수 근처)에 추가:
```python
# 대용량 PDF 방어: 페이지 수가 이보다 크면 느린 신호(find_tables/get_drawings)를 생략(0으로)해
# triage 지연을 제한한다. 그런 문서는 텍스트/이미지 신호만으로 분류(표/벡터 트리거 미발동).
_HEAVY_SCAN_MAX_PAGES = 300
```

```python
def extract_signals(page: "pymupdf.Page", *, heavy_scan: bool = True) -> PageSignals:
    """단일 fitz 페이지 → 저비용 신호. 개별 신호 추출 실패는 보수적 기본값으로 무시.

    page_number 는 **1-based**(page.number 는 0-based 라 +1) — parse() 출력 및 다운스트림
    page_idx 와 일치시킨다. heavy_scan=False 면 find_tables/get_drawings 를 생략(대용량 방어).
    """
    rect = page.rect
    page_area = (rect.width * rect.height) or 1.0
    sig = PageSignals(page_number=page.number + 1, width=rect.width, height=rect.height)

    words = page.get_text("words")  # (x0,y0,x1,y1,word,block,line,word_no)
    sig.word_count = len(words)
    sig.char_count = sum(len(w[4]) for w in words)
    sig.has_native_text = sig.char_count > 20

    blocks = page.get_text("blocks")  # (x0,y0,x1,y1,text,block_no,block_type)
    sig.block_count = len(blocks)
    text_area = sum((b[2] - b[0]) * (b[3] - b[1]) for b in blocks if b[6] == 0)
    sig.text_coverage = min(text_area / page_area, 1.0)

    try:
        images = page.get_image_info(hashes=False, xrefs=False)
    except Exception:  # noqa: BLE001
        images = []
    sig.image_count = len(images)
    img_area = sum(
        (im["bbox"][2] - im["bbox"][0]) * (im["bbox"][3] - im["bbox"][1])
        for im in images if im.get("bbox")
    )
    sig.image_coverage = min(img_area / page_area, 1.0)

    # 양식 위젯: PyMuPDF 1.27 에서 form widget 은 page.annots() 로는 안 잡히고
    # page.widgets() 로만 접근된다(annots 사용은 dead code — v2 수정).
    try:
        for _w in (page.widgets() or []):
            sig.has_forms = True
            break
    except Exception:  # noqa: BLE001
        pass

    if heavy_scan:
        try:
            tabs = page.find_tables()
            sig.table_count = len(tabs.tables)
        except Exception:  # noqa: BLE001
            sig.table_count = 0
        try:
            sig.drawing_count = len(page.get_drawings())
        except Exception:  # noqa: BLE001
            sig.drawing_count = 0
    sig.has_tables = sig.table_count > 0
    sig.vector_drawing = sig.drawing_count > 0

    return sig


def triage_document(pdf_bytes: bytes, **classify_kwargs) -> list[PageSignals]:
    """PDF bytes → 페이지 순서대로 분류된 PageSignals 리스트. 열기 실패 시 []."""
    try:
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    except Exception:  # noqa: BLE001 — 비-PDF/손상: 폴백을 위해 빈 리스트
        log.warning("triage: PDF 열기 실패 — 폴백(빈 리포트)")
        return []
    out: list[PageSignals] = []
    try:
        heavy = len(doc) <= _HEAVY_SCAN_MAX_PAGES
        if not heavy:
            log.warning("triage: %d 페이지(>%d) — heavy 신호(find_tables/get_drawings) 생략",
                        len(doc), _HEAVY_SCAN_MAX_PAGES)
        for page in doc:
            sig = extract_signals(page, heavy_scan=heavy)
            classify(sig, **classify_kwargs)
            out.append(sig)
    finally:
        doc.close()
    return out
```

- [ ] **Step 4: 통과 확인**

Run: `.venv-kb/bin/python -m pytest parse_service/tests/test_triage.py -q`
Expected: PASS (12 passed)

- [ ] **Step 5: 커밋**

```bash
git add parse_service/parsers/pdf/triage.py parse_service/tests/test_triage.py
git commit -m "feat(parse-svc): triage 신호추출(extract_signals)+문서분류(triage_document)"
```

---

### Task 3: parse() 통합 — triage 라우팅 (+ 폴백)

`parse()` 가 triage 결과로 페이지별 route 분기. triage 불가 시 기존 `_digital_text_len` 폴백. `_digital_text_len` 은 폴백 헬퍼로 **유지**.

**Files:**
- Modify: `parse_service/parsers/pdf/__init__.py` (parse 본문 + `_route_for` 헬퍼)
- Test: `parse_service/tests/test_parser_pdf.py` (라우팅 테스트 추가; 기존 테스트는 폴백으로 그대로 통과)

**Interfaces:**
- Consumes: Task 2 의 `triage_document`, `Bucket` (from `parse_service.parsers.pdf.triage`)
- Produces: `parse(file_bytes, filename, *, ocr_url) -> RouteResult` — 각 `pages[i]` 에 `route`(str), `triage_reason`(str) 메타 추가(additive). 라우팅: text→ODL md, ocr/llm→render+VL, skip→제외.

- [ ] **Step 1: 실패 테스트 작성** — `parse_service/tests/test_parser_pdf.py` 에 추가

```python
def test_parse_routes_by_triage(monkeypatch):
    """triage 버킷대로 라우팅 — text=ODL, llm/ocr=VL, skip=제외."""
    from parse_service.parsers import pdf as pdf_parser
    from parse_service.parsers.pdf import triage as triage_mod

    # p1=text(ODL), p2=llm(VL), p3=ocr(VL), p4=skip
    monkeypatch.setattr(pdf_parser, "_page_markdowns",
                        lambda fb, fn: ["# 본문 텍스트", "표페이지", "스캔페이지", "빈페이지"])

    def fake_triage(fb, **kw):
        def mk(n, bucket):
            s = triage_mod.PageSignals(page_number=n, width=595.0, height=842.0)
            s.bucket = bucket
            s.reason = bucket.name
            return s
        return [
            mk(0, triage_mod.Bucket.TEXT_ONLY),
            mk(1, triage_mod.Bucket.LLM_NEEDED),
            mk(2, triage_mod.Bucket.OCR_NEEDED),
            mk(3, triage_mod.Bucket.SKIP),
        ]
    monkeypatch.setattr(pdf_parser, "triage_document", fake_triage)

    class FakeRP:
        def __init__(self, n):
            self.page_number, self.jpeg = n, b"jpeg"
    monkeypatch.setattr(pdf_parser, "_render_pages",
                        lambda fb: [FakeRP(2), FakeRP(3)])
    vl_calls = []
    def fake_ocr(jpeg, name, ocr_url):
        vl_calls.append(name)
        return [{"category": "text", "content": {"markdown": "vl 내용"}, "page": 0}]
    monkeypatch.setattr(pdf_parser, "_ocr_elements_for_page", fake_ocr)

    res = pdf_parser.parse(b"%PDF-realish", "a.pdf", ocr_url="http://ocr")
    routes = {p["page_number"]: p.get("route") for p in res.pages}
    # skip(p4)은 출력에서 제외
    assert set(routes) == {1, 2, 3}
    assert routes[1] == "text"      # ODL
    assert routes[2] == "llm"       # VL
    assert routes[3] == "ocr"       # VL(seam)
    # llm/ocr 두 페이지만 VL 호출
    assert len(vl_calls) == 2
    # text 페이지는 ODL 블록(page_idx 세팅)
    p1 = next(p for p in res.pages if p["page_number"] == 1)
    assert p1["blocks"] and p1["blocks"][0]["page_idx"] == 1


def test_parse_falls_back_when_triage_empty(monkeypatch):
    """triage 가 [] (비-PDF/손상)면 기존 _digital_text_len 폴백으로 동작."""
    from parse_service.parsers import pdf as pdf_parser
    monkeypatch.setattr(pdf_parser, "_page_markdowns", lambda fb, fn: ["# 디지털 텍스트", "   "])
    monkeypatch.setattr(pdf_parser, "triage_document", lambda fb, **kw: [])

    class FakeRP:
        page_number, jpeg = 2, b"jpeg"
    monkeypatch.setattr(pdf_parser, "_render_pages", lambda fb: [FakeRP()])
    monkeypatch.setattr(pdf_parser, "_ocr_elements_for_page",
                        lambda jpeg, name, ocr_url: [{"category": "text", "content": {"markdown": "ocr"}, "page": 0}])
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    # p1 텍스트 → ODL, p2 빈 → VL 폴백
    assert res.pages[0]["blocks"][0]["page_idx"] == 1
    assert res.pages[1]["blocks"], "빈 페이지는 폴백 VL 보충"
```

- [ ] **Step 2: 실패 확인**

Run: `.venv-kb/bin/python -m pytest parse_service/tests/test_parser_pdf.py::test_parse_routes_by_triage -q`
Expected: FAIL — `AttributeError: module 'parse_service.parsers.pdf' has no attribute 'triage_document'`

- [ ] **Step 3: 구현** — `parse_service/parsers/pdf/__init__.py`

파일 상단 import 섹션(기존 `from parse_service.tools.opendataloader import ...` 아래)에 추가:
```python
from parse_service.parsers.pdf.triage import triage_document, Bucket
```

`_BUCKET_ROUTE` 매핑과 `_route_for` 헬퍼를 `parse` 함수 위에 추가:
```python
_BUCKET_ROUTE = {
    Bucket.TEXT_ONLY: "text",
    Bucket.OCR_NEEDED: "ocr",
    Bucket.LLM_NEEDED: "llm",
    Bucket.SKIP: "skip",
}


def _route_for(sig, md: str) -> tuple[str, str]:
    """triage 신호 → (route, reason). 신호 없으면 _digital_text_len 폴백."""
    if sig is not None and sig.bucket is not None:
        return _BUCKET_ROUTE[sig.bucket], sig.reason
    # 폴백: 기존 digital 판정(실텍스트 있으면 ODL, 없으면 VL)
    if _digital_text_len(md) >= _DIGITAL_MIN_CHARS:
        return "text", "fallback:digital"
    return "llm", "fallback:scanned"


def _sig_meta(sig) -> dict:
    """페이지 dict 에 붙일 관측용 신호(비어있으면 {}). additive."""
    if sig is None:
        return {}
    return {"triage_signals": {
        "char_count": sig.char_count,
        "image_coverage": round(sig.image_coverage, 3),
        "table_count": sig.table_count,
        "drawing_count": sig.drawing_count,
        "has_forms": sig.has_forms,
    }}
```

`parse()` 본문을 아래로 교체(기존 `for i, md ...` 루프 전체):
```python
def parse(file_bytes: bytes, filename: str, *, ocr_url: str) -> RouteResult:
    from kb_pipeline.blockify import hybrid_to_blocks, elements_to_blocks
    try:
        md_texts = _page_markdowns(file_bytes, filename)
    except ToolError as e:
        raise ParserError(str(e)) from e

    signals = triage_document(file_bytes)  # [] 이면 폴백
    if signals and len(signals) != len(md_texts):
        # ODL(PAGE_SEP 분할)과 fitz 페이지수가 어긋나면 signals[i]/reason 이 md_texts[i] 와
        # 다른 페이지에 붙을 수 있음(비치명 — index 안전, 폴백). 관측을 위해 경고.
        log.warning("triage 페이지수(%d) != ODL 페이지수(%d) — 페이지 정렬 주의",
                    len(signals), len(md_texts))
    rendered = None
    pages: list[dict] = []
    for i, md in enumerate(md_texts):
        page_number = i + 1
        sig = signals[i] if i < len(signals) else None
        route, reason = _route_for(sig, md)

        if route == "skip":
            continue

        if route == "text":
            pages.append({
                "page_number": page_number,
                "blocks": hybrid_to_blocks(md, page_idx=page_number),
                "route": route, "triage_reason": reason, **_sig_meta(sig),
            })
            continue

        # ocr / llm → render + VL(seam; OCR 엔진 미정 → 현재 둘 다 VL)
        if rendered is None:
            rendered = _render_pages(file_bytes)
        page_jpeg = next((rp.jpeg for rp in rendered if rp.page_number == page_number), None)
        if page_jpeg is None:
            log.warning("triage %s page %d has no rendered image", route, page_number)
            pages.append({"page_number": page_number, "blocks": [],
                          "route": route, "triage_reason": reason, **_sig_meta(sig)})
            continue
        try:
            elements = _ocr_elements_for_page(page_jpeg, f"page-{page_number}.jpeg", ocr_url)
        except Exception:  # noqa: BLE001 — 페이지 단위 실패 비치명
            log.exception("VL/OCR failed for %s page %d", route, page_number)
            pages.append({"page_number": page_number, "blocks": [],
                          "route": route, "triage_reason": reason, **_sig_meta(sig)})
            continue
        blocks = elements_to_blocks(elements)
        for b in blocks:
            b["page_idx"] = page_number
        pages.append({"page_number": page_number, "blocks": blocks,
                      "route": route, "triage_reason": reason, **_sig_meta(sig)})
    return RouteResult(kind="pages", chunk_needed=True, pages=pages)
```

주의: `_digital_text_len`, `_DIGITAL_MIN_CHARS`, `_page_markdowns`, `_render_pages`, `_ocr_elements_for_page` 는 **그대로 유지**(폴백/핸들러에서 사용).

- [ ] **Step 4: 통과 확인 (신규 + 기존 회귀)**

Run: `.venv-kb/bin/python -m pytest parse_service/tests/test_parser_pdf.py parse_service/tests/test_triage.py -q`
Expected: PASS — 신규 라우팅 2개 + 기존 pdf 테스트(폴백으로) 전부 통과.

- [ ] **Step 5: 커밋**

```bash
git add parse_service/parsers/pdf/__init__.py parse_service/tests/test_parser_pdf.py
git commit -m "feat(parse-svc): parse() 를 triage 라우팅으로 통합(+_digital_text_len 폴백)"
```

---

### Task 4: 라이브 검증 + parse-svc 재기동

실제 문제 PDF(신탁…)로 end-to-end 확인 + 서비스 반영.

**Files:** (코드 변경 없음 — 검증/기동)

- [ ] **Step 1: 전체 parse-svc 테스트 스위트**

Run: `.venv-kb/bin/python -m pytest parse_service/tests/ -q`
Expected: PASS (기존 전부 + triage 신규).

- [ ] **Step 2: 실제 PDF triage 리포트 확인 (VL 호출 없이 분류만)**

Run:
```bash
.venv-kb/bin/python - <<'PY'
from parse_service.parsers.pdf.triage import triage_document
fb=open("test_doc/신탁업무처리지침2016-02호(우발비용_사전점검_지침)_15차개정20260515.pdf","rb").read()
for s in triage_document(fb):
    print(f"p{s.page_number} {s.bucket.name}: {s.reason}")
PY
```
Expected: 신탁 PDF 는 스캔본이라 페이지들이 **OCR_NEEDED**(이미지+저텍스트) 또는 순서도/차트면 **LLM_NEEDED** 로 분류(빈 페이지 없음, 전 페이지 드롭 없음). ※ **디지털** 표 페이지라면 TEXT_ONLY(ODL), **스캔** 표 페이지는 find_tables 가 raster 에서 표를 못 잡아 table_count=0 → OCR_NEEDED(VL) 로 감.

- [ ] **Step 3: parse-svc 재기동(코드 반영)**

Run: `bash scripts/run-parse-svc.sh`
Expected: `parse-svc launched ... healthz: {"status":"ok"...}` (docker-shadow 가드 포함).

- [ ] **Step 4: triage 지연 측정** (성능 확인)

Run:
```bash
.venv-kb/bin/python - <<'PY'
import time
from parse_service.parsers.pdf.triage import triage_document
fb=open("test_doc/신탁업무처리지침2016-02호(우발비용_사전점검_지침)_15차개정20260515.pdf","rb").read()
t=time.perf_counter(); triage_document(fb); print(f"triage {(time.perf_counter()-t)*1000:.0f} ms / {len(fb)} bytes")
PY
```
Expected: 소형 문서 수백 ms 이내. 심하게 느리면(초 단위) 대용량/벡터 문서에서 `_HEAVY_SCAN_MAX_PAGES` 하향 또는 per-page 가드 검토(비범위, 후속 튜닝).

- [ ] **Step 5: (선택) UI 재파싱**으로 표 채워짐 확인 — 사용자 확인 요청.

---

## v2→v3 수정 (ultracode 2차 검증 반영)

- **[blocking] SKIP 이 has_forms 페이지 드롭**: 양식 페이지(char 0·이미지 0·벡터 0·has_forms=True)가 has_forms LLM 트리거 전에 SKIP 됨(3렌즈 합의). SKIP 가드에 `image_count == 0 AND not has_forms` 추가 → 양식/이미지 페이지 배제. 이로써 `test_triage_document_real_form_widget_llm` 성립.
- **[fix] 스캔 페이지 SKIP 유실**: 이미지 있는 near-textless 페이지가 SKIP/TEXT_ONLY 로 새는 것 방지 — SKIP 에 `image_count==0` 추가 + OCR 조건에 `or image_count>0` 추가(gap-band 0.02~0.25 커버).
- **[fix] 페이지 정렬 관측**: `len(signals)!=len(md_texts)` 시 경고 로그(ODL/fitz 페이지수 divergence).
- **[note] 성능**: triage 가 페이지마다 find_tables/get_drawings 를 도는 비용 — page-count 가드(`_HEAVY_SCAN_MAX_PAGES`) + Task4 지연 측정으로 관리. per-page 시간가드는 비범위(후속).

## v1→v2 수정 (ultracode 경쟁검증 반영)

- **[blocking] SKIP 순서 오검**: 순수 벡터 순서도(image_coverage=0, 텍스트 극소)가 SKIP 으로 유실 → SKIP 가드에 `drawing_count < vector_min` 추가. 회귀 테스트 `test_vector_flowchart_not_skipped`.
- **[blocking] has_forms dead code**: PyMuPDF 1.27 은 `page.annots()` 로 위젯 미검출 → `page.widgets()` 로 변경. 실위젯 테스트 `test_triage_document_real_form_widget_llm` 추가.
- **[blocking] 플로우차트 테스트 오도**: `test_flowchart_vectors_llm` 를 image_coverage=0.0 로 현실화(가짜 0.3 제거).
- **[fix] 벡터 트리거 오탐**: 디지털 표 경계선(다수 벡터)이 LLM 으로 오라우팅 → 벡터 트리거에 `and not has_native_text` 추가. 테스트 `test_digital_table_with_vector_borders_stays_text`.
- **[fix] sparse-text 트리거 제거**: `text_coverage` 가 bbox 면적이라 정상 텍스트도 <0.10 → 대부분 텍스트 페이지가 VL 로 과잉 라우팅. 트리거 삭제(양식은 실위젯 detection 으로 대체).
- **[fix] page_number 1-based**: `page.number + 1` 로 통일(출력/다운스트림 일치).
- **[fix] 대용량 방어**: `_HEAVY_SCAN_MAX_PAGES=300` 초과 시 find_tables/get_drawings 생략(지연 제한).
- **[fix] 관측 메타**: 페이지 dict 에 `triage_signals`(char/image_cov/table/drawing/forms) 추가.
- **비용 주장 정정**: 현 단계 절감은 **TEXT_ONLY 페이지가 VL 미호출**하는 부분뿐. OCR/LLM 은 둘 다 VL(로컬 OCR 엔진 후속)이라, 기존 `_digital_text_len` 2분기 대비 표>5·혼합·순서도 페이지는 VL 호출이 **늘 수 있음**(품질 이득). 순수 비용절감은 로컬 OCR 연결 후.

## Self-Review

- **Spec coverage**: triage 모듈(Task1/2), 신호(Task2), 분류 규칙 4버킷(Task1), parse 통합·라우팅·폴백(Task3), 에러처리(fitz open 실패 폴백 Task2/3; 페이지 단위 비치명 Task3), 관측 메타(Task3), 테스트(Task1/2/3), 라이브검증(Task4) — 스펙 각 절 대응 확인.
- **Placeholder scan**: 모든 스텝에 실제 코드/명령/기대출력 포함. OCR 엔진(Tesseract) 실연결은 스펙상 **비범위**(seam=VL) — placeholder 아님(의도적 이연).
- **Type consistency**: `Bucket`/`PageSignals`/`classify`/`extract_signals`(heavy_scan)/`triage_document`/`_route_for`/`_sig_meta`/`_BUCKET_ROUTE` 이름·시그니처 Task 간 일치. `pages[i]` 키(`page_number`/`blocks`/`route`/`triage_reason`/`triage_signals`) 일치. `page_number` 1-based 통일.
