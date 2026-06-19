"""W0 Blockify — convert "markdown + inline HTML table" into a block list.

Implements SoT 3.2 / 5.1. One block per structural unit, document order
preserved, tables ALWAYS stay as HTML (never flattened to pipe text).

Block schema (SoT 5.1, no extra required fields):
    {"type":"text",     "text":"...", "text_level":1, "page_idx":0}
    {"type":"table",    "table_body":"<table>…</table>", "table_caption":[], "page_idx":0}
    {"type":"image",    "img_path":"…", "image_caption":[], "page_idx":0}
    {"type":"equation", "latex":"…", "text_format":"latex", "page_idx":0}
"""

from __future__ import annotations

import re
from typing import Any

from markdown_it import MarkdownIt


def _new_parser() -> MarkdownIt:
    # commonmark + raw HTML passthrough + GFM pipe tables.
    return MarkdownIt("commonmark", {"html": True}).enable("table")


# --- math / equation detection ------------------------------------------------

# Block math fenced by $$ ... $$ (possibly multi-line).
_BLOCK_MATH_RE = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)


def _inline_image(token: Any) -> dict | None:
    """If an inline token tree contains an image, return an image block."""
    if not getattr(token, "children", None):
        return None
    for child in token.children:
        if child.type == "image":
            src = child.attrs.get("src", "") if hasattr(child, "attrs") else ""
            return {
                "type": "image",
                "img_path": src,
                "image_caption": [],
                "page_idx": None,  # filled by caller
            }
    return None


def _inline_html_img(text: str) -> str | None:
    m = re.search(r"<img[^>]*\bsrc=[\"']([^\"']+)[\"']", text, re.IGNORECASE)
    return m.group(1) if m else None


def _render_pipe_table_to_html(tokens: list, start: int) -> tuple[str, int]:
    """Render a markdown pipe table (table_open..table_close) to an HTML <table>.

    Returns (html_string, index_after_table_close).
    """
    parts: list[str] = []
    depth = 0
    i = start
    cell_tag = "td"
    while i < len(tokens):
        tok = tokens[i]
        t = tok.type
        if t == "table_open":
            depth += 1
            parts.append("<table>")
        elif t == "table_close":
            parts.append("</table>")
            depth -= 1
            if depth == 0:
                i += 1
                break
        elif t == "thead_open":
            parts.append("<thead>")
        elif t == "thead_close":
            parts.append("</thead>")
        elif t == "tbody_open":
            parts.append("<tbody>")
        elif t == "tbody_close":
            parts.append("</tbody>")
        elif t == "tr_open":
            parts.append("<tr>")
        elif t == "tr_close":
            parts.append("</tr>")
        elif t == "th_open":
            cell_tag = "th"
            parts.append("<th>")
        elif t == "th_close":
            parts.append("</th>")
        elif t == "td_open":
            cell_tag = "td"
            parts.append("<td>")
        elif t == "td_close":
            parts.append("</td>")
        elif t == "inline":
            parts.append(_render_inline_text(tok))
        i += 1
    return "".join(parts), i


def _render_inline_text(token: Any) -> str:
    """Flatten an inline token to plain text (cell content)."""
    if getattr(token, "children", None):
        out: list[str] = []
        for child in token.children:
            if child.type in ("text", "code_inline"):
                out.append(child.content)
            elif child.type == "softbreak" or child.type == "hardbreak":
                out.append("<br>")
            elif child.type == "image":
                out.append(child.attrs.get("alt", "") if hasattr(child, "attrs") else "")
            else:
                out.append(child.content or "")
        return "".join(out)
    return token.content or ""


def hybrid_to_blocks(doc: str, page_idx: int = 0) -> list[dict]:
    """Convert a hybrid markdown+HTML document into a block list (SoT 5.1)."""
    md = _new_parser()
    tokens = md.parse(doc)
    blocks: list[dict] = []

    i = 0
    n = len(tokens)
    pending_heading_level: int | None = None

    while i < n:
        tok = tokens[i]
        t = tok.type

        if t == "html_block":
            content = tok.content or ""
            if "<table" in content.lower():
                blocks.append(
                    {
                        "type": "table",
                        "table_body": content.strip(),
                        "table_caption": [],
                        "page_idx": page_idx,
                    }
                )
            else:
                src = _inline_html_img(content)
                if src is not None:
                    blocks.append(
                        {
                            "type": "image",
                            "img_path": src,
                            "image_caption": [],
                            "page_idx": page_idx,
                        }
                    )
                else:
                    text = re.sub(r"<[^>]+>", "", content).strip()
                    if text:
                        blocks.append({"type": "text", "text": text, "page_idx": page_idx})
            i += 1
            continue

        if t == "heading_open":
            pending_heading_level = int(tok.tag[1])  # h1 -> 1
            i += 1
            continue

        if t == "heading_close":
            i += 1
            continue

        if t == "table_open":
            html, i = _render_pipe_table_to_html(tokens, i)
            blocks.append(
                {
                    "type": "table",
                    "table_body": html,
                    "table_caption": [],
                    "page_idx": page_idx,
                }
            )
            continue

        if t == "inline":
            # heading inline
            if pending_heading_level is not None:
                text = _render_inline_text(tok).strip()
                blocks.append(
                    {
                        "type": "text",
                        "text": text,
                        "text_level": pending_heading_level,
                        "page_idx": page_idx,
                    }
                )
                pending_heading_level = None
                i += 1
                continue

            raw = tok.content or ""

            # standalone image paragraph -> image block
            img = _inline_image(tok)
            html_src = _inline_html_img(raw)
            stripped = raw.strip()
            if img is not None and stripped.startswith("!["):
                img["page_idx"] = page_idx
                blocks.append(img)
                i += 1
                continue
            if html_src is not None and stripped.lower().startswith("<img"):
                blocks.append(
                    {
                        "type": "image",
                        "img_path": html_src,
                        "image_caption": [],
                        "page_idx": page_idx,
                    }
                )
                i += 1
                continue

            # block equation $$...$$
            m = _BLOCK_MATH_RE.search(raw)
            if m is not None and stripped.startswith("$$") and stripped.endswith("$$"):
                blocks.append(
                    {
                        "type": "equation",
                        "latex": m.group(1).strip(),
                        "text_format": "latex",
                        "page_idx": page_idx,
                    }
                )
                i += 1
                continue

            # plain paragraph text
            text = _render_inline_text(tok).strip()
            if text:
                blocks.append({"type": "text", "text": text, "page_idx": page_idx})
            i += 1
            continue

        i += 1

    return blocks


# --- VLM / OCR elements[] mapping --------------------------------------------

_TABLE_CATEGORIES = {"table"}


def _extract_content(item: dict) -> tuple[str, str]:
    """Return (kind, value) where kind is 'html'|'markdown'|'text'."""
    content = item.get("content")
    if isinstance(content, dict):
        if content.get("html"):
            return "html", content["html"]
        if content.get("markdown"):
            return "markdown", content["markdown"]
        if content.get("text"):
            return "text", content["text"]
        return "text", ""
    if isinstance(content, str):
        return "text", content
    return "text", ""


def elements_to_blocks(elements: list[dict]) -> list[dict]:
    """Map a VLM/OCR service ``elements[]`` array to the SoT 5.1 block schema.

    ``category == "table"`` -> table block (prefer HTML, else render markdown);
    everything else -> text block. Document order preserved.
    """
    blocks: list[dict] = []
    for item in elements:
        category = (item.get("category") or "").lower()
        page_idx = item.get("page_idx", item.get("page", 0)) or 0

        if category in _TABLE_CATEGORIES:
            content = item.get("content")
            html = None
            if isinstance(content, dict):
                html = content.get("html")
                if not html and content.get("markdown"):
                    md_blocks = hybrid_to_blocks(content["markdown"], page_idx=page_idx)
                    tbl = next((b for b in md_blocks if b["type"] == "table"), None)
                    html = tbl["table_body"] if tbl else None
            if not html:
                _, html = _extract_content(item)
            blocks.append(
                {
                    "type": "table",
                    "table_body": html or "",
                    "table_caption": [],
                    "page_idx": page_idx,
                }
            )
            continue

        if category in ("image", "figure"):
            content = item.get("content")
            src = ""
            if isinstance(content, dict):
                src = content.get("img_path") or content.get("text") or ""
            blocks.append(
                {
                    "type": "image",
                    "img_path": src,
                    "image_caption": [],
                    "page_idx": page_idx,
                }
            )
            continue

        if category == "equation":
            _, value = _extract_content(item)
            blocks.append(
                {
                    "type": "equation",
                    "latex": value,
                    "text_format": "latex",
                    "page_idx": page_idx,
                }
            )
            continue

        # text / title / paragraph / list / caption / ...
        _, value = _extract_content(item)
        block = {"type": "text", "text": value, "page_idx": page_idx}
        if category in ("title", "heading"):
            block["text_level"] = item.get("text_level", 1)
        blocks.append(block)

    return blocks
