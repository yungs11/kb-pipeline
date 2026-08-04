"""PDF **페이지수준 혼합 라우팅**(Plan B-5) — 페이지마다 레인을 고르고 병합한다.

SKIP→skip / OCR_NEEDED→paddle_gw / TEXT_ONLY·LLM_NEEDED→odl.
문서수준 `vl` 레인은 삭제됐다(그림 비율만 보고 문서 전체를 VL 로 넘겨 표를 깨뜨리던 경로).
"""
import pytest

import parse_service.parsers.pdf as pdf_parser
import parse_service.parsers.pdf.paddle_gw as pg
from parse_service.parsers import RouteResult
from parse_service.parsers.pdf.gate import RouteDecision


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
            return [[{"category": "text", "content": {"markdown": text}, "page": 0}]
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
        {"page_number": 2, "blocks": [], "layout": [], "page_size": None},
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
        return [[{"category": "text", "content": {"markdown": "START→END"}, "page": 0}]
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
        return [[{"category": "text", "content": {"markdown": "전사"}, "page": 0}]
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
                            [{"category": "text", "content": {"markdown": raw}, "page": 0}]
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
        return [[{"category": "text", "content": {"markdown": _BAD_VL}, "page": 0}]
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
    """
    from parse_service.parsers.degen_filter import is_degenerate_text

    toc = "\n".join(f"{i}\tChapter {i} Title " + ". " * 40 + f"\t{i * 3}" for i in range(1, 20))
    assert is_degenerate_text(toc), "픽스처가 degen 판정을 받아야 앵커가 의미를 갖는다"

    monkeypatch.setattr(pdf_parser, "_ocr_elements_for_pages",
                        lambda jobs, ocr_url=None, **k: [
                            [{"category": "text", "content": {"markdown": _BAD_VL}, "page": 0}]
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
                            [{"category": "text", "content": {"markdown": _BAD_VL}, "page": 0}]
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
