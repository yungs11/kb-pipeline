<!-- plan-version: v2 -->
<!-- ultracode-validation: PENDING -->

# 구조화 텍스트 레인 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** html/htm 을 한컴 형변환 API 없이 파싱하고(표는 `<table>` HTML 보존), csv 를 엑셀 레인의 행 레코드 청크로 만들고, xml 이 더 이상 실패하지 않게 한다.

**Architecture:** 세 갈래다. ① html/htm → 신규 `parsers/html` — 표를 sentinel 로 빼두고 나머지만 `markdownify` 로 markdown 화한 뒤 원본 `<table>` HTML 을 되돌려 `blockify` 에 넘긴다. ② csv → 메모리상 xlsx 로 합성(헤더 행에 서식 부여)해 기존 엑셀 레인에 위임, 백엔드는 `openpyxl` 고정. ③ xml → `TEXT_EXTS` 추가(평문 통과). markitdown 은 도입하지 않는다(설계문서 §2, 재유입 가드 유지).

**Tech Stack:** `markdownify`(신규), `beautifulsoup4`, `openpyxl`, `markdown-it-py`, pytest. 런타임 컨테이너는 Python 3.12(`Dockerfile.parse-svc`), 개발 검증용 `.venv-kb` 는 Python 3.14.5 — **두 버전에서 모두 도는 코드만 쓴다**(버전별 분기 금지).

**설계문서:** `docs/superpowers/specs/2026-08-11-markup-lane-design.md`

## Global Constraints

- **표는 `<table>` HTML 로 보존한다. pipe 평탄화 금지**(colspan/rowspan 손실). 리포 불변식.
- **markitdown 을 도입하지 않는다.** 재유입 가드 `parse_service/tests/test_no_markitdown.py` 는 수정·삭제 금지.
- **신규 pip 의존성은 `markdownify` 하나.** `requirements.txt` 에만 추가한다. `pyproject.toml` 은 건드리지 않는다(`requires-python = ">=3.9"` 와 충돌 회피).
- **env 신설·삭제·기본값 변경 없음.** 따라서 `.env.example` 계열 6종은 수정 대상이 아니다.
- **디코딩에 `errors="replace"` 금지.** U+FFFD 범벅이 '성공한 쓰레기'로 임베딩까지 간다.
- **`domain_of` 의 확장자 집합은 한 커밋 안에서 정합해야 한다.** csv 를 `TEXT_EXTS` 에서 빼는 것과 `EXCEL_EXTS` 에 넣는 것을 다른 커밋으로 쪼개면, 사이 커밋에서 csv 가 `pdf` 도메인으로 떨어져 `app.py:269-271` 의 `%PDF` 가드가 **모든 csv 업로드를 거부**한다(지금 성공하는 입력의 회귀).
- 테스트 실행: `PYTHONPATH=$PWD /Users/xxx/workspace/8.kb-pipeline/.venv-kb/bin/python -m pytest …` (repo 루트 `.venv` 에는 fastapi/pymupdf 가 없다).
- 베이스라인: `646 passed, 3 skipped`.

---

## File Structure

**신규**
- `parse_service/tools/textdecode.py` — 바이트→문자열 디코딩 사다리. html/csv/text 세 레인이 공유.
- `parse_service/parsers/html/__init__.py` — html/htm 도메인 파서(표 보존 하이브리드).
- `parse_service/parsers/excel/csv_to_xlsx.py` — csv 바이트 → xlsx 바이트 합성.
- `parse_service/tests/test_tools_textdecode.py`
- `parse_service/tests/test_parser_html.py`
- `parse_service/tests/test_csv_to_xlsx.py`
- `deferred.md`(리포 루트, 신규 — 지금 존재하지 않는다. D 번호는 기존 최대 **D38** 다음인 D39 부터.)

**수정**
- `parse_service/tools/fileconvert.py:29`(`CONVERTIBLE_EXTS`), `:32`(`TEXT_EXTS`)
- `parse_service/router.py` — 모듈 docstring, import, `_PARSERS`, `domain_of`
- `parse_service/parsers/excel/__init__.py:14`(`EXCEL_EXTS`), `:61-66`(`_fetch_rag_chunks` 앞부분)
- `parse_service/tests/test_router.py:1`(docstring), `:15`(parametrize)
- `parse_service/tests/test_parser_excel.py` — 케이스 추가
- `requirements.txt`
- `scripts/airgap/verify-bundle.sh`
- `_workspace/01-architecture.md`, `_workspace/02-changes.md`, `_workspace/03-dev-progress.md`, `docs/kb-pipeline-process-definition.md`

---

### Task 1: 공용 디코딩 헬퍼

지금 디코딩 사다리는 `router._text_parse` 안에 인라인으로 있다. html·csv 레인도 같은 사다리가 필요하므로 하나로 뽑는다. 정의가 셋이 되면 드리프트가 생긴다.

**Files:**
- Create: `parse_service/tools/textdecode.py`
- Create: `parse_service/tests/test_tools_textdecode.py`
- Modify: `parse_service/router.py:34-54` (`_text_parse`)

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


def test_utf32_not_silently_mojibake():
    """UTF-32-LE BOM(ff fe 00 00)은 utf-16 BOM(ff fe)으로 시작한다.

    2바이트만 보고 utf-16 을 태우면 예외 없이 NUL 섞인 mojibake 가 나온다
    (실측: "규정".encode("utf-32").decode("utf-16") == '\\x00규\\x00정\\x00').
    errors="replace" 를 금지한 것과 같은 실패 유형이라 같은 강도로 막는다.
    """
    assert decode_text("규정".encode("utf-32"), "a.txt") == "규정"
    assert decode_text("규정".encode("utf-32-be"), "a.txt") == "규정"


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

**UTF-32 를 먼저 가른다.** UTF-32-LE BOM(``ff fe 00 00``)은 UTF-16-LE BOM(``ff fe``)으로
시작해서, 2바이트만 보면 utf-16 으로 '성공'하고 NUL 섞인 mojibake 가 예외 없이 통과한다
(실측: ``"규정".encode("utf-32").decode("utf-16") == '\\x00규\\x00정\\x00'``).

``errors="replace"`` 금지 — U+FFFD 범벅이 '성공한 쓰레기'로 적재된다.
"""
from __future__ import annotations

from parse_service.parsers import ParserError

_UTF32_BOMS = (b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")
_UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")


def _candidates(file_bytes: bytes) -> tuple[str, ...]:
    if file_bytes[:4] in _UTF32_BOMS:
        return ("utf-32", "utf-8-sig", "cp949")
    if file_bytes[:2] in _UTF16_BOMS:
        return ("utf-16", "utf-8-sig", "cp949")
    return ("utf-8-sig", "cp949")


def decode_text(file_bytes: bytes, filename: str) -> str:
    cands = _candidates(file_bytes)
    for enc in cands:
        try:
            return file_bytes.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ParserError(f"decode failed ({'/'.join(cands)}): {filename}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD /Users/xxx/workspace/8.kb-pipeline/.venv-kb/bin/python -m pytest parse_service/tests/test_tools_textdecode.py -q`
Expected: PASS (6 tests)

- [ ] **Step 5: `_text_parse` 를 헬퍼로 전환**

`parse_service/router.py` 의 `_text_parse` 본문에서 인라인 사다리(현재 `router.py:40-49`)를 지우고 헬퍼를 부른다. 나머지(빈 문자열 검사, `hybrid_to_blocks`, `RouteResult`)는 그대로 둔다.

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

- [ ] **Step 6: 회귀 확인**

Run: `PYTHONPATH=$PWD /Users/xxx/workspace/8.kb-pipeline/.venv-kb/bin/python -m pytest parse_service/tests -q`
Expected: PASS, 실패 0

- [ ] **Step 7: Commit**

```bash
git add parse_service/tools/textdecode.py parse_service/tests/test_tools_textdecode.py parse_service/router.py
git commit -m "refactor(parse-svc): 디코딩 사다리를 tools/textdecode 로 통일 + UTF-32 BOM 오판 차단"
```

---

### Task 2: html 하이브리드 파서

표를 먼저 빼내고 나머지만 markdown 으로 만든 뒤 되돌린다. markitdown 을 쓰지 않는 이유는 설계문서 §2 참조.

**Files:**
- Create: `parse_service/parsers/html/__init__.py`
- Create: `parse_service/tests/test_parser_html.py`
- Modify: `requirements.txt`

**Interfaces:**
- Consumes: `parse_service.tools.textdecode.decode_text`, `parse_service.parsers.RouteResult`/`ParserError`, `kb_pipeline.blockify.hybrid_to_blocks`
- Produces: `parse_service.parsers.html.HTML_EXTS: set[str]`, `parse_service.parsers.html.parse(file_bytes: bytes, filename: str, *, ocr_url=None) -> RouteResult`

- [ ] **Step 1: 의존성 추가**

`requirements.txt` 의 `openpyxl>=3.1.0` 바로 아래 줄에 추가:

```
markdownify>=1.2.0
```

설치 확인: `/Users/xxx/workspace/8.kb-pipeline/.venv-kb/bin/python -c "from markdownify import MarkdownConverter; print(MarkdownConverter(heading_style='ATX'))"`
(이미 1.2.2 가 설치돼 있어 통과해야 한다. 실패하면 `VIRTUAL_ENV=/Users/xxx/workspace/8.kb-pipeline/.venv-kb uv pip install "markdownify>=1.2.0"`.)

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

THREE = """<html><body>
<p>머리말</p>
<table><tr><td>표하나</td></tr></table>
<p>사이글</p>
<table><tr><td>표둘</td></tr></table>
<p>또사이</p>
<table><tr><td>표셋</td></tr></table>
</body></html>"""

# 표 **내부**에 빈 줄이 있는 경우. markdown-it 의 html_block 은 빈 줄에서 끝나므로
# 그대로 재삽입하면 table_body 가 잘리고 나머지 마크업이 raw 텍스트로 샌다(실측).
BLANKLINE_IN_TABLE = """<html><body>
<table><tr><td>줄1

줄2</td></tr><tr><td>끝행</td></tr></table>
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


def test_multiple_tables_keep_order_and_identity():
    """표가 하나뿐인 fixture 만 있으면 인덱스가 뒤바뀌어도 테스트가 초록이다."""
    blocks = _blocks(THREE.encode("utf-8"))
    tables = [b for b in blocks if b["type"] == "table"]
    assert len(tables) == 3
    for i, marker in enumerate(("표하나", "표둘", "표셋")):
        assert marker in tables[i]["table_body"]
    joined = " ".join(b.get("text", "") for b in blocks)
    assert "KBPTBL" not in joined          # sentinel 잔존 없음


def test_blank_line_inside_table_does_not_split_block():
    """표 내부 빈 줄이 html_block 을 끊으면 표 뒷부분이 raw HTML 텍스트로 샌다."""
    blocks = _blocks(BLANKLINE_IN_TABLE.encode("utf-8"))
    tables = [b for b in blocks if b["type"] == "table"]
    assert len(tables) == 1
    body = tables[0]["table_body"]
    assert "끝행" in body and body.rstrip().endswith("</table>")
    joined = " ".join(b.get("text", "") for b in blocks)
    assert "</table>" not in joined       # 마크업이 텍스트 블록으로 새지 않았다


def test_body_text_looking_like_sentinel_is_not_replaced():
    """본문에 자리표시자와 같은 문자열이 있어도 표로 치환되면 안 된다."""
    raw = ("<html><body><p>KBPTBL0ENDX 라고 적힌 문단</p>"
           "<table><tr><td>진짜표</td></tr></table></body></html>").encode("utf-8")
    blocks = _blocks(raw)
    tables = [b for b in blocks if b["type"] == "table"]
    assert len(tables) == 1
    assert "진짜표" in tables[0]["table_body"]


def test_data_uri_image_not_inlined():
    """data-URI 는 blockify 가 img_path 로 실어 modal payload 까지 흘려보낸다
    (kb_pipeline/modal.py:586). 수 MB base64 가 임베딩까지 가면 안 된다."""
    raw = ('<html><body><p>앞</p>'
           '<img src="data:image/png;base64,AAAABBBBCCCC" alt="도표1">'
           '</body></html>').encode("utf-8")
    blocks = _blocks(raw)
    dumped = repr(blocks)
    assert "base64," not in dumped
    assert "도표1" in dumped               # alt 텍스트는 살린다


def test_cp949_decoded():
    raw = "<html><body><p>규정가나</p></body></html>".encode("cp949")
    blocks = _blocks(raw)
    assert any("규정가나" in b.get("text", "") for b in blocks)


def test_table_only_html_still_yields_table_block():
    raw = "<html><body><table><tr><td>가</td></tr></table></body></html>".encode("utf-8")
    blocks = _blocks(raw)
    assert [b["type"] for b in blocks] == ["table"]


def test_fragment_without_body_tag():
    """<body> 없는 조각 html — soup 전체를 대상으로 삼아야 한다."""
    blocks = _blocks(b"<p>\xea\xb0\x80</p><table><tr><td>\xeb\x82\x98</td></tr></table>")
    assert any(b["type"] == "table" for b in blocks)


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
import uuid

from bs4 import BeautifulSoup, NavigableString
from markdownify import MarkdownConverter

from parse_service.parsers import RouteResult, ParserError
from parse_service.tools.textdecode import decode_text

#: 이 레인이 받는 확장자. **여기가 유일한 정의다** — router 가 import 해 쓴다.
HTML_EXTS = {"html", "htm"}

#: 표 **내부**의 빈 줄을 접는다. markdown-it 의 html_block 은 빈 줄에서 끝나므로
#: (CommonMark type 6), 표 안에 빈 줄이 남으면 table_body 가 거기서 잘리고 나머지
#: 마크업이 raw 텍스트 블록으로 샌다(실측). bs4 는 텍스트 노드 안의 \n\n 을 접지 않는다.
_BLANK_LINES = re.compile(r"\n[ \t]*\n+")


def _extract_tables(target, sentinel_fmt: str) -> list[str]:
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
        tables.append(_BLANK_LINES.sub("\n", str(t)))
        t.replace_with(NavigableString(sentinel_fmt.format(len(tables) - 1)))
    return tables


def _strip_data_uri_images(target) -> None:
    """data-URI <img> 를 alt 텍스트로 바꾼다.

    blockify 는 이미지를 ``{"type": "image", "img_path": <src 원문>}`` 으로 만들고
    modal 은 그 값을 payload 본문으로 쓴다(`kb_pipeline/modal.py:586`). data-URI 면
    수 MB base64 가 enriched_content → 청킹 → 임베딩까지 흐르는데, html 은
    `IMAGE_EXTS` 가 아니라 MinIO 업로드도 없어 **참조 불가능한 거대 문자열**만 남는다.
    표 추출보다 **먼저** 돌려야 표 안의 data-URI 도 걸린다.
    """
    for img in target.find_all("img"):
        if (img.get("src") or "").strip().lower().startswith("data:"):
            img.replace_with(NavigableString(img.get("alt") or ""))


def parse(file_bytes: bytes, filename: str, *, ocr_url: str | None = None) -> RouteResult:
    """html/htm → 단일 페이지 blocks. ocr_url 은 router 계약상 받되 쓰지 않는다."""
    text = decode_text(file_bytes, filename)
    soup = BeautifulSoup(text, "html.parser")
    for tag in soup(["script", "style"]):
        tag.extract()
    target = soup.find("body") or soup
    _strip_data_uri_images(target)

    # sentinel 은 호출마다 유일해야 한다. 고정 문자열이면 본문에 같은 글자가 있을 때
    # 그 텍스트가 표로 치환돼 표가 중복되고 문단이 조각난다(실측). 대문자 영숫자만
    # 쓰는 이유는 markdownify 이스케이프(`_`, `*`, `[` 앞 백슬래시)를 피하기 위해서다.
    nonce = uuid.uuid4().hex[:12].upper()
    sentinel_fmt = f"KBPTBL{nonce}" + "{}ENDX"
    sentinel_re = re.compile(f"KBPTBL{nonce}" + r"(\d+)ENDX")

    tables = _extract_tables(target, sentinel_fmt)
    md = MarkdownConverter(heading_style="ATX").convert_soup(target)

    def _restore(m: "re.Match[str]") -> str:
        idx = int(m.group(1))
        if idx >= len(tables):
            return m.group(0)
        # 앞뒤 빈 줄이 있어야 markdown-it 이 html_block 으로 연다(문단 안 inline HTML 이
        # 아니라). 들여쓰기 없이 열 0 에서 시작해야 한다 — 4칸 들여쓰면 코드블록이 된다.
        return "\n\n" + tables[idx] + "\n\n"

    md = sentinel_re.sub(_restore, md)
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
Expected: PASS (12 tests)

- [ ] **Step 6: Commit**

```bash
git add parse_service/parsers/html/__init__.py parse_service/tests/test_parser_html.py requirements.txt
git commit -m "feat(parse-svc): html 하이브리드 파서 — 표는 <table> 보존, 나머지는 markdownify"
```

---

### Task 3: html/xml 라우팅 배선

파서를 만들어도 라우터가 안 보내면 아무 일도 안 일어난다. **csv 는 여기서 건드리지 않는다** — csv 이동은 Task 5 에서 한 커밋으로 한다(Global Constraints 참조).

**Files:**
- Modify: `parse_service/tools/fileconvert.py:29`(`CONVERTIBLE_EXTS`), `:32`(`TEXT_EXTS` — **xml 추가만**, csv 는 그대로 둔다)
- Modify: `parse_service/router.py` — 모듈 docstring(`:1-12`), import(`:15-19`), `_PARSERS`(`:57-58`), `domain_of`(`:61-70`)
- Modify: `parse_service/tests/test_router.py:1`(docstring), `:15`(parametrize 행), 새 테스트 추가

**Interfaces:**
- Consumes: `parse_service.parsers.html.HTML_EXTS`, `parse_service.parsers.html.parse`
- Produces: `router.domain_of(filename)` 이 `"html"` 반환; `router._PARSERS` 키가 5개

- [ ] **Step 1: Write the failing test**

먼저 `parse_service/tests/test_router.py:15` 의 기존 행을 **교체**한다. csv 는 이 Task 에서 옮기지 않으므로 `"text"` 로 남긴다:

```python
    ("a.txt", "text"), ("a.csv", "text"), ("a.md", "text"), ("a.xml", "text"),
```

`test_router.py:1` docstring 을 교체:

```python
"""router — 확장자 → 도메인 5분기. 변환은 run_parse 가 한다(여기서 하지 않는다)."""
```

같은 parametrize 목록에 html 행을 추가:

```python
    ("a.html", "html"), ("A.HTM", "html"),
```

파일 끝에 추가:

```python
def test_html_does_not_hit_convert_api():
    """html 은 형변환 API 를 타지 않고 자체 레인으로 간다."""
    from parse_service.tools import fileconvert

    assert fileconvert.needs_convert("a.html") is False
    assert fileconvert.needs_convert("a.htm") is False


def test_office_still_converts():
    """형변환 API 대상은 hwp/office 만 남는다(축소는 했지만 없애지 않았다)."""
    from parse_service.tools import fileconvert

    for name in ("a.hwp", "a.hwpx", "a.doc", "a.docx", "a.ppt", "a.pptx"):
        assert fileconvert.needs_convert(name) is True


def test_parsers_table_covers_every_domain():
    """domain_of 에 분기를 추가하고 _PARSERS 키를 빠뜨리면 런타임 KeyError 로
    해당 레인 전체가 죽는다 — domain_of 단언만으로는 못 잡는다."""
    assert set(router._PARSERS) == {"pdf", "excel", "ocr", "html", "text"}


def test_route_dispatches_html_end_to_end():
    rr = router.route(b"<html><body><p>\xea\xb0\x80</p></body></html>", "a.html",
                      ocr_url="", excel_url="")
    assert rr.kind == "pages" and rr.chunk_needed is True
    assert rr.pages[0]["blocks"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD /Users/xxx/workspace/8.kb-pipeline/.venv-kb/bin/python -m pytest parse_service/tests/test_router.py -q`
Expected: FAIL — `domain_of("a.html")` 가 `"pdf"`, `domain_of("a.xml")` 가 `"pdf"`, `needs_convert("a.html")` 가 `True`, `_PARSERS` 키 4개

- [ ] **Step 3: `fileconvert` 의 두 집합 수정**

`parse_service/tools/fileconvert.py:27-29` 교체:

```python
#: 명세 §3.1.3 지원 목록 ∩ (비-excel · 비-이미지 · 비-pdf · 비-html). **여기가 유일한 정의다.**
#: odt/odp/ods/rtf 는 명세에 없다 — 넣으면 원격 422 로 문서 전체가 실패한다.
#: html/htm 은 2026-08-11 제외 — `parsers/html` 이 형변환 없이 처리한다(표 <table> 보존).
CONVERTIBLE_EXTS = {"hwp", "hwpx", "doc", "docx", "ppt", "pptx"}
```

`parse_service/tools/fileconvert.py:31-32` 교체(**csv 는 아직 남겨둔다** — Task 5 에서 EXCEL_EXTS 편입과 함께 뺀다):

```python
#: 변환도 파싱도 불필요한 평문. 그대로 블록화한다(router 의 text 도메인).
#: xml 은 2026-08-11 편입 — 그 전엔 어느 집합에도 없어 pdf 도메인으로 떨어졌고
#: `app.py:269-271` 의 `%PDF` 가드에서 `not a PDF (and not convertible)` 로 죽었다.
TEXT_EXTS = {"txt", "md", "markdown", "csv", "json", "log", "xml"}
```

- [ ] **Step 4: router 에 html 분기 추가**

모듈 docstring(`router.py:3-4`)의 매핑 서술을 교체:

```
매핑(2026-08-11): 엑셀→excel(자체청킹, chunk_needed=False), 이미지→ocr(in-process VL),
html/htm→html(markdownify + <table> 보존, 형변환 API 미경유), 평문→text(그대로 블록화),
**그 외 전부→pdf**.
```

import 무리(`router.py:15-19`)에 추가:

```python
from parse_service.parsers import html as _html
```

`_PARSERS` 위에 어댑터를 추가:

```python
def _html_parse(fb, fn, **_):
    return _html.parse(fb, fn)
```

`_PARSERS` 교체:

```python
_PARSERS = {"pdf": _pdf_parse, "excel": _excel_parse,
            "ocr": _ocr_parse, "html": _html_parse, "text": _text_parse}
```

`domain_of` 의 `TEXT_EXTS` 분기 **바로 위**에 삽입하고, `TEXT_EXTS` 분기의 주석도 갱신한다(현재 `router.py:68` 주석이 `txt md csv json` 이라 실제와 어긋나게 된다):

```python
    if ext in _html.HTML_EXTS:                  # html htm — 형변환 없이 자체 레인
        return "html"
    if ext in fileconvert.TEXT_EXTS:            # txt md json log xml — 변환 불가·불필요
        return "text"
```

- [ ] **Step 5: Run test to verify it passes**

Run: `PYTHONPATH=$PWD /Users/xxx/workspace/8.kb-pipeline/.venv-kb/bin/python -m pytest parse_service/tests/test_router.py -q`
Expected: PASS

- [ ] **Step 6: 전체 회귀**

Run: `PYTHONPATH=$PWD /Users/xxx/workspace/8.kb-pipeline/.venv-kb/bin/python -m pytest parse_service/tests tests service/tests -q`
Expected: PASS, 실패 0

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
    `A: 1001, B: 김철수` 로 퇴화한다(detection/header_detector.py:318-323 style gate)."""
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


def test_sheet_title_illegal_chars_sanitized():
    """`현황[최종].csv` 같은 국내 실무 파일명이 지금은 text 레인으로 문제없이 통과한다.
    openpyxl 은 [ ] : * ? / \\ 를 시트명에서 거부하므로(ValueError → ParserError →
    문서 전체 parse_failed) 정규화하지 않으면 순수 회귀다."""
    ws = _load(b"a\n1\n", "현황[최종]:2026/3분기*.csv").active
    assert ws.title == "현황_최종__2026_3분기_"


def test_illegal_control_chars_stripped():
    """openpyxl 은 제어문자가 든 셀을 append 시점에 IllegalCharacterError 로 거부한다."""
    ws = _load(b"a,b\n1,bad\x07here\n").active
    assert ws["B2"].value == "badhere"


def test_cp949_and_quoted_cells():
    raw = '사번,성명\n1001,"이영희, 차장"\n'.encode("cp949")
    ws = _load(raw).active
    assert ws["A1"].value == "사번"
    assert ws["B2"].value == "이영희, 차장"


def test_quoted_newline_cell_preserved():
    """csv.reader 에 넘기는 StringIO 는 newline='' 이어야 인용 셀 안의 개행이 보존된다."""
    ws = _load(b'a,b\n1,"\xea\xb0\x80\n\xeb\x82\x98"\n').active
    assert ws["B2"].value == "가\n나"


def test_pipe_in_cell_survives():
    ws = _load("a,b\n1,리스크|관리부\n".encode("utf-8")).active
    assert ws["B2"].value == "리스크|관리부"


def test_ragged_rows_are_kept():
    """열 수가 행마다 달라도 죽지 않는다(엑셀 파서가 헤더 기준으로 맞춘다)."""
    ws = _load(b"a,b,c\n1,2\n3,4,5,6\n").active
    assert ws.max_row == 3


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

**openpyxl 이 거부하는 입력 둘을 미리 막는다.** 시트명의 ``[ ] : * ? / \\`` 는
title 대입에서 ValueError, 셀의 제어문자는 append 에서 IllegalCharacterError 를 낸다.
둘 다 excel/__init__.py 의 `except Exception` 을 타고 ParserError 로 승격돼 **문서 전체가
parse_failed** 가 된다 — `현황[최종].csv` 같은 파일명은 지금 text 레인으로 잘 통과하므로
막지 않으면 순수 회귀다.
"""
from __future__ import annotations

import csv
import io
import re
from pathlib import Path

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Font, PatternFill

from parse_service.parsers import ParserError
from parse_service.tools.textdecode import decode_text

#: openpyxl 시트명 상한. 넘기면 저장이 아니라 title 대입에서 바로 터진다.
_SHEET_TITLE_MAX = 31
#: openpyxl 이 시트명에서 거부하는 문자.
_SHEET_BAD_CHARS = re.compile(r"[\\/*?:\[\]]")


def _sheet_title(filename: str) -> str:
    stem = _SHEET_BAD_CHARS.sub("_", Path(filename).stem)
    return stem[:_SHEET_TITLE_MAX] or "Sheet1"


def csv_bytes_to_xlsx(file_bytes: bytes, filename: str) -> bytes:
    text = decode_text(file_bytes, filename)
    # newline="" 이어야 인용 셀 안의 개행이 보존된다(csv 모듈 문서의 요구사항).
    rows = [r for r in csv.reader(io.StringIO(text, newline=""))
            if any((c or "").strip() for c in r)]
    if not rows:
        raise ParserError(f"empty csv: {filename}")

    wb = Workbook()
    ws = wb.active
    ws.title = _sheet_title(filename)
    for row in rows:
        ws.append([ILLEGAL_CHARACTERS_RE.sub("", c) if isinstance(c, str) else c
                   for c in row])

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
Expected: PASS (11 tests)

- [ ] **Step 5: Commit**

```bash
git add parse_service/parsers/excel/csv_to_xlsx.py parse_service/tests/test_csv_to_xlsx.py
git commit -m "feat(parse-svc): csv → xlsx 합성기 (헤더 서식 + 시트명/제어문자 정규화)"
```

---

### Task 5: csv 를 엑셀 레인에 배선 (한 커밋)

**`TEXT_EXTS` 에서 csv 를 빼는 것과 `EXCEL_EXTS` 에 넣는 것을 반드시 같은 커밋에서 한다.** 쪼개면 사이 커밋에서 csv 가 `pdf` 도메인으로 떨어져 `%PDF` 가드가 모든 csv 업로드를 거부한다.

**Files:**
- Modify: `parse_service/parsers/excel/__init__.py:14`(`EXCEL_EXTS`), `:61-66`(`_fetch_rag_chunks` 의 safe_filename/suffix/cfg_kwargs)
- Modify: `parse_service/tools/fileconvert.py:32`(`TEXT_EXTS` 에서 `csv` 제거)
- Modify: `parse_service/tests/test_router.py:15`(parametrize 의 `("a.csv", "text")` → `("a.csv", "excel")`)
- Modify: `parse_service/tests/test_parser_excel.py` — 케이스 추가

**Interfaces:**
- Consumes: `parse_service.parsers.excel.csv_to_xlsx.csv_bytes_to_xlsx`
- Produces: `EXCEL_EXTS` 에 `"csv"` 포함; `parse(csv_bytes, "x.csv")` 가 `kind="chunks"`, `chunk_needed=False`, `gate_summary` 포함 반환

- [ ] **Step 1: Write the failing test**

`parse_service/tests/test_router.py:15` 의 `("a.csv", "text")` 를 `("a.csv", "excel")` 로 바꾼다.

`parse_service/tests/test_parser_excel.py` 끝에 추가:

```python
def test_csv_routes_to_excel_lane():
    from parse_service import router
    from parse_service.tools import fileconvert

    assert router.domain_of("a.csv") == "excel"
    # 두 집합이 함께 바뀌어야 한다 — 하나만 바꾸면 pdf 도메인으로 떨어져
    # app.py 의 %PDF 가드가 모든 csv 를 거부한다.
    assert "csv" not in fileconvert.TEXT_EXTS
    assert fileconvert.needs_convert("a.csv") is False


def test_csv_yields_header_keyed_record_chunks(monkeypatch):
    """csv 청크는 `사번: 1001` 이어야 한다. `A: 1001` 이면 헤더 감지가 실패한 것."""
    from parse_service.parsers import excel as _excel

    # auto 는 전결 키워드/계층 지배도가 없으면 kordoc 으로 떨어진다 → csv 는 openpyxl 고정.
    # env 를 auto 로 두고도 성공해야 그 고정이 실제로 걸린 것이다.
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


def test_xlsx_still_honours_backend_env(monkeypatch):
    """csv 고정이 xlsx 경로의 EXCEL_PARSER_BACKEND 존중을 깨뜨리지 않았는지."""
    import parse_service.parsers.excel as _excel

    seen = {}

    class _FakeBackend:
        def parse(self, path, config):
            seen["backend"] = config.backend
            return [], {}

    monkeypatch.setenv("EXCEL_PARSER_BACKEND", "kordoc")
    monkeypatch.setattr(
        "parse_service.parsers.excel.excel_parser_rag.backends.get_backend",
        lambda name: _FakeBackend())
    monkeypatch.setattr(
        "parse_service.parsers.excel.excel_parser_rag.gate.compute_gate_summary",
        lambda p, c: {"ok": True, "sheets": []})

    from openpyxl import Workbook
    import io as _io
    wb = Workbook(); wb.active.append(["a", "b"])
    buf = _io.BytesIO(); wb.save(buf)

    _excel.parse(buf.getvalue(), "a.xlsx")
    assert seen["backend"] == "kordoc"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD /Users/xxx/workspace/8.kb-pipeline/.venv-kb/bin/python -m pytest parse_service/tests/test_parser_excel.py parse_service/tests/test_router.py -q`
Expected: FAIL — `domain_of("a.csv")` 가 `"text"`, csv 가 `TEXT_EXTS` 에 남아 있음

- [ ] **Step 3: 두 집합을 함께 수정**

`parse_service/parsers/excel/__init__.py:14` 교체:

```python
#: csv 는 2026-08-11 편입 — 메모리상 xlsx 로 합성해 같은 백엔드로 흘린다(`csv_to_xlsx`).
EXCEL_EXTS = {"xlsx", "xlsm", "xls", "csv"}
```

`parse_service/tools/fileconvert.py:32` 의 `TEXT_EXTS` 에서 `"csv"` 를 제거하고 주석을 갱신:

```python
#: 변환도 파싱도 불필요한 평문. 그대로 블록화한다(router 의 text 도메인).
#: csv 는 2026-08-11 엑셀 레인으로 이동(행 레코드 청크). xml 은 같은 날 편입 —
#: 그 전엔 어느 집합에도 없어 pdf 도메인으로 떨어졌고 `app.py:269-271` 의 `%PDF`
#: 가드에서 `not a PDF (and not convertible)` 로 죽었다.
TEXT_EXTS = {"txt", "md", "markdown", "json", "log", "xml"}
```

- [ ] **Step 4: `_fetch_rag_chunks` 에 csv 분기**

`parse_service/parsers/excel/__init__.py:61-66` 을 아래로 교체한다. **순서가 중요하다** — `is_csv` 가 `suffix` 계산에 필요하므로 반드시 `suffix` 앞이어야 한다:

```python
    safe_filename = Path((filename or "upload.xlsx").replace("\x00", "")).name or "upload.xlsx"
    is_csv = Path(safe_filename).suffix.lower() == ".csv"
    if is_csv:
        # csv → xlsx 합성. document_title 은 아래에서 원본 stem 을 그대로 쓰므로
        # 파일명은 바꾸지 않고 바이트와 suffix 만 갈아끼운다.
        from parse_service.parsers.excel.csv_to_xlsx import csv_bytes_to_xlsx
        file_bytes = csv_bytes_to_xlsx(file_bytes, safe_filename)
    suffix = ".xlsx" if is_csv else (Path(safe_filename).suffix.lower() or ".xlsx")
    cfg_kwargs: dict = {
        # csv 는 백엔드를 openpyxl 로 고정한다. 기본 `auto` 는 "전결" 키워드(Tier1)나
        # 계층 지배도(Tier1.5)가 있을 때만 openpyxl 을 쓰고 그 외에는 kordoc 으로
        # 떨어지는데(backends/auto_backend.py:88-150), csv 유래 평면 표는 둘 다 아니다.
        # csv 에는 병합셀·다중시트·수식이 없어 kordoc 의 렌더 충실도 이점이 없고,
        # KORDOC_BIN 이 없는 환경에선 아예 실패한다(실측).
        "backend": "openpyxl" if is_csv else os.environ.get("EXCEL_PARSER_BACKEND", "auto"),
        "document_title": Path(safe_filename).stem,
    }
```

- [ ] **Step 5: Run test to verify it passes**

Run: `PYTHONPATH=$PWD /Users/xxx/workspace/8.kb-pipeline/.venv-kb/bin/python -m pytest parse_service/tests/test_parser_excel.py parse_service/tests/test_router.py -q`
Expected: PASS

- [ ] **Step 6: 전체 회귀**

Run: `PYTHONPATH=$PWD /Users/xxx/workspace/8.kb-pipeline/.venv-kb/bin/python -m pytest parse_service/tests tests service/tests -q`
Expected: PASS, 실패 0

- [ ] **Step 7: Commit**

```bash
git add parse_service/parsers/excel/__init__.py parse_service/tools/fileconvert.py \
        parse_service/tests/test_parser_excel.py parse_service/tests/test_router.py
git commit -m "feat(parse-svc): csv 를 엑셀 레인으로 — openpyxl 고정 + 행 레코드 청크"
```

---

### Task 6: 폐쇄망 가드

가드를 만들어만 두고 안 돌리면 없는 것과 같다(`guard-exists-but-never-ran` 전례). `verify-bundle.sh` 는 이미 엑셀 왕복 스모크를 돌리므로 html 왕복도 같은 자리에 붙이고, **이 Task 안에서 실제로 실행해 로그를 남긴다.**

**Files:**
- Modify: `scripts/airgap/verify-bundle.sh` — `check_imports()` 안에 html 스모크 추가, 말미 형변환 API 주석 정정

- [ ] **Step 1: html 왕복 스모크 추가**

`verify-bundle.sh` 의 엑셀 왕복 `case "$xout" in … esac` **직후**, 형변환 API 주석 **앞**에 삽입한다:

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

`check_imports()` 말미 주석의 `docx/hwp/ppt/html 파싱은` 을 `docx/hwp/ppt 파싱은` 으로 고치고 한 줄 덧붙인다:

```bash
  # (html 은 2026-08-11 이 경로에서 빠졌다 — parsers/html 이 형변환 없이 처리한다.)
```

- [ ] **Step 3: 문법 검사**

Run: `bash -n scripts/airgap/verify-bundle.sh`
Expected: 출력 없음

- [ ] **Step 4: parse-svc 이미지 빌드**

호스트 venv 에는 markdownify 가 이미 있어서 **이미지에 들어갔는지를 원리적으로 검증하지 못한다** — 이 가드의 존재 이유가 바로 그 누락이다. 반드시 이미지를 새로 빌드한다.

Run: `docker build -f Dockerfile.parse-svc -t kbp-parse-svc:markup-lane .`
Expected: 성공. 실패하면 **여기서 멈추고** 원인(대개 requirements 해석)을 고친다.

- [ ] **Step 5: 가드 실제 실행**

Run: `bash scripts/airgap/verify-bundle.sh --imports`
Expected: 출력에 `✓ html 왕복 성공 — html_blocks=2` 가 포함된다. `✗ html 파싱 왕복 실패` 가 나오면 `markdownify` 가 이미지에 없는 것이다 — `requirements.txt` 추가가 Task 2 Step 1 에서 누락됐는지 확인한다.

빌드·실행이 불가능한 환경(docker/podman 부재)이면 **중단하지 말고** 아래 `## 구현 후 검증` 절에 "V9 미검증 — 사유"를 적고 다음 Task 로 간다. 조용히 통과시키지 않는다.

- [ ] **Step 6: Commit**

```bash
git add scripts/airgap/verify-bundle.sh
git commit -m "feat(airgap): html 왕복 스모크 — markdownify 누락과 표 평탄화 회귀를 배포 전에 잡는다"
```

---

### Task 7: 문서 반영

코드만 바꾸고 문서를 방치하지 않는다(CLAUDE.md 워크플로). **편집 지점을 라인으로 지목한다** — "주변 형식을 따른다" 같은 지시는 실행자마다 다른 결과를 낸다.

**Files:**
- Modify: `_workspace/01-architecture.md:52`(4분기→5분기), `:56-64`(라우팅 표), 청킹 소유 서술
- Modify: `_workspace/02-changes.md` (마지막 절 뒤에 새 절 추가)
- Modify: `_workspace/03-dev-progress.md`
- Modify: `docs/kb-pipeline-process-definition.md` (라우팅 서술)
- Create: `deferred.md` (리포 루트 — 지금 없다. D 번호는 **D39** 부터)

- [ ] **Step 1: `_workspace/01-architecture.md` 갱신**

`:52` 의 헤딩을 교체:

```markdown
### 3.1 Parse — 확장자별 파서 라우팅 (`parse_service/router.py`, 5분기 · 폴백 없음)
```

`:56-64` 라우팅 표에서 세 행을 고친다(컬럼은 `| 확장자 | 도메인 | 파서(in-process) | \`<table>\` HTML | \`chunk_needed\` | 비고 |` 6개 유지):

- `| XLSX/XLSM/XLS |` 행의 확장자를 `| XLSX/XLSM/XLS/**CSV** |` 로, 비고 끝에 ` csv 는 헤더 서식을 준 xlsx 로 합성해 **openpyxl 고정**(2026-08-11).` 추가
- `| DOCX·HWP·HWPX·DOC·PPT·PPTX·HTML |` 행에서 `·HTML` 을 뺀다
- `| TXT·MD·CSV·JSON |` 행을 `| TXT·MD·JSON·LOG·XML |` 로 바꾼다
- 그 아래에 html 행을 새로 넣는다:

```markdown
| HTML/HTM | `html` | **`parsers/html`**(bs4 + markdownify, 형변환 API 미경유) | ✅ (원본 `<table>` 보존) | True | 최상위 표를 sentinel 로 빼고 나머지만 markdown 화 후 복원. data-URI `<img>` 는 alt 로 대체(2026-08-11) |
```

- `| 그 외(폴백) | \`fallback\` | **kordoc** CLI |` 행을 교체한다. **지금 코드에 `fallback` 도메인은 없다**(`domain_of` 는 미지 확장자를 `pdf` 로 보내고 `app.py:269-271` 가드가 거른다):

```markdown
| 그 외(미지 확장자) | `pdf` | — | — | — | `domain_of` 가 pdf 로 보내고 `app.py:269-271` 의 `%PDF` 가드가 `not a PDF (and not convertible)` 로 거절한다. 별도 폴백 파서 없음 |
```

같은 파일의 Excel 서술(`:208` 부근, "Excel(xlsx/xlsm/xls)은 …") 뒤에 한 줄 추가:

```markdown
- **csv 의 청킹 소유는 엑셀 레인**(2026-08-11) — csv 는 메모리상 xlsx 로 합성돼
  `chunk_needed=False` 로 자체 청킹된다. facade `/chunk` 를 타지 않는다.
```

- [ ] **Step 2: `_workspace/02-changes.md` 에 절 추가**

파일 **맨 끝**에 추가한다. 이 문서의 최근 절들은 번호 없이 제목만 쓴다(`## D33 해소 — …(2026-08-10)`) — 그 형식을 따른다:

```markdown
## 구조화 텍스트 레인 — html/csv/xml (2026-08-11)

**결정**: html/htm 을 한컴 형변환 API 대상에서 제외하고 `parsers/html` 이 직접 처리한다.
csv 는 엑셀 레인으로 옮긴다. xml 을 평문 레인에 편입한다.

- **markitdown 재검토 후 기각.** 실측: 병합셀 html 표에서 열 정렬이 붕괴(헤더 3열 ·
  데이터행 2셀), json/xml 은 `PlainTextConverter` passthrough(변환 0줄), site-packages
  +140MB(onnxruntime/sympy/numpy/magika). Phase 2d 에서 같은 사유로 제거된 이력이 있고
  재유입 가드 `test_no_markitdown` 이 있다 — 가드 유지. 엔진으로는 markitdown 이 내부에서
  쓰는 `markdownify` 만 직접 채택.
- **html**: 최상위 `<table>` 을 호출별 nonce sentinel 로 빼두고 나머지만 markdown 화 →
  복원 → `hybrid_to_blocks`. colspan/rowspan 보존. 표 **내부** 빈 줄은 접는다 —
  안 접으면 markdown-it 의 html_block 이 거기서 끊겨 표 뒷부분이 raw 텍스트로 샌다(실측).
  data-URI `<img>` 는 alt 로 대체(`modal.py:586` 로 base64 가 흘러드는 것 차단).
- **csv**: 헤더 행에 서식(볼드+채우기)을 준 xlsx 로 합성. 서식이 없으면
  `header_detector` 의 style gate 에 걸려 청크가 `A: 1001` 로 퇴화한다. 백엔드는
  `openpyxl` 고정(`auto` 는 전결/계층이 없으면 kordoc 으로 떨어진다). 시트명의
  `[ ] : * ? / \` 와 셀 제어문자는 정규화(openpyxl 이 거부해 문서 전체가 parse_failed 가 된다).
- **xml**: 구 `pdf` 도메인 오분류로 `%PDF` 가드에서 실패하던 것을 `TEXT_EXTS` 편입으로 해소.
- **`TEXT_EXTS` 에서 csv 를 빼는 것과 `EXCEL_EXTS` 에 넣는 것은 한 커밋**이다. 쪼개면
  사이 커밋에서 csv 가 pdf 도메인으로 떨어져 모든 csv 업로드가 거부된다.
- 신규 의존성 `markdownify` 하나. env 변경 없음. `verify-bundle.sh` 에 html 왕복 스모크 추가.
```

- [ ] **Step 3: `_workspace/03-dev-progress.md` 갱신**

`| **W6** | 파서 라우팅 | ◐ 권고 반영 | markitdown 병합표 손실 …` 행(현재 `:33`) 의 비고 끝에 추가:

```
 2026-08-11 markup-lane 에서 html 이 형변환 API 밖으로 나오고 csv 가 엑셀 레인으로 이동(markitdown 재검토 후 재차 기각).
```

그리고 Phase 진행표의 마지막 행 뒤에 한 행 추가(형식은 그 표의 기존 행과 동일하게 `| 구분 | 내용 | 상태 |` 컬럼 수를 맞춘다):

```
| **markup-lane** | html→`parsers/html`(형변환 미경유), csv→엑셀 레인(openpyxl 고정), xml→text 편입 | ✅ 완료 (브랜치 `feat/markup-lane`) |
```

- [ ] **Step 4: `docs/kb-pipeline-process-definition.md` 갱신**

`:89` 의 "라우팅 소유는 `parse_service/router.py`… markitdown 은 코드·requirements 에서 완전 제거(재유입 가드 …)" 문장 뒤에 추가:

```markdown
2026-08-11 markup-lane 에서 markitdown 을 재검토했으나 같은 사유(병합표 손실)로 재차 기각했다 — 가드 유지. html 은 `parsers/html`(markdownify + `<table>` 보존)이 형변환 없이 처리한다.
```

`:96` 의 `- **그 외(폴백, 예: hwpx)** → \`fallback\`: **kordoc** CLI(구 markitdown 폴백 제거).` 를 교체:

```markdown
- **HTML/HTM** → `html`: `parse_service/parsers/html`(bs4 + markdownify, 최상위 `<table>` 은 원문 HTML 보존). 형변환 API 미경유(2026-08-11).
- **그 외(미지 확장자)** → `pdf` 도메인으로 가서 `app.py` 의 `%PDF` 가드가 거절한다. 별도 폴백 파서는 없다.
```

같은 파일에서 형변환 API 대상 목록에 `html` 이 있으면 뺀다(`grep -n "HTML\|html" docs/kb-pipeline-process-definition.md` 로 확인 후 해당 줄만 수정).

- [ ] **Step 5: `deferred.md` 생성**

리포 루트에 새로 만든다. 기존 D 번호 최대가 **D38** 이므로 D39 부터 잇는다:

```markdown
# deferred — 범위 밖으로 미룬 것

각 항목은 **왜 이번 범위가 아닌지**와 **언제 필요해지는지**를 함께 적는다.

- **D39 json/xml 구조 변환** (2026-08-11, markup-lane) — 지금은 평문 통과.
  markitdown 도 하지 않는 일이라(`PlainTextConverter` passthrough, 실측) 기능 손실은
  없다. 정형 API 응답 같은 문서가 실제로 들어오기 시작하면 키 계층 → 헤딩/표 변환을
  검토한다.
- **D40 tsv·세미콜론 구분자 csv** (2026-08-11, markup-lane) — 구분자 콤마 고정.
  `csv.Sniffer` 는 오작동 위험이 있어 실제 요구가 생길 때 붙인다.
- **D41 초대형 csv 의 청크 수 폭증** (2026-08-11, markup-lane) — 행당 1청크라 10만 행이면
  10만 청크다. 엑셀 레인이 xlsx 에 대해 이미 갖는 동일 문제라 여기서 따로 풀지 않았다.
  적재 지연·비용이 실제로 문제가 되면 엑셀 레인 차원에서 함께 다룬다.
- **D42 엑셀 레인 청크 메타의 임시파일 stem 누출** (2026-08-11, markup-lane) —
  `excel/__init__.py:72` 의 `prefix="excel_parser_"` 탓에 청크 `id`·`keywords` 에
  `excel_parser_<random>` 이 남는다. 청크 **본문**(`text`)은 `document_title` 로 덮여
  깨끗하므로 이번 목표(검색 품질)에는 영향이 없다. xlsx 레인이 이미 갖고 있던 문제이며
  csv 편입과 무관하다. 키워드 검색 오염이 관측되면 그때 함께 고친다.
- **D43 pytest testpaths 에 `parse_service/tests` 미포함** (2026-08-11, markup-lane) —
  `pyproject.toml:30` 이 `["tests", "service/tests"]` 라 맨 `pytest` 는 parse-svc 테스트를
  수집하지 않는다. 이번 계획은 항상 경로를 명시해 무해하지만, CI·타인 실행 시 조용히 안
  도는 부류다. 베이스라인 수치(646)가 바뀌므로 별건으로 처리한다.
- **D44 xml/html 업로드 allowlist(kb-backend 측)** (2026-08-11, markup-lane) —
  이 리포에는 업로드 allowlist 가 없다(`grep` 확인). 상위 kb-backend 가 확장자를 막고
  있으면 사용자 관점의 "xml 실패 해소"가 미완일 수 있다. 별도 리포라 이번 범위 밖.
```

- [ ] **Step 6: `todo_list.md` 확인**

`grep -n "html\|csv\|xml\|markitdown" todo_list.md` 로 이 작업에 해당하는 미완료 항목(`- [ ]`)이 있는지 본다. 있으면 그 줄을 **삭제**한다(체크만 하지 말고 제거). 현재 파일은 미완료 항목이 0개이므로 대개 변경 없음이다.

- [ ] **Step 7: 최종 회귀 + Commit**

Run: `PYTHONPATH=$PWD /Users/xxx/workspace/8.kb-pipeline/.venv-kb/bin/python -m pytest parse_service/tests tests service/tests -q`
Expected: PASS, 실패 0

```bash
git add _workspace docs deferred.md todo_list.md
git commit -m "docs: 구조화 텍스트 레인 반영 — 라우팅 표/청킹 소유/변경 이력/deferred"
```

---

## 구현 후 검증

계획서에서 100번 읽는 것보다 한 번 돌리는 게 확실한 항목들. 구현 중 실측으로 닫고 증거(테스트·실행 로그)를 남긴다.

- [ ] `markdownify` 1.2.2 의 `MarkdownConverter(heading_style="ATX").convert_soup(...)` 시그니처 — Task 2 테스트가 곧바로 드러낸다.
- [ ] `_fetch_rag_chunks` 편집 후 실제 라인 번호(계획서의 `:61-66` 은 편집으로 밀린다) — 문자열 앵커로 편집한다.
- [ ] `verify-bundle.sh` 의 `local` 선언이 함수 안에 있는지(bash 문법) — `bash -n` 으로 확인.
- [ ] **V9 폐쇄망 가드 실제 실행** — Task 6 Step 5 의 `✓ html 왕복 성공` 로그. 실행 못 했으면 **여기에 사유를 적는다**(미검증을 검증된 것처럼 두지 않는다).
- [ ] `_workspace/01-architecture.md` 라우팅 표의 실제 컬럼 수·정렬 — 파일을 열어 맞춘다.
- [ ] `_workspace/03-dev-progress.md` Phase 진행표의 실제 컬럼 수 — 파일을 열어 맞춘다.
