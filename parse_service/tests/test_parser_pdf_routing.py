"""PDF parse() 문서수준 분기(ODL/MinerU) + 게이트/MinerU 실패·빈결과 폴백."""
from parse_service.parsers import RouteResult
from parse_service.parsers import pdf as pdf_parser
from parse_service.parsers.pdf.gate import RouteDecision


def _mineru(pm="ocr"):
    return RouteDecision(lane="mineru", parse_method=pm)


def test_odl_lane_when_gate_says_odl(monkeypatch):
    monkeypatch.setattr(pdf_parser, "_safe_decide_route",
                        lambda b: RouteDecision(lane="odl", parse_method=None))
    monkeypatch.setattr(pdf_parser, "_page_markdowns", lambda fb, fn: ["# 텍스트"])
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert res.kind == "pages" and res.pages[0]["blocks"]


def test_mineru_lane_when_gate_says_mineru(monkeypatch):
    monkeypatch.setattr(pdf_parser, "_safe_decide_route", lambda b: _mineru("ocr"))
    seen = {}

    def fake_run(fb, fn, pm):
        seen["pm"] = pm  # parse_method 가 게이트→run_mineru 로 전달되는지(§4.3 유실수정 핵심)
        return [{"page_number": 1, "blocks": [{"type": "text", "text": "m"}]}]

    monkeypatch.setattr(pdf_parser, "run_mineru", fake_run)
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert seen["pm"] == "ocr", "게이트의 parse_method 가 run_mineru 로 전달돼야"
    assert res.kind == "pages" and res.pages[0]["blocks"][0]["text"] == "m"


def test_mineru_failure_falls_back_to_odl(monkeypatch):
    monkeypatch.setattr(pdf_parser, "_safe_decide_route", lambda b: _mineru())

    def boom(fb, fn, pm):
        raise RuntimeError("VLM down")

    monkeypatch.setattr(pdf_parser, "run_mineru", boom)
    monkeypatch.setattr(pdf_parser, "_page_markdowns", lambda fb, fn: ["# 폴백 텍스트"])
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert res.kind == "pages" and res.pages[0]["blocks"], "MinerU 실패 시 ODL 레인 폴백"


def test_mineru_empty_result_falls_back_to_odl(monkeypatch):
    """성공했으나 blocks 전무 → ODL 폴백(빈 출력 재발 방지)."""
    monkeypatch.setattr(pdf_parser, "_safe_decide_route", lambda b: _mineru())
    monkeypatch.setattr(pdf_parser, "run_mineru",
                        lambda fb, fn, pm: [{"page_number": 1, "blocks": []}])
    monkeypatch.setattr(pdf_parser, "_page_markdowns", lambda fb, fn: ["# 폴백 텍스트"])
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert res.pages[0]["blocks"], "MinerU 빈 결과 시 ODL 폴백"


def test_gate_exception_routes_to_odl(monkeypatch):
    """_safe_decide_route 가 게이트 예외를 삼켜 None → ODL(새 500 없음)."""
    monkeypatch.setattr(pdf_parser, "_page_markdowns", lambda fb, fn: ["# 텍스트"])
    # 실제 _safe_decide_route 사용: decide_route 가 예외를 던져도 삼켜지는지 검증
    import parse_service.parsers.pdf.gate as gate
    monkeypatch.setattr(gate, "decide_route",
                        lambda b: (_ for _ in ()).throw(RuntimeError("boom")))
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert res.kind == "pages"  # 예외 안 나고 ODL 로
