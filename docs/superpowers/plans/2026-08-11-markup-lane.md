<!-- plan-version: v1 -->
<!-- ultracode-validation: PENDING -->

# 구조화 텍스트 레인 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** html/htm 을 한컴 형변환 API 없이 파싱하고(표는 `<table>` HTML 보존), csv 를 엑셀 레인의 행 레코드 청크로 만들고, xml 이 더 이상 실패하지 않게 한다.

**Architecture:** 세 갈래다. ① html/htm → 신규 `parsers/html` — 표를 sentinel 로 빼두고 나머지만 `markdownify` 로 markdown 화한 뒤 원본 `<table>` HTML 을 되돌려 `blockify` 에 넘긴다. ② csv → 메모리상 xlsx 로 합성(헤더 행에 서식 부여)해 기존 엑셀 레인에 위임, 백엔드는 `openpyxl` 고정. ③ xml → `TEXT_EXTS` 추가(평문 통과). markitdown 은 도입하지 않는다(설계문서 §2, 재유입 가드 유지).

**Tech Stack:** Python 3.12, `markdownify`(신규), `beautifulsoup4`, `openpyxl`, `markdown-it-py`, pytest.

**설계문서:** `docs/superpowers/specs/2026-08-11-markup-lane-design.md`

## Global Constraints

- **표는 `<table>` HTML 로 보존한다. pipe 평탄화 금지**(colspan/rowspan 손실). 리포 불변식.
- **markitdown 을 도입하지 않는다.** 재유입 가드 `parse_service/tests/test_no_markitdown.py` 는 수정·삭제 금지.
- **신규 pip 의존성은 `markdownify` 하나.** `requirements.txt` 에만 추가한다. `pyproject.toml` 은 건드리지 않는다(`requires-python = ">=3.9"` 와 충돌 회피).
- **env 신설·삭제·기본값 변경 없음.** 따라서 `.env.example` 계열 6종은 수정 대상이 아니다.
- **디코딩에 `errors="replace"` 금지.** U+FFFD 범벅이 '성공한 쓰레기'로 임베딩까지 간다.
- 테스트 실행 인터프리터는 **`/Users/xxx/workspace/8.kb-pipeline/.venv-kb/bin/python`** (repo 루트 `.venv` 에는 fastapi/pymupdf 가 없다). `PYTHONPATH=$PWD` 필요.
- 베이스라인: `646 passed, 3 skipped`.

---

## File Structure

**신규**
- `parse_service/tools/textdecode.py` — 바이트→문자열 디코딩 사다리(BOM utf-16 → utf-8-sig → cp949). html/csv/text 세 레인이 공유.
- `parse_service/parsers/html/__init__.py` — html/htm 도메인 파서(표 보존 하이브리드).
- `parse_service/parsers/excel/csv_to_xlsx.py` — csv 바이트 → xlsx 바이트 합성(헤더 서식 부여).
- `parse_service/tests/test_tools_textdecode.py`
- `parse_service/tests/test_parser_html.py`
- `parse_service/tests/test_csv_to_xlsx.py`

**수정**
- `parse_service/tools/fileconvert.py:29,32` — `CONVERTIBLE_EXTS` 에서 `html`/`htm` 제거, `TEXT_EXTS` 에서 `csv` 제거·`xml` 추가.
- `parse_service/router.py` — `html` 도메인 분기 추가, `_text_parse` 가 공용 디코더 사용.
- `parse_service/parsers/excel/__init__.py` — `EXCEL_EXTS` 에 `csv` 추가, `_fetch_rag_chunks` 가 csv 를 xlsx 로 합성하고 백엔드를 `openpyxl` 로 고정.
- `requirements.txt` — `markdownify` 추가.
- `scripts/airgap/verify-bundle.sh` — html 왕복 스모크 추가 + 형변환 API 주석에서 html 제거.
- `_workspace/01-architecture.md`, `_workspace/02-changes.md`, `_workspace/03-dev-progress.md`, `docs/kb-pipeline-process-definition.md`, `deferred.md`.

---

### Task 1: 공용 디코딩 헬퍼

지금 디코딩 사다리는 `router._text_parse` 안에 인라인으로 있다. html·csv 레인도 같은 사다리가 필요하므로 하나로 뽑는다. 정의가 셋이 되면 드리프트가 생긴다.

**Files:**
- Create: `parse_service/tools/textdecode.py`
- Create: `parse_service/tests/test_tools_textdecode.py`
- Modify: `parse_service/router.py:34-54` (`_text_parse` 가 헬퍼를 쓰도록)

**Interfaces:**
- Consumes: `parse_service.parsers.ParserError`
- Produces: `parse_service.tools.textdecode.decode_text(file_bytes: bytes, filename: str) -> str` — 실패 시 `ParserError`

- [ ] **Step 1: Write the failing test**

`parse_service/tests/test_tools_textdecode.py`:

```python
import pytest

from parse_service.parsers import ParserError
from parse_service.tools.textdecode import decode_text


def test_utf8_plain():
    assert decode_text("규정 가나".encode("utf-8"), "a.txt") == "규정 가나"


def test_utf8_sig_strips_bom():
    assert decode_text("규정".encode("utf-8-sig"), "a.txt") == "규정"


def test_cp949_roundtrip():
    assert decode_text("규정가나".encode("cp949"), "a.csv") == "규정가나"


def test_utf16_only_with_bom():
    """BOM 이 있을 때만 utf-16 을 시도한다.

    무조건 utf-16 을 앞에 두면 cp949 한국어가 U+FFFD 없이 '성공'해 mojibake 가
    임베딩까지 간다: "규정가나".encode("cp949").decode("utf-16") == '풱꓁ꆰꪳ'
    """
    assert decode_text("규정".encode("utf-16"), "a.txt") == "규정"
    assert decode_text("규정가나".encode("cp949"), "a.txt") != "풱꓁ꆰꪳ"


def test_undecodable_raises_parser_error():
    with pytest.raises(ParserError):
        decode_text(b"\xff\xfe\x00", "a.txt")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD /Users/xxx/workspace/8.kb-pipeline/.venv-kb/bin/python -m pytest parse_service/tests/test_tools_textdecode.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'parse_service.tools.textdecode'`

- [ ] **Step 3: Write minimal implementation**

`parse_service/tools/textdecode.py`:

```python
"""바이트 → 문자열 디코딩 사다리. text/html/csv 세 레인이 공유한다.

**순서가 중요하다** — utf-16 을 무조건 앞에 두면 cp949 한국어가 U+FFFD 없이 '성공'해
mojibake 가 임베딩까지 간다(실측: "규정가나".encode("cp949").decode("utf-16") == '풱꓁ꆰꪳ').
그래서 utf-16 은 **BOM 이 있을 때만** 후보에 넣는다.

``errors="replace"`` 금지 — U+FFFD 범벅이 '성공한 쓰레기'로 적재된다.
"""
from __future__ import annotations

from parse_service.parsers import ParserError


def decode_text(file_bytes: bytes, filename: str) -> str:
    cands = (("utf-16",) if file_bytes[:2] in (b"\xff\xfe", b"\xfe\xff") else ()) + (
        "utf-8-sig", "cp949")
    for enc in cands:
        try:
            return file_bytes.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ParserError(f"decode failed ({'/'.join(cands)}): {filename}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD /Users/xxx/workspace/8.kb-pipeline/.venv-kb/bin/python -m pytest parse_service/tests/test_tools_textdecode.py -q`
Expected: PASS (5 tests)

- [ ] **Step 5: `_text_parse` 를 헬퍼로 전환**

`parse_service/router.py` 의 `_text_parse` 본문에서 인라인 사다리를 지우고 헬퍼를 부른다. 나머지(빈 문자열 검사, `hybrid_to_blocks`, `RouteResult`)는 그대로 둔다.

```python
def _text_parse(fb, fn, **_):
    """평문 → 단일 페이지 blocks. 변환도 파서도 거치지 않는다."""
    from kb_pipeline.blockify import hybrid_to_blocks
    from parse_service.tools.textdecode import decode_text
    md = decode_text(fb, fn)
    if not md.strip():
        raise ParserError(f"empty text file: {fn}")
    return RouteResult(kind="pages", chunk_needed=True,
                       pages=[{"page_number": 1,
                               "blocks": hybrid_to_blocks(md, page_idx=1)}])
```

- [ ] **Step 6: 회귀 확인 — text 레인 기존 테스트가 그대로 통과**

Run: `PYTHONPATH=$PWD /Users/xxx/workspace/8.kb-pipeline/.venv-kb/bin/python -m pytest parse_service/tests -q`
Expected: PASS, 실패 0

- [ ] **Step 7: Commit**

```bash
git add parse_service/tools/textdecode.py parse_service/tests/test_tools_textdecode.py parse_service/router.py
git commit -m "refactor(parse-svc): 디코딩 사다리를 tools/textdecode 로 통일 (html/csv 레인이 공유)"
```

---

### Task 2: html 하이브리드 파서

표를 먼저 빼내고 나머지만 markdown 으로 만든 뒤 되돌린다. markitdown 을 쓰지 않는 이유는 설계문서 §2 참조.

**Files:**
- Create: `parse_service/parsers/html/__init__.py`
- Create: `parse_service/tests/test_parser_html.py`
- Modify: `requirements.txt` (`markdownify` 추가)

**Interfaces:**
- Consumes: `parse_service.tools.textdecode.decode_text`, `parse_service.parsers.RouteResult`/`ParserError`, `kb_pipeline.blockify.hybrid_to_blocks`
- Produces: `parse_service.parsers.html.HTML_EXTS: set[str]`, `parse_service.parsers.html.parse(file_bytes: bytes, filename: str, *, ocr_url=None) -> RouteResult`

- [ ] **Step 1: 의존성 추가**

`requirements.txt` 의 `openpyxl>=3.1.0` 아래 줄에 추가한다(파일 끝이 아니라 파서 의존성 무리 안):

```
markdownify>=1.2.0
```

설치: `VIRTUAL_ENV=/Users/xxx/workspace/8.kb-pipeline/.venv-kb uv pip install "markdownify>=1.2.0"`
(이미 1.2.2 가 설치돼 있으면 그대로 통과한다.)

- [ ] **Step 2: Write the failing test**

`parse_service/tests/test_parser_html.py`:

```python
import pytest

from parse_service.parsers import ParserError
from parse_service.parsers import html as _html

MERGED = """<html><head><style>p{color:red}</style></head><body>
<h1>규정</h1><p>제1조 <b>목적</b></p>
<table><tr><th rowspan="2">구분</th><th colspan="2">금액</th></tr>
<tr><td>2025</td><td>2026</td></tr>
<tr><td>본사</td><td>10</td><td>20</td></tr></table>
<h2>부칙</h2>
<script>alert(1)</script>
</body></html>"""

NESTED = """<html><body>
<table><tr><td>바깥</td><td><table><tr><td>안쪽</td></tr></table></td></tr></table>
</body></html>"""


def _blocks(raw: bytes, name: str = "a.html"):
    rr = _html.parse(raw, name)
    assert rr.kind == "pages" and rr.chunk_needed is True
    assert len(rr.pages) == 1 and rr.pages[0]["page_number"] == 1
    return rr.pages[0]["blocks"]


def test_merged_cells_survive_as_html_table():
    blocks = _blocks(MERGED.encode("utf-8"))
    tables = [b for b in blocks if b["type"] == "table"]
    assert len(tables) == 1
    body = tables[0]["table_body"]
    assert 'rowspan="2"' in body and 'colspan="2"' in body
    assert body.lstrip().startswith("<table")


def test_headings_and_text_preserved():
    blocks = _blocks(MERGED.encode("utf-8"))
    texts = [b for b in blocks if b["type"] == "text"]
    assert any(b["text"] == "규정" and b.get("text_level") == 1 for b in texts)
    assert any(b["text"] == "부칙" and b.get("text_level") == 2 for b in texts)
    assert any("제1조" in b["text"] for b in texts)


def test_script_and_style_dropped():
    blocks = _blocks(MERGED.encode("utf-8"))
    joined = " ".join(b.get("text", "") for b in blocks)
    assert "alert(1)" not in joined
    assert "color:red" not in joined


def test_nested_table_counted_once():
    """중첩 표는 바깥 표에 통째로 포함된다 — 블록이 둘로 늘면 안 된다."""
    blocks = _blocks(NESTED.encode("utf-8"))
    tables = [b for b in blocks if b["type"] == "table"]
    assert len(tables) == 1
    assert "안쪽" in tables[0]["table_body"]


def test_cp949_decoded():
    raw = "<html><body><p>규정가나</p></body></html>".encode("cp949")
    blocks = _blocks(raw)
    assert any("규정가나" in b.get("text", "") for b in blocks)


def test_table_only_html_still_yields_table_block():
    raw = "<html><body><table><tr><td>가</td></tr></table></body></html>".encode("utf-8")
    blocks = _blocks(raw)
    assert [b["type"] for b in blocks] == ["table"]


def test_empty_html_raises():
    with pytest.raises(ParserError):
        _html.parse(b"<html><body>   </body></html>", "a.html")
```

- [ ] **Step 3: Run test to verify it fails**

Run: `PYTHONPATH=$PWD /Users/xxx/workspace/8.kb-pipeline/.venv-kb/bin/python -m pytest parse_service/tests/test_parser_html.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'parse_service.parsers.html'`

- [ ] **Step 4: Write the implementation**

`parse_service/parsers/html/__init__.py`:

```python
"""html/htm 도메인 파서 — 형변환 API 없이 "markdown + inline <table> HTML" 을 만든다.

**표를 먼저 빼낸다.** markdownify(그리고 그것을 감싼 markitdown)는 표를 GFM pipe 표로
평탄화하는데, 병합셀이 있으면 열 정렬 자체가 깨진다(실측: rowspan/colspan 표가
헤더 3열 · 데이터행 2셀로 나온다). 리포 불변식은 "표는 <table> HTML 보존"이므로
파싱 **전에** 원문 <table> 을 문자열로 보관하고 자리에 sentinel 을 심은 뒤,
markdown 변환이 끝난 다음 되돌린다.

markitdown 은 도입하지 않는다 — Phase 2d(`a8f9818`)에서 같은 병합 손실 사유로 제거됐고
재유입 가드 `tests/test_no_markitdown.py` 가 있다. 설계문서
`docs/superpowers/specs/2026-08-11-markup-lane-design.md` §2 참조.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup, NavigableString
from markdownify import MarkdownConverter

from parse_service.parsers import RouteResult, ParserError
from parse_service.tools.textdecode import decode_text

#: 이 레인이 받는 확장자. **여기가 유일한 정의다** — router 가 import 해 쓴다.
HTML_EXTS = {"html", "htm"}

#: 표 자리표시자. markdownify 의 이스케이프를 타지 않도록 대문자 영숫자만 쓴다
#: (`_`, `*`, `[` 등은 markdown 특수문자라 백슬래시가 끼어들어 복원이 깨진다).
_SENTINEL = "KBPTBL{}ENDX"
_SENTINEL_RE = re.compile(r"KBPTBL(\d+)ENDX")


def _extract_tables(target) -> list[str]:
    """최상위 <table> 을 원문 문자열로 걷어내고 자리에 sentinel 을 심는다.

    중첩 표는 건너뛴다 — 바깥 표 문자열에 통째로 들어가므로, 따로 뽑으면 같은 표가
    블록 둘로 중복된다. ``find_all`` 이 돌려주는 리스트는 정적이라 순회 중 교체해도
    안전하고, 바깥 표를 교체해도 안쪽 표의 부모 사슬은 그대로라 ``find_parent`` 판정이
    유지된다.
    """
    tables: list[str] = []
    for t in target.find_all("table"):
        if t.find_parent("table") is not None:
            continue
        tables.append(str(t))
        t.replace_with(NavigableString(_SENTINEL.format(len(tables) - 1)))
    return tables


def _restore_tables(md: str, tables: list[str]) -> str:
    """sentinel → 원본 <table> HTML. **앞뒤 빈 줄이 필수다** — 없으면 문단 안 inline
    HTML 이 되어 markdown-it 의 html_block 이 아니라 inline 으로 파싱되고, blockify 의
    table 분기를 타지 못해 표가 통짜 텍스트로 떨어진다."""
    def _sub(m: re.Match) -> str:
        idx = int(m.group(1))
        if idx >= len(tables):        # 방어: 본문에 우연히 같은 패턴이 있던 경우
            return m.group(0)
        return "\n\n" + tables[idx] + "\n\n"
    return _SENTINEL_RE.sub(_sub, md)


def parse(file_bytes: bytes, filename: str, *, ocr_url: str | None = None) -> RouteResult:
    """html/htm → 단일 페이지 blocks. ocr_url 은 router 계약상 받되 쓰지 않는다."""
    text = decode_text(file_bytes, filename)
    soup = BeautifulSoup(text, "html.parser")
    for tag in soup(["script", "style"]):
        tag.extract()
    target = soup.find("body") or soup
    tables = _extract_tables(target)
    md = _restore_tables(
        MarkdownConverter(heading_style="ATX").convert_soup(target), tables)
    if not md.strip():
        raise ParserError(f"empty html: {filename}")
    from kb_pipeline.blockify import hybrid_to_blocks
    blocks = hybrid_to_blocks(md, page_idx=1)
    if not blocks:
        raise ParserError(f"no blocks from html: {filename}")
    return RouteResult(kind="pages", chunk_needed=True,
                       pages=[{"page_number": 1, "blocks": blocks}])
```

- [ ] **Step 5: Run test to verify it passes**

Run: `PYTHONPATH=$PWD /Users/xxx/workspace/8.kb-pipeline/.venv-kb/bin/python -m pytest parse_service/tests/test_parser_html.py -q`
Expected: PASS (7 tests)

- [ ] **Step 6: Commit**

```bash
git add parse_service/parsers/html/__init__.py parse_service/tests/test_parser_html.py requirements.txt
git commit -m "feat(parse-svc): html 하이브리드 파서 — 표는 <table> 보존, 나머지는 markdownify"
```

---

### Task 3: html/xml 라우팅 배선

파서를 만들어도 라우터가 안 보내면 아무 일도 안 일어난다. 여기서 형변환 API 대상에서 html 을 빼고, xml 을 평문으로 돌린다.

**Files:**
- Modify: `parse_service/tools/fileconvert.py:29,32`
- Modify: `parse_service/router.py:15-19,57-70`
- Modify: `parse_service/tests/test_router.py` (기존 파일에 케이스 추가)

**Interfaces:**
- Consumes: `parse_service.parsers.html.HTML_EXTS`, `parse_service.parsers.html.parse`
- Produces: `router.domain_of(filename)` 이 `"html"` 을 반환하는 새 분기

- [ ] **Step 1: Write the failing test**

`parse_service/tests/test_router.py` 끝에 추가:

```python
def test_html_domain_and_no_convert():
    """html 은 형변환 API 를 타지 않고 자체 레인으로 간다."""
    from parse_service import router
    from parse_service.tools import fileconvert

    assert router.domain_of("a.html") == "html"
    assert router.domain_of("A.HTM") == "html"
    assert fileconvert.needs_convert("a.html") is False
    assert fileconvert.needs_convert("a.htm") is False


def test_office_still_converts():
    """형변환 API 대상은 hwp/office 만 남는다(축소는 했지만 없애지 않았다)."""
    from parse_service.tools import fileconvert

    for name in ("a.hwp", "a.hwpx", "a.doc", "a.docx", "a.ppt", "a.pptx"):
        assert fileconvert.needs_convert(name) is True


def test_xml_is_text_not_pdf():
    """xml 이 pdf 도메인으로 떨어지면 `%PDF` 가드에서 죽는다."""
    from parse_service import router

    assert router.domain_of("a.xml") == "text"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD /Users/xxx/workspace/8.kb-pipeline/.venv-kb/bin/python -m pytest parse_service/tests/test_router.py -q`
Expected: FAIL — `domain_of("a.html")` 가 `"pdf"`, `needs_convert("a.html")` 가 `True`, `domain_of("a.xml")` 가 `"pdf"`

- [ ] **Step 3: `fileconvert` 의 두 집합 수정**

`parse_service/tools/fileconvert.py:29` 을 아래로 교체(주석 포함):

```python
#: 명세 §3.1.3 지원 목록 ∩ (비-excel · 비-이미지 · 비-pdf · 비-html). **여기가 유일한 정의다.**
#: odt/odp/ods/rtf 는 명세에 없다 — 넣으면 원격 422 로 문서 전체가 실패한다.
#: html/htm 은 2026-08-11 제외 — `parsers/html` 이 형변환 없이 처리한다(표 <table> 보존).
CONVERTIBLE_EXTS = {"hwp", "hwpx", "doc", "docx", "ppt", "pptx"}
```

`parse_service/tools/fileconvert.py:32` 을 교체:

```python
#: 변환도 파싱도 불필요한 평문. 그대로 블록화한다(router 의 text 도메인).
#: csv 는 2026-08-11 엑셀 레인으로 이동(행 레코드 청크), xml 은 여기로 편입(구 pdf 도메인
#: 오분류 → `%PDF` 가드에서 실패했다).
TEXT_EXTS = {"txt", "md", "markdown", "json", "log", "xml"}
```

- [ ] **Step 4: router 에 html 분기 추가**

`parse_service/router.py` 의 import 무리에 추가:

```python
from parse_service.parsers import html as _html
```

`_PARSERS` 위에 어댑터를 추가:

```python
def _html_parse(fb, fn, **_):
    return _html.parse(fb, fn)
```

`_PARSERS` 를 교체:

```python
_PARSERS = {"pdf": _pdf_parse, "excel": _excel_parse,
            "ocr": _ocr_parse, "html": _html_parse, "text": _text_parse}
```

`domain_of` 의 `TEXT_EXTS` 분기 **바로 위**에 추가(순서 중요 — html 은 `TEXT_EXTS` 에 없으므로 아래에 둬도 동작하지만, 읽는 사람이 분기 순서를 도메인 우선순위로 읽는다):

```python
    if ext in _html.HTML_EXTS:                  # html htm — 형변환 없이 자체 레인
        return "html"
```

- [ ] **Step 5: Run test to verify it passes**

Run: `PYTHONPATH=$PWD /Users/xxx/workspace/8.kb-pipeline/.venv-kb/bin/python -m pytest parse_service/tests/test_router.py -q`
Expected: PASS

- [ ] **Step 6: 전체 회귀**

Run: `PYTHONPATH=$PWD /Users/xxx/workspace/8.kb-pipeline/.venv-kb/bin/python -m pytest parse_service/tests tests service/tests -q`
Expected: PASS, 실패 0 (baseline 646 + 신규)

- [ ] **Step 7: Commit**

```bash
git add parse_service/tools/fileconvert.py parse_service/router.py parse_service/tests/test_router.py
git commit -m "feat(parse-svc): html 을 형변환 API 밖으로, xml 을 평문 레인으로"
```

---

### Task 4: csv → xlsx 합성

**Files:**
- Create: `parse_service/parsers/excel/csv_to_xlsx.py`
- Create: `parse_service/tests/test_csv_to_xlsx.py`

**Interfaces:**
- Consumes: `parse_service.tools.textdecode.decode_text`, `parse_service.parsers.ParserError`
- Produces: `parse_service.parsers.excel.csv_to_xlsx.csv_bytes_to_xlsx(file_bytes: bytes, filename: str) -> bytes`

- [ ] **Step 1: Write the failing test**

`parse_service/tests/test_csv_to_xlsx.py`:

```python
import io

import pytest
from openpyxl import load_workbook

from parse_service.parsers import ParserError
from parse_service.parsers.excel.csv_to_xlsx import csv_bytes_to_xlsx


def _load(raw: bytes, name: str = "인사현황.csv"):
    return load_workbook(io.BytesIO(csv_bytes_to_xlsx(raw, name)))


def test_header_row_is_styled():
    """헤더 서식이 없으면 excel_parser_rag 의 header 감지가 실패해
    `A: 1001, B: 김철수` 로 퇴화한다(detection/header_detector.py 의 style gate)."""
    ws = _load(b"a,b\n1,2\n").active
    assert ws["A1"].font.bold is True
    assert ws["A1"].fill.fgColor.rgb.endswith("DDDDDD")
    assert ws["A2"].font.bold in (False, None)


def test_sheet_title_is_source_stem():
    """청크 텍스트에 시트명이 박힌다 — 임시파일 stem 이 새면 검색어가 오염된다."""
    ws = _load(b"a,b\n1,2\n", "인사현황.csv").active
    assert ws.title == "인사현황"


def test_sheet_title_truncated_to_31_chars():
    """openpyxl 은 31자 초과 시트명을 거부한다."""
    ws = _load(b"a\n1\n", "가" * 40 + ".csv").active
    assert ws.title == "가" * 31


def test_cp949_and_quoted_cells():
    raw = '사번,성명\n1001,"이영희, 차장"\n'.encode("cp949")
    ws = _load(raw).active
    assert ws["A1"].value == "사번"
    assert ws["B2"].value == "이영희, 차장"


def test_pipe_in_cell_survives():
    ws = _load("a,b\n1,리스크|관리부\n".encode("utf-8")).active
    assert ws["B2"].value == "리스크|관리부"


def test_blank_lines_skipped():
    ws = _load(b"a,b\n\n1,2\n\n").active
    assert ws.max_row == 2


def test_empty_csv_raises():
    with pytest.raises(ParserError):
        csv_bytes_to_xlsx(b"\n\n", "a.csv")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD /Users/xxx/workspace/8.kb-pipeline/.venv-kb/bin/python -m pytest parse_service/tests/test_csv_to_xlsx.py -q`
Expected: FAIL — `ModuleNotFoundError: ... csv_to_xlsx`

- [ ] **Step 3: Write the implementation**

`parse_service/parsers/excel/csv_to_xlsx.py`:

```python
"""csv 바이트 → xlsx 바이트. 엑셀 레인이 csv 를 받기 위한 얇은 어댑터.

**헤더 행에 서식(볼드 + 채우기)을 반드시 준다.** `excel_parser_rag` 의 헤더 감지는
`strong` 판정에 `eff_style >= _STYLE_GATE_MIN` 을 요구한다
(`excel_parser_rag/detection/header_detector.py:318-323`). 서식 없는 맨 셀로 합성하면
감지가 **구조적으로 실패**하고 `key = headers.get(c) or get_column_letter(c)`
(`parsers/flat_table.py:175`) 폴백이 걸려 청크가 `사번: 1001` 이 아니라 `A: 1001` 로
퇴화한다. 헤더 행 자체도 데이터행으로 오인돼 청크가 하나 늘어난다(실측).
"""
from __future__ import annotations

import csv
import io
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

from parse_service.parsers import ParserError
from parse_service.tools.textdecode import decode_text

#: openpyxl 시트명 상한. 넘기면 저장 시점이 아니라 title 대입에서 바로 터진다.
_SHEET_TITLE_MAX = 31


def csv_bytes_to_xlsx(file_bytes: bytes, filename: str) -> bytes:
    text = decode_text(file_bytes, filename)
    rows = [r for r in csv.reader(io.StringIO(text))
            if any((c or "").strip() for c in r)]
    if not rows:
        raise ParserError(f"empty csv: {filename}")

    stem = Path(filename).stem or "upload"
    wb = Workbook()
    ws = wb.active
    ws.title = stem[:_SHEET_TITLE_MAX] or "Sheet1"
    for row in rows:
        ws.append(row)

    header_font = Font(bold=True)
    header_fill = PatternFill("solid", fgColor="DDDDDD")
    for cell in ws[1]:
        cell.font = header_font
        cell.fill = header_fill

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD /Users/xxx/workspace/8.kb-pipeline/.venv-kb/bin/python -m pytest parse_service/tests/test_csv_to_xlsx.py -q`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add parse_service/parsers/excel/csv_to_xlsx.py parse_service/tests/test_csv_to_xlsx.py
git commit -m "feat(parse-svc): csv → xlsx 합성기 (헤더 서식 부여로 header 감지 보장)"
```

---

### Task 5: csv 를 엑셀 레인에 배선

**Files:**
- Modify: `parse_service/parsers/excel/__init__.py:14`(`EXCEL_EXTS`), `:59-72`(`_fetch_rag_chunks` 앞부분)
- Modify: `parse_service/tools/fileconvert.py` — Task 3 에서 이미 `TEXT_EXTS` 에서 `csv` 를 뺐다. 여기서 재확인만 한다.
- Modify: `parse_service/tests/test_parser_excel.py` (기존 파일에 케이스 추가)

**Interfaces:**
- Consumes: `parse_service.parsers.excel.csv_to_xlsx.csv_bytes_to_xlsx`
- Produces: `EXCEL_EXTS` 에 `"csv"` 포함; `parse(csv_bytes, "x.csv")` 가 `kind="chunks"`, `chunk_needed=False`, `gate_summary` 포함 반환

- [ ] **Step 1: Write the failing test**

`parse_service/tests/test_parser_excel.py` 끝에 추가:

```python
def test_csv_routes_to_excel_lane():
    from parse_service import router
    from parse_service.tools import fileconvert

    assert router.domain_of("a.csv") == "excel"
    assert "csv" not in fileconvert.TEXT_EXTS      # 둘 다 해야 한다 — 하나만 하면 옛 레인으로 샌다
    assert fileconvert.needs_convert("a.csv") is False


def test_csv_yields_header_keyed_record_chunks(monkeypatch):
    """csv 청크는 `사번: 1001` 이어야 한다. `A: 1001` 이면 헤더 감지가 실패한 것."""
    from parse_service.parsers import excel as _excel

    # auto 는 전결 키워드/계층 지배도가 없으면 kordoc 으로 떨어진다 → csv 는 openpyxl 고정.
    # 이 테스트는 그 고정이 env 와 무관하게 걸리는지도 함께 본다.
    monkeypatch.setenv("EXCEL_PARSER_BACKEND", "auto")
    monkeypatch.delenv("KORDOC_BIN", raising=False)

    raw = "사번,성명,부서\n1001,김철수,전략기획부\n1002,이영희,리스크관리부\n".encode("utf-8")
    rr = _excel.parse(raw, "인사현황.csv")

    assert rr.kind == "chunks" and rr.chunk_needed is False
    assert rr.gate_summary is not None and rr.gate_summary.get("ok") is True
    joined = "\n".join(c["text"] for c in rr.chunks)
    assert "사번: 1001, 성명: 김철수, 부서: 전략기획부" in joined
    assert "A: 1001" not in joined


def test_csv_chunks_do_not_leak_tempfile_stem():
    from parse_service.parsers import excel as _excel

    raw = "사번,성명\n1001,김철수\n".encode("utf-8")
    rr = _excel.parse(raw, "인사현황.csv")
    joined = "\n".join(c["text"] for c in rr.chunks)
    assert "excel_parser_" not in joined
    assert "인사현황" in joined
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD /Users/xxx/workspace/8.kb-pipeline/.venv-kb/bin/python -m pytest parse_service/tests/test_parser_excel.py -q`
Expected: FAIL — `domain_of("a.csv")` 가 `"text"`, 그리고 `_excel.parse(..., "*.csv")` 가 openpyxl 로 열리지 않아 실패

- [ ] **Step 3: `EXCEL_EXTS` 확장**

`parse_service/parsers/excel/__init__.py:14` 교체:

```python
#: csv 는 2026-08-11 편입 — 메모리상 xlsx 로 합성해 같은 백엔드로 흘린다(`csv_to_xlsx`).
EXCEL_EXTS = {"xlsx", "xlsm", "xls", "csv"}
```

- [ ] **Step 4: `_fetch_rag_chunks` 에 csv 분기**

`safe_filename` 계산 직후, `cfg_kwargs` 구성 **앞**에 삽입한다:

```python
    ext = Path(safe_filename).suffix.lower().lstrip(".")
    is_csv = ext == "csv"
    if is_csv:
        # csv → xlsx 합성. document_title 은 아래에서 원본 stem 을 그대로 쓰므로
        # 파일명은 바꾸지 않고 바이트와 suffix 만 갈아끼운다.
        from parse_service.parsers.excel.csv_to_xlsx import csv_bytes_to_xlsx
        file_bytes = csv_bytes_to_xlsx(file_bytes, safe_filename)
```

`suffix` 와 `cfg_kwargs` 를 교체:

```python
    suffix = ".xlsx" if is_csv else (Path(safe_filename).suffix.lower() or ".xlsx")
    cfg_kwargs: dict = {
        # csv 는 백엔드를 openpyxl 로 고정한다. 기본 `auto` 는 "전결" 키워드(Tier1)나
        # 계층 지배도(Tier1.5)가 있을 때만 openpyxl 을 쓰고 그 외에는 kordoc 으로
        # 떨어지는데(backends/auto_backend.py), csv 유래 평면 표는 둘 다 아니다.
        # csv 에는 병합셀·다중시트·수식이 없어 kordoc 의 렌더 충실도 이점이 없고,
        # KORDOC_BIN 이 없는 환경에선 아예 실패한다(실측).
        "backend": "openpyxl" if is_csv else os.environ.get("EXCEL_PARSER_BACKEND", "auto"),
        "document_title": Path(safe_filename).stem,
    }
```

- [ ] **Step 5: Run test to verify it passes**

Run: `PYTHONPATH=$PWD /Users/xxx/workspace/8.kb-pipeline/.venv-kb/bin/python -m pytest parse_service/tests/test_parser_excel.py -q`
Expected: PASS

- [ ] **Step 6: 전체 회귀**

Run: `PYTHONPATH=$PWD /Users/xxx/workspace/8.kb-pipeline/.venv-kb/bin/python -m pytest parse_service/tests tests service/tests -q`
Expected: PASS, 실패 0

- [ ] **Step 7: Commit**

```bash
git add parse_service/parsers/excel/__init__.py parse_service/tests/test_parser_excel.py
git commit -m "feat(parse-svc): csv 를 엑셀 레인으로 — openpyxl 고정 + 행 레코드 청크"
```

---

### Task 6: 폐쇄망 가드

가드를 만들어만 두고 안 돌리면 없는 것과 같다(`guard-exists-but-never-ran` 전례). `verify-bundle.sh` 는 이미 엑셀 왕복 스모크를 돌리므로 html 왕복도 같은 자리에 붙인다.

**Files:**
- Modify: `scripts/airgap/verify-bundle.sh` — `check_imports()` 안에 html 스모크 추가, 말미 형변환 API 주석 정정

**Interfaces:**
- Consumes: 이미지 안의 `parse_service.parsers.html.parse`
- Produces: 없음(쉘 가드)

- [ ] **Step 1: html 왕복 스모크 추가**

`verify-bundle.sh` 의 엑셀 왕복 `case "$xout" in ... esac` **직후**, 형변환 API 주석 **앞**에 삽입한다:

```bash
  # ★ html 레인은 2026-08-11 형변환 API 밖으로 나왔다(parsers/html + markdownify).
  #   markdownify 가 이미지에 빠지면 html 적재만 조용히 죽는다 — requirements.txt 에
  #   넣었어도 `pip install .` 이 건너뛰는 전례가 있었다(kb 이미지 문서 추출기 누락).
  #   import 존재만이 아니라 **병합셀 표가 <table> 로 살아 나오는지**까지 확인한다.
  echo "== html 파싱 왕복 스모크(markdownify + 표 보존) =="
  local HTML_PY='
import sys
from parse_service.parsers.html import parse as hparse
raw = b"<html><body><h1>T</h1><table><tr><th rowspan=\"2\">a</th><th colspan=\"2\">b</th></tr><tr><td>1</td><td>2</td></tr></table></body></html>"
rr = hparse(raw, "smoke.html")
blocks = (rr.pages or [{}])[0].get("blocks") or []
tables = [b for b in blocks if b.get("type") == "table"]
if not tables:
    print("NO_TABLE_BLOCK: %d blocks" % len(blocks)); sys.exit(1)
body = tables[0].get("table_body") or ""
if "rowspan" not in body or "colspan" not in body:
    print("MERGE_LOST: %s" % body[:120]); sys.exit(1)
print("OK html_blocks=%d" % len(blocks))
'
  local hout
  if [ -n "$TIMEOUT_BIN" ]; then
    hout="$("$TIMEOUT_BIN" "${IMPORTS_CHECK_TIMEOUT:-120}" "$ENGINE" run --rm -w /app -e PYTHONPATH=/app \
      --entrypoint python "$ref" -c "$HTML_PY" 2>&1)"
  else
    hout="$("$ENGINE" run --rm -w /app -e PYTHONPATH=/app --entrypoint python "$ref" -c "$HTML_PY" 2>&1)"
  fi
  case "$hout" in
    *OK\ html_blocks=*) echo "  ${GRN}✓ html 왕복 성공 — ${hout##*OK }${RST}" ;;
    *NO_TABLE_BLOCK:*|*MERGE_LOST:*) echo "  ${RED}✗ html 표 보존 실패 — pipe 평탄화 회귀다:${RST}"
       echo "$hout" | tail -4 | sed 's/^/    /'; return 1 ;;
    *) echo "  ${RED}✗ html 파싱 왕복 실패 — markdownify 누락이 유력하다:${RST}"
       echo "$hout" | tail -6 | sed 's/^/    /'; return 1 ;;
  esac
```

- [ ] **Step 2: 형변환 API 주석에서 html 제거**

`check_imports()` 말미의 주석에서 `docx/hwp/ppt/html 파싱은` 을 `docx/hwp/ppt 파싱은` 으로 고치고, 한 줄 덧붙인다:

```bash
  # (html 은 2026-08-11 이 경로에서 빠졌다 — parsers/html 이 형변환 없이 처리한다.)
```

- [ ] **Step 3: 문법 검사**

Run: `bash -n scripts/airgap/verify-bundle.sh`
Expected: 출력 없음(문법 오류 0)

- [ ] **Step 4: 스모크 본문을 이미지 없이 검증**

컨테이너 이미지가 없어도 파이썬 본문 자체는 호스트에서 돌려볼 수 있다. `HTML_PY` 와 같은 코드를 임시 파일로 떨어뜨려 실행한다.

Run:
```bash
PYTHONPATH=$PWD /Users/xxx/workspace/8.kb-pipeline/.venv-kb/bin/python -c '
from parse_service.parsers.html import parse as hparse
raw = b"<html><body><h1>T</h1><table><tr><th rowspan=\"2\">a</th><th colspan=\"2\">b</th></tr><tr><td>1</td><td>2</td></tr></table></body></html>"
rr = hparse(raw, "smoke.html")
blocks = rr.pages[0]["blocks"]
tables = [b for b in blocks if b.get("type") == "table"]
assert tables and "rowspan" in tables[0]["table_body"] and "colspan" in tables[0]["table_body"]
print("OK html_blocks=%d" % len(blocks))'
```
Expected: `OK html_blocks=2`

- [ ] **Step 5: Commit**

```bash
git add scripts/airgap/verify-bundle.sh
git commit -m "feat(airgap): html 왕복 스모크 — markdownify 누락과 표 평탄화 회귀를 배포 전에 잡는다"
```

---

### Task 7: 문서 반영

코드만 바꾸고 문서를 방치하지 않는다(CLAUDE.md 워크플로).

**Files:**
- Modify: `_workspace/01-architecture.md` (§ 확장자→도메인 라우팅 표, 청킹 소유 서술)
- Modify: `_workspace/02-changes.md` (신규 절)
- Modify: `_workspace/03-dev-progress.md` (진행 반영)
- Modify: `docs/kb-pipeline-process-definition.md` (라우팅 서술)
- Modify: `deferred.md` (비목표 기록)

**Interfaces:** 없음(문서).

- [ ] **Step 1: `_workspace/01-architecture.md` 라우팅 표 갱신**

`| 확장자 | 도메인 | 파서(in-process) | \`<table>\` HTML | \`chunk_needed\` | 비고 |` 표에서:
- `html`/`htm` 행을 추가하거나 고쳐 도메인 `html`, 파서 `parsers/html`(markdownify + `<table>` 보존), `<table>` ✅, `chunk_needed` True
- `csv` 를 text 도메인에서 빼고 `excel` 도메인(`openpyxl` 고정, `chunk_needed` False)으로 옮김
- `xml` 을 text 도메인에 추가
- 형변환 API 대상에서 html 제거

같은 파일의 "청킹 소유" 서술에 한 줄 추가:

```markdown
- **csv 의 청킹 소유는 엑셀 레인**(2026-08-11) — csv 는 메모리상 xlsx 로 합성돼
  `chunk_needed=False` 로 자체 청킹된다. facade `/chunk` 를 타지 않는다.
```

- [ ] **Step 2: `_workspace/02-changes.md` 에 절 추가**

```markdown
## N. 구조화 텍스트 레인 (2026-08-11)

**결정**: html/htm 을 한컴 형변환 API 대상에서 제외하고 `parsers/html` 이 직접 처리한다.
csv 는 엑셀 레인으로 옮긴다. xml 을 평문 레인에 편입한다.

- **markitdown 재검토 후 기각.** 실측: 병합셀 html 표에서 열 정렬이 붕괴(헤더 3열 ·
  데이터행 2셀), json/xml 은 `PlainTextConverter` passthrough(변환 0줄), site-packages
  +140MB(onnxruntime/sympy/numpy/magika). Phase 2d 에서 같은 사유로 제거된 이력이 있고
  재유입 가드 `test_no_markitdown` 이 있다 — 가드 유지. 엔진으로는 markitdown 이 내부에서
  쓰는 `markdownify` 만 직접 채택.
- **html**: 최상위 `<table>` 을 sentinel 로 빼두고 나머지만 markdown 화 → 복원 →
  `hybrid_to_blocks`. colspan/rowspan 보존.
- **csv**: 헤더 행에 서식(볼드+채우기)을 준 xlsx 로 합성. 서식이 없으면
  `header_detector` 의 style gate 에 걸려 청크가 `A: 1001` 로 퇴화한다. 백엔드는
  `openpyxl` 고정(`auto` 는 전결/계층이 없으면 kordoc 으로 떨어진다).
- **xml**: 구 `pdf` 도메인 오분류로 `%PDF` 가드에서 실패하던 것을 `TEXT_EXTS` 편입으로 해소.
- 신규 의존성 `markdownify` 하나. env 변경 없음. `verify-bundle.sh` 에 html 왕복 스모크 추가.
```

- [ ] **Step 3: `_workspace/03-dev-progress.md` 반영**

완료 항목으로 한 줄 추가한다(형식은 주변 항목을 따른다): 구조화 텍스트 레인 — html 형변환 이탈 / csv 엑셀 레인 / xml 편입, 브랜치 `feat/markup-lane`.

- [ ] **Step 4: `docs/kb-pipeline-process-definition.md` 라우팅 서술 갱신**

`- **그 외(폴백, 예: hwpx)** → ...` 부근의 라우팅 목록에 html 레인을 추가하고, 형변환 API 설명에서 html 을 뺀다.

- [ ] **Step 5: `deferred.md` 에 비목표 기록**

```markdown
- **json/xml 구조 변환** (2026-08-11, markup-lane) — 지금은 평문 통과. markitdown 도
  하지 않는 일이라 기능 손실은 없다. 정형 API 응답 같은 문서가 실제로 들어오기 시작하면
  키 계층 → 헤딩/표 변환을 검토한다.
- **tsv·세미콜론 구분자 csv** (2026-08-11, markup-lane) — 구분자 콤마 고정.
  `csv.Sniffer` 는 오작동 위험이 있어 실제 요구가 생길 때 붙인다.
- **초대형 csv 의 청크 수 폭증** (2026-08-11, markup-lane) — 행당 1청크라 10만 행이면
  10만 청크다. 엑셀 레인이 xlsx 에 대해 이미 갖는 동일 문제라 여기서 따로 풀지 않았다.
  적재 지연·비용이 실제로 문제가 되면 엑셀 레인 차원에서 함께 다룬다.
```

`deferred.md` 가 없으면 새로 만든다(리포 루트).

- [ ] **Step 6: `todo_list.md` 정리**

`todo_list.md` 에 이 작업에 해당하는 미완료 항목이 있으면 삭제한다(체크만 하지 말고 제거). 없으면 건드리지 않는다.

- [ ] **Step 7: 최종 회귀 + Commit**

Run: `PYTHONPATH=$PWD /Users/xxx/workspace/8.kb-pipeline/.venv-kb/bin/python -m pytest parse_service/tests tests service/tests -q`
Expected: PASS, 실패 0

```bash
git add _workspace docs deferred.md todo_list.md
git commit -m "docs: 구조화 텍스트 레인 반영 — 라우팅 표/청킹 소유/변경 이력/비목표"
```

---

## 구현 후 검증

계획서에서 100번 읽는 것보다 한 번 돌리는 게 확실한 항목들. 구현 중 실측으로 닫고 증거(테스트·실행 로그)를 남긴다.

- [ ] `markdownify` 의 `MarkdownConverter(heading_style="ATX").convert_soup(...)` 시그니처가 설치된 버전(1.2.2)과 맞는지 — Task 2 테스트가 곧바로 드러낸다.
- [ ] `_fetch_rag_chunks` 삽입 지점의 실제 라인 번호(계획서는 `:59-72` 로 적었으나 편집 후 밀린다) — 문자열 앵커로 편집한다.
- [ ] `verify-bundle.sh` 의 `local` 선언이 함수 안에 있는지(bash 문법) — `bash -n` 으로 확인.
- [ ] `_workspace/01-architecture.md` 라우팅 표의 실제 컬럼 수·정렬 — 파일을 열어 맞춘다.
- [ ] `deferred.md` 존재 여부와 기존 항목 번호 체계(D28 등) — 열어보고 형식을 따른다.
