"""Unit tests for ``parse_service/pdf_pages.py`` (spec §5.1.2).

We build a tiny multi-page PDF in-test with PyMuPDF(fitz) and render it, asserting
1-based page numbers, JPEG output bytes, and extracted text. Corrupt/non-PDF
input renders to an empty list (non-fatal). No live minio/OCR/LLM involved.
"""

from __future__ import annotations

import pytest

from parse_service.pdf_pages import RenderedPage, render_pdf_pages

fitz = pytest.importorskip("fitz", reason="PyMuPDF(fitz) required to build the test PDF")


def _make_pdf(page_texts: list[str]) -> bytes:
    """Build a tiny PDF with one text line per page using fitz."""
    doc = fitz.open()
    for text in page_texts:
        page = doc.new_page()
        page.insert_text((72, 72), text, fontsize=24)
    data = doc.tobytes()
    doc.close()
    return data


def _is_jpeg(b: bytes) -> bool:
    # JPEG SOI marker 0xFFD8 ... EOI 0xFFD9.
    return len(b) > 4 and b[:2] == b"\xff\xd8" and b[-2:] == b"\xff\xd9"


def test_render_pdf_pages_multipage_numbers_and_text():
    pdf = _make_pdf(["FIRST PAGE TEXT", "SECOND PAGE TEXT", "THIRD PAGE TEXT"])

    pages = render_pdf_pages(pdf)

    assert len(pages) == 3
    assert all(isinstance(p, RenderedPage) for p in pages)
    # 1-based, in order.
    assert [p.page_number for p in pages] == [1, 2, 3]
    # JPEG bytes per page.
    for p in pages:
        assert isinstance(p.jpeg, (bytes, bytearray))
        assert _is_jpeg(p.jpeg)
    # extracted text matches the page it came from.
    assert "FIRST PAGE TEXT" in pages[0].text
    assert "SECOND PAGE TEXT" in pages[1].text
    assert "THIRD PAGE TEXT" in pages[2].text


def test_render_pdf_pages_single_page():
    pdf = _make_pdf(["only page"])
    pages = render_pdf_pages(pdf)
    assert len(pages) == 1
    assert pages[0].page_number == 1
    assert _is_jpeg(pages[0].jpeg)
    assert "only page" in pages[0].text


def test_render_pdf_pages_respects_quality_and_dpi_kwargs():
    """Higher dpi yields a larger raster than a much lower dpi (same page)."""
    pdf = _make_pdf(["resolution probe page"])
    hi = render_pdf_pages(pdf, dpi=300, jpg_quality=90)
    lo = render_pdf_pages(pdf, dpi=72, jpg_quality=90)
    assert len(hi) == 1 and len(lo) == 1
    assert len(hi[0].jpeg) > len(lo[0].jpeg)


def test_render_pdf_pages_non_pdf_returns_empty():
    assert render_pdf_pages(b"not a pdf at all") == []


def test_render_pdf_pages_empty_bytes_returns_empty():
    assert render_pdf_pages(b"") == []


def test_render_pdf_pages_truncated_pdf_returns_empty():
    """A PDF header without a valid body is non-fatal → empty list."""
    assert render_pdf_pages(b"%PDF-1.7\n garbage truncated") == []


def test_rendered_page_is_frozen_dataclass():
    p = RenderedPage(page_number=1, jpeg=b"x", text="t")
    with pytest.raises((AttributeError, Exception)):
        p.page_number = 2  # type: ignore[misc]
