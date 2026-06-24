"""Unit tests for spec 5.1.4 — ``enrich_with_spans`` + ``enrich`` regression.

Covers:
  * ``enrich`` still returns the 2-tuple ``(enriched, modal_ids)`` BYTE-IDENTICAL
    (regression captures of current output across text/modal/absorption cases).
  * ``enrich_with_spans`` returns the 3-tuple ``(enriched, modal_ids, page_spans)``
    where ``enriched``/``modal_ids`` are byte-identical to ``enrich`` and
    ``page_spans = [{page_number, char_start, char_end}]`` such that slicing
    ``enriched[char_start:char_end]`` recovers exactly the page's segments
    (including the two-char ``"\\n\\n"`` blank-line join between segments).

The module is loaded in ISOLATION (importlib from the file path) so the test runs
even though the ``kb_pipeline`` package ``__init__`` pulls in ``markdown_it`` (a
blockify dependency that need not be installed to exercise modal.py). No live
LLM / minio / OCR / Java / db — ``text_llm`` / ``vision_llm`` are pure fakes.
"""

from __future__ import annotations

import importlib.util
import os

import pytest

# --- isolated import of kb_pipeline/modal.py (no package __init__) ------------
_MODAL_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "modal.py"
)
_spec = importlib.util.spec_from_file_location("kbp_modal_under_test", _MODAL_PATH)
modal = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(modal)

enrich = modal.enrich
enrich_with_spans = modal.enrich_with_spans
OPEN_PREFIX = modal.MODAL_OPEN_PREFIX  # 〈MODAL
CLOSE = modal.MODAL_CLOSE              # 〈/MODAL〉
JOIN = "\n\n"  # two-char blank-line join between segments


# --- fake LLMs (deterministic, no JSON => 0 absorption, summary == raw) -------

def fake_text_llm(prompt: str, payload: str) -> str:
    # Returns no JSON -> _parse_boundary_response falls back to (raw, 0, 0):
    # absorption disabled, summary == this raw string. Deterministic.
    return "TXTDESC"


def fake_vision_llm(img_path: str, prompt: str) -> str:
    return f"VISDESC<{img_path}>"


def fake_text_llm_absorb(prompt: str, payload: str) -> str:
    # Valid JSON with title_count=1, footnote_count=1 -> absorb 1 line each side.
    return '{"summary": "SUM", "title_count": 1, "footnote_count": 1}'


# =============================================================================
# enrich() regression — 2-tuple, byte-identical captured output
# =============================================================================

def test_enrich_text_only_byte_identical():
    blocks = [
        {"type": "text", "text": "first paragraph"},
        {"type": "text", "text": "second paragraph"},
    ]
    enriched, modal_ids = enrich(blocks, text_llm=None, vision_llm=None)
    assert enriched == "first paragraph\n\nsecond paragraph"
    assert modal_ids == []


def test_enrich_table_span_byte_identical():
    blocks = [{"type": "table", "table_body": "<table><tr><td>1</td></tr></table>"}]
    enriched, modal_ids = enrich(
        blocks, text_llm=fake_text_llm, vision_llm=fake_vision_llm
    )
    # Captured current output: open marker + summary + "\n" + payload + close.
    expected = (
        '〈MODAL id="T1" type="table"〉TXTDESC\n'
        "<table><tr><td>1</td></tr></table>〈/MODAL〉"
    )
    assert enriched == expected
    assert modal_ids == ["T1"]


def test_enrich_mixed_order_byte_identical():
    blocks = [
        {"type": "text", "text": "intro"},
        {"type": "table", "table_body": "<table>A</table>"},
        {"type": "text", "text": "middle"},
        {"type": "image", "img_path": "fig.png"},
        {"type": "text", "text": "outro"},
    ]
    enriched, modal_ids = enrich(
        blocks, text_llm=fake_text_llm, vision_llm=fake_vision_llm
    )
    expected = (
        "intro"
        + JOIN
        + '〈MODAL id="T1" type="table"〉TXTDESC\n<table>A</table>〈/MODAL〉'
        + JOIN
        + "middle"
        + JOIN
        + '〈MODAL id="I1" type="image"〉VISDESC<fig.png>\nfig.png〈/MODAL〉'
        + JOIN
        + "outro"
    )
    assert enriched == expected
    assert modal_ids == ["T1", "I1"]


def test_enrich_absorption_byte_identical():
    # title (i-1) and footnote (i+1) absorbed into the modal span.
    blocks = [
        {"type": "text", "text": "TITLE LINE"},
        {"type": "table", "table_body": "<table>B</table>"},
        {"type": "text", "text": "FOOTNOTE LINE"},
    ]
    enriched, modal_ids = enrich(
        blocks, text_llm=fake_text_llm_absorb, vision_llm=fake_vision_llm
    )
    # _wrap joins [title, summary, payload, footnote] with "\n" inside one span.
    expected = (
        '〈MODAL id="T1" type="table"〉'
        "TITLE LINE\nSUM\n<table>B</table>\nFOOTNOTE LINE"
        "〈/MODAL〉"
    )
    assert enriched == expected
    assert modal_ids == ["T1"]
    # No leftover standalone text segments (both absorbed).
    assert enriched.count(OPEN_PREFIX) == 1
    assert enriched.count(CLOSE) == 1


def test_enrich_empty_blocks():
    enriched, modal_ids = enrich([], text_llm=None, vision_llm=None)
    assert enriched == ""
    assert modal_ids == []


# =============================================================================
# enrich_with_spans() — 3-tuple, enriched/modal_ids identical to enrich()
# =============================================================================

def _assert_enrich_parity(blocks, **kw):
    """enrich and enrich_with_spans must agree on enriched + modal_ids (byte)."""
    e1, ids1 = enrich(blocks, **kw)
    e2, ids2, spans = enrich_with_spans(blocks, **kw)
    assert e2 == e1, "enriched diverged between enrich and enrich_with_spans"
    assert ids2 == ids1, "modal_ids diverged"
    return e2, ids2, spans


def _assert_spans_cover_pages(enriched, spans, blocks_by_page):
    """Each span's enriched slice must contain exactly that page's segment texts,
    and not the other pages' exclusive texts (basic non-overlap sanity)."""
    for span in spans:
        assert span.keys() == {"page_number", "char_start", "char_end"}
        assert 0 <= span["char_start"] <= span["char_end"] <= len(enriched)
        sliced = enriched[span["char_start"]:span["char_end"]]
        for needle in blocks_by_page.get(span["page_number"], []):
            assert needle in sliced, (
                f"page {span['page_number']} slice missing {needle!r}: {sliced!r}"
            )


def test_spans_single_page_text():
    blocks = [
        {"type": "text", "text": "alpha", "page_idx": 1},
        {"type": "text", "text": "beta", "page_idx": 1},
    ]
    enriched, ids, spans = _assert_enrich_parity(
        blocks, text_llm=None, vision_llm=None
    )
    assert ids == []
    assert spans == [{"page_number": 1, "char_start": 0, "char_end": len(enriched)}]
    assert enriched[spans[0]["char_start"]:spans[0]["char_end"]] == "alpha\n\nbeta"


def test_spans_two_pages_text_offsets_exact():
    blocks = [
        {"type": "text", "text": "page-one-A", "page_idx": 1},
        {"type": "text", "text": "page-one-B", "page_idx": 1},
        {"type": "text", "text": "page-two-A", "page_idx": 2},
    ]
    enriched, ids, spans = _assert_enrich_parity(
        blocks, text_llm=None, vision_llm=None
    )
    # enriched = "page-one-A\n\npage-one-B\n\npage-two-A"
    assert enriched == "page-one-A\n\npage-one-B\n\npage-two-A"
    assert ids == []
    # page 1 spans the first two segments INCLUDING the 2-char join between them,
    # but NOT the join that precedes page 2 (half-open per-page bounds).
    assert spans == [
        {"page_number": 1, "char_start": 0, "char_end": len("page-one-A\n\npage-one-B")},
        {
            "page_number": 2,
            "char_start": len("page-one-A\n\npage-one-B\n\n"),
            "char_end": len(enriched),
        },
    ]
    s1, s2 = spans
    assert enriched[s1["char_start"]:s1["char_end"]] == "page-one-A\n\npage-one-B"
    assert enriched[s2["char_start"]:s2["char_end"]] == "page-two-A"


def test_spans_modal_page_idx_from_modal_block():
    # Modal carries its own page_idx; its segment must be attributed to that page.
    blocks = [
        {"type": "text", "text": "p1-text", "page_idx": 1},
        {"type": "table", "table_body": "<table>Z</table>", "page_idx": 2},
        {"type": "text", "text": "p2-text", "page_idx": 2},
    ]
    enriched, ids, spans = _assert_enrich_parity(
        blocks, text_llm=fake_text_llm, vision_llm=fake_vision_llm
    )
    assert ids == ["T1"]
    by_page = {1: ["p1-text"], 2: ['id="T1"', "<table>Z</table>", "p2-text"]}
    _assert_spans_cover_pages(enriched, spans, by_page)
    page_numbers = [s["page_number"] for s in spans]
    assert page_numbers == [1, 2]
    # Page 1 slice must NOT contain the modal/table or p2 text.
    p1 = next(s for s in spans if s["page_number"] == 1)
    p1_slice = enriched[p1["char_start"]:p1["char_end"]]
    assert "p1-text" in p1_slice
    assert "T1" not in p1_slice and "p2-text" not in p1_slice


def test_spans_slices_reconstruct_each_page_exactly():
    # General property: for every page, the slice equals the JOIN of that page's
    # contiguous segments. Here pages are contiguous (the realistic parse_to_pages
    # ordering) so each page slice is a clean substring of enriched.
    blocks = [
        {"type": "text", "text": "AAA", "page_idx": 1},
        {"type": "text", "text": "BBB", "page_idx": 1},
        {"type": "image", "img_path": "p2.png", "page_idx": 2},
        {"type": "text", "text": "CCC", "page_idx": 2},
        {"type": "text", "text": "DDD", "page_idx": 3},
    ]
    enriched, ids, spans = _assert_enrich_parity(
        blocks, text_llm=fake_text_llm, vision_llm=fake_vision_llm
    )
    assert ids == ["I1"]
    # Rebuild expected page slices from the same JOIN logic.
    seg_modal = '〈MODAL id="I1" type="image"〉VISDESC<p2.png>\np2.png〈/MODAL〉'
    expected_slices = {
        1: "AAA" + JOIN + "BBB",
        2: seg_modal + JOIN + "CCC",
        3: "DDD",
    }
    for span in spans:
        got = enriched[span["char_start"]:span["char_end"]]
        assert got == expected_slices[span["page_number"]], (
            f"page {span['page_number']}: {got!r} != {expected_slices[span['page_number']]!r}"
        )
    assert [s["page_number"] for s in spans] == [1, 2, 3]


def test_spans_no_page_idx_degrades_to_single_page_one():
    # Blocks carry no page_idx (all default 0) -> single span covering page 1.
    blocks = [
        {"type": "text", "text": "no-pages-here"},
        {"type": "text", "text": "still-none"},
    ]
    enriched, ids, spans = _assert_enrich_parity(
        blocks, text_llm=None, vision_llm=None
    )
    assert ids == []
    assert spans == [{"page_number": 1, "char_start": 0, "char_end": len(enriched)}]
    assert enriched[0:len(enriched)] == enriched


def test_spans_empty_blocks():
    enriched, ids, spans = _assert_enrich_parity([], text_llm=None, vision_llm=None)
    assert enriched == ""
    assert ids == []
    assert spans == []


def test_spans_page_zero_explicit_kept_distinct():
    # page_idx 0 explicitly set on one block plus a real page 2 block: page 0 is a
    # genuine page index here (page_number = page_idx, per spec). Both spans appear.
    blocks = [
        {"type": "text", "text": "zero-page", "page_idx": 0},
        {"type": "text", "text": "two-page", "page_idx": 2},
    ]
    enriched, ids, spans = _assert_enrich_parity(
        blocks, text_llm=None, vision_llm=None
    )
    assert ids == []
    page_numbers = sorted(s["page_number"] for s in spans)
    assert page_numbers == [0, 2]
    for span in spans:
        sliced = enriched[span["char_start"]:span["char_end"]]
        if span["page_number"] == 0:
            assert sliced == "zero-page"
        else:
            assert sliced == "two-page"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
