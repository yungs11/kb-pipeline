"""Unit tests for the modal title/footnote boundary helpers (pure, no LLM)."""
import json

from kb_pipeline.modal import (
    BEFORE_WINDOW, AFTER_WINDOW, _is_text,
    _gather_before_window, _gather_after_window,
    _parse_boundary_response,
    _boundary_prompt, _boundary_payload,
    _wrap, MODAL_OPEN_PREFIX, MODAL_CLOSE,
)


# --- Task 1: candidate windows ------------------------------------------------

def test_window_constants():
    assert BEFORE_WINDOW == 3 and AFTER_WINDOW == 6


def test_is_text():
    assert _is_text({"type": "text", "text": "x"})
    assert not _is_text({"type": "table"})


def test_before_window_nearest_first_stops_at_nontext():
    blocks = [
        {"type": "text", "text": "a"},     # 0
        {"type": "table"},                 # 1 (non-text barrier)
        {"type": "text", "text": "b"},     # 2
        {"type": "text", "text": "c"},     # 3
        {"type": "table"},                 # 4  <- i
    ]
    out = _gather_before_window(blocks, 4, set())
    assert out == [(3, "c"), (2, "b")]  # nearest-first, stops before table@1


def test_before_window_stops_at_consumed():
    blocks = [
        {"type": "text", "text": "a"},  # 0
        {"type": "text", "text": "b"},  # 1
        {"type": "table"},              # 2  <- i
    ]
    assert _gather_before_window(blocks, 2, {1}) == []  # 1 consumed -> stop


def test_before_window_caps_at_BEFORE_WINDOW():
    blocks = [{"type": "text", "text": str(k)} for k in range(5)] + [{"type": "table"}]
    out = _gather_before_window(blocks, 5, set())
    assert [t for _, t in out] == ["4", "3", "2"]  # only 3 nearest


def test_after_window_nearest_first_stops_at_nontext():
    blocks = [
        {"type": "table"},                 # 0 <- i
        {"type": "text", "text": "a"},     # 1
        {"type": "text", "text": "b"},     # 2
        {"type": "image"},                 # 3 barrier
        {"type": "text", "text": "c"},     # 4
    ]
    out = _gather_after_window(blocks, 0, set())
    assert out == [(1, "a"), (2, "b")]


def test_after_window_caps_at_AFTER_WINDOW():
    blocks = [{"type": "table"}] + [{"type": "text", "text": str(k)} for k in range(8)]
    out = _gather_after_window(blocks, 0, set())
    assert len(out) == AFTER_WINDOW and out[0] == (1, "0")


# --- Task 2: boundary LLM response parser -------------------------------------

def test_parse_valid_json_clamps_counts():
    raw = json.dumps({"summary": "한글요약", "title_count": 2, "footnote_count": 5})
    assert _parse_boundary_response(raw, n_before=3, n_after=2) == ("한글요약", 2, 2)


def test_parse_strips_code_fence():
    raw = '```json\n{"summary":"s","title_count":1,"footnote_count":0}\n```'
    assert _parse_boundary_response(raw, 3, 3) == ("s", 1, 0)


def test_parse_negative_counts_floored_to_zero():
    raw = json.dumps({"summary": "s", "title_count": -1, "footnote_count": 1})
    assert _parse_boundary_response(raw, 3, 3) == ("s", 0, 1)


def test_parse_non_json_falls_back():
    assert _parse_boundary_response("그냥 설명 (JSON 아님)", 3, 3) == ("그냥 설명 (JSON 아님)", 0, 0)


def test_parse_missing_summary_falls_back():
    raw = json.dumps({"title_count": 2, "footnote_count": 1})
    assert _parse_boundary_response(raw, 3, 3) == (raw, 0, 0)


def test_parse_non_int_counts_fall_back():
    raw = json.dumps({"summary": "s", "title_count": "two", "footnote_count": 1})
    assert _parse_boundary_response(raw, 3, 3) == (raw, 0, 0)


def test_parse_ignores_trailing_text_and_braces():
    # 바깥 중괄호/후행 잡음이 있어도 첫 유효 JSON 객체만 파싱(greedy 회귀 방지).
    raw = '설명: {"summary":"s","title_count":1,"footnote_count":1} 추가 {잡음}'
    assert _parse_boundary_response(raw, 3, 3) == ("s", 1, 1)


# --- Task 3: Korean prompt + boundary payload ---------------------------------

def test_prompt_is_korean_and_requests_json():
    p = _boundary_prompt("table")
    assert ("한국어" in p) or ("한글" in p)
    assert "title_count" in p and "footnote_count" in p and "summary" in p


def test_prompt_varies_by_type():
    assert "표" in _boundary_prompt("table")
    assert "수식" in _boundary_prompt("equation")
    assert "이미지" in _boundary_prompt("image")


def test_prompt_pushes_title_and_markerless_footnote_inclusion():
    p = _boundary_prompt("table")
    assert "머리글" in p and "반드시 포함" in p      # 제목 줄을 더 적극 흡수
    assert "마커" in p                                 # 마커 없는 각주도 포함 지시


def test_payload_lists_candidates_nearest_first_and_body():
    before = [(5, "가까운제목"), (4, "먼제목")]   # nearest-first
    after = [(7, "가까운각주")]
    body = "<table>X</table>"
    out = _boundary_payload(before, after, body)
    assert "B1: 가까운제목" in out and "B2: 먼제목" in out
    assert "A1: 가까운각주" in out
    assert "<table>X</table>" in out
    # 본문은 앞 후보 뒤, 뒤 후보 앞에 위치
    assert out.index("B1:") < out.index("<table>X</table>") < out.index("A1:")


def test_payload_handles_empty_windows():
    out = _boundary_payload([], [], "BODY")
    assert "BODY" in out  # no crash, body present


# --- Task 4: extended _wrap ---------------------------------------------------

def test_wrap_backward_compatible_without_title_footnote():
    out = _wrap("T1", "table", "DESC", "<table/>")
    assert out == '〈MODAL id="T1" type="table"〉DESC\n<table/>〈/MODAL〉'


def test_wrap_inserts_title_before_and_footnote_after():
    out = _wrap("T1", "table", "요약", "<table/>", title="제목줄", footnote="각주줄")
    assert out.startswith(MODAL_OPEN_PREFIX) and out.endswith(MODAL_CLOSE)
    # 순서: 제목 < 요약 < payload < 각주
    assert out.index("제목줄") < out.index("요약") < out.index("<table/>") < out.index("각주줄")
    assert out.count(MODAL_OPEN_PREFIX) == 1 and out.count(MODAL_CLOSE) == 1
