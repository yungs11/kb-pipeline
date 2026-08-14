"""PDF **페이지수준 혼합 라우팅**(Plan B-5) — 페이지마다 레인을 고르고 병합한다.

SKIP→skip / OCR_NEEDED→paddle_gw / TEXT_ONLY·LLM_NEEDED→odl.
문서수준 `vl` 레인은 삭제됐다(그림 비율만 보고 문서 전체를 VL 로 넘겨 표를 깨뜨리던 경로).
"""
import json
import logging

import pytest

import parse_service.parsers.pdf as pdf_parser
import parse_service.parsers.pdf.paddle_gw as pg
from parse_service.parsers import ParserError, RouteResult
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
def chain_off(monkeypatch):
    """VL 폴백 체인을 **끈다** — 2b-2 정책(폴백 없음 → 문서 실패) 앵커용.

    2026-08-14 부터 `KBP_VL_FALLBACK_CHAIN` 기본값이 **ON** 이다(사용자 확정). 그래서
    "VL 이 실패하면 문서가 죽는다" 를 지키는 테스트들은 **명시적으로 꺼야** 그 분기를
    검사한다. 켠 상태의 동작은 같은 시나리오의 `_chain_on` 쌍둥이가 따로 지킨다.

    ⚠️ 이 fixture 를 붙였다고 정책이 약해진 게 아니다 — OFF 경로는 폐쇄망 탈출구이자
    "품질을 낮추지 않는다" 는 보장이므로 **계속 검증돼야 한다**.
    """
    monkeypatch.setenv("KBP_VL_FALLBACK_CHAIN", "0")


@pytest.fixture
def wire(monkeypatch):
    """게이트/ODL/렌더/VL/게이트웨이를 fake 로 잡고 호출 기록을 준다."""
    rec = {"vl_jobs": [], "render": [], "gw_pages": None, "md": [],
           # 게이트웨이 **모든** 호출 기록 — step3 배치 + 폴백 체인의 step4b.
           # `set_gw` 를 안 부른 테스트도 폴백이 게이트웨이를 부를 수 있으므로
           # (KBP_VL_FALLBACK_CHAIN 기본 ON) 스텁을 **항상** 깐다. 안 깔면 개발기·CI 에
           # KBP_PADDLE_OCR_GATEWAY_URL 이 있을 때 실 HTTP 를 치고 60~600초 매달린다.
           "gw_calls": []}

    def set_gate(decision):
        monkeypatch.setattr(pdf_parser, "_safe_decide_route", lambda fb: decision)

    def set_md(md_list):
        rec["md"] = md_list
        monkeypatch.setattr(pdf_parser, "_page_markdowns", lambda fb, fn: list(md_list))

    # 기본 동작: 아무것도 못 냄(빈 목록). `set_gw` 가 이걸 갈아끼운다.
    rec["_gw_impl"] = lambda page_numbers: []

    def fake_gw(fb, fn, page_numbers=None):
        rec["gw_pages"] = page_numbers
        rec["gw_calls"].append(tuple(sorted(page_numbers)) if page_numbers else None)
        return rec["_gw_impl"](page_numbers)

    monkeypatch.setattr(pg, "run_paddle_gateway", fake_gw)

    def set_gw(pages_or_exc):
        def impl(_page_numbers):
            if isinstance(pages_or_exc, Exception):
                raise pages_or_exc
            return list(pages_or_exc)
        rec["_gw_impl"] = impl

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
def test_transcribe_rejects_truncated_response(wire, monkeypatch, raw, chain_off):
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
    # 2b-2: 폴백을 지웠으므로 **문서 실패**가 정답이다. 그래도 raw JSON 은 어디에도
    # 실리면 안 된다 — 탐지기(_looks_like_failed_vl)를 지우면 이게 본문이 된다.
    with pytest.raises(ParserError) as ei:
        pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    dumped = json.dumps(ei.value.traces, ensure_ascii=False)
    assert "```json" not in dumped and "[Error:" not in dumped, \
        f"잘린 raw JSON·에러 플레이스홀더가 어디에도 실리면 안 된다: {dumped[:200]!r}"
    assert ei.value.traces[0]["source"] == "empty" and ei.value.traces[0]["chars"] == 0


_BAD_VL = '```json\n{\n  "elements": [\n    {\n      "content": {'


def test_thin_page_vl_failure_fails_document(wire, monkeypatch, chain_off):
    """thin 페이지에서 VL 이 실패하면 **문서 파싱 실패**다 — 네이티브 폴백은 없다(2b-2).

    **정책 반전 기록**: 이 테스트는 원래 "네이티브 텍스트로 폴백한다" 를 지켰다.
    그 폴백이 바로 "122b 가 아픈데 결과물이 정상으로 보이는" 원인이었다 — PyMuPDF
    평문은 표·레이아웃이 다 날아갔는데 **자수는 멀쩡해 성공으로 보인다.**
    사용자 결정(2026-08-14): VL 실패는 폴백하지 말고 멈춘다.

    **재시도 범위 = 실패 범위**(2026-08-14 조정) — VL 을 부른 페이지면 레인과 무관하게
    재시도를 받는다. 절단이므로 총 3회.
    """
    calls = []

    def fake_batch(jobs, ocr_url=None, **k):
        calls.append(1)
        return [([{"category": "text", "content": {"markdown": _BAD_VL}, "page": 0}], [])
                for _ in jobs]

    monkeypatch.setattr(pdf_parser, "_ocr_elements_for_pages", fake_batch)
    wire["set_gate"](_decision({1: "odl"}))
    wire["set_md"](["   "])
    with pytest.raises(ParserError, match="vl_failed"):
        pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert len(calls) == 3, f"thin 도 재시도를 받는다(절단이므로 3회): {calls}"


def test_native_fallback_survives_degen_filter_on_toc(wire, monkeypatch, chain_off):
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
    # 2b-2: 네이티브 폴백이 사라졌으므로 이 페이지는 빈 채로 실패한다.
    # 이 테스트가 지키는 것은 **degen 오탐이 없다**는 사실(위 단언)이고, 그건 그대로다.
    with pytest.raises(ParserError, match="vl_failed"):
        pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")


def test_escape_hatch_keeps_empty_page_instead_of_failing(wire, monkeypatch):
    """`KBP_FAIL_ON_EMPTY_PAGE=0` 이면 실패시키지 않는다 — **탈출구**(폐쇄망 되돌리기).

    ⚠️ 끈다고 **이전 동작이 복원되지 않는다** — 폴백은 이미 삭제됐으므로
    끄면 "빈 본문이 조용히 적재" 된다. 그 대가를 알고 켜야 한다.
    가드는 읽는 코드가 있어야 가드다 — 선언만 하고 소비처가 없으면 grep 검증이
    0줄인 채로 통과해버린다(CLAUDE.md).
    """
    monkeypatch.setenv("KBP_FAIL_ON_EMPTY_PAGE", "0")
    monkeypatch.setattr(pdf_parser, "_ocr_elements_for_pages",
                        lambda jobs, ocr_url=None, **k: [
                            ([{"category": "text", "content": {"markdown": _BAD_VL}, "page": 0}], [])
                            for _ in jobs])
    monkeypatch.setattr(pdf_parser, "_render_pages",
                        lambda fb, page_numbers=None, **k: [_RP(1, text="")])
    wire["set_gate"](_decision({1: "odl"}))
    wire["set_md"](["   "])
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert _texts(res.pages, 1) == "", "잘린 raw JSON 은 여전히 본문이 되면 안 된다"
    assert res.page_traces[0]["source"] == "empty"


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


# ══════════════════════════════════════════════════════════════════════════════
# Phase 2b-1 — PageTrace 관측 앵커
#
# 이 phase 는 **기록만 추가**한다. 동작 무변경이 수용 기준이고, 그 증거는
#   ① 위 `page_verdicts` 단언 13곳이 **한 줄도 수정되지 않고** 통과하는 것(개명 안 함)
#   ② `_workspace/planA-measurements/baseline-2a/structure.json` 과의 구조 대조
# 다. 아래는 신설 계약을 못박는다.
# ══════════════════════════════════════════════════════════════════════════════
def test_page_traces_covers_every_page(wire):
    """`page_traces` 는 **전 페이지**를 담는다 — `page_verdicts`(부분집합)와 다르다."""
    wire["set_gate"](_decision({1: "odl", 2: "skip", 3: "odl"}))
    wire["set_md"](["# 본문 하나", "", "# 본문 셋"])
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert [t["page_number"] for t in res.page_traces] == [1, 2, 3]
    assert res.page_verdicts is None, "paddle 페이지가 없으면 게이트 판정은 없다"
    assert all(t["verdict"] is None for t in res.page_traces), \
        "게이트를 안 탄 페이지는 verdict=None"


def test_page_traces_source_per_branch(wire):
    """병합 분기 → `source` 매핑. 어휘가 뭉개지면 지표가 거짓이 된다."""
    wire["set_gate"](_decision({1: "odl", 2: "skip"}))
    wire["set_md"](["# 디지털 본문", ""])
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    src = {t["page_number"]: t["source"] for t in res.page_traces}
    assert src[1] == "odl_md"
    assert src[2] == "skip", "SKIP 은 정상적으로 비는 경로 — empty 와 구분해야 한다"


def test_page_traces_empty_overrides_source(wire, monkeypatch, chain_off):
    """VL 이 elements 를 냈는데 **전량 필터**되면 `source` 는 `vl` 이 아니라 `empty`.

    안 덮어쓰면 §2 의 "품질 상한 = `source==empty` 비율" 이 거짓이 된다 —
    2b-2 의 문서실패 대상집합이 그 수치 위에 선다.
    """
    monkeypatch.setattr(pdf_parser, "_ocr_elements_for_pages",
                        lambda jobs, ocr_url=None, **k: [
                            # img_path 빈 image element → vl_elements_to_blocks 가 전량 제거
                            ([{"category": "figure", "page": 0,
                               "content": {"html": "", "markdown": "", "text": ""}}], [])
                            for _ in jobs])
    wire["set_gate"](_decision({1: "vl"}))
    wire["set_md"]([""])
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    t = res.page_traces[0]
    assert t["source"] == "empty", f"blocks 가 비면 empty 로 덮어써야 한다: {t}"
    assert t["chars"] == 0


def test_page_traces_attempts_carry_tokens_and_finish(wire, monkeypatch):
    """`attempts` 에 `tokens`·`finish` 가 **page_traces 까지** 도달한다.

    `finish` 가 `length`(상한 소진)냐 `stop`+짧은 응답(서빙 이상)이냐가 **처방을 가른다**
    (2026-08-13 V0 실측). 값이 안 오면 "8000 토큰인데 왜 잘려?" 에서 막힌다.
    """
    from parse_service.parsers.ocr import vl_api
    meta = vl_api.VLCallMeta(model="q/122b", tokens=86, finish="stop", elapsed=1.0)
    monkeypatch.setattr(pdf_parser, "_ocr_elements_for_pages",
                        lambda jobs, ocr_url=None, **k: [
                            ([{"category": "text", "content": {"markdown": "VL 전사"},
                               "page": 0}], [meta]) for _ in jobs])
    wire["set_gate"](_decision({1: "vl"}))
    wire["set_md"]([""])
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    atts = res.page_traces[0]["attempts"]
    assert atts and atts[0][0] == "vl"
    assert atts[0][2]["tokens"] == 86 and atts[0][2]["finish"] == "stop"


def test_page_traces_vl_error_distinguished_from_empty(wire, monkeypatch, chain_off):
    """VL **예외**와 **빈 응답**이 구분된다(삼킴 3층 해소).

    예외가 빈 리스트로만 도착하면 "서빙이 아프다" 와 "이 페이지는 원래 빈다" 가
    같은 신호가 된다 — 이 phase 가 대비하려는 바로 그 혼동이다.
    """
    def boom(*a, **k):
        raise RuntimeError("VL down")
    monkeypatch.setattr(pdf_parser, "_ocr_elements_for_pages", boom)
    wire["set_gate"](_decision({1: "vl"}))
    wire["set_md"]([""])
    # 2b-2: vl 레인 실패는 문서 실패다. 그래도 **관측은 살아 있어야** 한다 —
    # 실패한 문서에서만 trace 가 0 이 되면 "실패를 드러낸다" 는 목적이 무효가 된다.
    with pytest.raises(ParserError) as ei:
        pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    atts = ei.value.traces[0]["attempts"]
    assert any(a[1] == "error" and "RuntimeError" in str(a[2].get("error", ""))
               for a in atts), f"배치 전체 실패 사유가 남아야 한다: {atts}"
    assert sum(1 for a in atts if a[0] == "vl") == 2, \
        f"vl 레인은 총 2회 호출한다(최초 1 + 재시도 1): {atts}"


def test_page_traces_gw_hybrid_distinguished_from_gw(wire, monkeypatch):
    """hybrid 가 갈아끼운 페이지는 `gw` 가 아니라 `gw_hybrid` 다.

    내용이 게이트웨이 산출물이 아니라 **전면 VL** 산출물이라 구분하지 않으면
    "게이트웨이가 처리했다" 로 뭉뚱그려진다.
    """
    monkeypatch.setattr(pdf_parser, "_hybrid_scan_pages",
                        lambda pages, fb, tgt, ocr_url, counters, **kw: {1})
    wire["set_gate"](_decision({1: "paddle_gw"}))
    wire["set_gw"]([{"page_number": 1, "blocks": [{"type": "text", "text": "GW 본문"}],
                     "layout": [], "page_size": None, "status": "ok", "error": ""}])
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert res.page_traces[0]["source"] == "gw_hybrid"


def test_truncation_gets_one_extra_retry(wire, monkeypatch, chain_off):
    """**절단만** 재시도를 한 번 더 받는다(기본 2 → 절단 3).

    절단은 모델 퇴화가 아니라 **프로바이더 사정**일 수 있어 재호출에서 다른
    프로바이더에 걸리면 살아난다 — 2026-08-14 실측: 정의서 p9·p12 가 2회 다
    절단됐는데, 같은 문서가 다른 실행에선 15/15 성공했다(프로바이더 복권).
    전송오류·빈응답은 그 성질이 아니므로 기본 횟수 그대로다.
    """
    calls = []

    def truncating(jobs, ocr_url=None, **k):
        calls.append(len(jobs))
        return [([{"category": "text", "content": {"markdown": _BAD_VL}, "page": 0}], [])
                for _ in jobs]

    monkeypatch.setattr(pdf_parser, "_ocr_elements_for_pages", truncating)
    wire["set_gate"](_decision({1: "vl"}))
    wire["set_md"]([""])
    with pytest.raises(ParserError, match="vl_failed"):
        pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert len(calls) == 3, f"절단은 총 3회(기본 2 + 1): {calls}"


def test_empty_response_does_not_get_the_extra_retry(wire, monkeypatch, chain_off):
    """빈 응답(B형)은 추가 1회를 **안 받는다** — 절단과 성질이 다르다."""
    calls = []

    def empty(jobs, ocr_url=None, **k):
        calls.append(len(jobs))
        return [([], []) for _ in jobs]

    monkeypatch.setattr(pdf_parser, "_ocr_elements_for_pages", empty)
    wire["set_gate"](_decision({1: "vl"}))
    wire["set_md"]([""])
    with pytest.raises(ParserError, match="vl_failed"):
        pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert len(calls) == 2, f"빈 응답은 기본 2회: {calls}"


def test_attempts_env_scales_both_limits(wire, monkeypatch, chain_off):
    """`KBP_VL_PAGE_ATTEMPTS` 를 올리면 절단 한도도 **파생해서** 따라 오른다(+1)."""
    monkeypatch.setenv("KBP_VL_PAGE_ATTEMPTS", "1")
    calls = []
    monkeypatch.setattr(pdf_parser, "_ocr_elements_for_pages",
                        lambda jobs, ocr_url=None, **k: calls.append(len(jobs)) or [
                            ([{"category": "text", "content": {"markdown": _BAD_VL}, "page": 0}], [])
                            for _ in jobs])
    wire["set_gate"](_decision({1: "vl"}))
    wire["set_md"]([""])
    with pytest.raises(ParserError, match="vl_failed"):
        pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert len(calls) == 2, f"기본 1 → 절단 2: {calls}"


def test_demoted_paddle_page_also_gets_retries(wire, monkeypatch, chain_off):
    """**강등 paddle 페이지도 재시도를 받는다** — 실패 범위와 재시도 범위를 맞춘다.

    강등은 게이트웨이가 죽어 VL 로 내려온 스캔 페이지다. 즉 **이미 한 번 사고를 겪은
    페이지**인데, 예전에는 재시도 대상이 `vl_pnos` 뿐이라 여기서 VL 이 한 번만 불렸다 —
    인프라 두 곳이 동시에 삐끗하면 문서가 통째로 죽었다. **오탐이 가장 나기 쉬운 자리에
    기회가 가장 적은** 배분이었다(2026-08-14 조정).

    절단 회복률 80% 실측(정의서 15p)이 그 기회의 값을 보여준다.
    """
    calls = []
    monkeypatch.setattr(pdf_parser, "_ocr_elements_for_pages",
                        lambda jobs, ocr_url=None, **k: calls.append(len(jobs)) or [
                            ([{"category": "text", "content": {"markdown": _BAD_VL}, "page": 0}], [])
                            for _ in jobs])
    wire["set_gate"](_decision({1: "paddle_gw"}))
    # status=="error" 가 demote 조건이다(§4a).
    wire["set_gw"]([{"page_number": 1, "blocks": [], "layout": [], "page_size": None,
                     "status": "error", "error": "TimeoutError: poll"}])
    with pytest.raises(ParserError, match="vl_failed"):
        pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert len(calls) == 3, f"강등 페이지도 절단이면 3회 받는다: {calls}"


# ── 폴백 체인 (KBP_VL_FALLBACK_CHAIN, 기본 ON) ────────────────────────────────
# 위 `chain_off` 를 단 테스트들이 OFF 경로(2b-2 정책)를 지킨다. 아래는 **기본값 ON** 의
# 동작 — 체인 순서·기시도 skip·소진 시 여전히 실패하는지를 지킨다.

def test_chain_on_thin_page_falls_back_through_pw_then_native(wire, monkeypatch):
    """thin(트리아지가 놓친 스캔) 페이지: VL 실패 → **pw 시도** → 실패 → native.

    `thin` 은 odl 레인 출신이라 게이트웨이를 **한 번도 안 거쳤다** — 체인의 pw 단계가
    새로 커버하는 집단이다. 여기서는 게이트웨이가 빈손이라 native 로 이어진다.
    """
    monkeypatch.setattr(pdf_parser, "_ocr_elements_for_pages",
                        lambda jobs, ocr_url=None, **k: [
                            ([{"category": "text", "content": {"markdown": _BAD_VL}, "page": 0}], [])
                            for _ in jobs])
    wire["set_gate"](_decision({1: "odl"}))
    wire["set_md"](["   "])
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")   # 실패하지 않는다
    assert wire["gw_calls"] == [(1,)], f"thin 은 pw 를 시도해야 한다: {wire['gw_calls']}"
    assert res.page_traces[0]["source"] == "native_fallback"
    assert "native-text-p1" in _texts(res.pages, 1)


def test_chain_on_pw_fallback_wins_over_native(wire, monkeypatch):
    """게이트웨이가 내용을 내면 native 보다 **먼저** 채택된다(체인 순서)."""
    monkeypatch.setattr(pdf_parser, "_ocr_elements_for_pages",
                        lambda jobs, ocr_url=None, **k: [
                            ([{"category": "text", "content": {"markdown": _BAD_VL}, "page": 0}], [])
                            for _ in jobs])
    wire["set_gate"](_decision({1: "odl"}))
    wire["set_md"](["   "])
    wire["set_gw"]([{"page_number": 1, "blocks": [{"type": "text", "text": "GW 폴백 본문"}],
                     "layout": [], "page_size": None}])
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert res.page_traces[0]["source"] == "gw_fallback"
    assert "GW 폴백 본문" in _texts(res.pages, 1)


def test_chain_on_demoted_page_skips_pw(wire, monkeypatch):
    """강등(paddle_gw 출신) 페이지는 pw 를 **이미 거쳤으므로 건너뛴다**(사용자 확정 skip 규칙)."""
    monkeypatch.setattr(pdf_parser, "_ocr_elements_for_pages",
                        lambda jobs, ocr_url=None, **k: [
                            ([{"category": "text", "content": {"markdown": _BAD_VL}, "page": 0}], [])
                            for _ in jobs])
    wire["set_gate"](_decision({1: "paddle_gw"}))
    wire["set_gw"]([{"page_number": 1, "blocks": [], "layout": [], "page_size": None,
                     "status": "error", "error": "timeout"}])
    pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert wire["gw_calls"] == [(1,)], \
        f"step3 배치 1회뿐 — 폴백에서 다시 부르면 안 된다: {wire['gw_calls']}"


def test_chain_on_exhausted_still_fails_document(wire, monkeypatch):
    """**안전 불변식** — 체인을 다 소진하면 여전히 문서가 실패한다.

    pw 빈손 · md 공백 · rp.text 공백 → 채울 것이 없다. 폴백을 켰다고 실패가 사라지면
    안 된다(그러면 열화가 영영 안 드러난다).
    """
    monkeypatch.setattr(pdf_parser, "_ocr_elements_for_pages",
                        lambda jobs, ocr_url=None, **k: [
                            ([{"category": "text", "content": {"markdown": _BAD_VL}, "page": 0}], [])
                            for _ in jobs])
    monkeypatch.setattr(pdf_parser, "_render_pages",
                        lambda fb, page_numbers=None, **k: [_RP(1, text="   ")])
    wire["set_gate"](_decision({1: "odl"}))
    wire["set_md"](["   "])
    with pytest.raises(ParserError, match="vl_failed"):
        pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")


def test_chain_on_gateway_unavailable_is_not_fatal(wire, monkeypatch):
    """게이트웨이 URL 이 없어도(폐쇄망 parse-only 정상 구성) **문서 전체 500 이 아니다**.

    `run_paddle_gateway` 는 URL 미설정 시 RuntimeError 를 던진다. 안 잡으면 폴백을 켜는
    순간 폐쇄망만 죽는다 — 배포 게이트.
    """
    monkeypatch.setattr(pdf_parser, "_ocr_elements_for_pages",
                        lambda jobs, ocr_url=None, **k: [
                            ([{"category": "text", "content": {"markdown": _BAD_VL}, "page": 0}], [])
                            for _ in jobs])
    wire["set_gate"](_decision({1: "odl"}))
    wire["set_md"](["   "])
    wire["set_gw"](RuntimeError("KBP_PADDLE_OCR_GATEWAY_URL 미설정"))
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert res.page_traces[0]["source"] == "native_fallback", "체인이 다음 단계로 이어져야 한다"
    assert any(a[0] == "gw" and a[1] == "lane_unavailable"
               for a in res.page_traces[0]["attempts"])


def test_chain_on_gw_page_cap_limits_fallback(wire, monkeypatch):
    """페이지 상한 — 전 페이지가 VL 실패해도 게이트웨이에 무한정 보내지 않는다.

    최악(트리아지+ODL 동시 실패)에 전 페이지가 게이트웨이로 가면 페이지당 600초 poll 시한
    때문에 facade ReadTimeout 으로 문서가 죽는다.
    """
    monkeypatch.setenv("KBP_VL_FALLBACK_GW_MAX_PAGES", "2")
    monkeypatch.setattr(pdf_parser, "_ocr_elements_for_pages",
                        lambda jobs, ocr_url=None, **k: [
                            ([{"category": "text", "content": {"markdown": _BAD_VL}, "page": 0}], [])
                            for _ in jobs])
    wire["set_gate"](_decision({n: "odl" for n in (1, 2, 3, 4)}))
    wire["set_md"](["   "] * 4)
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert wire["gw_calls"] == [(1, 2)], f"앞 2장만 보낸다: {wire['gw_calls']}"
    over = [t for t in res.page_traces
            if any(a[0] == "gw" and a[1] == "over_cap" for a in t["attempts"])]
    assert {t["page_number"] for t in over} == {3, 4}


def test_chain_on_md_fallback_folds_leader_dots(wire, monkeypatch):
    """ODL md 폴백 경로도 leader dot 을 접는다 — 안 접으면 degen 이 목차를 통째로 지운다."""
    toc = "\n".join(f"{i}\tChapter {i} " + ". " * 40 + f"\t{i * 3}" for i in range(1, 20))
    monkeypatch.setattr(pdf_parser, "_ocr_elements_for_pages",
                        lambda jobs, ocr_url=None, **k: [
                            ([{"category": "text", "content": {"markdown": _BAD_VL}, "page": 0}], [])
                            for _ in jobs])
    monkeypatch.setattr(pdf_parser, "_render_pages",
                        lambda fb, page_numbers=None, **k: [_RP(1, text="   ")])
    wire["set_gate"](_decision({1: "vl"}))          # vl 레인 → odl 을 안 거쳤다
    wire["set_md"]([toc])
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert res.page_traces[0]["source"] == "vl_md_fallback"
    assert "Chapter 1" in _texts(res.pages, 1), "degen 이 지우면 안 된다"


def test_gw_stage_recorded_for_gateway_lane(wire):
    """게이트웨이 시도가 `attempts` 에 남는다 — 이전엔 stage 어휘에 `gw` 가 없었다."""
    wire["set_gate"](_decision({1: "paddle_gw"}))
    wire["set_gw"]([{"page_number": 1, "blocks": [{"type": "text", "text": "스캔"}],
                     "layout": [], "page_size": None, "status": "ok"}])
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert any(a[0] == "gw" and a[1] == "ok" for a in res.page_traces[0]["attempts"])
