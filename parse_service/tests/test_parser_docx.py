"""parsers/docx — kordoc md → 단일 페이지 blocks, chunk_needed=True."""
import pytest
from parse_service.parsers import ParserError
from parse_service.parsers import docx as docx_parser


def test_docx_md_to_single_page(monkeypatch):
    monkeypatch.setattr(docx_parser, "_to_markdown",
                        lambda fb, fn: "# 제목\n\n본문 텍스트")
    res = docx_parser.parse(b"PK", "a.docx")
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


def test_empty_markdown_raises(monkeypatch):
    monkeypatch.setattr(docx_parser, "_to_markdown", lambda fb, fn: "   ")
    with pytest.raises(ParserError):
        docx_parser.parse(b"PK", "a.docx")
