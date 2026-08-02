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
# A: wrap_modals / enrich_modals 분리 + 문맥 복사(LLM 0) + oversize 가드
# =============================================================================

_nonblank_cands = modal._nonblank_cands
_ctx_copy_before = modal._ctx_copy_before
_ctx_after_blocks = modal._ctx_after_blocks


def _ctx_copy_after(after, blocks=None):
    """테스트 편의: 이동 대상 블록들을 예전처럼 문자열로 합쳐 본다.

    뒤쪽은 이제 '이동'이라 (idx, text) 쌍을 돌려주지만, 문맥 내용 검증은 문자열이 편하다.
    """
    return "\n".join(t for _, t, _mv in _ctx_after_blocks(after, blocks))
BEFORE_CHARS = modal._CTX_COPY_BEFORE_CHARS      # 100
AFTER_CHARS = modal._CTX_COPY_AFTER_CHARS        # 200


def test_nonblank_cands_skips_blank_and_flags_heading():
    # strip 후 비지 않은 것만 (텍스트, 제목여부) 로. blocks 없으면 제목여부 전부 False.
    assert _nonblank_cands([(0, "  "), (1, "\n"), (2, "제목")]) == [("제목", False)]
    assert _nonblank_cands([(0, "  제목  "), (1, "뒤")]) == [("제목", False), ("뒤", False)]
    assert _nonblank_cands([]) == []
    assert _nonblank_cands([(0, "   "), (1, "")]) == []
    assert _nonblank_cands([(0, None)]) == []                      # None-safe
    # blocks 를 주면 text_level 있는 블록만 제목으로 표시된다.
    blocks = [{"type": "text", "text": "머리글", "text_level": 2},
              {"type": "text", "text": "본문"}]
    assert _nonblank_cands([(0, "머리글"), (1, "본문")], blocks) == [("머리글", True), ("본문", False)]


def test_ctx_copy_stops_at_heading_both_directions():
    """앞=제목까지 포함하고 중단 / 뒤=제목 직전에 중단(다음 섹션이므로 제외)."""
    blocks = [
        {"type": "text", "text": "이전 섹션 본문"},                       # 0 — 경계 밖
        {"type": "text", "text": "이 표의 제목", "text_level": 3},        # 1 — 제목(포함+중단)
        {"type": "text", "text": "부제"},                                  # 2
        {"type": "table", "table_body": "<table>T</table>"},              # 3
        {"type": "text", "text": "주1) 각주"},                             # 4 — 본문(포함)
        {"type": "text", "text": "다음 섹션 제목", "text_level": 2},      # 5 — 제목(제외+중단)
        {"type": "text", "text": "다음 섹션 본문"},                        # 6 — 경계 밖
    ]
    before = [(2, "부제"), (1, "이 표의 제목"), (0, "이전 섹션 본문")]     # nearest-first
    after = [(4, "주1) 각주"), (5, "다음 섹션 제목"), (6, "다음 섹션 본문")]
    got_b = _ctx_copy_before(before, blocks)
    # 앞쪽은 제목 경계를 쓰지 않는다(방향 반대 — 앞쪽 제목은 이 표의 제목이라 가져와야 함).
    # 예산(100자) 안이면 이전 섹션 본문까지 섞일 수 있고, 그건 수용된 트레이드오프다.
    assert "이 표의 제목" in got_b and "부제" in got_b
    got_a = _ctx_copy_after(after, blocks)
    assert got_a == "주1) 각주"                        # 다음 섹션 제목 직전에서 중단
    # PUA 등 빈 블록에 text_level 이 붙어 있어도 진짜 제목을 놓치지 않는다(실측 함정).
    blocks_pua = [
        {"type": "text", "text": "진짜 제목", "text_level": 3},
        {"type": "text", "text": "   ", "text_level": 4},               # 빈 가짜 제목
        {"type": "table", "table_body": "<table>T</table>"},
    ]
    assert "진짜 제목" in _ctx_copy_before([(1, "   "), (0, "진짜 제목")], blocks_pua)


def test_ctx_copy_before_ignores_heading_boundary():
    """앞쪽: 표 직전이 제목으로 표시돼도 멈추지 않고 그 위 진짜 제목까지 가져온다.

    실측 회귀(휴가규정): `(개정 2025.09.01.)` 에 text_level 이 붙어 있어 제목에서 멈추면
    바로 위 `가정의례와 관련된 청원휴가 허가기준` 을 놓쳤다.
    """
    blocks = [
        {"type": "text", "text": "가정의례와 관련된 청원휴가 허가기준", "text_level": 3},
        {"type": "text", "text": "(개정 2025.09.01.)", "text_level": 4},
        {"type": "table", "table_body": "<table>T</table>"},
    ]
    got = _ctx_copy_before([(1, "(개정 2025.09.01.)"), (0, "가정의례와 관련된 청원휴가 허가기준")], blocks)
    assert "가정의례와 관련된 청원휴가 허가기준" in got and "(개정 2025.09.01.)" in got


def test_ctx_copy_fills_budget_across_blocks():
    """예산(앞200/뒤100)을 여러 블록으로 채운다 — 1블록에서 멈추지 않는다.

    실측 회귀: 표 직전이 ``(개정 …)`` 17자뿐이면 그것만 담기고 그 앞의 진짜 제목이
    빠졌다. 이제 예산이 남는 한 계속 거슬러 올라간다.
    """
    before = [(2, "(개정 2025.09.01.)"), (1, "가정의례와 관련된 청원휴가 허가기준"), (0, "제27조(청원휴가)")]
    got = _ctx_copy_before(before)
    assert "가정의례와 관련된 청원휴가 허가기준" in got     # 2번째 블록도 포함
    assert "제27조(청원휴가)" in got                       # 3번째 블록도 포함
    assert got.endswith("(개정 2025.09.01.)")              # 문서순: 표에 가장 가까운 게 끝
    assert len(got) <= BEFORE_CHARS

    # 합계가 예산을 확실히 넘도록 구성 → 마지막 블록이 잘려 예산을 정확히 채운다.
    after = [(4, "각주1 " + "가" * 30), (5, "각주2 " + "나" * 30),
             (6, "각주3 " + "다" * (AFTER_CHARS + 50))]
    got_a = _ctx_copy_after(after)
    assert got_a.startswith("각주1 ")                      # 표에 가장 가까운 게 앞
    assert "각주2 " in got_a                               # 예산 안이면 다음 블록도
    assert len(got_a) <= AFTER_CHARS                       # 통째 블록만(이동 가능하게) → 예산은 상한


def test_ctx_copy_before_takes_last_chars():
    assert _ctx_copy_before([(0, "짧은 제목")]) == "짧은 제목"       # 짧으면 전체
    long = "가" * 500
    got = _ctx_copy_before([(0, long)])
    assert len(got) == BEFORE_CHARS and got == long[-BEFORE_CHARS:]  # 끝 BEFORE_CHARS 자
    exact = "나" * BEFORE_CHARS
    assert _ctx_copy_before([(0, exact)]) == exact                   # 경계=전체
    assert _ctx_copy_before([]) == ""                                # 직전이 표
    assert _ctx_copy_before([(0, "   ")]) == ""                      # 전부 공백


def test_ctx_copy_after_takes_whole_blocks_within_budget():
    assert _ctx_copy_after([(0, "짧은 각주")]) == "짧은 각주"
    # 문장 경계가 **하나도 없는** 초과 블록은 제외(어디서 끊어도 문장이 깨진다).
    long = "다" * 500
    assert _ctx_copy_after([(0, long)]) == ""
    # 예산 안이면 여러 블록을 통째로 이어 담고, 넘치는 블록에서 중단한다.
    a, b, c = "가" * 80, "나" * 80, "다" * 80
    got = _ctx_copy_after([(0, a), (1, b), (2, c)])
    assert got == a + "\n" + b and len(got) <= AFTER_CHARS
    assert _ctx_copy_after([]) == ""
    assert _ctx_copy_after([(0, "  \n ")]) == ""


def test_sentence_boundary_detection_ignores_decimals_and_dates():
    """문장 경계 오탐 차단 — 소수점(``18.9%``)·날짜(``2025.09.01.``)는 경계가 아니다."""
    assert modal._sentence_starts("비중은 18.9%로 낮고 안전자산은 66.9%다. 다음 문장.") == [
        len("비중은 18.9%로 낮고 안전자산은 66.9%다. ")]
    assert modal._sentence_starts("가정의례 허가기준 (개정 2025.09.01.)") == []
    # 줄바꿈은 그 자체로 안전한 경계.
    assert modal._sentence_starts("첫 줄\n둘째 줄") == [len("첫 줄\n")]


def test_ctx_before_never_cuts_mid_sentence():
    """앞 문맥은 예산 컷이 문장 중간이면 **문장 처음까지 거슬러 올라간다**(한도 내).

    실측(KIS): 200자 컷이 ``'단한다.'``·``'적인 자산운용**'`` 처럼 어절 중간이었다.
    """
    s1 = "첫 번째 문장이며 예산 밖으로 밀려난다. "
    s2 = "두 번째 문장은 표 바로 앞에 붙는 설명이다. "
    s3 = "세 번째 문장 " + "가" * 340 + " 로 끝난다."   # 블록 처음까지 확장은 한도 초과
    got = _ctx_copy_before([(0, s1 + s2 + s3)])
    assert got.startswith("세 번째 문장")                  # 컷이 걸린 문장의 '처음'으로 확장
    assert "첫 번째 문장" not in got                       # 예산 밖 문장은 제외
    assert "두 번째 문장" not in got
    assert len(got) <= BEFORE_CHARS + modal._CTX_SENTENCE_OVERSHOOT

    # 확장이 한도를 넘으면 **다음 문장부터**로 줄인다(예산 미달을 감수).
    huge = "앞 문장. " + "나" * (BEFORE_CHARS + modal._CTX_SENTENCE_OVERSHOOT + 50) + "."
    got2 = _ctx_copy_before([(0, huge)])
    assert len(got2) <= BEFORE_CHARS + modal._CTX_SENTENCE_OVERSHOOT


def test_ctx_after_long_block_copies_whole_sentences_instead_of_dropping():
    """예산 초과 각주 블록도 **버리지 않고** 온전한 문장까지 복사한다(이동은 금지).

    이전엔 첫 블록이 201자면 뒤 문맥이 통째로 0 이었다.
    """
    blk = ("주1) 첫 각주 문장이다. " + "주2) 두 번째 각주 문장이다. "
           + "주3) " + "다" * 250 + " 로 끝나는 아주 긴 각주다.")
    got = _ctx_after_blocks([(1, blk)])
    assert len(got) == 1
    idx, text, movable = got[0]
    assert text.startswith("주1) 첫 각주 문장이다.")
    assert "주2) 두 번째 각주 문장이다." in text
    assert movable is False                               # 부분 발췌 → 원본 보존(복사)
    assert len(text) <= AFTER_CHARS

    # 각주가 **아닌** 초과 블록은 구제하지 않는다 — 실측(KIS)에서 다음 절 본문
    # (`## Key Issue Update …`, `우수한 자본적정성 …`)이 표 문맥으로 딸려왔다.
    body = ("다음 절이 시작되는 본문 문장이다. " * 3) + "가" * 200 + " 끝."
    assert _ctx_after_blocks([(1, body)]) == []


def test_enrich_off_wrap_on_copies_context_without_llm():
    """shipped 기본 경로(enrich off & wrap on): 마커 + 문맥 **복사**, text_llm 0 호출.

    복사이므로 원본 블록이 자기 세그먼트로 **그대로 남는다**(흡수 아님) → 2회 등장.
    """
    calls = []

    def recording_llm(prompt, payload):
        calls.append((prompt, payload))
        return "SHOULD_NOT_BE_USED"

    blocks = [
        {"type": "text", "text": "[단위:원]"},           # 앞 블록 → 사본이 span 안으로
        {"type": "table", "table_body": "<table>T</table>"},
        {"type": "text", "text": "※각주 설명"},          # 뒤 블록 → 사본이 span 안으로
        {"type": "text", "text": "다음 본문"},           # 예산(100자) 안이라 이것도 사본에 포함
    ]
    enriched, ids = enrich(blocks, text_llm=recording_llm, vision_llm=None,
                           enrich_modals=False, wrap_modals=True)
    assert calls == []                                   # LLM 0 호출
    assert enriched.count(OPEN_PREFIX) == 1 and enriched.count(CLOSE) == 1
    span = enriched[enriched.index(OPEN_PREFIX):enriched.index(CLOSE)]
    assert "[단위:원]" in span and "※각주 설명" in span and "<table>T</table>" in span
    assert "SHOULD_NOT_BE_USED" not in enriched          # summary 빈문자열
    # 복사(이동 아님) — 사본 + 원본으로 2회 등장.
    assert enriched.count("[단위:원]") == 2      # 앞: 복사 → 사본 + 원본
    # 뒤: **이동**(consume) → 원본 세그먼트가 사라지고 MODAL 안에만 1회.
    assert enriched.count("※각주 설명") == 1   # 각주 → 이동
    # 원본이 제 세그먼트로 살아있다(마커 밖에도 존재).
    outside = enriched[:enriched.index(OPEN_PREFIX)] + enriched[enriched.index(CLOSE):]
    assert "[단위:원]" in outside          # 앞: 원본 생존(복사)
    assert "※각주 설명" not in outside     # 뒤: 각주 표기 → 이동(consume)
    # 각주 표기가 아닌 블록("다음 본문")은 문맥으로 쓰되 **복사**(원본 유지).
    assert "다음 본문" in span and "다음 본문" in outside
    assert ids == ["T1"]


def test_copy_segment_count_drops_only_moved_after_blocks():
    """앞=복사(원본 유지) / 뒤=이동(consume) — 세그먼트는 뒤쪽 블록 수만큼만 줄어든다."""
    blocks = [
        {"type": "text", "text": "캡션"},                    # 앞: 복사 → 세그먼트 생존
        {"type": "table", "table_body": "<table>T</table>"},
        {"type": "text", "text": "※ 각주"},                  # 뒤: 각주 표기 → 이동
    ]
    # ⚠️ enriched 를 JOIN 으로 split 하면 안 된다 — 복사 경로는 summary="" 라 MODAL 안에도
    # "\n\n" 이 생긴다. 세그먼트 수는 _assemble 결과로 직접 센다.
    def _run(wrap):
        decisions, consumed, _ = modal._enrich_core(
            blocks, text_llm=None, vision_llm=None, max_workers=1,
            enrich_modals=False, wrap_modals=wrap,
        )
        segs, _pi = modal._assemble(blocks, decisions, consumed, wrap_modals=wrap)
        return len(segs), consumed

    n_wrap, consumed_wrap = _run(True)
    n_plain, consumed_plain = _run(False)
    assert consumed_plain == set()                  # wrap off = 이동 없음
    assert consumed_wrap == {2}                     # 각주 표기 블록만 이동
    assert n_plain == 3                             # 캡션 / 표 / 각주
    assert n_wrap == 2                              # 캡션 / MODAL(표+각주) — 각주 세그먼트 소멸
    # 각주 표기가 없는 블록은 이동하지 않는다(복사) — 세그먼트 유지.
    plain = [dict(b) for b in blocks]; plain[2]["text"] = "다음 본문입니다"
    d2, c2, _ = modal._enrich_core(plain, text_llm=None, vision_llm=None, max_workers=1,
                                   enrich_modals=False, wrap_modals=True)
    assert c2 == set()                              # 본문 → 이동 안 함
    assert len(modal._assemble(plain, d2, c2, wrap_modals=True)[0]) == 3


def test_ctx_before_crosses_page_but_stops_at_heading():
    """앞 문맥은 **페이지 경계를 넘어서 긁되, 섹션 제목까지만** — 200자가 최우선 규칙.

    표가 페이지 최상단이면 이전 페이지에 제목이 있다. 페이지로 끊으면 문맥이 0 이 되므로
    페이지는 조건이 아니고, ``text_level`` 제목을 만나면 **그 제목을 포함하고** 멈춘다
    (그 위는 다른 절이다). 앞은 복사이므로 페이지 오귀속이 발생하지 않는다.
    """
    blocks = [
        {"type": "text", "text": "p1 이전 절 본문", "page_idx": 1},                     # 제목 위 — 제외
        {"type": "text", "text": "p1 표 제목", "page_idx": 1, "text_level": 2},        # 제목 — 포함하고 중단
        {"type": "text", "text": "p1 리드문", "page_idx": 1},                          # 이전 페이지지만 포함
        {"type": "table", "table_body": "<table>X</table>", "page_idx": 2},
    ]
    enriched, ids, spans = _assert_enrich_parity(
        blocks, text_llm=None, vision_llm=None,
        enrich_modals=False, wrap_modals=True,
    )
    assert ids == ["T1"]
    span = enriched[enriched.index(OPEN_PREFIX):enriched.index(CLOSE)]
    assert "p1 리드문" in span                               # 페이지가 달라도 복사됨
    assert "p1 표 제목" in span                              # 제목은 포함하고 중단
    assert "p1 이전 절 본문" not in span                     # 제목 위 = 다른 절
    by_page = {s["page_number"]: s for s in spans}
    assert set(by_page) == {1, 2}                           # 페이지 사라짐 없음
    assert enriched.count("p1 이전 절 본문") == 1            # 원본만
    assert enriched.count("p1 표 제목") == 2                 # 사본 + 원본(앞은 복사)


def test_copy_adjacent_tables_no_context():
    """인접표 [표1][표2] → before/after 가 비어 ctx="" (표끼리 안 섞임)."""
    blocks = [
        {"type": "table", "table_body": "<table>A</table>"},
        {"type": "table", "table_body": "<table>B</table>"},
    ]
    enriched, ids = enrich(blocks, text_llm=None, vision_llm=None,
                           enrich_modals=False, wrap_modals=True)
    assert ids == ["T1", "T2"]
    assert enriched.count("<table>A</table>") == 1
    assert enriched.count("<table>B</table>") == 1


def test_copy_after_fills_budget_over_multiple_footnote_blocks():
    """[표][※주1][※주2] → 예산(100자) 안이면 **둘 다** ctx_after 에 담긴다(복사).

    각주가 여러 블록으로 쪼개져 있어도 예산까지 이어 담는다(1블록 계약 폐기 — 실측에서
    각주 2·3번째가 통째로 빠지는 문제). 원본 블록은 그대로 남으므로 각각 2회 등장.
    """
    blocks = [
        {"type": "table", "table_body": "<table>T</table>"},
        {"type": "text", "text": "※주1 상기 금액은 부가세 별도"},
        {"type": "text", "text": "※주2 환율은 기준일 기준"},
    ]
    enriched, ids = enrich(blocks, text_llm=None, vision_llm=None,
                           enrich_modals=False, wrap_modals=True)
    span = enriched[enriched.index(OPEN_PREFIX):enriched.index(CLOSE)]
    assert "※주1 상기 금액은 부가세 별도" in span
    assert "※주2 환율은 기준일 기준" in span                     # 예산 안이라 둘째도 포함
    # 뒤는 **이동** — 원본 세그먼트가 사라져 각각 1회(MODAL 안)만 등장.
    assert enriched.count("※주1 상기 금액은 부가세 별도") == 1
    assert enriched.count("※주2 환율은 기준일 기준") == 1
    assert ids == ["T1"]


def test_copy_long_article_block_truncated_to_last_200():
    """조항 통째 유입 방지 — 직전 2000자 블록에서 끝 200자만 복사."""
    # 반복 없는 2000자(각 조각이 유일해야 '앞부분 미포함'을 단언할 수 있다).
    long_block = "".join(f"{k:04d}" for k in range(500))
    blocks = [
        {"type": "text", "text": long_block},
        {"type": "table", "table_body": "<table>T</table>"},
    ]
    enriched, _ = enrich(blocks, text_llm=None, vision_llm=None,
                         enrich_modals=False, wrap_modals=True)
    span = enriched[enriched.index(OPEN_PREFIX):enriched.index(CLOSE)]
    assert long_block[-BEFORE_CHARS:] in span
    assert long_block[:100] not in span                     # 앞부분은 안 들어옴
    assert long_block in enriched                           # 원본은 온전


def test_copy_disabled_when_wrap_off():
    """wrap_modals=False → ctx="" (§C else 분기) — 마커도 사본도 없음."""
    blocks = [
        {"type": "text", "text": "캡션"},
        {"type": "table", "table_body": "<table>T</table>"},
        {"type": "text", "text": "각주"},
    ]
    enriched, _ = enrich(blocks, text_llm=None, vision_llm=None,
                         enrich_modals=False, wrap_modals=False)
    assert OPEN_PREFIX not in enriched
    assert enriched.count("캡션") == 1 and enriched.count("각주") == 1


def test_copy_applies_to_image_and_equation_modals():
    """복사는 table 전용이 아니라 image/equation 모달에도 동일 적용."""
    for btype, key, body in (("image", "img_path", "fig.png"),
                             ("equation", "latex", "E=mc^2")):
        blocks = [
            {"type": "text", "text": "그림 캡션"},
            {"type": btype, key: body},
            {"type": "text", "text": "출처 표기"},
        ]
        enriched, ids = enrich(blocks, text_llm=None, vision_llm=None,
                               enrich_modals=False, wrap_modals=True)
        span = enriched[enriched.index(OPEN_PREFIX):enriched.index(CLOSE)]
        assert "그림 캡션" in span and "출처 표기" in span, btype
        assert enriched.count("그림 캡션") == 2, btype       # 사본 + 원본
        assert len(ids) == 1


def test_oversize_two_stage_drops_ctx_but_keeps_wrap():
    """§F 1순위 — 본체는 임계 이하인데 ctx 를 더하면 초과: ctx 만 버리고 **래핑 유지**."""
    # 본체는 임계 이하지만 ctx(앞 예산 가득)를 더하면 초과하도록 여유를 예산보다 작게 둔다.
    body = "x" * (modal._OVERSIZE_CHARS - BEFORE_CHARS // 2)
    caption = "제" * BEFORE_CHARS                            # 예산 가득 → 합계 초과
    blocks = [
        {"type": "text", "text": caption},
        {"type": "table", "table_body": body},
    ]
    enriched, ids = enrich(blocks, text_llm=None, vision_llm=None,
                           enrich_modals=False, wrap_modals=True)
    assert OPEN_PREFIX in enriched and CLOSE in enriched     # bare 아님(원자성 유지)
    span = enriched[enriched.index(OPEN_PREFIX):enriched.index(CLOSE)]
    assert caption not in span                               # ctx 는 포기
    assert enriched.count(caption) == 1                      # 원본만 남음
    assert ids == ["T1"]


def test_oversize_two_stage_body_alone_over_goes_bare():
    """§F 2순위 — 본체만으로 초과: bare(마커 0) + ctx 무효화."""
    body = "x" * (modal._OVERSIZE_CHARS + 100)
    blocks = [
        {"type": "text", "text": "짧은 제목"},
        {"type": "table", "table_body": body},
    ]
    enriched, ids = enrich(blocks, text_llm=None, vision_llm=None,
                           enrich_modals=False, wrap_modals=True)
    assert OPEN_PREFIX not in enriched and CLOSE not in enriched
    assert enriched.count("짧은 제목") == 1                   # 사본 없음
    assert body in enriched
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
    """shipped 경로(enrich_with_spans, wrap on): 마커 삽입 + 문맥 복사 후에도 page_spans 정합."""
    blocks = [
        {"type": "text", "text": "인트로 페이지1", "page_idx": 1},
        {"type": "text", "text": "[단위:원]", "page_idx": 2},        # 앞 문맥 복사(원본 유지)
        {"type": "table", "table_body": "<table>P</table>", "page_idx": 2},
        {"type": "text", "text": "※각주 설명", "page_idx": 2},        # 뒤 문맥 복사(원본 유지)
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
