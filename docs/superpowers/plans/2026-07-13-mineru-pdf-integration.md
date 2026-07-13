<!-- plan-version: v2 -->
<!-- ultracode-validation: PENDING -->

# MinerU PDF 레인 통합 Implementation Plan

> **v2 변경(ultracode 검증 반영)**: (1) 스캔 페이지 하나라도 있으면 `parse_method='ocr'` 강제('auto' 유실 정정, Task 2). (2) env 경로 `scripts/parse-svc.env` 로 정정(Task 6·Global Constraints). (3) 게이트 호출 lazy+try/except 가드 → pymupdf 부재/triage 예외 시 ODL 폴백(Task 5). (4) MinerU 빈 결과도 폴백(Task 5). (5) do_parse 디스크 출력 계약 반영(Task 4).

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** parse-svc PDF 파서에 문서수준 게이트를 붙여, 순수 텍스트 PDF 는 기존 ODL 레인, 스캔/혼합/복잡 PDF 는 MinerU(hybrid: VLM 원격 + PaddleOCR 로컬) 레인으로 분기한다.

**Architecture:** `parse()` 진입 시 PyMuPDF triage(저비용 신호)로 문서수준 라우팅을 정한다. 게이트는 순수함수(`gate.decide_route`), MinerU 어댑터는 import 경계(`_invoke_mineru`) 하나만 실제 MinerU 를 부르고 나머지는 순수 매핑이라 로컬에서 fake 로 전부 단위검증된다. MinerU 산출 `content_list.json` 은 기존 `elements_to_blocks` 로 blocks 화(표 `<table>` 보존 재사용). MinerU 실패 시 기존 ODL/VL 레인으로 폴백 → 가용성 회귀 없음.

**Tech Stack:** Python 3.11(`.venv-kb`), PyMuPDF(`pymupdf`/fitz), MinerU(`hybrid-http-client`), PaddleOCR(PP-OCRv5, 로컬), pytest.

## Global Constraints

설계: `docs/superpowers/specs/2026-07-13-mineru-pdf-integration-design.md`. 아래 불변식은 모든 Task 에 암묵 포함(CLAUDE.md):

- **청크는 KB당 단일 우주** — 두 레인 모두 `blocks` 만 산출, 청킹은 facade `/chunk` 소유(`chunk_needed=True`). 별도 청크 생성 금지.
- **표는 `<table>` HTML 보존** — pipe 평탄화 금지. MinerU `table_body` HTML 원형 유지.
- **모달 마커 U+3008/U+3009** — blockify 경유(직접 생성 금지).
- **page_idx 1-based canonical** — MinerU 0-based `page_idx` → `page_number = page_idx + 1` 정규화(기존 OCR 경로와 동일).
- **in-process 일원화** — MinerU 는 라이브러리 import(외부 HTTP 서비스 신설 금지). 단 VLM 은 원격(설계 의도).
- **RouteResult 계약 불변** — `RouteResult(kind="pages", chunk_needed=True, pages=[{page_number, blocks}])`. 두 레인 동일.
- **비밀 커밋 금지** — `MINERU_VLM_SERVER_URL`/키는 gitignored `scripts/parse-svc.env` 에만(런처 `scripts/run-parse-svc.sh` 가 로드, `.gitignore` `scripts/*.env` 로 무시). `parse_service/parse-svc.env` 는 gitignore·로드 안 됨 — 쓰지 말 것.
- **배포서버 전제** — 로컬 dev(Intel Mac)는 MinerU/torch/paddle 구동 불가. 실 MinerU 경로(Task 4·8)는 배포서버 스택검증으로 분리, 로컬은 fake 로 단위검증.

## File Structure

- `parse_service/parsers/pdf/triage.py` — **Create**(feat/pdf-triage 에서 이식). `triage_document(pdf_bytes) -> list[PageSignals]`, `Bucket`. 저비용 신호만(부수효과 없음).
- `parse_service/parsers/pdf/gate.py` — **Create**. `decide_route(pdf_bytes) -> RouteDecision`. 순수(triage 출력 집계 → 레인+parse_method).
- `parse_service/parsers/pdf/mineru_lane.py` — **Create**. `run_mineru(...)` = `_invoke_mineru`(import 경계) → `_content_list_to_elements`(순수) → `elements_to_blocks`(재사용) → `_elements_to_pages`(순수).
- `parse_service/parsers/pdf/__init__.py` — **Modify**. 기존 parse 본문을 `_odl_lane` 로 추출, `parse()` 는 게이트 분기 + MinerU 폴백.
- `parse_service/tests/test_triage.py` — **Create**(이식). `parse_service/tests/test_pdf_gate.py`, `test_mineru_lane.py`, `test_parser_pdf_routing.py` — **Create**.
- `parse_service/parse-svc.env`(gitignored) — **Modify**. `MINERU_VLM_SERVER_URL` 등.
- `_workspace/01-architecture.md`·`02-changes.md`·`03-dev-progress.md` — **Modify**(완료 후 반영).

---

### Task 1: triage.py 이식 (게이트 신호원)

**Files:**
- Create: `parse_service/parsers/pdf/triage.py`
- Create(test): `parse_service/tests/test_triage.py`

**Interfaces:**
- Produces: `Bucket`(Enum: SKIP/TEXT_ONLY/OCR_NEEDED/LLM_NEEDED), `PageSignals`(dataclass, `.bucket`), `triage_document(pdf_bytes: bytes, **classify_kwargs) -> list[PageSignals]`, `extract_signals(page)`, `classify(sig, *, mixed_image_cov=0.25, content_min=300)`.

- [ ] **Step 1: 브랜치에서 triage.py + 테스트 이식**

```bash
cd /Users/xxx/workspace/8.kb-pipeline
git show feat/pdf-triage:parse_service/parsers/pdf/triage.py > parse_service/parsers/pdf/triage.py
git show feat/pdf-triage:parse_service/tests/test_triage.py > parse_service/tests/test_triage.py
```

- [ ] **Step 2: 이식 테스트 실행 — 통과 확인**

Run: `cd /Users/xxx/workspace/8.kb-pipeline && .venv-kb/bin/python -m pytest parse_service/tests/test_triage.py -q`
Expected: PASS (모든 triage 테스트). 실패 시 `pymupdf` 설치 확인: `.venv-kb/bin/python -c "import pymupdf"`.

- [ ] **Step 3: 커밋**

```bash
git add parse_service/parsers/pdf/triage.py parse_service/tests/test_triage.py
git commit -m "feat(parse-svc): triage(PyMuPDF 저비용 신호) 이식 — MinerU 게이트 신호원"
```

---

### Task 2: gate.py — 문서수준 라우팅 결정 (순수)

**Files:**
- Create: `parse_service/parsers/pdf/gate.py`
- Create(test): `parse_service/tests/test_pdf_gate.py`

**Interfaces:**
- Consumes: Task 1 `triage_document`, `Bucket`, `PageSignals`.
- Produces: `RouteDecision`(frozen dataclass: `lane: str`("odl"|"mineru"), `parse_method: str | None`("ocr"|"auto"|None)), `decide_route(pdf_bytes: bytes) -> RouteDecision`.

집계 규칙(spec §3.1·§4.3 정정): 비어있지 않은 버킷이 전부 `TEXT_ONLY`(또는 전부 SKIP/열기실패) → ODL. **`OCR_NEEDED` 가 하나라도 있으면 → MinerU `'ocr'` 강제**(스캔 페이지엔 네이티브 텍스트가 없어 'auto' 로 두면 문서수준 classify='txt' 시 유실). `OCR_NEEDED` 없이 `LLM_NEEDED` 만(스캔 없는 텍스트+이미지 혼합) → MinerU `'auto'`(유실 위험 없이 VLM 호출 최소화). **triage 예외(암호화/손상 페이지 반복 실패)는 삼켜 ODL 로**(가용성 회귀 방지).

- [ ] **Step 1: 실패 테스트 작성**

```python
# parse_service/tests/test_pdf_gate.py
import pytest
from parse_service.parsers.pdf import gate
from parse_service.parsers.pdf.triage import PageSignals, Bucket


def _sig(bucket):
    s = PageSignals(page_number=1, width=600, height=800)
    s.bucket = bucket
    return s


@pytest.mark.parametrize("buckets,lane,method", [
    ([Bucket.TEXT_ONLY, Bucket.TEXT_ONLY], "odl", None),       # 전부 텍스트
    ([Bucket.TEXT_ONLY, Bucket.SKIP], "odl", None),            # 텍스트+빈페이지
    ([Bucket.SKIP], "odl", None),                              # 전부 빈페이지
    ([], "odl", None),                                         # 열기 실패(빈 리스트)
    ([Bucket.OCR_NEEDED, Bucket.OCR_NEEDED], "mineru", "ocr"), # 순수 스캔
    ([Bucket.OCR_NEEDED, Bucket.SKIP], "mineru", "ocr"),       # 스캔+빈페이지
    ([Bucket.TEXT_ONLY, Bucket.OCR_NEEDED], "mineru", "ocr"),  # 혼합(텍스트+스캔)=ocr 강제(유실 방지)
    ([Bucket.OCR_NEEDED, Bucket.LLM_NEEDED], "mineru", "ocr"), # 스캔 포함=ocr 강제
    ([Bucket.LLM_NEEDED], "mineru", "auto"),                   # 스캔 없는 텍스트+이미지=auto 안전
    ([Bucket.TEXT_ONLY, Bucket.LLM_NEEDED], "mineru", "auto"), # 스캔 없는 혼합=auto
])
def test_decide_route(monkeypatch, buckets, lane, method):
    monkeypatch.setattr(gate, "triage_document", lambda b: [_sig(x) for x in buckets])
    d = gate.decide_route(b"%PDF")
    assert (d.lane, d.parse_method) == (lane, method)


def test_triage_exception_falls_back_to_odl(monkeypatch):
    def boom(b):
        raise RuntimeError("corrupt page iteration")
    monkeypatch.setattr(gate, "triage_document", boom)
    d = gate.decide_route(b"%PDF")
    assert (d.lane, d.parse_method) == ("odl", None)
```

- [ ] **Step 2: 실패 확인**

Run: `.venv-kb/bin/python -m pytest parse_service/tests/test_pdf_gate.py -q`
Expected: FAIL (`ModuleNotFoundError: parse_service.parsers.pdf.gate`).

- [ ] **Step 3: 구현**

```python
# parse_service/parsers/pdf/gate.py
"""PDF 문서수준 라우팅 — triage 버킷 집계로 ODL vs MinerU 레인 + parse_method 결정.

설계: docs/superpowers/specs/2026-07-13-mineru-pdf-integration-design.md §3.1
"""
from __future__ import annotations

from dataclasses import dataclass

from parse_service.parsers.pdf.triage import triage_document, Bucket


@dataclass(frozen=True)
class RouteDecision:
    lane: str                 # "odl" | "mineru"
    parse_method: str | None  # "ocr" | "auto" | None(=odl)


_ODL = RouteDecision(lane="odl", parse_method=None)


def decide_route(pdf_bytes: bytes) -> RouteDecision:
    try:
        sigs = triage_document(pdf_bytes)
    except Exception:  # noqa: BLE001 — triage 페이지반복 예외(암호화/손상)는 삼켜 ODL 로(가용성)
        return _ODL
    buckets = {s.bucket for s in sigs if s.bucket != Bucket.SKIP}
    # 비어있지 않은 페이지가 전부 순수 텍스트(또는 전부 빈/열기실패) → ODL 레인
    if not buckets or buckets == {Bucket.TEXT_ONLY}:
        return _ODL
    # 스캔 페이지(네이티브 텍스트 없음)가 하나라도 있으면 'ocr' 강제 —
    # 'auto'로 두면 MinerU 문서수준 classify='txt' 판정 시 그 스캔 텍스트 유실(2026-07-07 버그 재발).
    if Bucket.OCR_NEEDED in buckets:
        return RouteDecision(lane="mineru", parse_method="ocr")
    # OCR_NEEDED 없이 LLM_NEEDED 만(스캔 없는 텍스트+이미지) → 'auto' 안전(모든 페이지 네이티브 텍스트 보유)
    return RouteDecision(lane="mineru", parse_method="auto")
```

- [ ] **Step 4: 통과 확인**

Run: `.venv-kb/bin/python -m pytest parse_service/tests/test_pdf_gate.py -q`
Expected: PASS (10 파라미터 케이스 + triage 예외 폴백 1).

- [ ] **Step 5: 커밋**

```bash
git add parse_service/parsers/pdf/gate.py parse_service/tests/test_pdf_gate.py
git commit -m "feat(parse-svc): PDF 문서수준 게이트 — triage 버킷 집계로 ODL/MinerU 라우팅"
```

---

### Task 3: mineru_lane.py — content_list → pages 매핑 (순수)

**Files:**
- Create: `parse_service/parsers/pdf/mineru_lane.py`
- Create(test): `parse_service/tests/test_mineru_lane.py`

**Interfaces:**
- Consumes: `kb_pipeline.blockify.elements_to_blocks`.
- Produces: `_content_list_to_elements(content_list: list[dict]) -> list[dict]`, `_elements_to_pages(elements: list[dict]) -> list[dict]`(=`[{page_number, blocks}]`), `run_mineru(pdf_bytes, filename, parse_method) -> list[dict]`, `_invoke_mineru(pdf_bytes, filename, parse_method) -> list[dict]`(Task 4에서 실구현).

MinerU `content_list.json` item(권위: MinerU 소스 — Task 4 Step 1에서 대조): `type`("text"|"title"|"list"|"table"|"image"|"equation"), `text`(text/title/list/equation), `table_body`(table, `<table>` HTML), `img_path`(image), `page_idx`(0-based).

- [ ] **Step 1: 실패 테스트 작성**

```python
# parse_service/tests/test_mineru_lane.py
from parse_service.parsers.pdf import mineru_lane


def test_content_list_maps_to_pages_preserving_table_html():
    content_list = [
        {"type": "title", "text": "제목", "page_idx": 0},
        {"type": "text", "text": "본문 문단", "page_idx": 0},
        {"type": "table", "table_body": "<table><tr><td>셀</td></tr></table>", "page_idx": 0},
        {"type": "text", "text": "둘째 페이지 텍스트", "page_idx": 1},
        {"type": "image", "img_path": "imgs/p2.jpg", "page_idx": 1},
    ]
    pages = mineru_lane._elements_to_pages(
        mineru_lane._content_list_to_elements(content_list))
    assert [p["page_number"] for p in pages] == [1, 2]          # 0-based→1-based
    # 표 HTML 원형 보존(불변식)
    tbl = next(b for b in pages[0]["blocks"] if b["type"] == "table")
    assert tbl["table_body"] == "<table><tr><td>셀</td></tr></table>"
    # 블록 page_idx 는 1-based page_number 로 정규화(기존 ODL 경로와 동일)
    assert all(b["page_idx"] == 1 for b in pages[0]["blocks"])
    assert all(b["page_idx"] == 2 for b in pages[1]["blocks"])
    img = next(b for b in pages[1]["blocks"] if b["type"] == "image")
    assert img["img_path"] == "imgs/p2.jpg"


def test_run_mineru_uses_invoke_boundary(monkeypatch):
    seen = {}

    def fake_invoke(pdf_bytes, filename, parse_method):
        seen["method"] = parse_method
        return [{"type": "text", "text": "ocr 결과", "page_idx": 0}]

    monkeypatch.setattr(mineru_lane, "_invoke_mineru", fake_invoke)
    pages = mineru_lane.run_mineru(b"%PDF", "a.pdf", "ocr")
    assert seen["method"] == "ocr"
    assert pages[0]["page_number"] == 1 and pages[0]["blocks"]
```

- [ ] **Step 2: 실패 확인**

Run: `.venv-kb/bin/python -m pytest parse_service/tests/test_mineru_lane.py -q`
Expected: FAIL (`ModuleNotFoundError: ...mineru_lane`).

- [ ] **Step 3: 구현 (순수 매핑 + import 경계 스텁)**

```python
# parse_service/parsers/pdf/mineru_lane.py
"""MinerU 레인 — 스캔/혼합/복잡 PDF 를 MinerU(hybrid: VLM 원격 + PaddleOCR 로컬)로 파싱.

설계: docs/superpowers/specs/2026-07-13-mineru-pdf-integration-design.md §4·§5
_invoke_mineru 만 실제 MinerU 를 부르는 import 경계(테스트는 이 함수를 monkeypatch).
나머지(_content_list_to_elements/_elements_to_pages)는 순수 매핑 → 로컬 단위검증.
"""
from __future__ import annotations

import logging

from kb_pipeline.blockify import elements_to_blocks

log = logging.getLogger("kb_pipeline.parse_service.parsers.pdf.mineru_lane")

# MinerU content_list `type` → blockify `elements[]` `category`
_TYPE_TO_CATEGORY = {
    "text": "text", "title": "title", "list": "text",
    "table": "table", "image": "image", "equation": "equation",
}


def _content_list_to_elements(content_list: list[dict]) -> list[dict]:
    """MinerU content_list item → blockify elements[] 형태(표 HTML/이미지/수식/텍스트)."""
    elements: list[dict] = []
    for item in content_list:
        t = (item.get("type") or "text").lower()
        page_idx = item.get("page_idx", 0) or 0
        category = _TYPE_TO_CATEGORY.get(t, "text")
        if t == "table":
            content = {"html": item.get("table_body") or ""}
        elif t == "image":
            content = {"img_path": item.get("img_path") or ""}
        elif t == "equation":
            content = {"text": item.get("text") or ""}
        else:  # text/title/list
            content = {"markdown": item.get("text") or ""}
        elements.append({"category": category, "content": content, "page_idx": page_idx})
    return elements


def _elements_to_pages(elements: list[dict]) -> list[dict]:
    """elements → blocks(elements_to_blocks 재사용) → 0-based page_idx 로 그룹핑 → 1-based pages."""
    blocks = elements_to_blocks(elements)
    by_page: dict[int, list] = {}
    for b in blocks:
        by_page.setdefault(b.get("page_idx", 0) or 0, []).append(b)
    pages: list[dict] = []
    # NOTE: content_list 에 없는 페이지(빈 페이지)는 pages 에 누락 → page_number 비연속 가능
    # (ODL 레인은 렌더된 모든 페이지를 냄). 하류는 page_number 로 키하므로 갭 허용(청킹/페이지이미지 무관).
    for pidx in sorted(by_page):
        page_number = pidx + 1
        for b in by_page[pidx]:
            b["page_idx"] = page_number  # 1-based 정규화(기존 ODL/OCR 경로와 동일)
        pages.append({"page_number": page_number, "blocks": by_page[pidx]})
    return pages


def _invoke_mineru(pdf_bytes: bytes, filename: str, parse_method: str) -> list[dict]:
    """MinerU import 경계 — 여기서만 mineru 를 import. Task 4에서 실구현.
    반환: content_list(list[dict])."""
    raise NotImplementedError("Task 4: MinerU in-process 호출 구현")


def run_mineru(pdf_bytes: bytes, filename: str, parse_method: str) -> list[dict]:
    """MinerU 레인 진입 — content_list 획득 → pages 반환."""
    content_list = _invoke_mineru(pdf_bytes, filename, parse_method)
    return _elements_to_pages(_content_list_to_elements(content_list))
```

- [ ] **Step 4: 통과 확인**

Run: `.venv-kb/bin/python -m pytest parse_service/tests/test_mineru_lane.py -q`
Expected: PASS (2 테스트).

- [ ] **Step 5: 커밋**

```bash
git add parse_service/parsers/pdf/mineru_lane.py parse_service/tests/test_mineru_lane.py
git commit -m "feat(parse-svc): MinerU 레인 매핑 — content_list→elements→blocks→pages(표 HTML 보존)"
```

---

### Task 4: _invoke_mineru 실구현 (MinerU in-process 호출 — 배포서버 검증)

**Files:**
- Modify: `parse_service/parsers/pdf/mineru_lane.py`(`_invoke_mineru` 본문)
- Modify(test): `parse_service/tests/test_mineru_lane.py`(인자 배선 검증 추가)

**Interfaces:**
- Consumes: env `MINERU_VLM_SERVER_URL`(+선택 `MINERU_VLM_API_KEY`), MinerU 라이브러리.
- Produces: `_invoke_mineru` 가 실제 content_list(list[dict]) 반환.

> ⚠️ **로컬(Intel Mac) 실행 불가** — MinerU/torch/paddle 미설치. 이 Task 는 **인자 배선(server_url/parse_method/backend)만 로컬 단위검증**(mineru import 를 monkeypatch)하고, **실 end-to-end 는 Task 8 배포서버 스택검증**으로 분리한다.

- [ ] **Step 1: MinerU in-process 진입 시그니처 소스 대조**

Run(배포서버 or MinerU 소스):
```bash
python -c "from mineru.cli.common import do_parse; import inspect; print(inspect.signature(do_parse))"
python -c "from mineru.cli.backend_options import BACKEND_HYBRID_HTTP_CLIENT; print(BACKEND_HYBRID_HTTP_CLIENT)"
```
확인 사항: `do_parse` 의 정확한 인자(`output_dir`, `pdf_file_names`, `pdf_bytes_list`, `backend`, `server_url`, `parse_method`, `p_lang_list` 등)와 **반환값(디스크로 출력하고 None 반환)** 및 content_list.json 출력 경로 패턴(`{output_dir}/{stem}/auto/{stem}_content_list.json` 등). content_list item 필드(`type`/`text`/`text_level`/`table_body`/`img_path`/`page_idx`)가 Task 3 매핑과 일치하는지 대조 — MinerU 는 표준적으로 heading 을 `type=='text'`+`text_level` 로 내고 별도 `title`/`list` 타입이 없을 수 있으니, 어긋나면 `_content_list_to_elements`/`_TYPE_TO_CATEGORY` 를 실제 enum(특히 `text_level`)으로 정정한다.

- [ ] **Step 2: 실패 테스트 추가 (디스크 계약 반영 — do_parse 는 파일로 씀)**

```python
# parse_service/tests/test_mineru_lane.py 에 추가
import json, os
import pytest
import parse_service.parsers.pdf.mineru_lane as ml


def test_invoke_requires_server_url(monkeypatch):
    monkeypatch.delenv("MINERU_VLM_SERVER_URL", raising=False)
    with pytest.raises(RuntimeError):
        ml._invoke_mineru(b"%PDF", "a.pdf", "ocr")


def test_invoke_passes_args_and_reads_disk_content_list(monkeypatch):
    monkeypatch.setenv("MINERU_VLM_SERVER_URL", "http://vlm:8000")
    captured = {}

    # _run_mineru_do_parse 가 실제 mineru do_parse(디스크 출력)를 감싸는 경계.
    # 테스트는 이를 monkeypatch 해서 (1) 전달 인자 검증 (2) content_list.json 을 디스크에 써서
    # _invoke_mineru 의 디스크-read 분기를 실검증한다.
    def fake_run(**kw):
        captured.update(kw)
        out = kw["output_dir"]
        sub = os.path.join(out, "a", "auto")
        os.makedirs(sub, exist_ok=True)
        with open(os.path.join(sub, "a_content_list.json"), "w", encoding="utf-8") as f:
            json.dump([{"type": "text", "text": "ocr 결과", "page_idx": 0}], f)
        return None  # 실제 do_parse 처럼 None 반환

    monkeypatch.setattr(ml, "_run_mineru_do_parse", fake_run)
    content_list = ml._invoke_mineru(b"%PDF", "a.pdf", "ocr")
    assert captured["backend"] == "hybrid-http-client"
    assert captured["server_url"] == "http://vlm:8000"
    assert captured["parse_method"] == "ocr"
    assert content_list == [{"type": "text", "text": "ocr 결과", "page_idx": 0}]
```

- [ ] **Step 3: 실패 확인**

Run: `.venv-kb/bin/python -m pytest parse_service/tests/test_mineru_lane.py -k invoke -q`
Expected: FAIL (`_run_mineru_do_parse` 없음 / `_invoke_mineru` NotImplementedError).

- [ ] **Step 4: 구현 (do_parse 디스크 출력 계약 — Step 1 로 경로/시그니처 확정)**

```python
# mineru_lane.py — 상단 import 에 추가
import glob
import json
import os
import shutil
import tempfile

_MINERU_BACKEND = "hybrid-http-client"


def _run_mineru_do_parse(**kwargs) -> None:
    """실제 mineru do_parse 경계 — mineru import 를 이 helper 안으로 격리(테스트 monkeypatch 지점).
    do_parse 는 결과를 output_dir 로 **디스크 출력**하고 None 반환(반환값 사용 안 함).
    정확한 인자/출력경로는 Task 4 Step 1 소스 대조로 확정."""
    from mineru.cli.common import do_parse  # noqa: PLC0415 (지연 import — 로컬 미설치 허용)
    do_parse(**kwargs)


def _invoke_mineru(pdf_bytes: bytes, filename: str, parse_method: str) -> list[dict]:
    server_url = os.environ.get("MINERU_VLM_SERVER_URL")
    if not server_url:
        raise RuntimeError("MINERU_VLM_SERVER_URL 미설정 — MinerU 레인 사용 불가")
    scratch = os.environ.get("SCRATCHPAD_DIR") or None
    output_dir = tempfile.mkdtemp(prefix="mineru_", dir=scratch)
    try:
        _run_mineru_do_parse(
            output_dir=output_dir,
            pdf_bytes_list=[pdf_bytes],
            pdf_file_names=[os.path.splitext(os.path.basename(filename))[0]],
            backend=_MINERU_BACKEND,
            server_url=server_url,
            parse_method=parse_method,
        )
        matches = glob.glob(os.path.join(output_dir, "**", "*content_list.json"), recursive=True)
        if not matches:
            raise RuntimeError(f"MinerU content_list.json 미생성: {output_dir}")
        with open(matches[0], encoding="utf-8") as f:
            return json.load(f)
    finally:
        shutil.rmtree(output_dir, ignore_errors=True)
```
> Step 1 대조로 `do_parse` 실제 인자명/출력 파일경로를 확정해 위 kwargs·glob 패턴을 정정한다. MinerU 하위레벨 API(`doc_analyze`+`union_make`)가 in-memory content_list 를 준다면 그쪽으로 바꿔 디스크 왕복을 없애도 좋다(선택).

- [ ] **Step 5: 통과 확인**

Run: `.venv-kb/bin/python -m pytest parse_service/tests/test_mineru_lane.py -q`
Expected: PASS (mineru 는 지연 import 라 미설치여도 monkeypatch 로 디스크-read 분기까지 검증됨).

- [ ] **Step 6: 커밋**

```bash
git add parse_service/parsers/pdf/mineru_lane.py parse_service/tests/test_mineru_lane.py
git commit -m "feat(parse-svc): MinerU in-process 호출(hybrid-http-client, VLM 원격, do_parse 디스크 출력 read)"
```

---

### Task 5: __init__.py — 게이트 분기 + MinerU 폴백

**Files:**
- Modify: `parse_service/parsers/pdf/__init__.py`
- Create(test): `parse_service/tests/test_parser_pdf_routing.py`

**Interfaces:**
- Consumes: Task 2 `gate.decide_route`, Task 3 `mineru_lane.run_mineru`.
- Produces: `parse(file_bytes, filename, *, ocr_url) -> RouteResult`(변경), `_odl_lane(file_bytes, filename, ocr_url) -> RouteResult`(기존 본문 추출).

- [ ] **Step 1: 실패 테스트 작성 (라우팅 + 폴백 + 빈결과)**

```python
# parse_service/tests/test_parser_pdf_routing.py
from parse_service.parsers import RouteResult
from parse_service.parsers import pdf as pdf_parser
from parse_service.parsers.pdf.gate import RouteDecision


def _mineru(pm="ocr"):
    return RouteDecision(lane="mineru", parse_method=pm)


def test_odl_lane_when_gate_says_odl(monkeypatch):
    monkeypatch.setattr(pdf_parser, "_safe_decide_route",
                        lambda b: RouteDecision(lane="odl", parse_method=None))
    monkeypatch.setattr(pdf_parser, "_page_markdowns", lambda fb, fn: ["# 텍스트"])
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert res.kind == "pages" and res.pages[0]["blocks"]


def test_mineru_lane_when_gate_says_mineru(monkeypatch):
    monkeypatch.setattr(pdf_parser, "_safe_decide_route", lambda b: _mineru())
    monkeypatch.setattr(pdf_parser, "run_mineru",
                        lambda fb, fn, pm: [{"page_number": 1, "blocks": [{"type": "text", "text": "m"}]}])
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert res.kind == "pages" and res.pages[0]["blocks"][0]["text"] == "m"


def test_mineru_failure_falls_back_to_odl(monkeypatch):
    monkeypatch.setattr(pdf_parser, "_safe_decide_route", lambda b: _mineru())
    def boom(fb, fn, pm):
        raise RuntimeError("VLM down")
    monkeypatch.setattr(pdf_parser, "run_mineru", boom)
    monkeypatch.setattr(pdf_parser, "_page_markdowns", lambda fb, fn: ["# 폴백 텍스트"])
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert res.kind == "pages" and res.pages[0]["blocks"], "MinerU 실패 시 ODL 레인 폴백"


def test_mineru_empty_result_falls_back_to_odl(monkeypatch):
    """성공했으나 blocks 전무 → ODL 폴백(빈 출력 재발 방지)."""
    monkeypatch.setattr(pdf_parser, "_safe_decide_route", lambda b: _mineru())
    monkeypatch.setattr(pdf_parser, "run_mineru",
                        lambda fb, fn, pm: [{"page_number": 1, "blocks": []}])
    monkeypatch.setattr(pdf_parser, "_page_markdowns", lambda fb, fn: ["# 폴백 텍스트"])
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert res.pages[0]["blocks"], "MinerU 빈 결과 시 ODL 폴백"


def test_gate_exception_routes_to_odl(monkeypatch):
    """_safe_decide_route 가 게이트 예외를 삼켜 None → ODL(새 500 없음)."""
    monkeypatch.setattr(pdf_parser, "_page_markdowns", lambda fb, fn: ["# 텍스트"])
    # 실제 _safe_decide_route 사용: gate import 를 깨서 None 반환 유도
    import parse_service.parsers.pdf.gate as gate
    monkeypatch.setattr(gate, "decide_route",
                        lambda b: (_ for _ in ()).throw(RuntimeError("boom")))
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert res.kind == "pages"  # 예외 안 나고 ODL 로
```

- [ ] **Step 2: 실패 확인**

Run: `.venv-kb/bin/python -m pytest parse_service/tests/test_parser_pdf_routing.py -q`
Expected: FAIL (`_safe_decide_route`/`run_mineru` 미정의).

- [ ] **Step 3: 구현 — 기존 본문을 `_odl_lane` 로 추출하고 `parse()` 를 가드된 분기로 교체**

`parse_service/parsers/pdf/__init__.py` 상단 import 에 추가(mineru_lane 은 mineru 를 지연 import 하므로 top-level 안전):
```python
from parse_service.parsers.pdf.mineru_lane import run_mineru
```
> gate 는 **top-level import 하지 않는다** — gate→triage→`import pymupdf` 라 pymupdf 부재 시 모듈 로드가 통째로 깨져 ODL 까지 회귀. 아래처럼 **parse() 안에서 지연 import + try/except** 로 격리한다.

기존 `def parse(file_bytes, filename, *, ocr_url) -> RouteResult:` 의 **함수 이름을 `_odl_lane` 로 변경**(본문 전체 그대로 유지). 그 아래 추가:
```python
def _safe_decide_route(file_bytes: bytes):
    """게이트 호출 — pymupdf 부재/triage 예외를 삼켜 None(=ODL) 반환. 새 500 방지(가용성)."""
    try:
        from parse_service.parsers.pdf.gate import decide_route  # 지연 import(pymupdf 격리)
    except Exception:  # noqa: BLE001
        log.exception("게이트 import 실패(pymupdf 부재?) — ODL 레인")
        return None
    try:
        return decide_route(file_bytes)
    except Exception:  # noqa: BLE001
        log.exception("게이트 판정 실패 — ODL 레인")
        return None


def parse(file_bytes: bytes, filename: str, *, ocr_url: str) -> RouteResult:
    """문서수준 게이트 → ODL 레인 or MinerU 레인(게이트/MinerU 실패·빈결과 시 ODL 폴백)."""
    decision = _safe_decide_route(file_bytes)
    if decision is not None and decision.lane == "mineru":
        try:
            pages = run_mineru(file_bytes, filename, decision.parse_method)
        except Exception:  # noqa: BLE001 — MinerU 실패는 비치명, ODL/VL 폴백
            log.exception("MinerU 레인 실패 — ODL/VL 폴백 (%s)", filename)
        else:
            if pages and any(p.get("blocks") for p in pages):
                return RouteResult(kind="pages", chunk_needed=True, pages=pages)
            log.warning("MinerU 빈 결과 — ODL/VL 폴백 (%s)", filename)
    return _odl_lane(file_bytes, filename, ocr_url=ocr_url)
```

- [ ] **Step 4: 통과 확인 (신규 + 기존 회귀)**

Run: `.venv-kb/bin/python -m pytest parse_service/tests/test_parser_pdf_routing.py parse_service/tests/test_parser_pdf.py -q`
Expected: PASS (신규 5 + 기존 test_parser_pdf.py 전부). 기존 테스트는 `parse(b"%PDF", ...)` 를 호출하는데, 실제 `_safe_decide_route(b"%PDF")` → `decide_route` → `triage_document(b"%PDF")` → `pymupdf.open` 이 4바이트 스트림 열기 실패 → triage 가 `[]` 반환 → 빈 버킷 → **ODL 레인**으로 라우팅되어 기존 `_page_markdowns` monkeypatch 가 그대로 동작(즉 fixture 내용이 아니라 **triage 열기 실패로** ODL 로 감). 만약 실제 PDF 바이트 fixture 를 쓰는 테스트가 MinerU 로 새면 그 테스트에 `_safe_decide_route`→odl monkeypatch 를 추가한다.

- [ ] **Step 5: 커밋**

```bash
git add parse_service/parsers/pdf/__init__.py parse_service/tests/test_parser_pdf_routing.py
git commit -m "feat(parse-svc): PDF parse 문서수준 분기(ODL/MinerU) + MinerU 실패 ODL 폴백"
```

---

### Task 6: 환경설정 배선 + 의존성 노트

**Files:**
- Modify: `scripts/parse-svc.env`(gitignored via `scripts/*.env`, 런처가 로드 — 커밋 안 됨)
- Create: `docs/mineru-deploy-notes.md`(배포서버 전제조건)

- [ ] **Step 1: env 추가 (올바른 경로 — 런처가 로드하고 gitignore 됨)**

`scripts/parse-svc.env` 에 추가(값은 실 서버 주소). **이 파일이 런처 `scripts/run-parse-svc.sh` 가 `set -a; . scripts/parse-svc.env` 로 프로세스에 로드하는 파일**이고 `.gitignore` `scripts/*.env` 로 무시된다. (`parse_service/parse-svc.env` 는 로드도 gitignore 도 안 되니 쓰지 말 것):
```
MINERU_VLM_SERVER_URL=http://<mineru-vlm-gpu-host>:<port>
# MINERU_VLM_API_KEY=... (필요 시)
```
확인(둘 다):
```bash
git check-ignore scripts/parse-svc.env          # 경로 출력돼야(무시됨)
grep -n "parse-svc.env" scripts/run-parse-svc.sh  # 런처가 source 하는지 확인
```

- [ ] **Step 2: 배포 노트 작성**

`docs/mineru-deploy-notes.md` 에 기록: parse-svc 배포서버 전제조건 — (1) MinerU + torch + PaddleOCR(PP-OCRv5 모델) 설치, (2) `MINERU_VLM_SERVER_URL` 이 가리키는 별도 VLM GPU 서버 가동, (3) `hybrid-http-client` backend 사용(PaddleOCR 로컬 + VLM 원격), (4) 로컬 Intel Mac dev 는 MinerU 레인 미구동 → 게이트가 스캔 문서를 MinerU 로 보내면 `_invoke_mineru` 가 import 실패 → ODL/VL 폴백(dev 에서 정상 동작 보장).

- [ ] **Step 3: 커밋 (노트만 — env 는 gitignored)**

```bash
git add docs/mineru-deploy-notes.md
git commit -m "docs(mineru): 배포서버 전제조건(MinerU+PaddleOCR+VLM 서버) 노트"
```

---

### Task 7: 전체 회귀 + _workspace 문서 반영

**Files:**
- Modify: `_workspace/01-architecture.md`, `_workspace/02-changes.md`, `_workspace/03-dev-progress.md`

- [ ] **Step 1: parse-svc 전체 테스트 그린 확인**

Run: `.venv-kb/bin/python -m pytest parse_service/tests -q`
Expected: PASS (신규 포함 전부; MinerU 실경로는 미설치라 fake/폴백 경로만 — 실 end-to-end 는 Task 8).

- [ ] **Step 2: _workspace 갱신**

- `01-architecture.md`: PDF 레인 표에 "문서수준 게이트(triage) → 순수텍스트=ODL / 스캔·혼합=MinerU(hybrid, VLM 원격+PaddleOCR 로컬)" 추가. 스캔 처리 행을 "MinerU 레인(실패 시 in-process VL 폴백)"으로 갱신.
- `02-changes.md`: MinerU 도입 결정·게이트 규칙(혼합=auto, 순수스캔=ocr 강제)·폴백·검증 스파이크(문서수준 ocr_classify, VLM 상시추출) 기록.
- `03-dev-progress.md`: 작업항목/리스크(배포서버 MinerU 설치 미검증 = Task 8 잔여) 추가.

- [ ] **Step 3: 커밋**

```bash
git add _workspace/01-architecture.md _workspace/02-changes.md _workspace/03-dev-progress.md
git commit -m "docs(_workspace): MinerU PDF 레인 통합 반영(게이트/레인/폴백/리스크)"
```

---

### Task 8: 배포서버 스택검증 (실 MinerU — 로컬 불가)

**Files:** 없음(수동 검증). 발견 이슈는 Task 4 매핑/배선 정정으로 환류.

- [ ] **Step 1: 배포서버에 MinerU 런타임 설치·구동 확인**

Run(배포서버): `python -c "import mineru; from mineru.cli.common import do_parse; print('ok')"` + PP-OCRv5 모델 존재 확인 + `MINERU_VLM_SERVER_URL` 헬스체크.

- [ ] **Step 2: 실 스캔 PDF 1건 end-to-end**

실제 스캔 PDF(2026-07-07 빈 표 재현 문서)로 `POST /parse` → 응답의 pages/blocks 에 표가 `<table>` 로 비어있지 않게 추출되는지 확인(버그 재발 없음). content_list 필드가 Task 3 매핑과 일치하는지 대조 — 불일치 시 `_content_list_to_elements` 정정 후 재검.

- [ ] **Step 3: 혼합 PDF 1건 — 'auto' 경로**

네이티브 텍스트 + 스캔 페이지 혼합 문서 → 게이트가 MinerU 'auto' 로 라우팅, 텍스트 페이지 정상 + 스캔 페이지 추출 확인.

---

## Self-Review

- **Spec coverage**: §2 함의(정정)→Task 2 parse_method · §3.1 게이트(Task 2) · §4 MinerU 레인(Task 4) · §4.4 VLM env(Task 6) · §5 출력 매핑(Task 3) · §6 폴백(게이트/ MinerU/빈결과, Task 2·5) · §7 불변식(Global Constraints) · §8 테스트(Task 2·3·4·5·7·8) · §9 리스크(Task 6·8) — 모두 대응. ✅
- **Placeholder scan**: 모든 코드 스텝에 실제 코드. Task 4·8 의 "Step 1 대조/배포검증"은 로컬(Intel Mac) MinerU 미설치 항목의 명시적 검증 절차 — spec §10 열린결정과 일치. ✅
- **Type consistency**: `RouteDecision(lane, parse_method)` Task 2 정의 → Task 5 `_safe_decide_route` 소비 일치. `run_mineru(pdf_bytes, filename, parse_method)` Task 3 정의 → Task 5 호출 일치. `_run_mineru_do_parse`(경계)/`_invoke_mineru`(디스크 read) Task 4 → Task 3 `run_mineru` 호출 일치. `elements[]`(`category`/`content`/`page_idx`) Task 3 → blockify `elements_to_blocks` 실제 스키마(확인함) 일치. `RouteResult(kind,chunk_needed,pages)` 계약 일치. ✅

### v2 검증 반영 (ultracode NEEDS_REVISION → 3 blocking 해소)
- **B1 혼합 'auto' 스캔 유실**: Task 2 게이트를 "`OCR_NEEDED` 하나라도 있으면 `'ocr'` 강제, 없고 `LLM_NEEDED` 만이면 `'auto'`"로 변경. spec §2/§4.3 근거 정정. 테스트에 `{TEXT_ONLY,OCR_NEEDED}→ocr` 케이스 추가. ✅
- **B2 env 경로**: `parse_service/parse-svc.env`(비-gitignore·비-로드) → `scripts/parse-svc.env`(gitignore·런처 로드, 실측 확인). Global Constraints·Task 6 반영. ✅
- **B3 무가드 게이트 500**: Task 5 에 `_safe_decide_route`(gate 지연 import + try/except → None=ODL) 도입, decide_route 자체도 triage 예외 삼킴(Task 2). pymupdf 부재·암호화/손상 PDF 도 ODL 폴백. 테스트 `test_gate_exception_routes_to_odl` 추가. ✅
- **minor**: MinerU 빈결과 폴백(Task 5 `test_mineru_empty_result_falls_back_to_odl`) · do_parse 디스크 출력 계약+디스크-read 테스트(Task 4) · `text_level` enum 대조(Task 4 Step 1) · page_number 갭 허용 주석(Task 3) 반영. ✅
