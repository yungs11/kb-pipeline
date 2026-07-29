"""PDF 문서수준 게이트 — triage 버킷 집계로 ODL / VL / Paddle gateway 라우팅."""
import pytest
from parse_service.parsers.pdf import gate
from parse_service.parsers.pdf.triage import PageSignals, Bucket


def _sig(bucket):
    s = PageSignals(page_number=1, width=600, height=800)
    s.bucket = bucket
    return s


T, L, O, S = Bucket.TEXT_ONLY, Bucket.LLM_NEEDED, Bucket.OCR_NEEDED, Bucket.SKIP


@pytest.mark.parametrize("buckets,lane", [
    # 순수 디지털 텍스트 → ODL
    ([T, T], "odl"),
    ([T, S], "odl"),
    ([S], "odl"),
    ([], "odl"),
    # 차트/그림 페이지 비율 ≥0.5 → VL 레인 (스캔 여부 무관 — 2026-07-15 결정)
    ([L, L], "vl"),
    ([T, L], "vl"),                         # 1/2 = 0.5 ≥ 0.5
    ([L], "vl"),
    ([O, L], "vl"),                         # 스캔+차트 혼합도 비율 충족 시 VL
    ([O, L, L], "vl"),                      # 2/3 ≥ 0.5
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
