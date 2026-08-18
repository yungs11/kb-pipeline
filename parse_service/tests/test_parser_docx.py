"""parsers/docx — HWP/HWPX/DOCX kordoc md → 단일 페이지 blocks."""
import pytest
from parse_service.parsers import ParserError
from parse_service.parsers import docx as docx_parser


@pytest.mark.parametrize("filename", ["a.hwp", "a.hwpx", "a.docx"])
def test_kordoc_formats_md_to_single_page(monkeypatch, filename):
    monkeypatch.setattr(docx_parser, "_to_markdown",
                        lambda fb, fn: "# 제목\n\n본문 텍스트")
    res = docx_parser.parse(b"PK", filename)
    assert res.kind == "pages" and res.chunk_needed is True
    assert res.pages[0]["page_number"] == 1
    assert any(b.get("text") for b in res.pages[0]["blocks"])


def test_docx_table_html_preserved(monkeypatch):
    monkeypatch.setattr(
        docx_parser, "_to_markdown",
        lambda fb, fn: "앞 문단\n\n<table><tr><td rowspan=\"2\">병합</td><td>x</td></tr></table>")
    res = docx_parser.parse(b"PK", "a.docx")
    tables = [b for b in res.pages[0]["blocks"] if b["type"] == "table"]
    assert tables and "rowspan" in tables[0]["table_body"]


def test_markdown_pipe_table_becomes_html_table_block(monkeypatch):
    """단순 표는 kordoc pipe Markdown을 받되 최종 표준 블록은 HTML이어야 한다."""
    monkeypatch.setattr(
        docx_parser, "_to_markdown",
        lambda fb, fn: "| 구분 | 값 |\n| --- | --- |\n| A | 1 |")
    res = docx_parser.parse(b"PK", "a.hwp")
    tables = [b for b in res.pages[0]["blocks"] if b["type"] == "table"]
    assert len(tables) == 1
    assert tables[0]["table_body"].startswith("<table>")
    assert "<td>A</td>" in tables[0]["table_body"]


def test_empty_markdown_raises(monkeypatch):
    monkeypatch.setattr(docx_parser, "_to_markdown", lambda fb, fn: "   ")
    with pytest.raises(ParserError):
        docx_parser.parse(b"PK", "a.docx")
