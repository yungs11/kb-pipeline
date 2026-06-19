import os

import pytest

from kb_pipeline.blockify import hybrid_to_blocks, elements_to_blocks

REAL_SAMPLE = (
    "/Users/xxx/workspace/excel-parser-markitdown/compare/out/"
    "10_2-1._위임전결기준표_2026.04.17._개정/kordoc.md"
)


def test_html_table_becomes_single_table_block():
    doc = '<table>\n<tr><td>a</td><td>b</td></tr>\n</table>'
    blocks = hybrid_to_blocks(doc)
    tables = [b for b in blocks if b["type"] == "table"]
    assert len(tables) == 1
    assert tables[0]["table_body"].lstrip().lower().startswith("<table")
    assert "<td>a</td>" in tables[0]["table_body"]
    assert tables[0]["table_caption"] == []
    assert tables[0]["page_idx"] == 0


def test_headings_become_text_blocks_with_levels():
    doc = "# Title one\n\n## Sub two\n\n### Deep three\n"
    blocks = hybrid_to_blocks(doc)
    headings = [b for b in blocks if b["type"] == "text" and "text_level" in b]
    assert [(b["text"], b["text_level"]) for b in headings] == [
        ("Title one", 1),
        ("Sub two", 2),
        ("Deep three", 3),
    ]


def test_pipe_table_rendered_to_html_table_block():
    doc = (
        "| Name | Age |\n"
        "| --- | --- |\n"
        "| Alice | 30 |\n"
        "| Bob | 25 |\n"
    )
    blocks = hybrid_to_blocks(doc)
    tables = [b for b in blocks if b["type"] == "table"]
    assert len(tables) == 1
    body = tables[0]["table_body"]
    assert body.startswith("<table>") and body.endswith("</table>")
    assert "|" not in body  # never left as pipe text
    assert "Alice" in body and "Bob" in body
    assert "<th>" in body and "Name" in body


def test_image_markdown_becomes_image_block():
    doc = "![alt text](images/figure1.png)\n"
    blocks = hybrid_to_blocks(doc)
    imgs = [b for b in blocks if b["type"] == "image"]
    assert len(imgs) == 1
    assert imgs[0]["img_path"] == "images/figure1.png"
    assert imgs[0]["image_caption"] == []


def test_html_img_becomes_image_block():
    doc = '<img src="pic.jpg" alt="x">\n'
    blocks = hybrid_to_blocks(doc)
    imgs = [b for b in blocks if b["type"] == "image"]
    assert len(imgs) == 1
    assert imgs[0]["img_path"] == "pic.jpg"


def test_equation_becomes_equation_block():
    doc = "$$E = mc^2$$\n"
    blocks = hybrid_to_blocks(doc)
    eqs = [b for b in blocks if b["type"] == "equation"]
    assert len(eqs) == 1
    assert eqs[0]["latex"] == "E = mc^2"
    assert eqs[0]["text_format"] == "latex"


def test_mixed_doc_preserves_order():
    doc = (
        "# Heading\n\n"
        "Some intro paragraph.\n\n"
        "| a | b |\n| --- | --- |\n| 1 | 2 |\n\n"
        "<table><tr><td>raw</td></tr></table>\n\n"
        "![](img.png)\n\n"
        "Closing text.\n"
    )
    blocks = hybrid_to_blocks(doc)
    types = [b["type"] for b in blocks]
    assert types == ["text", "text", "table", "table", "image", "text"]
    # first block is the heading
    assert blocks[0].get("text_level") == 1
    # the raw HTML table preserved verbatim
    assert "raw" in blocks[3]["table_body"]


def test_table_body_is_html_not_pipe():
    doc = "| x |\n| --- |\n| y |\n"
    blocks = hybrid_to_blocks(doc)
    table = next(b for b in blocks if b["type"] == "table")
    assert "<table" in table["table_body"]
    assert "| x |" not in table["table_body"]


def test_elements_to_blocks_mapping():
    elements = [
        {"category": "title", "content": {"text": "Report"}},
        {"category": "text", "content": {"text": "Body paragraph."}},
        {"category": "table", "content": {"html": "<table><tr><td>c</td></tr></table>"}},
        {"category": "table", "content": {"markdown": "| h |\n| --- |\n| v |"}},
        {"category": "image", "content": {"img_path": "p.png"}},
    ]
    blocks = elements_to_blocks(elements)
    assert [b["type"] for b in blocks] == ["text", "text", "table", "table", "image"]
    assert blocks[0]["text_level"] == 1
    assert blocks[2]["table_body"] == "<table><tr><td>c</td></tr></table>"
    # markdown table promoted to HTML
    assert blocks[3]["table_body"].startswith("<table>")
    assert "v" in blocks[3]["table_body"]
    assert blocks[4]["img_path"] == "p.png"


def test_elements_preserve_order():
    elements = [
        {"category": "text", "content": {"text": "one"}},
        {"category": "table", "content": {"html": "<table></table>"}},
        {"category": "text", "content": {"text": "two"}},
    ]
    blocks = elements_to_blocks(elements)
    assert [b["type"] for b in blocks] == ["text", "table", "text"]
    assert blocks[0]["text"] == "one"
    assert blocks[2]["text"] == "two"


@pytest.mark.skipif(not os.path.exists(REAL_SAMPLE), reason="real sample not present")
def test_real_kordoc_sample_html_tables_preserved():
    with open(REAL_SAMPLE, encoding="utf-8") as f:
        doc = f.read()
    raw_table_count = doc.lower().count("<table")
    blocks = hybrid_to_blocks(doc)

    table_blocks = [b for b in blocks if b["type"] == "table"]
    heading_blocks = [b for b in blocks if b["type"] == "text" and "text_level" in b]

    # every raw <table> in the source becomes exactly one table block, HTML preserved
    assert len(table_blocks) == raw_table_count
    assert raw_table_count >= 1
    for tb in table_blocks:
        assert "<table" in tb["table_body"].lower()
        assert "<tr" in tb["table_body"].lower()
    # the markdown ## headings became text blocks with levels
    assert len(heading_blocks) >= 1
    assert any(h["text_level"] == 2 for h in heading_blocks)


# --- W6: parser routing for merge-critical formats --------------------------

from kb_pipeline.blockify import recommended_parser, PARSER_ROUTING


def test_blockify_preserves_merges_from_structural_html():
    """A structural parser's colspan/rowspan survives blockify intact."""
    doc = (
        "<table><tr><td rowspan=\"2\">A</td><td colspan=\"3\">B</td></tr>"
        "<tr><td>c</td><td>d</td><td>e</td></tr></table>"
    )
    blocks = hybrid_to_blocks(doc)
    tables = [b for b in blocks if b["type"] == "table"]
    assert len(tables) == 1
    body = tables[0]["table_body"]
    assert 'rowspan="2"' in body
    assert 'colspan="3"' in body


def test_blockify_cannot_recover_merges_from_pipe_table():
    """markitdown-style pipe table has no spans; blockify cannot invent them.

    This is the W6 loss: a merged cell arrives as one filled + N blank cells.
    """
    doc = (
        "| 구분 | 시간 | 발표자 |\n"
        "| --- | --- | --- |\n"
        "| 착수보고회 | 16:30 | 김프로 |\n"
        "|  | 16:35 | 정프로 |\n"
    )
    blocks = hybrid_to_blocks(doc)
    tables = [b for b in blocks if b["type"] == "table"]
    assert len(tables) == 1
    body = tables[0]["table_body"]
    # blockify renders <table> but no merges can be recovered.
    assert "<table>" in body
    assert "colspan" not in body.lower()
    assert "rowspan" not in body.lower()


def test_recommended_parser_routes_office_to_structural():
    assert recommended_parser("deck.pptx") == "structural"
    assert recommended_parser("guide.DOCX") == "structural"
    assert recommended_parser("sheet.xlsx") == "markitdown"
    assert recommended_parser("notes.txt") == "markitdown"
    assert PARSER_ROUTING[".pptx"] == "structural"
