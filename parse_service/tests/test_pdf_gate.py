"""PDF 문서수준 게이트 — triage 버킷 집계로 ODL / MinerU(pipeline·hybrid) 라우팅."""
import pytest
from parse_service.parsers.pdf import gate
from parse_service.parsers.pdf.triage import PageSignals, Bucket


def _sig(bucket):
    s = PageSignals(page_number=1, width=600, height=800)
    s.bucket = bucket
    return s


T, L, O, S = Bucket.TEXT_ONLY, Bucket.LLM_NEEDED, Bucket.OCR_NEEDED, Bucket.SKIP


@pytest.mark.parametrize("buckets,lane,backend,method", [
    # 순수 디지털 텍스트 → ODL
    ([T, T], "odl", None, None),
    ([T, S], "odl", None, None),
    ([S], "odl", None, None),
    ([], "odl", None, None),
    # 스캔 페이지 존재(OCR_NEEDED) → MinerU pipeline(ocr) — 하나라도 있으면
    ([O, O], "mineru", "pipeline", "ocr"),
    ([O, S], "mineru", "pipeline", "ocr"),
    ([T, O], "mineru", "pipeline", "ocr"),              # 혼합(디지털+스캔)도 pipeline
    ([O, L], "mineru", "pipeline", "ocr"),
    # 스캔 없음 + 차트/그림 페이지 비율 높음(≥0.5) → MinerU hybrid(auto)
    ([L, L], "mineru", "hybrid-http-client", "auto"),
    ([T, L], "mineru", "hybrid-http-client", "auto"),   # 1/2 = 0.5 ≥ 0.5
    ([L], "mineru", "hybrid-http-client", "auto"),
    # 스캔 없음 + 차트 소수(<0.5) → ODL(텍스트 위주; 그림은 modal-enrich VL)
    ([T, T, L], "odl", None, None),                     # 1/3 < 0.5
    ([T, T, T, L], "odl", None, None),                  # 1/4 < 0.5
])
def test_decide_route(monkeypatch, buckets, lane, backend, method):
    monkeypatch.setattr(gate, "triage_document", lambda b: [_sig(x) for x in buckets])
    d = gate.decide_route(b"%PDF")
    assert (d.lane, d.backend, d.parse_method) == (lane, backend, method)


def test_triage_exception_falls_back_to_odl(monkeypatch):
    def boom(b):
        raise RuntimeError("corrupt page iteration")
    monkeypatch.setattr(gate, "triage_document", boom)
    d = gate.decide_route(b"%PDF")
    assert (d.lane, d.backend, d.parse_method) == ("odl", None, None)


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


def test_mineru_route_has_no_diagram_pages(monkeypatch):
    """스캔 문서(pipeline 라우팅)는 diagram_pages 불필요(MinerU 가 chart 를 image 블록으로 냄)."""
    monkeypatch.setattr(gate, "triage_document", lambda b: [_sig(O)])
    d = gate.decide_route(b"%PDF")
    assert d.lane == "mineru" and d.diagram_pages == ()
