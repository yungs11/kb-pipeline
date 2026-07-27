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


# =============================================================================
# A: wrap_modals / enrich_modals 분리 + 휴리스틱 흡수(LLM 0) + oversize 가드
# =============================================================================

_heuristic_title = modal._heuristic_title
_heuristic_footnote = modal._heuristic_footnote


def test_heuristic_title_patterns():
    # before 는 nearest-first → before[0] 가 직전 블록.
    assert _heuristic_title([(0, "[단위:원]")]) == 1     # 단위 prefix
    assert _heuristic_title([(0, "서식1-1#")]) == 1       # 끝 '#'
    assert _heuristic_title([(0, "별표 3")]) == 1         # 별표 prefix
    assert _heuristic_title([(0, "제1조")]) == 1          # 제 N prefix
    assert _heuristic_title([(0, "① 신청항목")]) == 1     # 항목번호(원문자)
    assert _heuristic_title([(0, "1) 개요")]) == 1        # 항목번호(숫자))
    assert _heuristic_title([(0, "짧은 제목")]) == 1      # len<=40
    long_prose = "이 문단은 표와 무관한 매우 긴 산문 단락으로 제목이 결코 아니며 계속 이어진다 " * 3
    assert _heuristic_title([(0, long_prose)]) == 0      # 긴 산문=비제목
    assert _heuristic_title([]) == 0
    assert _heuristic_title([(0, "   ")]) == 0


def test_heuristic_footnote_patterns():
    after = [(0, "※상기 금액은..."), (1, "* 부가세 별도"), (2, "본문 재개")]
    assert _heuristic_footnote(after) == 2               # 연속 2개, 3번째서 중단
    assert _heuristic_footnote([(0, "주) 비고"), (1, "(단위: 천원)")]) == 2
    assert _heuristic_footnote([(0, "[단위:원]")]) == 1
    assert _heuristic_footnote([(0, "일반 본문")]) == 0
    assert _heuristic_footnote([]) == 0


def test_enrich_off_wrap_on_absorbs_without_llm():
    """shipped 기본 경로(enrich off & wrap on): 마커 + 휴리스틱 흡수, text_llm 0 호출."""
    calls = []

    def recording_llm(prompt, payload):
        calls.append((prompt, payload))
        return "SHOULD_NOT_BE_USED"

    blocks = [
        {"type": "text", "text": "[단위:원]"},           # 제목형 → 흡수
        {"type": "table", "table_body": "<table>T</table>"},
        {"type": "text", "text": "※각주 설명"},          # 각주형 → 흡수
        {"type": "text", "text": "다음 본문"},           # 각주 prefix 아님 → 별도
    ]
    enriched, ids = enrich(blocks, text_llm=recording_llm, vision_llm=None,
                           enrich_modals=False, wrap_modals=True)
    assert calls == []                                   # LLM 0 호출
    assert enriched.count(OPEN_PREFIX) == 1 and enriched.count(CLOSE) == 1
    span = enriched[enriched.index(OPEN_PREFIX):enriched.index(CLOSE)]
    assert "[단위:원]" in span and "※각주 설명" in span and "<table>T</table>" in span
    assert "SHOULD_NOT_BE_USED" not in enriched          # summary 빈문자열
    assert enriched.count("[단위:원]") == 1               # 흡수분 외부중복 0
    assert enriched.count("※각주 설명") == 1
    assert "다음 본문" in enriched[enriched.index(CLOSE):]  # 흡수 안 됨
    assert ids == ["T1"]


def test_wrap_off_enrich_off_no_markers_lossless():
    """escape 조합(wrap off & enrich off): 마커 0, 전 텍스트/payload 무손실."""
    blocks = [
        {"type": "text", "text": "제목줄"},
        {"type": "table", "table_body": "<table>Z</table>"},
        {"type": "text", "text": "※각주"},
    ]
    enriched, ids = enrich(blocks, text_llm=None, vision_llm=None,
                           enrich_modals=False, wrap_modals=False)
    assert OPEN_PREFIX not in enriched and CLOSE not in enriched
    assert "제목줄" in enriched and "<table>Z</table>" in enriched and "※각주" in enriched
    assert ids == ["T1"]                                 # decision 유지(drop 아님)


def test_wrap_off_enrich_on_consume_gated_title_survives():
    """진짜 실패모드(wrap off & enrich on, LLM tc/fc>0): mock 호출됨 AND 제목/각주 verbatim
    생존. consume 이 게이트 안 되면 흡수돼 bare payload 만 남아 사라진다(데이터 유실)."""
    calls = []

    def absorb_llm(prompt, payload):
        calls.append(1)
        return '{"summary": "SUM", "title_count": 1, "footnote_count": 1}'

    blocks = [
        {"type": "text", "text": "캡션"},
        {"type": "table", "table_body": "<table>Q</table>"},
        {"type": "text", "text": "각주"},
    ]
    enriched, ids = enrich(blocks, text_llm=absorb_llm, vision_llm=None,
                           enrich_modals=True, wrap_modals=False)
    assert calls == [1]                                  # enrich on → mock 호출됨
    assert OPEN_PREFIX not in enriched                   # wrap off → 마커 0
    assert "캡션" in enriched and "각주" in enriched      # consume 게이트 → verbatim 생존
    assert "<table>Q</table>" in enriched
    assert "SUM" not in enriched                         # bare payload 만 emit
    assert enriched.count("캡션") == 1 and enriched.count("각주") == 1


def test_oversize_boundary_just_under_wraps():
    """조립 span 추정이 임계 이하면 정상 래핑(마커)."""
    body = "<td>" + "x" * (modal._OVERSIZE_CHARS - 20) + "</td>"
    assert len(body) <= modal._OVERSIZE_CHARS
    blocks = [{"type": "table", "table_body": body}]
    enriched, ids = enrich(blocks, text_llm=None, vision_llm=None,
                           enrich_modals=False, wrap_modals=True)
    assert enriched.startswith(OPEN_PREFIX) and enriched.endswith(CLOSE)
    assert body in enriched
    assert ids == ["T1"]


def test_oversize_boundary_just_over_bare_lossless():
    """임계 초과면 bare(마커 0·흡수 0·decision 유지·무손실) — 적재실패 방지."""
    body = "<td>" + "x" * (modal._OVERSIZE_CHARS + 100) + "</td>"
    assert len(body) > modal._OVERSIZE_CHARS
    blocks = [
        {"type": "text", "text": "짧은 제목"},           # 제목형이나 oversize 라 흡수 0
        {"type": "table", "table_body": body},
    ]
    enriched, ids = enrich(blocks, text_llm=None, vision_llm=None,
                           enrich_modals=False, wrap_modals=True)
    assert OPEN_PREFIX not in enriched and CLOSE not in enriched
    assert body in enriched                              # payload 무손실
    assert "짧은 제목" in enriched                        # 흡수 안 됨 → 별도 세그먼트
    assert ids == ["T1"]                                 # decision 유지(drop 아님)


def test_enrich_with_spans_wrap_on_page_spans_align():
    """shipped 경로(enrich_with_spans, wrap on): 마커 삽입 + 흡수 후에도 page_spans 정합."""
    blocks = [
        {"type": "text", "text": "인트로 페이지1", "page_idx": 1},
        {"type": "text", "text": "[단위:원]", "page_idx": 2},        # 제목 흡수
        {"type": "table", "table_body": "<table>P</table>", "page_idx": 2},
        {"type": "text", "text": "※각주 설명", "page_idx": 2},        # 각주 흡수
    ]
    enriched, ids, spans = enrich_with_spans(
        blocks, text_llm=None, vision_llm=None,
        enrich_modals=False, wrap_modals=True,
    )
    assert ids == ["T1"]
    assert OPEN_PREFIX in enriched
    by_page = {s["page_number"]: s for s in spans}
    assert set(by_page) == {1, 2}
    s1, s2 = by_page[1], by_page[2]
    assert "인트로 페이지1" in enriched[s1["char_start"]:s1["char_end"]]
    sub2 = enriched[s2["char_start"]:s2["char_end"]]
    assert OPEN_PREFIX in sub2 and CLOSE in sub2
    assert "[단위:원]" in sub2 and "※각주 설명" in sub2
    assert s1["char_start"] == 0
    assert s1["char_end"] <= s2["char_start"]
    assert s2["char_end"] == len(enriched)               # enriched 총길이와 span 합치 일치


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
