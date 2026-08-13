"""PDF **페이지수준 혼합 라우팅**(Plan B-5) — 페이지마다 레인을 고르고 병합한다.

SKIP→skip / OCR_NEEDED→paddle_gw / TEXT_ONLY·LLM_NEEDED→odl.
문서수준 `vl` 레인은 삭제됐다(그림 비율만 보고 문서 전체를 VL 로 넘겨 표를 깨뜨리던 경로).
"""
import logging

import pytest

import parse_service.parsers.pdf as pdf_parser
import parse_service.parsers.pdf.paddle_gw as pg
from parse_service.parsers import RouteResult
from parse_service.parsers.pdf.gate import RouteDecision
from parse_service.parsers.pdf.triage import Bucket, PageSignals


# ── 헬퍼 ────────────────────────────────────────────────────────────────────
def _decision(lanes, *, narrate=(), total=None):
    """lanes = {pno: "odl"|"skip"|"paddle_gw"}"""
    return RouteDecision(
        lane="odl",                                   # B-5 는 이 필드를 읽지 않는다
        page_lanes=tuple(sorted(lanes.items())),
        narrate_pages=tuple(narrate),
        total_pages=total if total is not None else len(lanes),
    )


class _RP:
    # `text` = PyMuPDF 네이티브 추출본(pdf_pages.py:59). VL 전사 실패 시 폴백 소스라
    # 기본값을 페이지마다 구분되는 값으로 둔다 — 빈 문자열이면 폴백 앵커가 무의미해진다.
    def __init__(self, n, text=None):
        self.page_number, self.jpeg = n, b"jpegbytes"
        self.text = f"native-text-p{n}" if text is None else text


@pytest.fixture
def wire(monkeypatch):
    """게이트/ODL/렌더/VL/게이트웨이를 fake 로 잡고 호출 기록을 준다."""
    rec = {"vl_jobs": [], "render": [], "gw_pages": None, "md": []}

    def set_gate(decision):
        monkeypatch.setattr(pdf_parser, "_safe_decide_route", lambda fb: decision)

    def set_md(md_list):
        rec["md"] = md_list
        monkeypatch.setattr(pdf_parser, "_page_markdowns", lambda fb, fn: list(md_list))

    def set_gw(pages_or_exc):
        def fake_gw(fb, fn, page_numbers=None):
            rec["gw_pages"] = page_numbers
            if isinstance(pages_or_exc, Exception):
                raise pages_or_exc
            return list(pages_or_exc)
        monkeypatch.setattr(pg, "run_paddle_gateway", fake_gw)

    def set_vl(text="VL 전사"):
        def fake_batch(jobs, ocr_url=None, **k):
            rec["vl_jobs"].extend(name for _j, name in jobs)
            return [([{"category": "text", "content": {"markdown": text}, "page": 0}], [])
                    for _ in jobs]
        monkeypatch.setattr(pdf_parser, "_ocr_elements_for_pages", fake_batch)

    def fake_render(fb, page_numbers=None, **k):
        rec["render"].append(tuple(sorted(page_numbers)) if page_numbers else None)
        return [_RP(n) for n in sorted(page_numbers or ())]

    monkeypatch.setattr(pdf_parser, "_render_pages", fake_render)
    rec.update(set_gate=set_gate, set_md=set_md, set_gw=set_gw, set_vl=set_vl)
    set_vl()
    return rec


def _texts(pages, pno):
    p = next(x for x in pages if x["page_number"] == pno)
    return " ".join(b.get("text") or b.get("table_body") or "" for b in p["blocks"])


# ── 레인 파티션 ──────────────────────────────────────────────────────────────
def test_mixed_document_routes_each_page(wire):
    """혼재 문서: 스캔 페이지만 게이트웨이로, 나머지는 ODL — 문서 전체가 한 레인으로 안 간다."""
    wire["set_gate"](_decision({1: "odl", 2: "paddle_gw", 3: "odl"}))
    wire["set_md"](["# p1 본문", "", "# p3 본문"])
    wire["set_gw"]([{"page_number": 2, "blocks": [{"type": "text", "text": "스캔 p2"}],
                     "layout": [], "page_size": None}])
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert wire["gw_pages"] == {2}, "게이트웨이엔 스캔 페이지만 전달"
    assert "p1 본문" in _texts(res.pages, 1)
    assert "스캔 p2" in _texts(res.pages, 2)
    assert "p3 본문" in _texts(res.pages, 3)


def test_no_scan_page_skips_gateway(wire, monkeypatch):
    """스캔 페이지가 없으면 게이트웨이를 부르지 않는다(KIS 류 — 표가 ODL <table> 로 보존된다)."""
    called = []
    monkeypatch.setattr(pg, "run_paddle_gateway",
                        lambda *a, **k: called.append(1) or [])
    wire["set_gate"](_decision({1: "odl", 2: "odl"}))
    wire["set_md"](["<table><tr><td>표</td></tr></table>", "# 본문"])
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert called == []
    assert "<table>" in _texts(res.pages, 1), "표는 ODL <table> 로 보존"


def test_pure_scan_document_skips_odl(wire, monkeypatch):
    """paddle 레인만 있는 문서는 ODL(JRE)을 부르지 않는다."""
    called = []
    monkeypatch.setattr(pdf_parser, "_page_markdowns",
                        lambda fb, fn: called.append(1) or [])
    wire["set_gate"](_decision({1: "paddle_gw", 2: "paddle_gw"}))
    wire["set_gw"]([{"page_number": n, "blocks": [{"type": "text", "text": f"스캔 p{n}"}],
                     "layout": [], "page_size": None} for n in (1, 2)])
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert called == []
    assert "스캔 p1" in _texts(res.pages, 1)


def test_skip_page_uses_md_but_never_vl(wire):
    """SKIP 페이지: md 있으면 블록화, 없으면 빈 blocks. **VL 은 어느 쪽이든 안 부른다.**"""
    wire["set_gate"](_decision({1: "skip", 2: "skip"}))
    wire["set_md"](["# 간지 제목", "   "])
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert "간지 제목" in _texts(res.pages, 1)
    assert _texts(res.pages, 2) == ""
    assert wire["vl_jobs"] == []


# ── 페이지 정합 ──────────────────────────────────────────────────────────────
def test_gateway_result_maps_to_absolute_page_number(wire):
    """게이트웨이 결과 키(문서 절대 번호)와 odl_md[pno-1] 이 같은 페이지를 가리킨다."""
    wire["set_gate"](_decision({1: "odl", 2: "odl", 3: "paddle_gw", 4: "odl"}))
    wire["set_md"](["p1", "p2", "", "p4"])
    wire["set_gw"]([{"page_number": 3, "blocks": [{"type": "text", "text": "스캔 세번째"}],
                     "layout": [], "page_size": None}])
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert "스캔 세번째" in _texts(res.pages, 3), "밀리면 다른 페이지에 붙는다"
    assert "p4" in _texts(res.pages, 4)


def test_page_count_mismatch_delegates_to_odl_lane(wire, monkeypatch):
    """ODL md 페이지수가 어긋나면 페이지수준 병합을 포기하고 문서 전체 ODL 위임 + 서술 미부착."""
    seen = {}

    def fake_odl(fb, fn, *, ocr_url, diagram_pages=()):
        seen["diagram_pages"] = diagram_pages
        return RouteResult(kind="pages", chunk_needed=True, pages=[])

    monkeypatch.setattr(pdf_parser, "_odl_lane", fake_odl)
    wire["set_gate"](_decision({1: "odl", 2: "odl", 3: "odl"}, narrate=(2,)))
    wire["set_md"](["p1", "p2"])                 # 2 != 3
    pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert seen["diagram_pages"] == (), "인덱스를 믿을 수 없으므로 서술을 붙이지 않는다"


# ── 폴백 ────────────────────────────────────────────────────────────────────
def test_gateway_lane_failure_falls_back_to_vl_per_page(wire):
    """게이트웨이 불능(프로브 실패/URL 미설정) → 그 페이지들을 VL 전사로 살린다."""
    wire["set_gate"](_decision({1: "odl", 2: "paddle_gw"}))
    wire["set_md"](["# p1", ""])
    wire["set_gw"](RuntimeError("gateway down"))
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert "page-2.jpeg" in wire["vl_jobs"]
    assert "VL 전사" in _texts(res.pages, 2), "문서가 통째로 비면 안 된다"


def test_gateway_single_page_failure_falls_back_to_vl(wire):
    """개별 페이지 실패(키는 남고 blocks 만 빔)도 그 페이지만 VL 전사."""
    wire["set_gate"](_decision({1: "paddle_gw", 2: "paddle_gw"}))
    wire["set_gw"]([
        {"page_number": 1, "blocks": [{"type": "text", "text": "정상 p1"}],
         "layout": [], "page_size": None},
        # 6-key 계약(2026-08-12): 개별 페이지 실패는 `status="error"` 를 싣는다.
        # **이 키가 demote 조건이다** — `status=="ok"` + 빈 blocks 는 강등하지 않고
        # 게이트가 EMPTY 로 판정한다(§4a, v1 이 실측 기각한 escalation 부활 방지).
        {"page_number": 2, "blocks": [], "layout": [], "page_size": None,
         "status": "error", "error": "TimeoutError: poll"},
    ])
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert "정상 p1" in _texts(res.pages, 1)
    assert "VL 전사" in _texts(res.pages, 2)
    assert wire["vl_jobs"] == ["page-2.jpeg"], "실패한 페이지만"


def test_thin_odl_page_gets_vl(wire):
    """ODL 이 빈약하게 뽑은 페이지는 VL 전사(현행 규칙 유지)."""
    wire["set_gate"](_decision({1: "odl", 2: "odl"}))
    wire["set_md"](["# 본문 있음", "![img](x.png)"])     # p2 = 실텍스트 0
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert wire["vl_jobs"] == ["page-2.jpeg"]
    assert "VL 전사" in _texts(res.pages, 2)


def test_gate_none_falls_back_to_odl_lane(monkeypatch):
    """게이트 예외(pymupdf 부재 등) → 문서 전체 ODL. 새 500 을 만들지 않는다."""
    monkeypatch.setattr(pdf_parser, "_safe_decide_route", lambda fb: None)
    monkeypatch.setattr(pdf_parser, "_page_markdowns", lambda fb, fn: ["# p1"])
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert res.kind == "pages" and res.pages


# ── 렌더 ────────────────────────────────────────────────────────────────────
def test_render_called_once_for_union_of_thin_and_narrate(wire):
    """300dpi 렌더는 `thin ∪ narrate` 로 **1회**. narrate 를 빼면 서술이 전멸한다."""
    wire["set_gate"](_decision({1: "odl", 2: "odl", 3: "odl"}, narrate=(1,)))
    wire["set_md"](["# 순서도 페이지(텍스트 있음)", "", "# p3"])   # p2 = thin
    pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert wire["render"] == [(1, 2)], "thin(2) ∪ narrate(1) 한 번"


def test_no_render_when_nothing_needs_it(wire):
    """thin 도 narrate 도 없으면 렌더하지 않는다(전량 렌더 부활 방지)."""
    wire["set_gate"](_decision({1: "odl", 2: "odl"}))
    wire["set_md"](["# p1", "# p2"])
    pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert wire["render"] == []


# ── 서술 보충 ────────────────────────────────────────────────────────────────
def test_narrate_pages_get_diagram_supplement(wire):
    """순서도 페이지에 VL 서술이 **덧붙는다**(원본 텍스트 유지 — append 모드)."""
    wire["set_gate"](_decision({1: "odl", 2: "odl"}, narrate=(2,)))
    wire["set_md"](["# p1", "# 순서도 라벨들"])
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    t2 = _texts(res.pages, 2)
    assert "순서도 라벨들" in t2, "native 텍스트 유지"
    assert "VL 전사" in t2, "서술 덧붙음"


def test_diagram_supplement_uses_diagram_prompt(wire, monkeypatch):
    """서술은 DIAGRAM 프롬프트로 호출된다 — 배선이 뒤집혀 전사 프롬프트로 회귀하면 실패."""
    from parse_service.parsers.ocr import prompts
    seen = {}

    def fake_many(jobs):
        seen.setdefault("override", jobs[0][2])
        return [([{"category": "text", "content": {"markdown": "START→END"}, "page": 0}], [])
                for _ in jobs]

    import parse_service.parsers.ocr as ocr_mod
    monkeypatch.setattr(ocr_mod, "ocr_elements_many_sync", fake_many)
    # 실제 배선을 태운다(fake 배치 seam 을 걷어낸다)
    monkeypatch.undo()
    monkeypatch.setattr(pdf_parser, "_safe_decide_route",
                        lambda fb: _decision({1: "odl"}, narrate=(1,)))
    monkeypatch.setattr(pdf_parser, "_page_markdowns", lambda fb, fn: ["# 순서도"])
    monkeypatch.setattr(pdf_parser, "_render_pages",
                        lambda fb, page_numbers=None, **k: [_RP(1)])
    monkeypatch.setattr(ocr_mod, "ocr_elements_many_sync", fake_many)
    pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert seen["override"] == (prompts.DIAGRAM_SYSTEM_PROMPT, prompts.DIAGRAM_USER_PROMPT)


def test_diagram_supplement_failure_is_nonfatal(wire, monkeypatch):
    """서술 VL 이 실패해도 기존 블록은 유지된다."""
    def boom(*a, **k):
        raise RuntimeError("VL down")
    monkeypatch.setattr(pdf_parser, "_ocr_elements_for_pages", boom)
    wire["set_gate"](_decision({1: "odl"}, narrate=(1,)))
    wire["set_md"](["# 순서도 원문"])
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert "순서도 원문" in _texts(res.pages, 1)


# ── 출구 ────────────────────────────────────────────────────────────────────
def test_parse_filters_degenerate_vl_blocks(wire, monkeypatch):
    """parse() 출구에서 퇴화 블록 필터가 돈다(배선 앵커)."""
    called = {}
    import parse_service.parsers.degen_filter as df
    monkeypatch.setattr(df, "filter_degenerate_pages",
                        lambda pages: called.setdefault("n", len(pages)) or 0)
    wire["set_gate"](_decision({1: "odl"}))
    wire["set_md"](["# p1"])
    pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert called["n"] == 1


# ── VL 전사 경로의 절단 방어 (2026-08-04 실측 회귀) ──────────────────────────
def test_transcribe_passes_page_max_tokens(wire, monkeypatch):
    """전사도 hybrid 와 같은 max_tokens 상한을 쓴다.

    기본값 2000 이면 목차·조밀한 본문이 절단된다(arXiv 논문 p5·p6: 4001·2526자 → 응답 잘림 →
    파싱 실패 → **빈 페이지**). 실측으로 발견한 회귀다.
    """
    monkeypatch.setenv("KBP_VL_PAGE_MAX_TOKENS", "8000")
    seen = {}

    def fake_batch(jobs, ocr_url=None, **k):
        seen["max_tokens"] = k.get("max_tokens")
        return [([{"category": "text", "content": {"markdown": "전사"}, "page": 0}], [])
                for _ in jobs]

    monkeypatch.setattr(pdf_parser, "_ocr_elements_for_pages", fake_batch)
    wire["set_gate"](_decision({1: "odl"}))
    wire["set_md"](["   "])                       # thin → 전사 경로
    pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert seen["max_tokens"] == 8000


@pytest.mark.parametrize("raw", [
    '```json\n{\n  "elements": [\n    {\n      "category": "figure",\n      "content": {',
    '[Error: Failed to parse API response - x]',
])
def test_transcribe_rejects_truncated_response(wire, monkeypatch, raw):
    """절단·에러 플레이스홀더가 그대로 본문 블록이 되면 안 된다.

    `elements_parser` 는 파싱 실패 시 **잘린 raw JSON 을 그대로 담은** element 1개를 만든다 —
    예외도 빈 결과도 아니라서 그냥 두면 문서에 실린다. hybrid 경로와 같은 판정을 적용한다.
    """
    monkeypatch.setattr(pdf_parser, "_ocr_elements_for_pages",
                        lambda jobs, ocr_url=None, **k: [
                            ([{"category": "text", "content": {"markdown": raw}, "page": 0}], [])
                            for _ in jobs])
    wire["set_gate"](_decision({1: "odl"}))
    wire["set_md"](["   "])
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    text = _texts(res.pages, 1)
    assert "```json" not in text and "[Error:" not in text, \
        f"잘린 raw JSON·에러 플레이스홀더가 본문이 되면 안 된다: {text!r}"
    assert "native-text-p1" in text, "네이티브 텍스트가 있으면 그것으로 폴백한다"


_BAD_VL = '```json\n{\n  "elements": [\n    {\n      "content": {'


def test_transcribe_failure_falls_back_to_native_text(wire, monkeypatch):
    """VL 전사 실패 페이지는 **네이티브 텍스트로 폴백**한다(빈 페이지 금지).

    실패 원인은 절단이 아니라 모델측 퇴화다(2026-08-04 실측: arXiv p5 목차가 leader dot
    반복 루프에 빠져 `finish_reason="stop"`, `completion_tokens=226`/상한 8000).
    **재시도는 무효**였다 — 회복률 0%(5/5). `temperature=0.1` 이라 같은 이미지는 같은 실패를
    반복한다. 이 경로는 정의상 네이티브 텍스트가 있는 odl 레인이므로 그것을 쓴다.
    """
    calls = []

    def fake_batch(jobs, ocr_url=None, **k):
        calls.append(1)
        return [([{"category": "text", "content": {"markdown": _BAD_VL}, "page": 0}], [])
                for _ in jobs]

    monkeypatch.setattr(pdf_parser, "_ocr_elements_for_pages", fake_batch)
    wire["set_gate"](_decision({1: "odl"}))
    wire["set_md"](["   "])
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")

    assert len(calls) == 1, "재시도하지 않는다(temperature 0.1 — 같은 실패 반복)"
    text = _texts(res.pages, 1)
    assert "native-text-p1" in text, f"네이티브 텍스트로 폴백해야 한다: {text!r}"
    assert "```json" not in text, "잘린 raw JSON 이 본문이 되면 안 된다"


def test_native_fallback_survives_degen_filter_on_toc(wire, monkeypatch):
    """목차 leader dot 을 접어 `degen_filter` 오탐을 피한다.

    실측(2026-08-04, arXiv p5): 네이티브 폴백이 4002자로 정상 발동했는데도 최종 blocks 가
    비었다. `degen_filter` 5-gram 지배 규칙이 점선을 반복 구절로 보고 페이지를 통째로 지웠다.

    **2026-08-12 재통합**: 방어 지점이 위로 옮겨졌다. scan-lane 은 네이티브 폴백 경로에서
    `_strip_leader_dots` 로 접었는데, Phase 1 이 `degen_filter.normalize_for_measure` 를
    **판정 입구**에 넣어 경로와 무관하게 접는다(상위 호환). 그래서 아래 픽스처는 더 이상
    degen 판정을 받지 않는다 — 옛 전제 단언(`assert is_degenerate_text(toc)`)은 제거했다.
    지키는 동작(목차 본문이 살아남는다)은 그대로이므로 앵커는 유지한다.
    """
    from parse_service.parsers.degen_filter import is_degenerate_text

    toc = "\n".join(f"{i}\tChapter {i} Title " + ". " * 40 + f"\t{i * 3}" for i in range(1, 20))
    assert not is_degenerate_text(toc), \
        "Phase 1 normalize_for_measure 가 leader dot 을 접어 오탐을 없앤다(회귀 앵커)"

    monkeypatch.setattr(pdf_parser, "_ocr_elements_for_pages",
                        lambda jobs, ocr_url=None, **k: [
                            ([{"category": "text", "content": {"markdown": _BAD_VL}, "page": 0}], [])
                            for _ in jobs])
    monkeypatch.setattr(pdf_parser, "_render_pages",
                        lambda fb, page_numbers=None, **k: [_RP(1, text=toc)])
    wire["set_gate"](_decision({1: "odl"}))
    wire["set_md"](["   "])
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")

    text = _texts(res.pages, 1)
    assert "Chapter 7 Title" in text, f"목차 본문이 살아남아야 한다: {text[:120]!r}"


def test_transcribe_failure_without_native_text_yields_empty(wire, monkeypatch):
    """네이티브 텍스트마저 없으면 빈 결과 — 잘린 raw JSON 을 싣지 않는다."""
    monkeypatch.setattr(pdf_parser, "_ocr_elements_for_pages",
                        lambda jobs, ocr_url=None, **k: [
                            ([{"category": "text", "content": {"markdown": _BAD_VL}, "page": 0}], [])
                            for _ in jobs])
    monkeypatch.setattr(pdf_parser, "_render_pages",
                        lambda fb, page_numbers=None, **k: [_RP(1, text="")])
    wire["set_gate"](_decision({1: "odl"}))
    wire["set_md"](["   "])
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert _texts(res.pages, 1) == "", "네이티브 텍스트도 없으면 빈 결과여야 한다"


def test_odl_process_failure_falls_back_to_vl(wire, monkeypatch):
    """ODL 이 **어떤 예외로든** 실패하면 VL 폴백이다(ToolError 만 잡으면 안 된다).

    실측(2026-08-04): 자바 없는 환경에서 `_odl_convert` 가 `subprocess.CalledProcessError` 를
    그대로 올려 `except ToolError` 를 빠져나갔고, 10개 문서가 전부 파싱 실패했다.
    """
    import subprocess

    def boom(fb, fn):
        raise subprocess.CalledProcessError(1, ["java", "-jar", "odl.jar"])

    monkeypatch.setattr(pdf_parser, "_page_markdowns", boom)
    wire["set_gate"](_decision({1: "odl", 2: "odl"}))
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert len(res.pages) == 2, "ODL 실패해도 페이지가 사라지면 안 된다"
    assert "VL 전사" in _texts(res.pages, 1)


# ══════════════════════════════════════════════════════════════════════════════
# v1 GW quarantine 게이트 앵커 — HEAD(`65e2adc`)에서 이식 (2026-08-12 Phase 2a 재통합)
#
# `_gw()` 헬퍼를 **페이지수준 계약으로 재작성**했다. 옛 판은
#   RouteDecision(lane="paddle_gw", diagram_pages=...)   ← page_lanes/total_pages 없음
#   lambda fb, fn                                        ← page_numbers 인자 없음
# 이라 새 `_parse_routed` 에서 (a) 레인이 형성되지 않고 (b) 스텁이 `TypeError` 를 내며
# 그것을 `:417 except Exception` 이 삼켜 **전 페이지 강등**으로 샌다 — 테스트가 조용히
# 다른 것을 검증하게 된다.
#
# ⚠️ HEAD `:301 test_gw_lane_engine_error_is_not_quarantine` 은 **이식하지 않았다.**
#    `page_verdicts[1]["verdict"] == "engine_error"` 를 단언하는데, `status=="error"` 는
#    이제 demote 되어 `gate_pnos` 에서 빠지므로 판정 자체가 만들어지지 않는다(§4a 사용자 결정).
#    대체 앵커는 아래 `test_gw_lane_engine_error_is_demoted_not_judged`.
# ══════════════════════════════════════════════════════════════════════════════
_DEGEN = "기계음 손상완을 잡고 " * 60

_HEALTHY = ("원고는 피고와 2021년 3월 체결한 분양계약에 따라 계약금 일억원을 지급하였다. "
            "그런데 피고는 준공예정일을 도과하고도 소유권이전등기 절차를 이행하지 아니하였다. "
            "이에 원고는 계약해제 의사를 표시하고 기지급금 반환을 구하는 바이다.")


def _psig(page_number, bucket, **kw):
    from parse_service.parsers.pdf.triage import PageSignals
    s = PageSignals(page_number=page_number, width=600, height=800)
    s.bucket = bucket
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def _gw(monkeypatch, pages, *, diagram_pages=(), ink=0.30):
    """paddle_gw 레인 전 페이지 배선 — **페이지수준 RouteDecision** 으로 준다."""
    pnos = [p["page_number"] for p in pages]
    monkeypatch.setattr(
        pdf_parser, "_safe_decide_route",
        lambda b: RouteDecision(lane="paddle_gw",
                                page_lanes=tuple((n, "paddle_gw") for n in pnos),
                                total_pages=len(pnos),
                                narrate_pages=(),
                                diagram_pages=tuple(diagram_pages)))
    # 스텁도 `page_numbers=` 를 받아야 한다 — 안 받으면 TypeError 가 삼켜져 전 페이지 강등.
    monkeypatch.setattr(pg, "run_paddle_gateway",
                        lambda fb, fn, page_numbers=None: pages)
    monkeypatch.setattr(pdf_parser, "_supplement_diagram_pages", lambda *a, **kw: None)
    import parse_service.parsers.pdf.page_verdict as pv
    monkeypatch.setattr(pv, "page_ink", lambda fb, p: ink)
    return pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")


def test_gw_lane_quarantines_collapsed_page(monkeypatch):
    """paddle_gw: HARD 퇴화로 대부분이 사라진 페이지는 **페이지 통째 quarantine**.

    blocks 가 비어 색인에서 실제로 빠지고(★4), 사유가 page_verdicts 에 남는다.
    """
    res = _gw(monkeypatch, [
        {"page_number": 1, "status": "ok", "blocks": [
            {"type": "text", "text": "정상 본문 텍스트입니다.", "page_idx": 1},
            {"type": "text", "text": _DEGEN, "page_idx": 1},
        ]},
    ])
    assert res.pages[0]["blocks"] == [], "quarantine 페이지는 blocks 가 비어야 한다"
    v = res.page_verdicts[0]
    assert v["verdict"] == "quarantine" and v["state"] == "quarantined_failure"
    assert v["reason"].startswith("퇴화 붕괴")
    assert v["signals"]["hard_rules"] and v["signals"]["chars_after"] < v["signals"]["chars_before"]


def test_gw_lane_keeps_healthy_page(monkeypatch):
    """정상 페이지는 ACCEPT_GW + blocks 보존(PageState.OK 앵커)."""
    res = _gw(monkeypatch, [
        {"page_number": 1, "status": "ok", "blocks": [
            {"type": "text", "text": _HEALTHY, "page_idx": 1},
        ]},
    ])
    assert res.pages[0]["blocks"], "정상 페이지 blocks 보존"
    assert res.page_verdicts[0]["verdict"] == "accept_gw"
    assert res.page_verdicts[0]["state"] == "ok"

def test_gw_lane_blank_page_is_skipped_not_quarantined(monkeypatch):
    """저잉크 빈 페이지는 EMPTY_SKIPPED — **blocks 를 보존**하고 실패로 세지 않는다."""
    res = _gw(monkeypatch, [
        {"page_number": 1, "status": "ok",
         "blocks": [{"type": "text", "text": "표지", "page_idx": 1}]},
    ], ink=0.008)
    v = res.page_verdicts[0]
    assert v["state"] == "empty_skipped" and v["verdict"] == "accept_gw"
    assert res.pages[0]["blocks"], "EMPTY_SKIPPED 는 색인에서 빼지 않는다"


def test_gw_lane_empty_with_ink_is_quarantined(monkeypatch):
    """잉크는 있는데 텍스트가 거의 없으면 OCR 실패 → quarantine."""
    res = _gw(monkeypatch, [
        {"page_number": 1, "status": "ok",
         "blocks": [{"type": "text", "text": "표지", "page_idx": 1}]},
    ])
    assert res.page_verdicts[0]["state"] == "quarantined_failure"
    assert res.pages[0]["blocks"] == []


def test_gw_lane_ink_unknown_preserves_page(monkeypatch):
    """ink 측정 실패(None)면 EMPTY 를 hard fail 로 보지 않는다 — 보존 우선."""
    res = _gw(monkeypatch, [
        {"page_number": 1, "status": "ok",
         "blocks": [{"type": "text", "text": "표지", "page_idx": 1}]},
    ], ink=None)
    assert res.page_verdicts[0]["state"] == "ok"
    assert res.pages[0]["blocks"], "판정 불가면 보존"


def test_gw_lane_diagram_page_short_vl_is_accepted(monkeypatch):
    """diagram 페이지는 행 4 **대신** 4'(chars_after == 0) 를 적용한다.

    VL 이 성공했지만 간결하게(40자) 응답한 도면 페이지가 EMPTY 로 격리되면 안 된다.
    """
    res = _gw(monkeypatch, [
        {"page_number": 1, "status": "ok", "blocks": [
            {"type": "text", "text": "START→검토→승인→END 순서로 진행한다", "page_idx": 1}]},
    ], diagram_pages=(1,))
    assert res.page_verdicts[0]["state"] == "ok", "diagram + 짧은 VL 서술은 통과"
    assert res.pages[0]["blocks"]


def test_gw_lane_diagram_page_zero_chars_is_quarantined(monkeypatch):
    """diagram 페이지라도 VL 이 0자면 quarantine."""
    res = _gw(monkeypatch, [
        {"page_number": 1, "status": "ok", "blocks": []},
        {"page_number": 2, "status": "ok",
         "blocks": [{"type": "text", "text": _HEALTHY, "page_idx": 2}]},
    ], diagram_pages=(1,))
    assert res.page_verdicts[0]["state"] == "quarantined_failure"
    assert res.page_verdicts[1]["state"] == "ok"


def test_gw_gate_off_switch(monkeypatch):
    """KBP_GW_GATE=0 이면 전 페이지 ACCEPT_GW 이고 blocks 를 건드리지 않는다."""
    monkeypatch.setenv("KBP_GW_GATE", "0")
    res = _gw(monkeypatch, [
        {"page_number": 1, "status": "ok", "blocks": [
            {"type": "text", "text": "정상 본문 텍스트입니다.", "page_idx": 1},
            {"type": "text", "text": _DEGEN, "page_idx": 1},
        ]},
    ])
    assert all(v["verdict"] == "accept_gw" for v in res.page_verdicts)
    # 게이트는 껐지만 parse() 출구의 전역 degen 필터는 그대로 — 퇴화 블록만 빠진다.
    texts = [b.get("text", "") for b in res.pages[0]["blocks"]]
    assert any("정상 본문" in t for t in texts)


def test_gw_lane_never_returns_escalate_vl(monkeypatch):
    """v1 정책 앵커 — ESCALATE_VL 은 contract 로만 존재하고 발화하지 않는다."""
    res = _gw(monkeypatch, [
        {"page_number": 1, "status": "ok", "blocks": [
            {"type": "text", "text": _HEALTHY, "page_idx": 1}]},
        {"page_number": 2, "status": "error", "error": "boom", "blocks": []},
        {"page_number": 3, "status": "ok", "blocks": [
            {"type": "text", "text": _DEGEN, "page_idx": 1}]},
    ])
    assert all(v["verdict"] != "escalate_vl" for v in res.page_verdicts)


# ── 페이지별 판정 로그(2026-08-06 도입, 2026-08-12 페이지수준화) ────────────────

def test_triage_log_table_appears_with_page_signals(monkeypatch, caplog):
    """page_signals 가 채워진 decision 이면 헤더+행이 로그에 남는다."""
    sigs = (_psig(1, Bucket.TEXT_ONLY), _psig(2, Bucket.TEXT_ONLY))
    monkeypatch.setattr(pdf_parser, "_safe_decide_route",
                        lambda b: RouteDecision(lane="odl", page_signals=sigs))
    monkeypatch.setattr(pdf_parser, "_page_markdowns", lambda fb, fn: ["# p1", "# p2"])
    with caplog.at_level(logging.INFO, logger=pdf_parser.log.name):
        pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    msgs = [r.message for r in caplog.records]
    assert any("판정근거" in m for m in msgs), "헤더 행 출현"
    assert any(m.startswith("| 1 |") for m in msgs), "페이지 1 행 출현"
    assert any(m.startswith("| 2 |") for m in msgs), "페이지 2 행 출현"


def test_triage_log_table_toggle_off(monkeypatch, caplog):
    """KBP_TRIAGE_LOG_TABLE=0 이면 완전히 스킵."""
    monkeypatch.setenv("KBP_TRIAGE_LOG_TABLE", "0")
    sigs = (_psig(1, Bucket.TEXT_ONLY),)
    monkeypatch.setattr(pdf_parser, "_safe_decide_route",
                        lambda b: RouteDecision(lane="odl", page_signals=sigs))
    monkeypatch.setattr(pdf_parser, "_page_markdowns", lambda fb, fn: ["# p1"])
    with caplog.at_level(logging.INFO, logger=pdf_parser.log.name):
        pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert not any("판정근거" in r.message for r in caplog.records)


def test_triage_log_handles_decision_none(monkeypatch, caplog):
    """decision=None(게이트 import 실패/decide_route 예외) — AttributeError 없이 짧은
    안내 로그만 남기고 정상 반환한다(§C 가드 앵커)."""
    monkeypatch.setattr(pdf_parser, "_page_markdowns", lambda fb, fn: ["# 텍스트"])
    import parse_service.parsers.pdf.gate as gate
    monkeypatch.setattr(gate, "decide_route",
                        lambda b: (_ for _ in ()).throw(RuntimeError("boom")))
    with caplog.at_level(logging.INFO, logger=pdf_parser.log.name):
        res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert res.kind == "pages"  # 예외 없이 정상 완료
    assert any("decision=None" in r.message for r in caplog.records)


# ── figure + html 전소 방지 (앵커 14) ────────────────────────────────────────
def test_vl_lane_keeps_figure_html_table(wire, monkeypatch):
    """VL 이 표를 `category="figure"` + `content.html` 로 내도 **표가 살아남는다**.

    실측 결함(2026-08-12): `elements_to_blocks` 를 직접 부르면 이 형태가
    **img_path 빈 image 블록**이 되어 `<table>` HTML 이 통째로 사라진다.
    `ocr/__init__.py` 의 figure→text 재라벨은 html 이 **빌 때만** 발동해 구제하지 못한다.
    표가 많은 슬라이드가 정확히 이 형태다(사실 #25) — vl 레인이 노리는 바로 그 대상.
    같은 element 의 산문도 함께 살아야 한다.
    """
    monkeypatch.setattr(pdf_parser, "_ocr_elements_for_pages",
                        lambda jobs, ocr_url=None, **k: [([{
                            "category": "figure", "page": 0,
                            "content": {"html": "<table><tr><td>셀A</td><td>셀B</td></tr></table>",
                                        "markdown": "표 아래 설명 산문이다."},
                        }], []) for _ in jobs])
    wire["set_gate"](_decision({1: "vl"}))
    wire["set_md"]([""])                       # md 없음 → VL 산출물이 정본
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")

    blocks = res.pages[0]["blocks"]
    tbl = [b for b in blocks if b.get("type") == "table"]
    assert tbl, f"figure+html 표가 전소됐다: {[b.get('type') for b in blocks]}"
    assert "셀A" in tbl[0]["table_body"], "표 HTML 보존"
    assert any("산문" in (b.get("text") or "") for b in blocks), "같은 element 의 산문도 보존"
    assert not any(b.get("type") == "image" and not b.get("img_path") for b in blocks), \
        "img_path 빈 image 블록이 남으면 안 된다"
