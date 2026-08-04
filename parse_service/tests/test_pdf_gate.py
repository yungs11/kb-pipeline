"""PDF 문서수준 게이트 — triage 버킷 집계로 ODL / VL / Paddle gateway 라우팅."""
import pytest
from parse_service.parsers.pdf import gate
from parse_service.parsers.pdf.triage import PageSignals, Bucket


def _sig(bucket, page_number: int = 1, *, is_diagram: bool = False):
    s = PageSignals(page_number=page_number, width=600, height=800)
    s.bucket = bucket
    s.is_diagram = is_diagram
    return s


def _sigs(*buckets):
    """페이지 번호를 1,2,3… 으로 자동 부여 — 페이지수준 필드 검증용."""
    return [_sig(b, i) for i, b in enumerate(buckets, start=1)]


T, L, O, S = Bucket.TEXT_ONLY, Bucket.LLM_NEEDED, Bucket.OCR_NEEDED, Bucket.SKIP


@pytest.mark.parametrize("buckets,lane", [
    # 순수 디지털 텍스트 → ODL
    ([T, T], "odl"),
    ([T, S], "odl"),
    ([S], "odl"),
    ([], "odl"),
    # 차트/그림 페이지 비율 ≥0.5 → VL 레인 (스캔 여부 무관 — 2026-07-15 결정)
    # `vl` 레인 삭제(2026-08-04) — 차트/그림 비율과 무관하게 스캔 유무로만 갈린다.
    # 그림 많은 문서도 이제 페이지마다 odl 로 가서 표가 ODL <table> 로 보존된다.
    ([L, L], "odl"),
    ([T, L], "odl"),
    ([L], "odl"),
    ([O, L], "paddle_gw"),                  # 스캔이 있으면 paddle_gw
    ([O, L, L], "paddle_gw"),
    # 스캔 페이지 존재(OCR_NEEDED, 차트비율 미달) → paddle_gw(게이트웨이)
    ([O, O], "paddle_gw"),
    ([O, S], "paddle_gw"),
    ([T, O], "paddle_gw"),                  # 혼합(디지털+스캔)
    ([O, T, L], "paddle_gw"),               # 차트 1/3 < 0.5 → 스캔 우선
    # 차트 소수(<0.5) + 스캔 없음 → ODL(텍스트 위주; 그림은 modal-enrich VL)
    ([T, T, L], "odl"),                     # 1/3 < 0.5
    ([T, T, T, L], "odl"),                  # 1/4 < 0.5
])
def test_decide_route(monkeypatch, buckets, lane):
    monkeypatch.setattr(gate, "triage_document", lambda b: [_sig(x) for x in buckets])
    d = gate.decide_route(b"%PDF")
    assert d.lane == lane


def test_triage_exception_falls_back_to_odl(monkeypatch):
    def boom(b):
        raise RuntimeError("corrupt page iteration")
    monkeypatch.setattr(gate, "triage_document", boom)
    d = gate.decide_route(b"%PDF")
    assert d.lane == "odl"


def test_odl_route_carries_diagram_pages(monkeypatch):
    """다이어그램 페이지(LLM_NEEDED+is_diagram)가 비율 미달로 ODL 라우팅될 때
    diagram_pages 로 전달돼야(ODL 레인 VL 보충용). 정의서 패턴: 15p 중 1p 순서도."""
    sigs = []
    for n in range(1, 7):                     # 6p: p5 만 다이어그램
        s = _sig(T if n != 5 else L)
        s.page_number = n
        if n == 5:
            s.is_diagram = True
        sigs.append(s)
    monkeypatch.setattr(gate, "triage_document", lambda b: sigs)
    d = gate.decide_route(b"%PDF")
    assert d.lane == "odl"                    # 1/6 < 0.5 → ODL
    assert d.diagram_pages == (5,)


def test_scan_route_has_no_diagram_pages(monkeypatch):
    """스캔 문서(paddle_gw 라우팅)는 diagram_pages 불필요(게이트웨이가 전 페이지 VL 처리)."""
    monkeypatch.setattr(gate, "triage_document", lambda b: [_sig(O)])
    d = gate.decide_route(b"%PDF")
    assert d.lane == "paddle_gw" and d.diagram_pages == ()


def test_paddle_gw_route_carries_diagram_pages(monkeypatch):
    """스캔 문서에 디지털 다이어그램 페이지가 섞이면(소유권 p4 패턴) paddle_gw 라우팅에도
    diagram_pages 전달 — 게이트웨이가 순서도를 이미지참조로만 내므로 VL 보충 필요."""
    sigs = []
    for n, b in [(1, O), (2, T), (3, T), (4, L), (5, T), (6, T), (7, T)]:
        s = _sig(b); s.page_number = n
        if n == 4:
            s.is_diagram = True
        sigs.append(s)
    monkeypatch.setattr(gate, "triage_document", lambda b: sigs)
    d = gate.decide_route(b"%PDF")
    assert d.lane == "paddle_gw"      # 스캔 존재, 차트 1/7 < 0.5
    assert d.diagram_pages == (4,)


# ── Plan B-1 (2026-08-04): 페이지수준 필드 — **순수 추가**(lane 판정 무변경) ────────────
def test_page_lanes_maps_each_bucket(monkeypatch):
    """SKIP→skip / OCR_NEEDED→paddle_gw / TEXT_ONLY·LLM_NEEDED→odl, 페이지 번호 보존."""
    monkeypatch.setattr(gate, "triage_document",
                        lambda b: _sigs(T, S, O, L))
    d = gate.decide_route(b"%PDF")
    assert d.page_lanes == ((1, "odl"), (2, "skip"), (3, "paddle_gw"), (4, "odl"))
    assert d.total_pages == 4


def test_narrate_pages_is_diagram_not_llm_needed(monkeypatch):
    """narrate_pages 는 is_diagram 페이지다 — LLM_NEEDED 상위집합이 아니다.

    LLM_NEEDED 는 두 갈래다: ① is_diagram(순서도) ② 혼합 콘텐츠(텍스트+큰 이미지).
    ②까지 넣으면 순서도가 아닌 페이지에 DIAGRAM 서술 프롬프트가 붙는 동작 확장이 된다.
    """
    # LLM 비율을 0.5 미만으로 둬 odl 레인을 태운다(vl 레인은 diagram_pages 를 비우므로 별도 테스트).
    sigs = [_sig(L, 1, is_diagram=True), _sig(L, 2), _sig(T, 3), _sig(T, 4), _sig(T, 5)]
    monkeypatch.setattr(gate, "triage_document", lambda b: sigs)   # p2 = 혼합 콘텐츠
    d = gate.decide_route(b"%PDF")
    assert d.lane == "odl"
    assert d.narrate_pages == (1,), "혼합 콘텐츠(p2)는 서술 대상이 아니다"
    assert d.narrate_pages == d.diagram_pages, "odl 레인에서는 현행 diagram_pages 와 같다"


def test_page_level_fields_do_not_change_lane(monkeypatch):
    """B-1 은 순수 추가 — 기존 lane/diagram_pages/ocr_pages 판정이 그대로여야 한다."""
    sigs = [_sig(O, 1), _sig(T, 2), _sig(L, 3, is_diagram=True)]
    monkeypatch.setattr(gate, "triage_document", lambda b: sigs)
    d = gate.decide_route(b"%PDF")
    assert d.lane == "paddle_gw"          # n_ocr > 0 (LLM 비율 1/3 < 0.5)
    assert d.diagram_pages == (3,) and d.ocr_pages == (1,)


def test_chart_heavy_document_goes_to_odl_not_vl(monkeypatch):
    """차트/그림 비율이 높아도 스캔이 없으면 odl 이다(`vl` 레인 삭제, B-5).

    이전에는 `n_llm/total >= 0.5` 면 문서 전체가 VL 로 갔고, 그 경로에서 KIS 같은 표 문서가
    표 테두리 curve 를 순서도로 오탐당해 통째로 재전사되며 표가 깨졌다.
    """
    monkeypatch.setattr(gate, "triage_document",
                        lambda b: [_sig(L, 1, is_diagram=True), _sig(L, 2, is_diagram=True)])
    d = gate.decide_route(b"%PDF")
    assert d.lane == "odl"
    assert d.page_lanes == ((1, "odl"), (2, "odl"))
    assert d.narrate_pages == (1, 2), "서술 보충 대상으로는 잡힌다"


def test_gate_failure_returns_empty_page_level_fields(monkeypatch):
    def boom(b):
        raise RuntimeError("pymupdf 부재")
    monkeypatch.setattr(gate, "triage_document", boom)
    d = gate.decide_route(b"%PDF")
    assert d.lane == "odl" and d.page_lanes == () and d.total_pages == 0
