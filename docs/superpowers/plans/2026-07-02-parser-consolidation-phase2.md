<!-- plan-version: v3 -->
<!-- codex-validation: READY v3 at 2026-07-03T00:42:49Z (ultracode adversarial 2 rounds — round1 13건 triage→8건 반영, round2 잔존 1건 반영, 미해결 0건) -->

# 파서 일원화 (Phase 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 3개 파서(parse-svc/excel-parser/document-parser)를 parse-svc 하나로 in-process 통일하고, facade 의 파싱 로직을 전부 제거하며, 청킹 여부를 `chunk_needed` flag 로 계약한다.

**Architecture:** parse-svc 를 `parsers/<도메인>/`(pdf·excel·docx·ocr) + `tools/`(opendataloader·kordoc) 2층으로 재구조화. Phase 2a 는 구조+flag 만(외부 HTTP 위임 유지), 2b/2c 에서 excel/ocr 를 in-process 흡수, 2d 에서 markitdown 완전 제거+facade 정리, 2e 에서 compose/Dockerfile 정리.

**Tech Stack:** Python 3.12 / FastAPI / OpenDataLoader(JRE21) / kordoc(node CLI) / PyMuPDF+Pillow / gotenberg·minio·VL API(외부 유지)

**Spec:** `docs/superpowers/specs/2026-07-02-parser-consolidation-phase2-design.md`

## Global Constraints

- 표는 `<table>` HTML 보존 — pipe 평탄화 금지 (불변식).
- 모달 마커 U+3008/U+3009 byte-identical (불변식).
- 청킹·모달원자성은 facade `/chunk` 소유(chunk_needed=true 경로) — edgequake 는 passthrough (불변식).
- venv: `/Users/xxx/workspace/8.kb-pipeline/.venv-kb/bin/python` 으로 pytest 실행.
- 각 Phase 종료 시: 전체 pytest green + (2a 이후) `docker compose build parse-svc facade && docker compose up -d --wait` green 확인 후 다음 Phase.
- 커밋 메시지 어미: `Co-Authored-By: Claude <noreply@anthropic.com>` 불필요(레포 관례 없음) — 기존 관례(한글/영문 혼용 conventional commits)를 따른다.
- Phase 2d 전까지 markitdown/기존 라우팅(`kb_pipeline.blockify.recommended_parser`)은 **건드리지 않는다**(동작 보존).

---

# Phase 2a — parse-svc 재구조화 (동작 보존, excel/ocr HTTP 위임 유지)

### Task 1: `tools/opendataloader.py` — ODL 도구 추출

**Files:**
- Create: `parse_service/tools/__init__.py` (빈 파일)
- Create: `parse_service/tools/opendataloader.py`
- Test: `parse_service/tests/test_tools_opendataloader.py`

**Interfaces:**
- Produces: `convert_pdf_to_page_markdowns(file_bytes: bytes, filename: str) -> list[str]` — 페이지별 markdown 리스트(1-based 순서). 실패 시 `ToolError` raise.
- Produces: `class ToolError(Exception)` — 모든 tools 공용 오류 타입 (`parse_service/tools/__init__.py` 에 정의).

- [x] **Step 1: 실패 테스트 작성**

`parse_service/tests/test_tools_opendataloader.py`:
```python
"""tools/opendataloader — PDF bytes → 페이지별 md 리스트 (ODL sentinel 분할)."""
import pytest
from parse_service.tools import ToolError
from parse_service.tools import opendataloader as odl


def test_split_pages_by_sentinel(monkeypatch):
    # opendataloader_pdf.convert 를 monkeypatch — md 1개 파일에 SEP 로 3페이지.
    def fake_convert(input_path, output_dir, **kw):
        import os
        with open(os.path.join(output_dir, "out.md"), "w", encoding="utf-8") as f:
            f.write(f"{odl.PAGE_SEP}page-1{odl.PAGE_SEP}page-2{odl.PAGE_SEP}page-3")
    monkeypatch.setattr(odl, "_odl_convert", fake_convert)
    pages = odl.convert_pdf_to_page_markdowns(b"%PDF-fake", "a.pdf")
    assert pages == ["page-1", "page-2", "page-3"]


def test_no_md_raises_toolerror(monkeypatch):
    monkeypatch.setattr(odl, "_odl_convert", lambda **kw: None)
    with pytest.raises(ToolError):
        odl.convert_pdf_to_page_markdowns(b"%PDF-fake", "a.pdf")
```

- [x] **Step 2: 실패 확인**

Run: `.venv-kb/bin/python -m pytest parse_service/tests/test_tools_opendataloader.py -q`
Expected: FAIL (`ModuleNotFoundError: parse_service.tools`)

- [x] **Step 3: 구현**

`parse_service/tools/__init__.py`:
```python
"""파서 '도구' — 외부 바이너리/라이브러리 래퍼. 파서(parsers/<도메인>)가 호출한다."""


class ToolError(Exception):
    """도구 실행 실패(변환 산출물 없음/CLI 오류). 파서가 ParseError 로 감싼다."""
```

`parse_service/tools/opendataloader.py` — 기존 `parse_service/parsing.py:_parse_pdf_to_pages` 의 ODL 호출부(207~245행)를 이식:
```python
"""OpenDataLoader PDF 도구 — PDF bytes → 페이지별 markdown 리스트.

opendataloader_pdf(JRE 21) 를 subprocess 로 부른다. 문서당 .md 1개가 나오고
``markdown_page_separator`` 로 페이지 앞에 sentinel 이 삽입된다 → split 해 복원.
"""
from __future__ import annotations

import glob
import os
import re
import tempfile

from parse_service.tools import ToolError

#: 콘텐츠에 나타날 일 없는 페이지 sentinel (기존 parsing.py:_PAGE_SEP 그대로).
PAGE_SEP = "<<<ODL_PAGE_BREAK>>>"


def _safe_basename(name: str) -> str:
    base = os.path.basename((name or "").replace("\\", "/")).replace("\x00", "")
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base) or "upload"
    return "_" + base if base.startswith(".") else base


def _odl_convert(*, input_path: str, output_dir: str) -> None:
    import opendataloader_pdf

    opendataloader_pdf.convert(
        input_path=input_path, output_dir=output_dir, format="markdown",
        markdown_with_html=True, markdown_page_separator=PAGE_SEP, quiet=True,
    )


def convert_pdf_to_page_markdowns(file_bytes: bytes, filename: str) -> list[str]:
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, _safe_basename(filename))
        if os.path.commonpath([os.path.realpath(tmp), os.path.realpath(src)]) != os.path.realpath(tmp):
            raise ToolError("unsafe filename")
        with open(src, "wb") as fh:
            fh.write(file_bytes)
        _odl_convert(input_path=src, output_dir=tmp)
        mds = sorted(glob.glob(os.path.join(tmp, "**", "*.md"), recursive=True))
        if not mds:
            raise ToolError(f"opendataloader produced no md for {filename}")
        full = PAGE_SEP.join(
            open(m, encoding="utf-8", errors="replace").read() for m in mds
        )
        md_texts = full.split(PAGE_SEP)
        if len(md_texts) > 1 and not md_texts[0].strip():
            md_texts = md_texts[1:]
        return md_texts
```

- [x] **Step 4: 통과 확인**

Run: `.venv-kb/bin/python -m pytest parse_service/tests/test_tools_opendataloader.py -q`
Expected: 2 passed

- [x] **Step 5: Commit**

```bash
git add parse_service/tools/ parse_service/tests/test_tools_opendataloader.py
git commit -m "refactor(parse-svc): extract OpenDataLoader tool (Phase 2a-1)"
```

### Task 2: `parsers/pdf/` — PDF 도메인 파서

**Files:**
- Create: `parse_service/parsers/__init__.py`
- Create: `parse_service/parsers/pdf/__init__.py`
- Test: `parse_service/tests/test_parser_pdf.py`

**Interfaces:**
- Consumes: `tools.opendataloader.convert_pdf_to_page_markdowns`, `tools.ToolError`
- Produces: `parse_service/parsers/__init__.py` 에 공용 계약:
  ```python
  @dataclass
  class RouteResult:
      kind: str            # "pages" | "chunks"
      chunk_needed: bool
      pages: list | None = None    # PageDoc[] = [{"page_number": int, "blocks": list[dict]}]
      chunks: list | None = None   # [{"chunk_index", "text", "titles_context", "pages"}]
  class ParserError(Exception): ...
  ```
- Produces: `parsers.pdf.parse(file_bytes, filename, *, ocr_url: str) -> RouteResult` — kind="pages", chunk_needed=True. 스캔 페이지 OCR 보충 포함(기존 로직 이식).

- [x] **Step 1: 실패 테스트 작성**

`parse_service/tests/test_parser_pdf.py`:
```python
"""parsers/pdf — 페이지 보존 + 스캔페이지 OCR 보충 + chunk_needed=True."""
import pytest
from parse_service.parsers import RouteResult, ParserError
from parse_service.parsers import pdf as pdf_parser


def test_digital_pdf_pages(monkeypatch):
    monkeypatch.setattr(pdf_parser, "_page_markdowns",
                        lambda fb, fn: ["# p1 text", "# p2 text"])
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert isinstance(res, RouteResult)
    assert res.kind == "pages" and res.chunk_needed is True
    assert [p["page_number"] for p in res.pages] == [1, 2]
    assert res.pages[0]["blocks"][0]["page_idx"] == 1


def test_scanned_page_gets_ocr(monkeypatch):
    # p2 가 빈 md → 렌더+OCR 보충 경로. 렌더/오CR 를 fake 로.
    monkeypatch.setattr(pdf_parser, "_page_markdowns", lambda fb, fn: ["# p1", "   "])
    class FakeRP:  # render_pdf_pages 반환 원소 흉내
        page_number, jpeg = 2, b"jpegbytes"
    monkeypatch.setattr(pdf_parser, "_render_pages", lambda fb: [FakeRP()])
    monkeypatch.setattr(pdf_parser, "_ocr_elements_for_page",
                        lambda jpeg, name, ocr_url: [
                            {"category": "text", "content": {"markdown": "ocr text"}, "page": 0}
                        ])
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert res.pages[1]["page_number"] == 2
    assert res.pages[1]["blocks"], "OCR 보충 블록이 있어야"
    assert res.pages[1]["blocks"][0]["page_idx"] == 2


def test_tool_error_becomes_parser_error(monkeypatch):
    from parse_service.tools import ToolError
    def boom(fb, fn):
        raise ToolError("no md")
    monkeypatch.setattr(pdf_parser, "_page_markdowns", boom)
    with pytest.raises(ParserError):
        pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
```

- [x] **Step 2: 실패 확인**

Run: `.venv-kb/bin/python -m pytest parse_service/tests/test_parser_pdf.py -q`
Expected: FAIL (`ModuleNotFoundError: parse_service.parsers`)

- [x] **Step 3: 구현**

`parse_service/parsers/__init__.py`:
```python
"""도메인 파서 공용 계약 — 각 parsers/<도메인>.parse() 는 RouteResult 를 반환한다."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RouteResult:
    kind: str                    # "pages" | "chunks"
    chunk_needed: bool
    pages: list | None = None    # PageDoc[] = [{"page_number", "blocks"}]
    chunks: list | None = None   # facade 청크 스키마 [{"chunk_index","text","titles_context","pages"}]


class ParserError(Exception):
    """파서 실패 — app.py 가 FrontError("parse_failed") 로 매핑."""
```

`parse_service/parsers/pdf/__init__.py` — 기존 `parsing.py:_parse_pdf_to_pages`(206~277행) 로직 이식(도구 호출부만 tools 로 대체):
```python
"""PDF 도메인 파서 — OpenDataLoader(도구) 페이지별 md → blocks. 스캔 페이지는 OCR 보충."""
from __future__ import annotations

import logging

from parse_service.parsers import RouteResult, ParserError
from parse_service.tools import ToolError
from parse_service.tools.opendataloader import convert_pdf_to_page_markdowns

log = logging.getLogger("kb_pipeline.parse_service.parsers.pdf")
_DIGITAL_MIN_CHARS = 1  # 기존 parsing.py 와 동일(보수적)


def _page_markdowns(file_bytes: bytes, filename: str) -> list[str]:
    return convert_pdf_to_page_markdowns(file_bytes, filename)


def _render_pages(file_bytes: bytes):
    from parse_service.pdf_pages import render_pdf_pages
    return render_pdf_pages(file_bytes)


def _ocr_elements_for_page(jpeg: bytes, name: str, ocr_url: str) -> list[dict]:
    # Phase 2a: 기존 HTTP OCR 재사용(parsing._ocr_page). 2c 에서 in-process 로 대체.
    from parse_service.parsing import _ocr_page
    return _ocr_page(jpeg, name, ocr_url=ocr_url)


def parse(file_bytes: bytes, filename: str, *, ocr_url: str) -> RouteResult:
    from kb_pipeline.blockify import hybrid_to_blocks, elements_to_blocks
    try:
        md_texts = _page_markdowns(file_bytes, filename)
    except ToolError as e:
        raise ParserError(str(e)) from e

    rendered = None
    pages: list[dict] = []
    for i, md in enumerate(md_texts):
        page_number = i + 1
        if len((md or "").strip()) >= _DIGITAL_MIN_CHARS:
            pages.append({"page_number": page_number,
                          "blocks": hybrid_to_blocks(md, page_idx=page_number)})
            continue
        if rendered is None:
            rendered = _render_pages(file_bytes)
        page_jpeg = next((rp.jpeg for rp in rendered if rp.page_number == page_number), None)
        if page_jpeg is None:
            log.warning("scanned page %d has no rendered image", page_number)
            pages.append({"page_number": page_number, "blocks": []})
            continue
        try:
            elements = _ocr_elements_for_page(page_jpeg, f"page-{page_number}.jpeg", ocr_url)
        except Exception:  # noqa: BLE001 — 페이지 단위 OCR 실패는 비치명
            log.exception("OCR failed for scanned page %d", page_number)
            pages.append({"page_number": page_number, "blocks": []})
            continue
        blocks = elements_to_blocks(elements)
        for b in blocks:
            b["page_idx"] = page_number
        pages.append({"page_number": page_number, "blocks": blocks})
    return RouteResult(kind="pages", chunk_needed=True, pages=pages)
```

- [x] **Step 4: 통과 확인**

Run: `.venv-kb/bin/python -m pytest parse_service/tests/test_parser_pdf.py -q`
Expected: 3 passed

- [x] **Step 5: Commit**

```bash
git add parse_service/parsers/ parse_service/tests/test_parser_pdf.py
git commit -m "refactor(parse-svc): parsers/pdf domain parser (Phase 2a-2)"
```

### Task 3: `parsers/ocr/` — pptx+이미지 도메인 파서 (Phase 2a = HTTP 위임)

**Files:**
- Create: `parse_service/parsers/ocr/__init__.py`
- Test: `parse_service/tests/test_parser_ocr.py`

**Interfaces:**
- Produces: `parsers.ocr.parse(file_bytes, filename, *, ocr_url: str) -> RouteResult` — kind="pages", chunk_needed=True. pptx/이미지 통파일을 OCR 에 보내 elements → page 별 PageDoc.
- Produces: `parsers.ocr.IMAGE_EXTS = {"png","jpg","jpeg","gif","bmp","tif","tiff","webp"}` — router 가 사용.

- [x] **Step 1: 실패 테스트 작성**

`parse_service/tests/test_parser_ocr.py`:
```python
"""parsers/ocr — 통파일 OCR elements → 페이지별 PageDoc, chunk_needed=True."""
import pytest
from parse_service.parsers import RouteResult, ParserError
from parse_service.parsers import ocr as ocr_parser


def test_elements_grouped_into_pages(monkeypatch):
    monkeypatch.setattr(ocr_parser, "_whole_file_elements",
                        lambda fb, fn, ocr_url: [
                            {"category": "text", "content": {"markdown": "a"}, "page_idx": 0},
                            {"category": "text", "content": {"markdown": "b"}, "page_idx": 1},
                        ])
    res = ocr_parser.parse(b"PK", "slide.pptx", ocr_url="http://ocr")
    assert isinstance(res, RouteResult)
    assert res.kind == "pages" and res.chunk_needed is True
    assert [p["page_number"] for p in res.pages] == [1, 2]  # 0-based → 1-based


def test_empty_elements_raise(monkeypatch):
    monkeypatch.setattr(ocr_parser, "_whole_file_elements", lambda fb, fn, ocr_url: [])
    with pytest.raises(ParserError):
        ocr_parser.parse(b"\x89PNG", "img.png", ocr_url="http://ocr")
```

- [x] **Step 2: 실패 확인**

Run: `.venv-kb/bin/python -m pytest parse_service/tests/test_parser_ocr.py -q`
Expected: FAIL (`cannot import name 'ocr'`)

- [x] **Step 3: 구현**

`parse_service/parsers/ocr/__init__.py` — 기존 `parsing.py` 의 `_ocr_page`/`_elements_to_pages`(147~197행) 로직 사용:
```python
"""OCR 도메인 파서 — pptx + 이미지/스캔. Phase 2a: HTTP(:18050) 위임, 2c 에서 in-process."""
from __future__ import annotations

from parse_service.parsers import RouteResult, ParserError

IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff", "webp"}


def _whole_file_elements(file_bytes: bytes, filename: str, ocr_url: str) -> list[dict]:
    from parse_service.parsing import _ocr_page  # 기존 HTTP contract 재사용
    return _ocr_page(file_bytes, filename, ocr_url=ocr_url)


def _elements_to_pages(elements: list[dict]) -> list[dict]:
    from kb_pipeline.blockify import elements_to_blocks
    blocks = elements_to_blocks(elements)
    by_page: dict[int, list[dict]] = {}
    for b in blocks:
        page_number = int(b.get("page_idx", 0) or 0) + 1  # 0-based → 1-based canonical
        b["page_idx"] = page_number
        by_page.setdefault(page_number, []).append(b)
    return [{"page_number": pn, "blocks": by_page[pn]} for pn in sorted(by_page)]


def parse(file_bytes: bytes, filename: str, *, ocr_url: str) -> RouteResult:
    try:
        elements = _whole_file_elements(file_bytes, filename, ocr_url)
    except Exception as e:  # noqa: BLE001 — HTTP/네트워크 오류 정규화
        raise ParserError(f"ocr failed for {filename}: {e}") from e
    if not elements:
        raise ParserError(f"ocr/vlm empty for {filename}")
    return RouteResult(kind="pages", chunk_needed=True, pages=_elements_to_pages(elements))
```

- [x] **Step 4: 통과 확인**

Run: `.venv-kb/bin/python -m pytest parse_service/tests/test_parser_ocr.py -q`
Expected: 2 passed

- [x] **Step 5: Commit**

```bash
git add parse_service/parsers/ocr/ parse_service/tests/test_parser_ocr.py
git commit -m "refactor(parse-svc): parsers/ocr domain parser, HTTP delegation (Phase 2a-3)"
```

### Task 4: `parsers/excel/` — 엑셀 도메인 파서 (Phase 2a = HTTP 위임 + 정규화 이동)

**Files:**
- Create: `parse_service/parsers/excel/__init__.py`
- Test: `parse_service/tests/test_parser_excel.py`
- 참조(복사원): `service/excel_parser_client.py` (facade — 2d 에서 삭제 예정. 코드를 parse-svc 로 **이동**)

**Interfaces:**
- Produces: `parsers.excel.parse(file_bytes, filename, *, excel_url: str) -> RouteResult` — kind="chunks", **chunk_needed=False**, chunks=facade 청크 스키마.
- Produces: `parsers.excel.EXCEL_EXTS = {"xlsx","xlsm","xls"}` — router 가 사용.
- Produces: `normalize_chunks(rag_chunks: list[dict]) -> list[dict]` — RagChunk → `{chunk_index,text,titles_context,pages}` (facade `excel_parser_client.normalize_chunks` 를 그대로 이동).

- [x] **Step 1: 실패 테스트 작성**

`parse_service/tests/test_parser_excel.py`:
```python
"""parsers/excel — 자체청킹 결과를 facade 청크 스키마로, chunk_needed=False."""
import pytest
from parse_service.parsers import RouteResult, ParserError
from parse_service.parsers import excel as excel_parser


def test_chunks_normalized_and_flag_false(monkeypatch):
    rag = [{"content_text": "표1 내용", "title": "시트1", "path": ["시트1"]},
           {"content_text": "표2 내용", "title": "시트2", "path": ["시트2"]}]
    monkeypatch.setattr(excel_parser, "_fetch_rag_chunks", lambda fb, fn, excel_url: rag)
    res = excel_parser.parse(b"PK", "a.xlsx", excel_url="http://x")
    assert res.kind == "chunks" and res.chunk_needed is False
    assert res.chunks[0]["chunk_index"] == 0
    assert res.chunks[0]["text"] == "표1 내용"
    assert "titles_context" in res.chunks[0] and "pages" in res.chunks[0]


def test_empty_chunks_raise(monkeypatch):
    monkeypatch.setattr(excel_parser, "_fetch_rag_chunks", lambda fb, fn, excel_url: [])
    with pytest.raises(ParserError):
        excel_parser.parse(b"PK", "a.xlsx", excel_url="http://x")
```

- [x] **Step 2: 실패 확인**

Run: `.venv-kb/bin/python -m pytest parse_service/tests/test_parser_excel.py -q`
Expected: FAIL (`cannot import name 'excel'`)

- [x] **Step 3: 구현**

`parse_service/parsers/excel/__init__.py`:
```python
"""Excel 도메인 파서 — parse+chunk 결합(자체청킹) → chunk_needed=False.

Phase 2a: excel-rag-parser(:18055) HTTP 위임(잡 제출→폴링) — facade 의
service/excel_parser_client.py 로직을 이동. 2b 에서 excel_parser_rag in-process 로 대체.
"""
from __future__ import annotations

import time

import httpx

from parse_service.parsers import RouteResult, ParserError

EXCEL_EXTS = {"xlsx", "xlsm", "xls"}
_TERMINAL = {"succeeded", "failed", "cancelled"}


def normalize_rag_chunk(rc: dict, index: int) -> dict | None:
    # facade service/excel_parser_client.py:normalize_rag_chunk 를 **byte-identical 이동**
    # (아래 본문 = 현행 원본 그대로 — v2: text 폴백은 content_text or title,
    #  .get() 안전접근, titles_context 는 path 우선 폴백 체인. 리뷰 A1~A3 반영).
    text = (rc.get("content_text") or rc.get("title") or "").strip()
    if not text:
        return None
    title = rc.get("title")
    path = rc.get("path") or []
    titles_context = path or ([title] if title else None)
    return {"chunk_index": index, "text": text,
            "titles_context": titles_context, "pages": []}


def normalize_chunks(rag_chunks: list[dict]) -> list[dict]:
    out = []
    for rc in rag_chunks or []:
        n = normalize_rag_chunk(rc, len(out))
        if n is not None:
            out.append(n)
    return out


def _fetch_rag_chunks(file_bytes: bytes, filename: str, excel_url: str) -> list[dict]:
    """POST /parse/jobs/file → poll — facade ExcelRagParserClient.parse_chunks 이동."""
    base = excel_url.rstrip("/")
    with httpx.Client(timeout=600.0) as http:
        r = http.post(f"{base}/parse/jobs/file",
                      files={"file": (filename, file_bytes)},
                      data={"doc_name": filename})
        r.raise_for_status()
        job_id = (r.json() or {}).get("job_id")
        if not job_id:
            raise ParserError("excel-rag-parser returned no job_id")
        deadline = time.monotonic() + 1800.0
        while time.monotonic() < deadline:
            t = http.get(f"{base}/parse/jobs/{job_id}")
            t.raise_for_status()
            body = t.json() or {}
            if body.get("status") in _TERMINAL:
                if body.get("status") != "succeeded":
                    raise ParserError(f"excel job {body.get('status')}: {body.get('error')}")
                return (body.get("result") or {}).get("chunks") or []
            time.sleep(3.0)
        raise ParserError("excel job poll timeout")


def parse(file_bytes: bytes, filename: str, *, excel_url: str) -> RouteResult:
    try:
        rag_chunks = _fetch_rag_chunks(file_bytes, filename, excel_url)
    except ParserError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ParserError(f"excel parse failed for {filename}: {e}") from e
    chunks = normalize_chunks(rag_chunks)
    if not chunks:
        raise ParserError(f"excel produced no chunks for {filename}")
    return RouteResult(kind="chunks", chunk_needed=False, chunks=chunks)
```
**구현 주의**: `normalize_rag_chunk` 본문은 위 스켈레톤이 아니라 **`service/excel_parser_client.py` 의 실제 함수 본문을 복사**한다(필드 매핑 회귀 방지). 복사 후 위 테스트 + 기존 facade 테스트로 대조.

- [x] **Step 4: 통과 확인**

Run: `.venv-kb/bin/python -m pytest parse_service/tests/test_parser_excel.py -q`
Expected: 2 passed

- [x] **Step 5: Commit**

```bash
git add parse_service/parsers/excel/ parse_service/tests/test_parser_excel.py
git commit -m "refactor(parse-svc): parsers/excel with chunk_needed=False, HTTP delegation (Phase 2a-4)"
```

### Task 5: `router.py` — 확장자 디스패치 (+ 임시 markitdown 폴백 유지)

**Files:**
- Create: `parse_service/router.py`
- Test: `parse_service/tests/test_router.py`

**Interfaces:**
- Consumes: `parsers.{pdf,ocr,excel}.parse`, `parsers.ocr.IMAGE_EXTS`, `parsers.excel.EXCEL_EXTS`
- Produces: `route(file_bytes, filename, *, ocr_url: str, excel_url: str) -> RouteResult`
- **Phase 2a 라우팅(동작 보존)**: pdf→pdf / xlsx·xlsm·xls→excel / pptx·docx·이미지→ocr / **그 외→기존 markitdown 경로 유지**(`parsing._parse_markitdown` — 2d 에서 kordoc 폴백으로 교체). docx→kordoc 전환도 2d.

- [x] **Step 1: 실패 테스트 작성**

`parse_service/tests/test_router.py`:
```python
"""router — 확장자 → 도메인 파서 디스패치 (Phase 2a 매핑)."""
import pytest
from parse_service.parsers import RouteResult
from parse_service import router


@pytest.mark.parametrize("fname,expected_domain", [
    ("a.pdf", "pdf"), ("a.xlsx", "excel"), ("a.xlsm", "excel"), ("a.xls", "excel"),
    ("a.pptx", "ocr"), ("a.docx", "ocr"),  # 2a: docx 아직 ocr(기존 structural 동작 보존)
    ("a.png", "ocr"), ("a.webp", "ocr"),
])
def test_dispatch(monkeypatch, fname, expected_domain):
    called = {}
    def fake(domain):
        def _p(fb, fn, **kw):
            called["domain"] = domain
            return RouteResult(kind="pages", chunk_needed=True, pages=[])
        return _p
    monkeypatch.setattr(router, "_PARSERS",
                        {d: fake(d) for d in ("pdf", "excel", "ocr", "fallback")})
    router.route(b"x", fname, ocr_url="u", excel_url="v")
    assert called["domain"] == expected_domain


def test_unknown_ext_falls_back(monkeypatch):
    called = {}
    def fb_parse(fb, fn, **kw):
        called["domain"] = "fallback"
        return RouteResult(kind="pages", chunk_needed=True, pages=[])
    monkeypatch.setattr(router, "_PARSERS", {"pdf": None, "excel": None,
                                             "ocr": None, "fallback": fb_parse})
    router.route(b"x", "a.hwpx", ocr_url="u", excel_url="v")
    assert called["domain"] == "fallback"
```

- [x] **Step 2: 실패 확인**

Run: `.venv-kb/bin/python -m pytest parse_service/tests/test_router.py -q`
Expected: FAIL (`No module named 'parse_service.router'`)

- [x] **Step 3: 구현**

`parse_service/router.py`:
```python
"""확장자 → 도메인 파서 디스패치. 파싱 로직 없음(얇은 계층).

Phase 2a 매핑(동작 보존): pdf→pdf, 엑셀→excel(chunk_needed=False),
pptx/docx/이미지→ocr, 그 외→markitdown 폴백(임시 — 2d 에서 kordoc 로 교체).
"""
from __future__ import annotations

from parse_service.parsers import RouteResult, ParserError
from parse_service.parsers import pdf as _pdf
from parse_service.parsers import ocr as _ocr
from parse_service.parsers import excel as _excel


def _fallback_parse(file_bytes: bytes, filename: str, *, ocr_url: str, excel_url: str) -> RouteResult:
    # 임시(2a): 기존 markitdown 경로 보존 — 단일 페이지 강등. 2d 에서 kordoc 폴백으로 교체.
    from kb_pipeline.blockify import hybrid_to_blocks
    from parse_service.parsing import _parse_markitdown
    md = _parse_markitdown(file_bytes, filename)
    return RouteResult(kind="pages", chunk_needed=True,
                       pages=[{"page_number": 1, "blocks": hybrid_to_blocks(md, page_idx=1)}])


def _pdf_parse(fb, fn, *, ocr_url, excel_url):
    return _pdf.parse(fb, fn, ocr_url=ocr_url)


def _ocr_parse(fb, fn, *, ocr_url, excel_url):
    return _ocr.parse(fb, fn, ocr_url=ocr_url)


def _excel_parse(fb, fn, *, ocr_url, excel_url):
    return _excel.parse(fb, fn, excel_url=excel_url)


_PARSERS = {"pdf": _pdf_parse, "excel": _excel_parse, "ocr": _ocr_parse,
            "fallback": _fallback_parse}


def _domain(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext == "pdf":
        return "pdf"
    if ext in _excel.EXCEL_EXTS:
        return "excel"
    if ext in ({"pptx", "docx"} | _ocr.IMAGE_EXTS):
        return "ocr"
    return "fallback"


def route(file_bytes: bytes, filename: str, *, ocr_url: str, excel_url: str) -> RouteResult:
    return _PARSERS[_domain(filename)](file_bytes, filename,
                                       ocr_url=ocr_url, excel_url=excel_url)
```

- [x] **Step 4: 통과 확인**

Run: `.venv-kb/bin/python -m pytest parse_service/tests/test_router.py -q`
Expected: 9 passed

- [x] **Step 5: Commit**

```bash
git add parse_service/router.py parse_service/tests/test_router.py
git commit -m "refactor(parse-svc): extension router with temporary markitdown fallback (Phase 2a-5)"
```

### Task 6: `app.py` 재배선 — router 사용 + `chunk_needed` 응답 필드

**Files:**
- Modify: `parse_service/app.py` (`run_parse` 219행 부근 + `/parse` 핸들러 316행 부근)
- Test: `parse_service/tests/test_app_chunk_needed.py`

**Interfaces:**
- Consumes: `router.route(...) -> RouteResult`
- Produces: `/parse` 응답에 additive 필드:
  - kind="pages": 기존 응답 그대로 + `"chunk_needed": true`
  - kind="chunks": `{"enriched_content": "\n\n".join(chunk texts), "n_blocks": len(chunks), "modal_spans": [], "chunks": [...], "chunk_needed": false, "docs_id", "page_count": 0, "pages": [], "page_spans": [], "timing_metrics": {...}}` (excel 은 모달/렌더 없음)

- [x] **Step 1: 실패 테스트 작성**

`parse_service/tests/test_app_chunk_needed.py`:
```python
"""/parse 가 RouteResult.kind 에 따라 chunk_needed 를 응답에 싣는다."""
from fastapi.testclient import TestClient
import parse_service.app as appmod
from parse_service.parsers import RouteResult


def _client():
    return TestClient(appmod.app)


def test_pages_path_sets_chunk_needed_true(monkeypatch):
    monkeypatch.setattr(appmod, "_route",
        lambda fb, fn, **kw: RouteResult(kind="pages", chunk_needed=True, pages=[
            {"page_number": 1, "blocks": [{"type": "text", "text": "hello", "page_idx": 1}]}]))
    r = _client().post("/parse", files={"file": ("a.pdf", b"%PDF")},
                       data={"filename": "a.pdf"})
    assert r.status_code == 200
    body = r.json()
    assert body["chunk_needed"] is True
    assert "enriched_content" in body


def test_chunks_path_sets_chunk_needed_false(monkeypatch):
    monkeypatch.setattr(appmod, "_route",
        lambda fb, fn, **kw: RouteResult(kind="chunks", chunk_needed=False, chunks=[
            {"chunk_index": 0, "text": "표1", "titles_context": ["s1"], "pages": []}]))
    r = _client().post("/parse", files={"file": ("a.xlsx", b"PK")},
                       data={"filename": "a.xlsx"})
    assert r.status_code == 200
    body = r.json()
    assert body["chunk_needed"] is False
    assert body["chunks"][0]["text"] == "표1"
    assert body["modal_spans"] == []
```

- [x] **Step 2: 실패 확인**

Run: `.venv-kb/bin/python -m pytest parse_service/tests/test_app_chunk_needed.py -q`
Expected: FAIL (`no attribute '_route'`)

- [x] **Step 3: 구현**

`parse_service/app.py` 수정:
1. import 에 추가: `from parse_service.router import route as _route_impl` / `from parse_service.parsers import RouteResult, ParserError`
2. 모듈 레벨 훅(테스트 monkeypatch 대상): `def _route(fb, fn, **kw): return _route_impl(fb, fn, **kw)`
3. `run_parse` 시그니처 유지하되 내부 첫 단계를 교체:
```python
        # (기존) pages = parse_pages(file_bytes, filename, ocr_url=..., excel_url=...)
        rr = _route(file_bytes, filename, ocr_url=ocr_url, excel_url=excel_url)
        if rr.kind == "chunks":
            # excel: 자체청킹 — 모달/blockify/렌더 스킵. additive 계약 유지.
            return {
                "enriched_content": "\n\n".join(c.get("text", "") for c in rr.chunks),
                "n_blocks": len(rr.chunks),
                "modal_spans": [],
                "chunks": rr.chunks,
                "chunk_needed": False,
                "docs_id": docs_id,
                "page_count": 0, "pages": [], "page_spans": [],
                # v2(리뷰 B1): pages 경로와 동일 형태(modal_llm 포함) — 모니터링 집계자 호환.
                "timing_metrics": {"parse_ms": round((time.perf_counter() - _t) * 1000.0, 1),
                                   "modal_enrich_ms": 0.0, "render_upload_ms": 0.0,
                                   "counters": {"page_count": 0, "n_blocks": len(rr.chunks)},
                                   "modal_llm": {"wall_ms": None, "calls": None,
                                                 "by_type": None, "per_call_ms": None,
                                                 "max_workers": None}},
            }
        pages = rr.pages
```
4. `ParserError` 를 기존 `ParseError` 처럼 `FrontError("parse_failed")` 로 매핑(기존 except 절에 `ParserError` 추가).
5. pages 경로 최종 return dict 에 `"chunk_needed": True` 추가.
6. `/parse` 핸들러는 run_parse 반환을 그대로 JSON 으로 — 변경 불필요(필드 passthrough).

- [x] **Step 4: 통과 + 기존 스위트 회귀 확인**

Run: `.venv-kb/bin/python -m pytest parse_service/tests -q`
Expected: 전체 passed (기존 test_parse.py 포함 — run_parse 의 pages 경로 동작 불변)

- [x] **Step 5: Commit**

```bash
git add parse_service/app.py parse_service/tests/test_app_chunk_needed.py
git commit -m "feat(parse-svc): /parse chunk_needed flag via router (Phase 2a-6)"
```

### Task 7: facade — excel 분기 제거 + `/ingest` chunk_needed 분기

**Files:**
- Modify: `service/app.py` (`/parse` 72~99행, `/ingest` 239~283행)
- Modify: `service/parse_client.py` (응답 passthrough — 변경 없음 확인만)
- Test: `service/tests/test_ingest_chunk_needed.py`
- Modify: `service/tests/test_parse_endpoint.py` (excel 분기 단언 제거)

**Interfaces:**
- Consumes: parse-svc `/parse` 응답의 `chunk_needed`(bool) + `chunks`(list, chunk_needed=false 일 때)
- Produces: facade `/parse` — excel 분기 없이 전부 parse-svc 위임(응답 passthrough, `chunk_strategy` 필드는 excel 일 때 `"excel_rag_parser"` 유지 — 소비자 호환).
- Produces: facade `/ingest` — `chunk_needed` 로 분기: true → adaptive `/chunk`, false → parsed["chunks"] 바로 insert.

- [x] **Step 1: 실패 테스트 작성**

`service/tests/test_ingest_chunk_needed.py`:
```python
"""/ingest 가 parse-svc 의 chunk_needed 로 청킹을 분기한다."""
from fastapi.testclient import TestClient
import service.app as appmod


class FakeParse:
    def __init__(self, resp):
        self.resp = resp
    def parse(self, **kw):
        return self.resp


class FakeAdaptive:
    def __init__(self):
        self.called = False
    def chunk(self, **kw):
        self.called = True
        return {"chunks": [{"chunk_index": 0, "chunk_text": "c0"}],
                "method_selected": "m", "scores": {}, "methods_compared": []}


class FakeEq:
    def ensure_workspace(self, wid, name=None):
        return "ws-uuid"
    def insert_chunks(self, **kw):
        self.last_chunks = kw["chunk_texts"]
        return {"document_id": "d1", "chunk_count": len(kw["chunk_texts"]),
                "status": "indexed"}


def _override(parse_resp, ac, eq):
    app = appmod.app
    app.dependency_overrides[appmod.get_parse_client] = lambda: FakeParse(parse_resp)
    app.dependency_overrides[appmod.get_adaptive_chunk] = lambda: ac
    app.dependency_overrides[appmod.get_edgequake] = lambda: eq
    return TestClient(app)


def test_chunk_needed_true_calls_adaptive():
    ac, eq = FakeAdaptive(), FakeEq()
    c = _override({"enriched_content": "text", "chunk_needed": True}, ac, eq)
    r = c.post("/ingest", data={"workspace_id": "w", "doc_id": "d"},
               files={"file": ("a.pdf", b"%PDF")})
    assert r.status_code == 200
    assert ac.called is True
    appmod.app.dependency_overrides.clear()


def test_chunk_needed_false_skips_adaptive_and_inserts_native_chunks():
    ac, eq = FakeAdaptive(), FakeEq()
    c = _override({"enriched_content": "표1", "chunk_needed": False,
                   "chunks": [{"chunk_index": 0, "text": "표1",
                               "titles_context": None, "pages": []}]}, ac, eq)
    r = c.post("/ingest", data={"workspace_id": "w", "doc_id": "d"},
               files={"file": ("a.xlsx", b"PK")})
    assert r.status_code == 200
    assert ac.called is False
    assert eq.last_chunks == ["표1"]
    appmod.app.dependency_overrides.clear()


def test_failed_parse_returns_immediately():
    """v3(리뷰 round2): parse 실패({status:"failed"})는 adaptive 미호출·그대로 반환."""
    ac, eq = FakeAdaptive(), FakeEq()
    c = _override({"status": "failed", "detail": "parse error"}, ac, eq)
    r = c.post("/ingest", data={"workspace_id": "w", "doc_id": "d"},
               files={"file": ("a.pdf", b"%PDF")})
    assert r.status_code == 200
    assert r.json()["status"] == "failed"
    assert ac.called is False
    appmod.app.dependency_overrides.clear()
```

- [x] **Step 2: 실패 확인**

Run: `.venv-kb/bin/python -m pytest service/tests/test_ingest_chunk_needed.py -q`
Expected: FAIL (현재 `/ingest` 는 무조건 adaptive 호출 → 2번째·3번째 테스트 실패)

- [x] **Step 3: 구현**

`service/app.py`:
1. `/parse`(72~99행): `_is_excel` 분기 블록(88~96행) **삭제** — 전부 `pc.parse(...)` 위임. 단 excel 소비자 호환을 위해 응답에 `chunk_strategy` 를 재구성:
```python
@app.post("/parse")
async def parse(file: UploadFile = File(...), content_type: str | None = Form(None),
                docs_id: str | None = Form(None), pc=Depends(get_parse_client)):
    data = await file.read()
    safe_name = _safe_basename(file.filename or "upload")
    parsed = pc.parse(file_bytes=data, filename=safe_name,
                      content_type=content_type or file.content_type, docs_id=docs_id)
    if parsed.get("chunk_needed") is False:
        parsed.setdefault("chunk_strategy", "excel_rag_parser")  # 소비자 호환 필드
    return parsed
```
2. `/ingest`(239~283행): 2)단계(adaptive) 를 분기로:
```python
    parsed = pc.parse(file_bytes=data, filename=safe_name,
                      content_type=content_type or file.content_type)
    # v2(리뷰 B10): parse-svc 파싱 실패({status:"failed"}) 는 빈 컨텐츠로 adaptive 를
    # 태우지 않고 그대로 반환(호출자가 실패 인지).
    if parsed.get("status") == "failed":
        return parsed
    if parsed.get("chunk_needed", True):
        enriched = parsed.get("enriched_content", "")
        chunk_res = ac.chunk(text=enriched, doc_name=doc_id,
                             atomic_markers=MODAL_ATOMIC_MARKERS)
        chunk_texts = [ch.get("chunk_text", "") for ch in (chunk_res.get("chunks") or [])]
        chunking_selection = {"method_selected": chunk_res.get("method_selected"),
                              "scores": chunk_res.get("scores") or {},
                              "methods_compared": chunk_res.get("methods_compared") or []}
    else:
        chunk_texts = [c.get("text", "") for c in (parsed.get("chunks") or [])]
        chunking_selection = {"method_selected": "excel_rag_parser",
                              "scores": {}, "methods_compared": []}
```
3. `get_excel_client`/`ec=Depends(...)` 파라미터는 이 Task 에선 남겨둔다(참조만 제거) — 파일 삭제는 2d.
4. `service/tests/test_parse_endpoint.py` 에서 excel 분기(`_is_excel` → ec 호출) 단언 테스트를 "chunk_needed=False passthrough + chunk_strategy 셋업" 단언으로 교체.

- [x] **Step 4: 통과 + facade 스위트 회귀**

Run: `.venv-kb/bin/python -m pytest service/tests -q`
Expected: 전체 passed

- [x] **Step 5: Phase 2a 통합 검증 (스택)**

```bash
cd /Users/xxx/workspace/8.kb-pipeline
docker compose build parse-svc facade && docker compose up -d --wait
# excel lane 회귀(자체청킹 유지 + adaptive 미호출) + 일반 lane:
curl -sS -X POST http://localhost:19000/parse -F "file=@<xlsx 샘플>" | python3 -c "import sys,json;d=json.load(sys.stdin);assert d['chunk_needed'] is False and d['chunks']"
curl -sS -X POST http://localhost:19000/ingest -F "file=@<작은 md/pdf 샘플>" -F workspace_id=t -F doc_id=t1
```
Expected: 둘 다 성공, `docker compose ps` 전부 healthy.

- [x] **Step 6: Commit**

```bash
git add service/app.py service/tests/
git commit -m "feat(facade): chunk_needed branch in /ingest, excel branch moved to parse-svc (Phase 2a-7)"
```

---

# Phase 2b — excel_parser_rag in-process 흡수

### Task 8: `excel_parser_rag` 패키지 이식

**Files:**
- Create: `parse_service/parsers/excel/excel_parser_rag/` (7.excel-parser 에서 복사)
- Modify: `requirements.txt` (openpyxl 추가)
- Test: `parse_service/tests/test_excel_rag_import.py`

**Interfaces:**
- Produces: `from parse_service.parsers.excel.excel_parser_rag.backends import get_backend` 가 import 가능. 내부 상대임포트 유지로 무수정 동작.

- [x] **Step 1: 복사 + 자기참조 검사**

```bash
cp -R /Users/xxx/workspace/7.excel-parser/excel_parser_rag \
      /Users/xxx/workspace/8.kb-pipeline/parse_service/parsers/excel/excel_parser_rag
find parse_service/parsers/excel/excel_parser_rag -name __pycache__ -type d -exec rm -rf {} +
# 절대 자기참조(import excel_parser_rag / from excel_parser_rag ...) 전수:
grep -rnE '^(from|import) excel_parser_rag' parse_service/parsers/excel/excel_parser_rag --include='*.py'
```
Expected: grep 결과의 각 라인을 `from parse_service.parsers.excel.excel_parser_rag ...` 로 치환(수 건 예상 — cli.py/__main__.py 등). **치환 후 grep 재실행 → 0건.**

- [x] **Step 2: 실패 테스트 작성**

`parse_service/tests/test_excel_rag_import.py`:
```python
"""이식된 excel_parser_rag 가 import 되고 backend 팩토리가 동작한다."""
def test_get_backend_importable():
    from parse_service.parsers.excel.excel_parser_rag.backends import get_backend
    b = get_backend("openpyxl")   # node 불필요 백엔드로 임포트/생성만 검증
    assert b is not None
```

- [x] **Step 3: 의존 추가 + 통과 확인**

`requirements.txt` 에 `openpyxl>=3.1.0` 추가 후:
```bash
.venv-kb/bin/pip install -r requirements.txt
.venv-kb/bin/python -m pytest parse_service/tests/test_excel_rag_import.py -q
```
Expected: 1 passed. (원본 excel-parser 의 `pyproject.toml`/`requirements.txt` 의존 대비 누락 검사: `grep -E 'dependencies|install_requires' /Users/xxx/workspace/7.excel-parser/pyproject.toml` 로 추가 의존(예: pillow 등) 확인·반영.)

- [x] **Step 4: Commit**

```bash
git add parse_service/parsers/excel/excel_parser_rag requirements.txt parse_service/tests/test_excel_rag_import.py
git commit -m "feat(parse-svc): vendor excel_parser_rag package in-process (Phase 2b-1)"
```

### Task 9: excel 파서 in-process 전환 (HTTP 제거)

**Files:**
- Modify: `parse_service/parsers/excel/__init__.py` (`_fetch_rag_chunks` HTTP → in-process)
- Test: `parse_service/tests/test_parser_excel.py` (기존 테스트 유지 — `_fetch_rag_chunks` monkeypatch 그대로 동작) + in-process 스모크 추가

**Interfaces:**
- Consumes: `excel_parser_rag.backends.get_backend`, `excel_parser_rag.config.ParserConfig`
- Produces: `parse(file_bytes, filename)` — `excel_url` 파라미터 제거(하위호환: `**_` 로 무시). env `EXCEL_PARSER_BACKEND`(기본 auto), `KORDOC_BIN`(기본 kordoc), `KORDOC_MD_OUT`.

- [x] **Step 1: `_fetch_rag_chunks` 를 in-process 로 교체**

원본 `/Users/xxx/workspace/7.excel-parser/service/main.py:_run_parse`(110~135행 부근) 의 동기 파싱 본문을 이식:
```python
def _fetch_rag_chunks(file_bytes: bytes, filename: str, excel_url: str | None = None) -> list[dict]:
    """in-process: excel_parser_rag backend 직접 호출 (HTTP 제거)."""
    import os
    import tempfile
    from pathlib import Path

    from parse_service.parsers.excel.excel_parser_rag.backends import get_backend
    from parse_service.parsers.excel.excel_parser_rag.config import ParserConfig

    suffix = Path(filename).suffix.lower() or ".xlsx"
    cfg_kwargs = {"backend": os.environ.get("EXCEL_PARSER_BACKEND", "auto")}
    if os.environ.get("KORDOC_BIN"):
        cfg_kwargs["kordoc_bin"] = os.environ["KORDOC_BIN"]
    if os.environ.get("KORDOC_MD_OUT"):
        cfg_kwargs["kordoc_md_out"] = os.environ["KORDOC_MD_OUT"]
    config = ParserConfig(**cfg_kwargs)
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False, prefix="excel_parser_") as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name
    try:
        chunks, stats = get_backend(config.backend).parse(tmp_path, config)
        return [c if isinstance(c, dict) else c.__dict__ for c in (chunks or [])]
    finally:
        os.unlink(tmp_path)
```
**구현 주의**: `ParserConfig` 필드명은 구현 시
`grep -nE 'backend|kordoc' parse_service/parsers/excel/excel_parser_rag/config.py | head`
로 실명 확인 후 맞춘다(원본 service/main.py 가 `data.setdefault("backend", ...)` /
`kordoc_bin`/`kordoc_md_out` 키를 쓰는 것은 확인됨). chunks 원소가 dataclass 면
원본 service/main.py 의 직렬화 방식을 그대로 복사.

- [x] **Step 2: 스모크 테스트 추가 + 전체 확인**

`parse_service/tests/test_parser_excel.py` 에 추가:
```python
def test_inprocess_openpyxl_smoke(tmp_path):
    """실제 openpyxl 백엔드로 초소형 xlsx 를 파싱(파일시스템/kordoc 무관 백엔드)."""
    import openpyxl, io, os
    os.environ["EXCEL_PARSER_BACKEND"] = "openpyxl"
    wb = openpyxl.Workbook(); ws = wb.active
    ws["A1"], ws["B1"], ws["A2"], ws["B2"] = "이름", "값", "가", 1
    buf = io.BytesIO(); wb.save(buf)
    from parse_service.parsers import excel as excel_parser
    res = excel_parser.parse(buf.getvalue(), "t.xlsx")
    assert res.chunk_needed is False and res.chunks
    os.environ.pop("EXCEL_PARSER_BACKEND")
```
Run: `.venv-kb/bin/python -m pytest parse_service/tests/test_parser_excel.py -q`
Expected: 전체 passed (기존 monkeypatch 테스트 + 스모크)

- [x] **Step 3: 시그니처 정리**

`parse_service/parsers/excel/__init__.py` 의 `parse` 시그니처를
`def parse(file_bytes: bytes, filename: str, *, excel_url: str | None = None) -> RouteResult:`
로 변경(키워드 optional — Task 9 스모크 테스트가 excel_url 없이 호출).
`parse_service/router.py` 의 `_excel_parse` 는 그대로(excel_url 전달해도 무해).
`parse_service/app.py` 의 `KBP_EXCEL_URL` env 읽기는 이 시점부터 미사용 — 제거는 2e compose 정리와 함께.

- [x] **Step 4: 전체 스위트 + 스택 검증**

```bash
.venv-kb/bin/python -m pytest parse_service/tests service/tests tests -q
docker compose build parse-svc && docker compose up -d --wait
curl -sS -X POST http://localhost:19000/parse -F "file=@<xlsx 샘플>" | python3 -c "import sys,json;d=json.load(sys.stdin);assert d['chunk_needed'] is False"
```
Expected: green. 이 시점부터 excel-parser 컨테이너를 꺼도 excel 파싱 동작(확인: `docker compose stop excel-parser` 후 재시도 → 성공 → `docker compose start excel-parser` 원복).

- [x] **Step 5: Commit**

```bash
git add parse_service/parsers/excel/__init__.py parse_service/router.py parse_service/tests/test_parser_excel.py
git commit -m "feat(parse-svc): excel parsing in-process via vendored excel_parser_rag (Phase 2b-2)"
```

---

# Phase 2c — document-parser OCR(pptx+이미지) in-process 흡수

### Task 10: OCR 모듈 이식 (vl_api / elements_parser / image_utils / pdf_converter / prompts)

**Files:**
- Create: `parse_service/parsers/ocr/vl_api.py` ← `model/vision_language_model.py` (call_vl_api_with_base64 경로만: `_build_payload`/`_apply_guided_json`/`_request_vl_api`/`_extract_result`/`OCR_JSON_SCHEMA`/http client. multimodal 함수 제외)
- Create: `parse_service/parsers/ocr/elements_parser.py` ← `pipeline/document_processor.py` (`parse_vision_language_response_to_elements`, `normalize_all_elements`, `normalize_element_content`)
- Create: `parse_service/parsers/ocr/image_utils.py` ← `utils/image.py` + `pipeline/handlers/image_handler.py:image_file_to_base64_list`
- Create: `parse_service/parsers/ocr/pdf_converter.py` ← `converter/pdf_utilities.py`(convert_to_pdf_bytes, is_convertible_to_pdf) + `pipeline/handlers/pdf_handler.py`(pdf_bytes_to_base64_list) + `converter/safe_fitz.py`(필요 함수)
- Create: `parse_service/parsers/ocr/prompts.py` ← `core/config/prompts.py` (SYSTEM/USER 빌더)
- Modify: `requirements.txt` — `PyMuPDF>=1.23.0`, `Pillow>=10.0.0` 추가(중복 시 skip)
- Test: `parse_service/tests/test_ocr_modules.py`

원본 루트: `/Users/xxx/workspace/99.projects/jiju_chaekmu/sourceCode/document-parser-backend-src/`

**Interfaces (이식 후 시그니처 — 원본과 동일 유지):**
- `vl_api.call_vl_api_with_base64(base64_image: str, user_prompt: str, system_prompt: str) -> tuple[str, float]`
- `elements_parser.parse_vision_language_response_to_elements(vl_response: str, page_number: int, start_id: int) -> tuple[list[dict], int]`
- `elements_parser.normalize_all_elements(elements: list[dict]) -> list[dict]`
- `image_utils.image_file_to_base64_list(file_path: str, page_range=None) -> list[str]`
- `pdf_converter.convert_to_pdf_bytes(file_path: str, gotenberg_url: str, libreoffice_options=None) -> tuple[bytes, bool, str]`
- `pdf_converter.pdf_bytes_to_base64_list(pdf_bytes: bytes, **kw) -> list[str]`

**이식 규칙(모든 파일 공통):**
1. 원본 파일에서 위 함수 + 그 내부 의존 함수만 복사. 원본의 `from core.context import get_config_value` / `get_config()` 참조는 **env 직독으로 치환**: `MODEL_API_URL`, `MODEL_API_KEY`, `MODEL_NAME`(기본 원본 config 의 default), `VL_MAX_TOKENS`(기본 2000), `USE_GUIDED_JSON`(기본 "1"), `VL_MODEL_TIMEOUT`(기본 600).
2. redis import(`distributed_semaphore`) 나오면 그 블록 삭제 — 동시성은 `asyncio.Semaphore(int(os.environ.get("KBP_VL_MAX_CONCURRENT", "3")))` 모듈 전역으로 대체.
3. `infrastructure/storage`(minio) import 나오면 삭제(저장은 parse-svc 기존 `_render_and_upload` 가 담당).
4. 각 파일 복사 후 `python -c "import parse_service.parsers.ocr.<mod>"` 로 import 오류 0 확인.

- [x] **Step 1: 실패 테스트 작성**

`parse_service/tests/test_ocr_modules.py`:
```python
"""이식된 OCR 모듈 — VL 응답 파싱과 payload 빌드가 원본 계약대로 동작."""
import json


def test_parse_vl_response_to_elements():
    from parse_service.parsers.ocr.elements_parser import (
        parse_vision_language_response_to_elements)
    vl = json.dumps({"elements": [
        {"category": "table", "content": {"html": "<table><tr><td>x</td></tr></table>",
                                          "markdown": "", "text": ""},
         "id": 0, "page": 1}]})
    els, next_id = parse_vision_language_response_to_elements(vl, page_number=3, start_id=7)
    assert els[0]["category"] == "table"
    assert els[0]["page"] == 3 and els[0]["id"] == 7 and next_id == 8


def test_parse_vl_response_fallback_figure():
    from parse_service.parsers.ocr.elements_parser import (
        parse_vision_language_response_to_elements)
    els, _ = parse_vision_language_response_to_elements("not-json at all", 1, 0)
    assert els[0]["category"] == "figure"
    assert els[0]["content"]["markdown"] == "not-json at all"


def test_vl_payload_contains_image_and_schema(monkeypatch):
    monkeypatch.setenv("MODEL_API_URL", "http://vl.example/v1/chat/completions")
    monkeypatch.setenv("MODEL_NAME", "test-vl")
    from parse_service.parsers.ocr import vl_api
    payload = vl_api._build_payload("QUJD", "user-p", "sys-p")
    text = json.dumps(payload)
    assert "data:image/jpeg;base64,QUJD" in text and "sys-p" in text
```

- [x] **Step 2: 실패 확인**

Run: `.venv-kb/bin/python -m pytest parse_service/tests/test_ocr_modules.py -q`
Expected: FAIL (모듈 없음)

- [x] **Step 3: 이식 수행**

위 "이식 규칙"대로 5개 파일 생성. 파일별 복사 원천(함수 단위):
```
vl_api.py          ← model/vision_language_model.py:41-71(OCR_JSON_SCHEMA),76-110(timeout/client),122-139(call_vl_api_with_base64),142-172(_apply_guided_json),175-210(_build_payload),213-222(_extract_result),330-427(_request_vl_api)
elements_parser.py ← pipeline/document_processor.py:16-114,117-140(normalize_element_content),288-299(normalize_all_elements)
image_utils.py     ← utils/image.py 전체 중 image_to_base64/multipage_image_to_base64/get_image_page_count/compress_image_bytes + handlers/image_handler.py:31-56
pdf_converter.py   ← converter/pdf_utilities.py:convert_to_pdf_bytes,is_convertible_to_pdf + handlers/pdf_handler.py:pdf_bytes_to_base64_list(+내부 의존) + safe_fitz 의 렌더 유틸(해당 함수가 참조하는 것만)
prompts.py         ← core/config/prompts.py 의 build_system_prompt/build_user_prompt 와 PROMPT_* 상수
```
각 파일 끝에서 `python -c import` 확인.

- [x] **Step 4: 통과 확인 + Commit**

Run: `.venv-kb/bin/python -m pytest parse_service/tests/test_ocr_modules.py -q` → passed
```bash
git add parse_service/parsers/ocr/ requirements.txt parse_service/tests/test_ocr_modules.py
git commit -m "feat(parse-svc): vendor document-parser VL OCR modules (pptx+image path) (Phase 2c-1)"
```

### Task 11: `ocr_file_to_elements` 진입 함수 + HTTP 제거

**Files:**
- Modify: `parse_service/parsers/ocr/__init__.py` (`_whole_file_elements` 교체 + 진입 함수)
- Modify: `parse_service/parsers/pdf/__init__.py` (`_ocr_elements_for_page` 교체)
- Test: `parse_service/tests/test_ocr_entry.py`

**Interfaces:**
- Produces: `async ocr_file_to_elements(file_bytes: bytes, filename: str) -> dict` — `{"elements":[...], "metadata":{"page_cnt": int}}`. 내부: pptx→`convert_to_pdf_bytes`(gotenberg)→`pdf_bytes_to_base64_list`; 이미지→`image_file_to_base64_list`; 페이지별 `call_vl_api_with_base64`→`parse_vision_language_response_to_elements`→`normalize_all_elements`. 페이지 실패 비치명(skip).
- Produces(동기 래퍼): `ocr_elements_sync(file_bytes, filename) -> list[dict]` — `asyncio.run` 래핑, 기존 `_ocr_page` 호출부 대체용. element 는 `page`(1-based) 필드 포함 — `elements_to_blocks` 소비 규약: `item.get("page_idx", item.get("page", 0))` 이므로 `page` 를 **0-based 로 변환**해 넘기거나 `page_idx` 를 직접 채운다. **구현: 반환 직전 각 element 에 `el["page_idx"] = el["page"] - 1` 세팅**(기존 HTTP 응답과 동일 소비 결과 보장 — 기존 `_elements_to_pages` 가 +1 하므로).

- [x] **Step 1: 실패 테스트 작성**

`parse_service/tests/test_ocr_entry.py`:
```python
"""ocr_file_to_elements — 이미지/pptx 를 in-process 로 VL OCR."""
import json
import pytest
from parse_service.parsers import ocr as ocr_parser


@pytest.fixture
def fake_vl(monkeypatch):
    async def fake_call(base64_image, user_prompt, system_prompt):
        return json.dumps({"elements": [
            {"category": "figure",
             "content": {"html": "", "markdown": "hello", "text": ""},
             "id": 0, "page": 1}]}), 0.1
    monkeypatch.setattr("parse_service.parsers.ocr.vl_api.call_vl_api_with_base64",
                        fake_call)


def test_image_ocr_inprocess(monkeypatch, fake_vl):
    monkeypatch.setattr("parse_service.parsers.ocr.image_utils.image_file_to_base64_list",
                        lambda path, page_range=None: ["QUJD"])
    import asyncio
    res = asyncio.run(ocr_parser.ocr_file_to_elements(b"\x89PNG-fake", "img.png"))
    assert res["metadata"]["page_cnt"] == 1
    assert res["elements"][0]["content"]["markdown"] == "hello"
    assert res["elements"][0]["page_idx"] == 0  # elements_to_blocks 규약


def test_parse_uses_inprocess(monkeypatch, fake_vl):
    monkeypatch.setattr("parse_service.parsers.ocr.image_utils.image_file_to_base64_list",
                        lambda path, page_range=None: ["QUJD"])
    res = ocr_parser.parse(b"\x89PNG-fake", "img.png")   # ocr_url 파라미터 없이 동작
    assert res.kind == "pages" and res.pages[0]["page_number"] == 1
```

- [x] **Step 2: 실패 확인**

Run: `.venv-kb/bin/python -m pytest parse_service/tests/test_ocr_entry.py -q`
Expected: FAIL

- [x] **Step 3: 구현**

`parse_service/parsers/ocr/__init__.py` 에 추가/교체:
```python
import asyncio
import os
import tempfile

from parse_service.parsers import RouteResult, ParserError

IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff", "webp"}
_VL_SEM: asyncio.Semaphore | None = None


def _sem() -> asyncio.Semaphore:
    global _VL_SEM
    if _VL_SEM is None:
        _VL_SEM = asyncio.Semaphore(int(os.environ.get("KBP_VL_MAX_CONCURRENT", "3")))
    return _VL_SEM


async def _file_to_base64_pages(file_bytes: bytes, filename: str) -> list[str]:
    from parse_service.parsers.ocr import image_utils, pdf_converter
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    suffix = "." + ext if ext else ""
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(file_bytes)
        path = tmp.name
    try:
        if ext in IMAGE_EXTS:
            return image_utils.image_file_to_base64_list(path)
        # pptx (및 pdf 변환 가능 office): gotenberg → PDF → 페이지 base64
        gotenberg = os.environ.get("GOTENBERG_URL", "http://localhost:3000")
        pdf_bytes, ok, _name = pdf_converter.convert_to_pdf_bytes(path, gotenberg)
        if not ok:
            raise ParserError(f"gotenberg conversion failed for {filename}")
        return pdf_converter.pdf_bytes_to_base64_list(pdf_bytes)
    finally:
        os.unlink(path)


async def ocr_file_to_elements(file_bytes: bytes, filename: str) -> dict:
    from parse_service.parsers.ocr import vl_api, elements_parser, prompts
    b64_pages = await _file_to_base64_pages(file_bytes, filename)
    system_p, user_p = prompts.build_system_prompt(), prompts.build_user_prompt()
    all_elements: list[dict] = []
    next_id = 0
    for page_num, b64 in enumerate(b64_pages, start=1):
        try:
            async with _sem():
                vl_resp, _t = await vl_api.call_vl_api_with_base64(b64, user_p, system_p)
            els, next_id = elements_parser.parse_vision_language_response_to_elements(
                vl_resp, page_num, next_id)
            all_elements.extend(els)
        except Exception:  # noqa: BLE001 — 페이지 실패 비치명
            import logging
            logging.getLogger(__name__).exception("VL OCR failed page %d", page_num)
    elements_parser.normalize_all_elements(all_elements)
    for el in all_elements:
        el["page_idx"] = int(el.get("page", 1)) - 1  # elements_to_blocks 규약(0-based)
    return {"elements": all_elements, "metadata": {"page_cnt": len(b64_pages)}}


def ocr_elements_sync(file_bytes: bytes, filename: str) -> list[dict]:
    # v2(리뷰 B6): parse-svc /parse 핸들러는 async def 라 이벤트루프가 도는 스레드에서
    # 호출된다 — 그 안에서 asyncio.run() 은 RuntimeError. 루프가 돌고 있으면 별도
    # 스레드에서 asyncio.run 을 실행해 안전하게 블로킹한다.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(ocr_file_to_elements(file_bytes, filename))["elements"]
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(lambda: asyncio.run(ocr_file_to_elements(file_bytes, filename)))
        return fut.result()["elements"]


def _whole_file_elements(file_bytes: bytes, filename: str, ocr_url: str | None = None) -> list[dict]:
    return ocr_elements_sync(file_bytes, filename)


def parse(file_bytes: bytes, filename: str, *, ocr_url: str | None = None) -> RouteResult:
    # Task 3 에서 작성한 본문 그대로(monkeypatch 지점 _whole_file_elements 만 in-process 로 바뀜):
    try:
        elements = _whole_file_elements(file_bytes, filename, ocr_url)
    except Exception as e:  # noqa: BLE001
        raise ParserError(f"ocr failed for {filename}: {e}") from e
    if not elements:
        raise ParserError(f"ocr/vlm empty for {filename}")
    return RouteResult(kind="pages", chunk_needed=True, pages=_elements_to_pages(elements))
```
(`_elements_to_pages` 는 Task 3 정의 그대로 유지.)
`parsers/pdf/__init__.py` 의 `_ocr_elements_for_page` 교체:
```python
def _ocr_elements_for_page(jpeg: bytes, name: str, ocr_url: str | None = None) -> list[dict]:
    from parse_service.parsers.ocr import ocr_elements_sync
    return ocr_elements_sync(jpeg, name)
```
`router.py` 의 `ocr_url` 전달은 유지(파서가 무시) — env 정리는 2e.

- [x] **Step 4: 통과 + 전체 회귀 + 스택 검증**

```bash
.venv-kb/bin/python -m pytest parse_service/tests service/tests tests -q
docker compose build parse-svc && docker compose up -d --wait
# pptx/이미지 실파일 스모크(원격 VL 필요 — MODEL_API_URL 설정 상태에서):
curl -sS -X POST http://localhost:19000/parse -F "file=@<png 샘플>" | python3 -c "import sys,json;d=json.load(sys.stdin);assert d['chunk_needed'] is True and d['enriched_content']"
docker compose stop document-parser   # 끄고도 pptx/이미지 파싱 되는지
curl -sS -X POST http://localhost:19000/parse -F "file=@<png 샘플>" -o /dev/null -w "%{http_code}\n"
docker compose start document-parser  # 원복(제거는 2e)
```
Expected: 전부 green, document-parser off 상태에서도 200.

- [x] **Step 5: Commit**

```bash
git add parse_service/parsers/ocr/__init__.py parse_service/parsers/pdf/__init__.py parse_service/tests/test_ocr_entry.py
git commit -m "feat(parse-svc): in-process VL OCR entry, drop :18050 HTTP dependency (Phase 2c-2)"
```

---

# Phase 2d — markitdown 완전 제거 + docx/폴백=kordoc + facade 파싱 제거

### Task 12: `tools/kordoc.py` + `parsers/docx/` + 폴백 교체

**Files:**
- Create: `parse_service/tools/kordoc.py`
- Create: `parse_service/parsers/docx/__init__.py`
- Modify: `parse_service/router.py` (docx→docx파서, fallback→docx파서, markitdown 폴백 삭제)
- Test: `parse_service/tests/test_tools_kordoc.py`, `parse_service/tests/test_parser_docx.py`
- Modify: `parse_service/tests/test_router.py` (매핑 갱신: docx→docx, hwpx→docx(fallback))

**Interfaces:**
- Produces: `tools.kordoc.convert_to_markdown(file_bytes: bytes, filename: str) -> str` — kordoc CLI(`kordoc <src> --output out.md --format markdown`, env `KORDOC_BIN` 기본 "kordoc") 실행, `<table>` HTML 포함 md 반환. 실패 시 `ToolError`.
- Produces: `parsers.docx.parse(file_bytes, filename, **_) -> RouteResult` — kind="pages", chunk_needed=True, 단일 페이지 없음 → md 를 `hybrid_to_blocks(md, page_idx=1)` 로 1페이지 구성(docx 는 페이지 개념 근사).

- [x] **Step 1: 실패 테스트 작성**

`parse_service/tests/test_tools_kordoc.py`:
```python
"""tools/kordoc — CLI 래퍼: out.md 생성 확인, 실패 시 ToolError."""
import pytest
from parse_service.tools import ToolError
from parse_service.tools import kordoc


def test_cli_invocation_and_output(monkeypatch, tmp_path):
    def fake_run(cmd, **kw):
        # cmd = [bin, src, "--output", out, "--format", "markdown"]
        out = cmd[cmd.index("--output") + 1]
        with open(out, "w", encoding="utf-8") as f:
            f.write("# t\n<table><tr><td rowspan=\"2\">a</td></tr></table>")
        class R: returncode, stdout, stderr = 0, "", ""
        return R()
    monkeypatch.setattr(kordoc.subprocess, "run", fake_run)
    md = kordoc.convert_to_markdown(b"PK-docx", "a.docx")
    assert "<table>" in md and "rowspan" in md


def test_no_output_raises(monkeypatch):
    def fake_run(cmd, **kw):
        class R: returncode, stdout, stderr = 1, "", "boom"
        return R()
    monkeypatch.setattr(kordoc.subprocess, "run", fake_run)
    with pytest.raises(ToolError):
        kordoc.convert_to_markdown(b"PK", "a.docx")
```

`parse_service/tests/test_parser_docx.py`:
```python
from parse_service.parsers import docx as docx_parser


def test_docx_md_to_single_page(monkeypatch):
    monkeypatch.setattr(docx_parser, "_to_markdown",
                        lambda fb, fn: "# 제목\n\n본문 텍스트")
    res = docx_parser.parse(b"PK", "a.docx")
    assert res.kind == "pages" and res.chunk_needed is True
    assert res.pages[0]["page_number"] == 1
    assert any(b.get("text") for b in res.pages[0]["blocks"])
```

- [x] **Step 2: 실패 확인**

Run: `.venv-kb/bin/python -m pytest parse_service/tests/test_tools_kordoc.py parse_service/tests/test_parser_docx.py -q`
Expected: FAIL

- [x] **Step 3: 구현**

`parse_service/tools/kordoc.py`:
```python
"""kordoc CLI 도구 — docx(네이티브)/폴백 포맷 → markdown(+<table> HTML).

호출 계약(참조 구현: excel-parser-markitdown/compare/adapters/kordoc_adapter.py):
    kordoc <src> --output <out.md> --format markdown
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile

from parse_service.tools import ToolError


def _safe_basename(name: str) -> str:
    base = os.path.basename((name or "").replace("\\", "/")).replace("\x00", "")
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base) or "upload"
    return "_" + base if base.startswith(".") else base


def convert_to_markdown(file_bytes: bytes, filename: str, *, timeout: float = 600.0) -> str:
    binp = os.environ.get("KORDOC_BIN", "kordoc")
    if not (shutil.which(binp) or os.path.exists(binp)):
        raise ToolError(f"kordoc binary not found: {binp}")
    with tempfile.TemporaryDirectory(prefix="kordoc_") as tmp:
        src = os.path.join(tmp, _safe_basename(filename))
        with open(src, "wb") as fh:
            fh.write(file_bytes)
        out = os.path.join(tmp, "out.md")
        p = subprocess.run([binp, src, "--output", out, "--format", "markdown"],
                           capture_output=True, text=True, timeout=timeout)
        if not os.path.exists(out):
            msg = (p.stderr or p.stdout or "kordoc produced no output").strip()
            raise ToolError(msg[:600])
        with open(out, encoding="utf-8", errors="replace") as fh:
            return fh.read()
```

`parse_service/parsers/docx/__init__.py`:
```python
"""DOCX 도메인 파서(+미지 확장자 폴백) — kordoc 네이티브. 병합표 <table> 보존."""
from __future__ import annotations

from parse_service.parsers import RouteResult, ParserError
from parse_service.tools import ToolError
from parse_service.tools.kordoc import convert_to_markdown


def _to_markdown(file_bytes: bytes, filename: str) -> str:
    return convert_to_markdown(file_bytes, filename)


def parse(file_bytes: bytes, filename: str, **_) -> RouteResult:
    from kb_pipeline.blockify import hybrid_to_blocks
    try:
        md = _to_markdown(file_bytes, filename)
    except ToolError as e:
        raise ParserError(str(e)) from e
    if not (md or "").strip():
        raise ParserError(f"kordoc produced empty markdown for {filename}")
    return RouteResult(kind="pages", chunk_needed=True,
                       pages=[{"page_number": 1,
                               "blocks": hybrid_to_blocks(md, page_idx=1)}])
```

`parse_service/router.py` 갱신:
```python
from parse_service.parsers import docx as _docx
# _fallback_parse 함수 삭제(markitdown 폴백 제거)
def _docx_parse(fb, fn, **kw):
    return _docx.parse(fb, fn)
_PARSERS = {"pdf": _pdf_parse, "excel": _excel_parse, "ocr": _ocr_parse,
            "docx": _docx_parse, "fallback": _docx_parse}   # 폴백 = kordoc
def _domain(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext == "pdf":
        return "pdf"
    if ext in _excel.EXCEL_EXTS:
        return "excel"
    if ext == "docx":
        return "docx"
    if ext in ({"pptx"} | _ocr.IMAGE_EXTS):
        return "ocr"
    return "fallback"
```
`test_router.py` 매핑 갱신: `("a.docx","docx")`, `("a.hwpx","fallback")` 등.

- [x] **Step 4: 통과 확인 + Commit**

Run: `.venv-kb/bin/python -m pytest parse_service/tests -q` → passed
```bash
git add parse_service/tools/kordoc.py parse_service/parsers/docx/ parse_service/router.py parse_service/tests/
git commit -m "feat(parse-svc): docx=kordoc, fallback=kordoc, drop markitdown fallback (Phase 2d-1)"
```

### Task 13: markitdown 완전 제거 (코드+패키지) + blockify 라우팅 정리

**Files:**
- Modify: `parse_service/parsing.py` — `_parse_markitdown`/`parse_to_markdown`/`parse_to_pages`/`_parse_structural` 등 router 로 대체된 함수 삭제. **남길 것**: `_ocr_markdown`·`_ocr_page`(pdf 파서가 2c 이후 미사용이면 함께 삭제 — grep 으로 참조 0 확인 후), `_safe_basename`. 참조 0 이면 파일 자체 삭제하고 `_safe_basename` 은 `parse_service/tools/__init__.py` 로 이동.
- Modify: `kb_pipeline/blockify.py` — `PARSER_ROUTING`/`recommended_parser` 삭제(라우팅은 parse-svc router 소유). W6 측정 주석은 유지(역사 기록) 하되 "라우팅은 parse_service/router.py 로 이동(2026-07-02)" 각주 추가.
- Modify: `requirements.txt` — `markitdown>=0.0.2` 라인 삭제.
- Modify: `tests/test_blockify.py` — `recommended_parser`/`PARSER_ROUTING` import·테스트 제거(195행 부근).
- Test(가드): `parse_service/tests/test_no_markitdown.py`

- [x] **Step 1: 가드 테스트 작성**

`parse_service/tests/test_no_markitdown.py`:
```python
"""markitdown 이 코드베이스에서 완전히 제거됐다(재유입 가드)."""
import subprocess


def test_no_markitdown_imports():
    # v2(리뷰 B8): Task 13 시점 범위 = parse_service + kb_pipeline.
    # (service/ 는 Task 14 에서 parsing.py 삭제 후 이 리스트에 "service" 를 추가한다.)
    r = subprocess.run(
        ["grep", "-rlE", "^(from|import) markitdown|from markitdown import",
         "parse_service", "kb_pipeline"],
        capture_output=True, text=True, cwd="/Users/xxx/workspace/8.kb-pipeline")
    assert r.stdout.strip() == "", f"markitdown imports remain: {r.stdout}"


def test_no_markitdown_in_requirements():
    txt = open("/Users/xxx/workspace/8.kb-pipeline/requirements.txt").read()
    assert "markitdown" not in txt
```

- [x] **Step 2: 삭제 수행 + 참조 0 확인**

```bash
grep -rnE 'parse_to_markdown|parse_to_pages|_parse_markitdown|_parse_structural|recommended_parser|PARSER_ROUTING' \
  service parse_service kb_pipeline tests --include='*.py' | grep -v test_
```
결과의 각 참조를 라우터/삭제로 정리. `service/` 쪽 참조(`service/parsing.py`, `service/ingest.py`)는 Task 14 에서 삭제하므로 이 시점엔 남아 있어도 됨 — **이 Task 의 grep 통과 기준은 `parse_service/`+`kb_pipeline/` 범위**.

- [x] **Step 3: 통과 확인 + Commit**

Run: `.venv-kb/bin/python -m pytest parse_service/tests tests -q` → passed
(주: `test_no_markitdown.py` 의 service 범위 단언은 Task 14 완료 후 green — Task 14 와 같은 phase 안에서 연속 실행)
```bash
git add -A parse_service kb_pipeline requirements.txt tests
git commit -m "refactor: remove markitdown package/rout — parsing owned by parse-svc router (Phase 2d-2)"
```

### Task 14: facade 파싱 로직 삭제 + `/ingest/submit`·`/ingest/status` 제거

**Files:**
- Delete: `service/parsing.py`, `service/excel_parser_client.py`
- Modify: `service/ingest.py` — `run_front`/`FrontError` 및 `service.parsing` import 삭제(파일 내 남는 것이 `_TENANT_ID` 뿐이면 상수를 `service/app.py` 로 이동 후 파일 삭제)
- Modify: `service/app.py` — `/ingest/submit`(286~311행)·`/ingest/status`(314~326행) 핸들러 삭제, `from service.parsing import ...`/`from service.ingest import run_front, FrontError`/`from service.excel_parser_client import ...`/`get_excel_client` 삭제. `_safe_basename` 은 아래 로컬 정의로 대체(v2 리뷰 B9 — 명시 코드):
```python
def _safe_basename(name: str) -> str:
    import os
    import re
    base = os.path.basename((name or "").replace("\\", "/")).replace("\x00", "")
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base) or "upload"
    return "_" + base if base.startswith(".") else base
```
- Modify: `Dockerfile.facade` — JRE(openjdk-21) 설치 라인 삭제(파싱 제거로 java 불필요).
- Delete/Modify tests: `service/tests/test_parsing.py` 삭제, `service/tests/test_app.py` 의 `/ingest/submit`·`/ingest/status` 테스트 삭제, `service/tests/test_ingest.py`(run_front 테스트) 삭제.

**사전 확인(계약 안전)**: kb-backend 실코드 미사용 재확인 —
`grep -rn "ingest/submit\|ingest/status" /Users/xxx/workspace/99.projects/shinhan_trust/knowledge_base/backend/app` → 0건이어야 진행.

- [x] **Step 1: 삭제 수행**

```bash
git rm service/parsing.py service/excel_parser_client.py service/tests/test_parsing.py
# app.py/ingest.py 수정은 에디터로 (위 명세)
```

- [x] **Step 2: import/참조 0 확인 + markitdown 가드 범위 확장**

```bash
grep -rnE 'service\.parsing|excel_parser_client|run_front|FrontError|ingest/submit|ingest/status|_is_excel|get_excel_client' service --include='*.py'
```
Expected: 0건 (테스트 포함)

`parse_service/tests/test_no_markitdown.py` 의 grep 대상 리스트에 `"service"` 를 추가
(v2 리뷰 B8 — Task 13 에서 예고한 범위 확장):
```python
         "parse_service", "kb_pipeline", "service"],
```

- [x] **Step 3: 전체 스위트 확인 + Commit**

Run: `.venv-kb/bin/python -m pytest service/tests parse_service/tests tests -q` → 전체 passed
```bash
git add -A service Dockerfile.facade
git commit -m "refactor(facade)!: remove parsing logic + /ingest/submit,/ingest/status (parse-svc owns parsing) (Phase 2d-3)"
```

---

# Phase 2e — compose/Dockerfile 정리 + E2E

### Task 15: parse-svc Dockerfile 런타임 (node/kordoc + fitz)

**Files:**
- Modify: `Dockerfile.parse-svc`

- [ ] **Step 1: Dockerfile 갱신**

기존 (python3.12-slim + openjdk-21-jre) 에 추가:
```dockerfile
# node + kordoc: docx/폴백 파서 + excel_parser_rag kordoc 백엔드
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm \
    && rm -rf /var/lib/apt/lists/* && npm install -g kordoc
ENV KORDOC_BIN=kordoc KORDOC_MD_OUT=/tmp/kordoc_md_out EXCEL_PARSER_BACKEND=auto
RUN mkdir -p /tmp/kordoc_md_out
```
(PyMuPDF/Pillow 는 requirements.txt 경유 — 별도 apt 불필요. markitdown 은 이미 requirements 에서 제거됨.)

- [ ] **Step 2: 빌드 확인**

Run: `docker compose build parse-svc 2>&1 | tail -5`
Expected: Built. 컨테이너에서 `docker compose run --rm parse-svc sh -c "kordoc --version && java -version && python -c 'import fitz'"` 성공.

- [ ] **Step 3: Commit**

```bash
git add Dockerfile.parse-svc
git commit -m "build(parse-svc): node/kordoc runtime for docx+excel backends (Phase 2e-1)"
```

### Task 16: compose 정리 — excel-parser·document-parser·redis 제거

**Files:**
- Modify: `docker-compose.yml`
- Modify: `.env.example` (불필요 키 정리)

- [ ] **Step 1: compose 수정**

1. `excel-parser`, `document-parser`, `redis` 서비스 블록 삭제. `volumes:` 의 `redis_data` 삭제.
2. `parse-svc` env: `KBP_OCR_URL`/`KBP_EXCEL_URL` 삭제, 추가 —
   `GOTENBERG_URL: http://gotenberg:3000`, `MODEL_API_URL: ${MODEL_API_URL}`, `MODEL_API_KEY: ${MODEL_API_KEY}`, `KBP_VL_MAX_CONCURRENT: "3"`. `depends_on` 을 `{gotenberg: {condition: service_healthy}, minio: {condition: service_healthy}}` 로 교체(excel-parser/document-parser 의존 삭제).
3. `facade` env: `KBP_EXCEL_URL`/`KBP_OCR_URL` 삭제.
4. `adaptive_chunk` 의 `ADAPTIVE_CHUNK_OCR_BASE_URL: http://document-parser:8000` —
   **[v2 확정, 리뷰 B4]** 소비처 확인 완료: `service/dependencies.py:89` 가
   `OcrParser(base_url=settings.ocr_base_url)` 로 실소비하나, 이는 adaptive 의
   **file-OCR 경로(`/chunk/jobs/file`) 전용**이고 kb-pipeline facade 는 **text 경로
   (`/chunk/jobs`)만 사용**한다(facade `service/adaptive_chunk.py` 참조). → **env 라인
   삭제** (adaptive 의 file-OCR 는 이 compose 에서 미사용/비활성 — 수용된 축소.
   추후 필요 시 별도 OCR 서비스 지정).
5. **[v2, 리뷰 B7]** 위 1~4는 **한 편집·한 커밋**으로 적용(부분 적용 금지 — depends_on 이
   삭제된 서비스를 가리키는 중간 상태 방지). 적용 후 `docker compose config --quiet` 로
   유효성 확인, 재기동은 `docker compose down && docker compose up -d --wait` (부분
   restart 금지).

- [ ] **Step 2: 재기동 + 전 서비스 healthy**

```bash
docker compose down && docker compose build && docker compose up -d --wait
docker compose ps   # excel-parser/document-parser/redis 부재 + 나머지 전부 healthy
```

- [ ] **Step 3: Commit**

```bash
git add docker-compose.yml .env.example
git commit -m "build(compose): remove excel-parser/document-parser/redis — parse-svc in-process (Phase 2e-2)"
```

### Task 17: E2E 회귀 (전 확장자) + 문서 반영

- [ ] **Step 1: E2E 스모크 — 확장자별 /ingest → /search**

테스트 파일: `/Users/xxx/workspace/excel-parser-markitdown/test_doc/` 의
`3-3. 휴가규정(...).pdf`(표+본문), `AI활용을 위한_문서 표준 가이드_....docx`(kordoc 병합표),
`신한자산신탁 AX플랫폼 구축 착수보고.pptx`(OCR), `순서도_예시.webp`(이미지), 소형 xlsx(자체청킹).
```bash
WS="phase2-e2e-$(date +%s)"
for f in <위 5개 경로>; do
  curl -sS -X POST http://localhost:19000/ingest -F "file=@$f" \
       -F "workspace_id=$WS" -F "doc_id=$(basename "$f")" --max-time 600
done
curl -sS -X POST http://localhost:19000/search -H 'Content-Type: application/json' \
     -d "{\"workspace_id\":\"$WS\",\"query\":\"연차 휴가는 며칠?\",\"top_k\":5}"
```
Expected: 5건 모두 `status: indexed`(xlsx 는 chunking_selection.method_selected="excel_rag_parser"), 검색 응답에 휴가규정 내용.
회귀 포인트: PDF 별표1 `<table>`+`rowspan` 보존(적재 청크에 포함), docx 병합표 `<table>` 존재.

- [ ] **Step 2: 문서 반영**

- `_workspace/01-architecture.md`: §3 파서 라우팅 표를 새 라우팅(pdf=ODL/xlsx=excel_rag/docx=kordoc/pptx·이미지=OCR/폴백=kordoc, markitdown 제거)으로 갱신, facade §의 excel lane 서술을 chunk_needed flag 로 갱신.
- `_workspace/02-changes.md`: Phase 2 파서 일원화 항목 추가(결정·실측 근거 링크).
- `_workspace/03-dev-progress.md`: Phase 2a~2e 완료 기록.
- `docs/kb-pipeline-process-definition.md`: §4.1 파싱 라우팅/§계약(`chunk_needed`)/`/ingest/submit` 제거 반영.
- `docs/kbp-docker-startup.md`: 서비스 표에서 excel-parser/document-parser/redis 제거.

- [ ] **Step 3: 최종 Commit**

```bash
git add _workspace docs
git commit -m "docs: parser consolidation Phase 2 — routing/flag/compose reflected (Phase 2e-3)"
```
