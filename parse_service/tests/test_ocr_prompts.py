"""Plan B-4 — DIAGRAM 프롬프트 개정 + 서술 append 시 표 처리.

프롬프트 본문에 대한 assert 가 레포에 0건이라, 개정이 무검증으로 남는 것을 막는다.
"""
import parse_service.parsers.pdf as pdf_parser
from parse_service.parsers.ocr import prompts


# ── 프롬프트 개정 ────────────────────────────────────────────────────────────
def test_diagram_prompt_is_self_judging_not_assertive():
    """단정형("이 이미지는 순서도다")이면 표 페이지에 없는 흐름을 지어낸다.

    B 에서 KIS 같은 표 문서가 odl 레인 + narrate_pages 가 되어 이 프롬프트를 받게 된다
    (현행은 vl 레인이라 서술 경로를 아예 안 탔다).
    """
    u = prompts.DIAGRAM_USER_PROMPT
    assert "이 이미지는 업무 순서도" not in u, "단정형 금지"
    assert "무엇인지 먼저 판단" in u


def test_diagram_prompt_covers_table_and_chart_and_other():
    u = prompts.DIAGRAM_USER_PROMPT
    assert "3줄 이내" in u, "차트는 요약"
    assert "2줄 이내" in u, "표는 요약"
    assert "빈 배열" in u, "그 외는 빈 elements"


def test_diagram_prompt_has_no_transcription_order():
    """'표·주석은 원문 그대로 보존(전사)' 지시는 위 요약 규칙과 정면 충돌한다."""
    assert "원문 그대로 보존" not in prompts.DIAGRAM_USER_PROMPT


def test_diagram_prompt_keeps_flow_rules():
    u = prompts.DIAGRAM_USER_PROMPT
    assert "논리 흐름을 서술" in u and "스윔레인" in u and "조건 분기" in u


# ── 서술 append 시 표 처리(_diagram_blocks) ──────────────────────────────────
_MIXED = ("순서도 앞 산문입니다.\n\n"
          "| 유형 | 값 |\n|---|---|\n| a | 1 |\n\n"
          "순서도 뒤 산문입니다.")


def _els(md):
    return [{"category": "figure", "content": {"html": "", "markdown": md, "text": ""},
             "page": 1}]


def test_append_mode_drops_only_tables_not_prose():
    """표만 빼고 산문은 남긴다 — 블록을 통째로 버리면 서술이 사라진다(v6 설계 오류)."""
    out = pdf_parser._diagram_blocks(_els(_MIXED), 3, drop_tables=True)
    texts = " ".join(b.get("text") or "" for b in out)
    assert "순서도 앞 산문입니다." in texts and "순서도 뒤 산문입니다." in texts
    assert not any(b["type"] == "table" for b in out), "표 정본은 베이스 파서가 소유"


def test_replace_mode_keeps_tables():
    """교체 모드는 원본 블록이 통째로 대체되므로 표를 뺄 이유가 없다."""
    out = pdf_parser._diagram_blocks(_els(_MIXED), 3, drop_tables=False)
    assert any(b["type"] == "table" for b in out)


def test_page_idx_is_set():
    out = pdf_parser._diagram_blocks(_els("서술"), 7, drop_tables=True)
    assert out and all(b.get("page_idx") == 7 for b in out)


def test_empty_markdown_yields_nothing():
    assert pdf_parser._diagram_blocks(_els("   "), 1, drop_tables=True) == []


def test_text_fallback_when_markdown_missing():
    """markdown 이 비고 text 만 있는 element 도 살린다(무음 소실 방지)."""
    els = [{"category": "figure",
            "content": {"html": "", "markdown": "", "text": "텍스트만 있는 서술"}, "page": 1}]
    out = pdf_parser._diagram_blocks(els, 2, drop_tables=True)
    assert any("텍스트만 있는 서술" in (b.get("text") or "") for b in out)
