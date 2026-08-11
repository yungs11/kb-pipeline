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
    """<body> 없는 조각 html."""
    blocks = _blocks(b"<p>\xea\xb0\x80</p><table><tr><td>\xeb\x82\x98</td></tr></table>")
    assert any(b["type"] == "table" for b in blocks)


def test_table_after_body_close_is_not_lost():
    """bs4 html.parser 는 브라우저와 달리 body 밖 노드를 body 안으로 옮기지 않는다.
    `soup.find("body")` 로 스코핑하면 이 표가 경고 없이 사라진다(실측 tables=0)."""
    raw = ("<html><body><p>안쪽</p></body>"
           "<table><tr><td>바깥표</td></tr></table></html>").encode("utf-8")
    tables = [b for b in _blocks(raw) if b["type"] == "table"]
    assert len(tables) == 1
    assert "바깥표" in tables[0]["table_body"]


def test_table_before_body_open_is_not_lost():
    raw = ("<html><table><tr><td>먼저표</td></tr></table>"
           "<body><p>안쪽</p></body></html>").encode("utf-8")
    tables = [b for b in _blocks(raw) if b["type"] == "table"]
    assert len(tables) == 1
    assert "먼저표" in tables[0]["table_body"]


def test_title_does_not_leak_into_text():
    """soup 전체를 변환하므로 <title>/<meta> 를 떼지 않으면 본문에 섞인다."""
    raw = ("<html><head><title>문서제목ZZZ</title></head>"
           "<body><p>본문</p></body></html>").encode("utf-8")
    joined = " ".join(b.get("text", "") for b in _blocks(raw))
    assert "문서제목ZZZ" not in joined
    assert "본문" in joined


def test_xhtml_prolog_does_not_leak_into_text():
    """XHTML prolog 는 ProcessingInstruction 이고 markdownify 가 텍스트로 렌더한다 —
    지우지 않으면 `xml version="1.0" encoding="UTF-8"?` 가 본문 블록이 된다(실측)."""
    raw = ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE html>\n'
           '<html><head><title>T</title></head>'
           '<body><p>본문있음</p></body></html>').encode("utf-8")
    joined = " ".join(b.get("text", "") for b in _blocks(raw))
    assert "xml version" not in joined
    assert "본문있음" in joined


def test_comment_does_not_leak_into_text():
    raw = "<html><body><!-- 숨은주석ZZZ --><p>본문</p></body></html>".encode("utf-8")
    joined = " ".join(b.get("text", "") for b in _blocks(raw))
    assert "숨은주석ZZZ" not in joined
    assert "본문" in joined


def test_unclosed_head_does_not_wipe_body():
    """`</head>` 생략은 HTML5 가 허용하고 실무에서 흔하다. bs4 는 브라우저와 달리 head 를
    자동으로 닫지 않고 <body> 를 head 자식으로 중첩시키므로, <head> 를 통째로 extract 하면
    본문·표가 전멸한다(실측: '<html></html>' 만 남는다)."""
    raw = ("<html><head><title>TTT</title>"
           "<body><p>본문살아있음</p><table><tr><td>표살아있음</td></tr></table>"
           "</body></html>").encode("utf-8")
    blocks = _blocks(raw)
    joined = " ".join(b.get("text", "") for b in blocks)
    assert "본문살아있음" in joined
    assert "TTT" not in joined
    tables = [b for b in blocks if b["type"] == "table"]
    assert len(tables) == 1 and "표살아있음" in tables[0]["table_body"]


def test_noscript_and_template_dropped():
    """브라우저가 렌더하지 않는 텍스트가 본문 첫 청크에 섞이면 검색 노이즈가 된다."""
    raw = ("<html><body><noscript>JS를켜세요</noscript>"
           "<template><p>템플릿내용</p></template>"
           "<p>진짜본문</p></body></html>").encode("utf-8")
    joined = " ".join(b.get("text", "") for b in _blocks(raw))
    assert "JS를켜세요" not in joined
    assert "템플릿내용" not in joined
    assert "진짜본문" in joined


def test_empty_html_raises():
    with pytest.raises(ParserError):
        _html.parse(b"<html><body>   </body></html>", "a.html")
