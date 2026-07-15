"""PDF parse() 문서수준 분기(ODL/MinerU) + 게이트/MinerU 실패·빈결과 폴백."""
from parse_service.parsers import RouteResult
from parse_service.parsers import pdf as pdf_parser
from parse_service.parsers.pdf.gate import RouteDecision


def _mineru(pm="ocr", backend="pipeline"):
    return RouteDecision(lane="mineru", backend=backend, parse_method=pm)


def test_odl_lane_when_gate_says_odl(monkeypatch):
    monkeypatch.setattr(pdf_parser, "_safe_decide_route",
                        lambda b: RouteDecision(lane="odl", backend=None, parse_method=None))
    monkeypatch.setattr(pdf_parser, "_page_markdowns", lambda fb, fn: ["# 텍스트"])
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert res.kind == "pages" and res.pages[0]["blocks"]


def test_mineru_lane_forwards_method_and_backend(monkeypatch):
    monkeypatch.setattr(pdf_parser, "_safe_decide_route",
                        lambda b: _mineru("ocr", "pipeline"))
    seen = {}

    def fake_run(fb, fn, pm, backend):
        seen["pm"], seen["backend"] = pm, backend  # 게이트→run_mineru 전달 검증
        return [{"page_number": 1, "blocks": [{"type": "text", "text": "m"}]}]

    monkeypatch.setattr(pdf_parser, "run_mineru", fake_run)
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert seen == {"pm": "ocr", "backend": "pipeline"}
    assert res.kind == "pages" and res.pages[0]["blocks"][0]["text"] == "m"


def test_mineru_failure_falls_back_to_odl(monkeypatch):
    monkeypatch.setattr(pdf_parser, "_safe_decide_route", lambda b: _mineru())

    def boom(fb, fn, pm, backend):
        raise RuntimeError("MinerU down")

    monkeypatch.setattr(pdf_parser, "run_mineru", boom)
    monkeypatch.setattr(pdf_parser, "_page_markdowns", lambda fb, fn: ["# 폴백 텍스트"])
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert res.kind == "pages" and res.pages[0]["blocks"], "MinerU 실패 시 ODL 레인 폴백"


def test_mineru_empty_result_falls_back_to_odl(monkeypatch):
    """성공했으나 blocks 전무 → ODL 폴백(빈 출력 재발 방지)."""
    monkeypatch.setattr(pdf_parser, "_safe_decide_route", lambda b: _mineru())
    monkeypatch.setattr(pdf_parser, "run_mineru",
                        lambda fb, fn, pm, backend: [{"page_number": 1, "blocks": []}])
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


def test_odl_diagram_pages_get_vl_supplement(monkeypatch):
    """ODL 라우팅 + diagram_pages: 해당 페이지만 렌더→VL 서술 블록이 **추가**된다
    (native 텍스트 블록 유지)."""
    monkeypatch.setattr(
        pdf_parser, "_safe_decide_route",
        lambda b: RouteDecision(lane="odl", backend=None, parse_method=None,
                                diagram_pages=(2,)))
    monkeypatch.setattr(pdf_parser, "_page_markdowns",
                        lambda fb, fn: ["# p1 텍스트", "# p2 순서도 라벨들"])

    class FakeRP:
        page_number, jpeg = 2, b"jpegbytes"

    monkeypatch.setattr(pdf_parser, "_render_pages", lambda fb: [FakeRP()])
    called = {}

    def fake_vl(jpeg, name, ocr_url, diagram=False):
        called["name"] = name
        # 실제 ocr_elements_sync 는 순수텍스트 figure 를 text 로 재분류해 반환한다(Phase 2c).
        return [{"category": "text",
                 "content": {"markdown": "순서도: 업로드→파싱→가드 분기 구조"}, "page": 1}]

    monkeypatch.setattr(pdf_parser, "_ocr_elements_for_page", fake_vl)
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert "diagram" in called["name"]                      # 다이어그램 보충 경로로 호출됨
    p2 = next(p for p in res.pages if p["page_number"] == 2)
    texts = " ".join(b.get("text", "") for b in p2["blocks"])
    assert "순서도 라벨들" in texts, "native 텍스트 유지"
    assert any("업로드→파싱" in (b.get("text") or "") for b in p2["blocks"]), "VL 서술 추가"
    p1 = next(p for p in res.pages if p["page_number"] == 1)
    assert len(p1["blocks"]) >= 1                            # p1 은 보충 없음(원래 블록만)


def test_odl_diagram_vl_failure_nonfatal(monkeypatch):
    """다이어그램 VL 보충 실패 → 해당 페이지 native 블록만으로 정상 반환(비치명)."""
    monkeypatch.setattr(
        pdf_parser, "_safe_decide_route",
        lambda b: RouteDecision(lane="odl", backend=None, parse_method=None,
                                diagram_pages=(1,)))
    monkeypatch.setattr(pdf_parser, "_page_markdowns", lambda fb, fn: ["# 텍스트"])

    class FakeRP:
        page_number, jpeg = 1, b"jpegbytes"

    monkeypatch.setattr(pdf_parser, "_render_pages", lambda fb: [FakeRP()])

    def boom(jpeg, name, ocr_url):
        raise RuntimeError("VL down")

    monkeypatch.setattr(pdf_parser, "_ocr_elements_for_page", boom)
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert res.kind == "pages" and res.pages[0]["blocks"], "native 블록 유지 + 예외 없음"


def test_paddle_gw_lane_dispatch(monkeypatch):
    """스캔 라우팅(paddle_gw) → run_paddle_gateway 호출, pages 반환."""
    monkeypatch.setattr(pdf_parser, "_safe_decide_route",
                        lambda b: RouteDecision(lane="paddle_gw", backend=None,
                                                parse_method=None))
    import parse_service.parsers.pdf.paddle_gw as pg
    monkeypatch.setattr(pg, "run_paddle_gateway",
                        lambda fb, fn: [{"page_number": 1,
                                         "blocks": [{"type": "text", "text": "gw", "page_idx": 1}]}])
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert res.kind == "pages" and res.pages[0]["blocks"][0]["text"] == "gw"


def test_paddle_gw_failure_falls_back_to_odl_vl(monkeypatch):
    """게이트웨이 실패 → ODL 레인 폴백(스캔 페이지는 in-process VL 보충 — MinerU 미경유)."""
    monkeypatch.setattr(pdf_parser, "_safe_decide_route",
                        lambda b: RouteDecision(lane="paddle_gw", backend=None,
                                                parse_method=None))
    import parse_service.parsers.pdf.paddle_gw as pg

    def boom(fb, fn):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(pg, "run_paddle_gateway", boom)
    called = {"mineru": False}
    monkeypatch.setattr(pdf_parser, "run_mineru",
                        lambda *a, **k: called.__setitem__("mineru", True))
    # ODL 폴백 경로: 스캔 md(빈) → 렌더 → in-process VL
    monkeypatch.setattr(pdf_parser, "_page_markdowns", lambda fb, fn: ["   "])

    class FakeRP:
        page_number, jpeg = 1, b"jpegbytes"

    monkeypatch.setattr(pdf_parser, "_render_pages", lambda fb: [FakeRP()])
    monkeypatch.setattr(pdf_parser, "_ocr_elements_for_page",
                        lambda jpeg, name, ocr_url: [
                            {"category": "text", "content": {"markdown": "vl 폴백"}, "page": 0}])
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert not called["mineru"], "폴백 체인에 MinerU 없어야(2026-07-15 결정)"
    assert any("vl 폴백" in (b.get("text") or "") for b in res.pages[0]["blocks"])


def test_paddle_gw_empty_result_falls_back(monkeypatch):
    """게이트웨이 성공했으나 blocks 전무(전 페이지 실패) → ODL/VL 폴백."""
    monkeypatch.setattr(pdf_parser, "_safe_decide_route",
                        lambda b: RouteDecision(lane="paddle_gw", backend=None,
                                                parse_method=None))
    import parse_service.parsers.pdf.paddle_gw as pg
    monkeypatch.setattr(pg, "run_paddle_gateway",
                        lambda fb, fn: [{"page_number": 1, "blocks": []}])
    monkeypatch.setattr(pdf_parser, "_page_markdowns", lambda fb, fn: ["# 폴백 텍스트"])
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert res.pages[0]["blocks"], "빈 결과 시 ODL 폴백"


def test_vl_lane_dispatch(monkeypatch):
    """차트비율≥0.5 라우팅(vl) → 전 페이지 렌더→in-process VL → blocks."""
    monkeypatch.setattr(pdf_parser, "_safe_decide_route",
                        lambda b: RouteDecision(lane="vl", backend=None, parse_method=None))

    class RP1:
        page_number, jpeg = 1, b"j1"

    class RP2:
        page_number, jpeg = 2, b"j2"

    monkeypatch.setattr(pdf_parser, "_render_pages", lambda fb: [RP1(), RP2()])
    monkeypatch.setattr(pdf_parser, "_ocr_elements_for_page",
                        lambda jpeg, name, ocr_url: [
                            {"category": "text", "content": {"markdown": f"vl:{name}"}, "page": 0}])
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert [p["page_number"] for p in res.pages] == [1, 2]
    assert "vl:page-1" in res.pages[0]["blocks"][0]["text"]
    assert all(b["page_idx"] == 2 for b in res.pages[1]["blocks"])


def test_vl_lane_all_failed_falls_back_to_odl(monkeypatch):
    """VL 전 페이지 실패 → blocks 전무 → ODL 폴백."""
    monkeypatch.setattr(pdf_parser, "_safe_decide_route",
                        lambda b: RouteDecision(lane="vl", backend=None, parse_method=None))

    class RP1:
        page_number, jpeg = 1, b"j1"

    monkeypatch.setattr(pdf_parser, "_render_pages", lambda fb: [RP1()])

    def boom(jpeg, name, ocr_url):
        raise RuntimeError("VL down")

    monkeypatch.setattr(pdf_parser, "_ocr_elements_for_page", boom)
    monkeypatch.setattr(pdf_parser, "_page_markdowns", lambda fb, fn: ["# 폴백 텍스트"])
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert res.pages[0]["blocks"], "VL 전멸 시 ODL 폴백"


def test_paddle_gw_diagram_pages_replaced_by_vl(monkeypatch):
    """paddle_gw 다이어그램 페이지는 VL 서술로 **교체** — 게이트웨이 OCR 조각(오타·뒤죽박죽)과
    죽은 이미지참조(게이트웨이 상대경로)를 남기지 않는다(2026-07-15 결정). 비다이어그램 페이지 불변."""
    monkeypatch.setattr(
        pdf_parser, "_safe_decide_route",
        lambda b: RouteDecision(lane="paddle_gw", backend=None, parse_method=None,
                                diagram_pages=(2,)))
    import parse_service.parsers.pdf.paddle_gw as pg
    monkeypatch.setattr(pg, "run_paddle_gateway", lambda fb, fn: [
        {"page_number": 1, "blocks": [{"type": "text", "text": "p1", "page_idx": 1}]},
        {"page_number": 2, "blocks": [
            {"type": "image", "img_path": "imgs/x.jpg", "image_caption": [], "page_idx": 2},
            {"type": "text", "text": "소유궁이전 조각", "page_idx": 2},  # 게이트웨이 OCR 오타 조각
        ]},
    ])

    class FakeRP:
        page_number, jpeg = 2, b"jpegbytes"

    monkeypatch.setattr(pdf_parser, "_render_pages", lambda fb: [FakeRP()])
    monkeypatch.setattr(pdf_parser, "_ocr_elements_for_page",
                        lambda jpeg, name, ocr_url, diagram=False: [
                            {"category": "text",
                             "content": {"markdown": "순서도: START→요청→확인→END"}, "page": 1}])
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    p2 = next(p for p in res.pages if p["page_number"] == 2)
    assert any("START→요청" in (b.get("text") or "") for b in p2["blocks"]), "VL 서술 존재"
    assert not any(b["type"] == "image" for b in p2["blocks"]), "죽은 이미지참조 제거"
    assert not any("소유궁이전" in (b.get("text") or "") for b in p2["blocks"]), "OCR 조각 제거"
    p1 = next(p for p in res.pages if p["page_number"] == 1)
    assert p1["blocks"][0]["text"] == "p1", "비다이어그램 페이지 불변"


def test_paddle_gw_diagram_vl_failure_keeps_gateway_blocks(monkeypatch):
    """교체 모드에서 VL 실패 시 게이트웨이 블록 유지(없는 것보단 조각이 낫다 — 비치명)."""
    monkeypatch.setattr(
        pdf_parser, "_safe_decide_route",
        lambda b: RouteDecision(lane="paddle_gw", backend=None, parse_method=None,
                                diagram_pages=(1,)))
    import parse_service.parsers.pdf.paddle_gw as pg
    monkeypatch.setattr(pg, "run_paddle_gateway", lambda fb, fn: [
        {"page_number": 1, "blocks": [{"type": "text", "text": "게이트웨이 조각", "page_idx": 1}]},
    ])

    class FakeRP:
        page_number, jpeg = 1, b"jpegbytes"

    monkeypatch.setattr(pdf_parser, "_render_pages", lambda fb: [FakeRP()])

    def boom(jpeg, name, ocr_url):
        raise RuntimeError("VL down")

    monkeypatch.setattr(pdf_parser, "_ocr_elements_for_page", boom)
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert any("게이트웨이 조각" in (b.get("text") or "") for b in res.pages[0]["blocks"])


def test_parse_filters_degenerate_vl_blocks(monkeypatch):
    """어느 레인이든 parse() 출구에서 VL 퇴화(무한반복) 블록이 제거된다."""
    degen = "기계음 손상완을 잡고 " * 60
    monkeypatch.setattr(pdf_parser, "_safe_decide_route",
                        lambda b: RouteDecision(lane="paddle_gw", backend=None,
                                                parse_method=None))
    import parse_service.parsers.pdf.paddle_gw as pg
    monkeypatch.setattr(pg, "run_paddle_gateway", lambda fb, fn: [
        {"page_number": 1, "blocks": [
            {"type": "text", "text": "정상 본문 텍스트입니다.", "page_idx": 1},
            {"type": "text", "text": degen, "page_idx": 1},
        ]},
    ])
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    texts = [b.get("text", "") for b in res.pages[0]["blocks"]]
    assert any("정상 본문" in t for t in texts), "정상 블록 유지"
    assert not any("기계음 손상완" in t for t in texts), "퇴화 블록 제거"


def test_diagram_supplement_uses_diagram_prompt(monkeypatch):
    """다이어그램 보충은 순서도 전용 프롬프트(DIAGRAM_*)로 VL 호출 — 범용 전사 프롬프트 아님."""
    monkeypatch.setattr(
        pdf_parser, "_safe_decide_route",
        lambda b: RouteDecision(lane="odl", backend=None, parse_method=None,
                                diagram_pages=(1,)))
    monkeypatch.setattr(pdf_parser, "_page_markdowns", lambda fb, fn: ["# p1 순서도 라벨"])

    class FakeRP:
        page_number, jpeg = 1, b"jpegbytes"

    monkeypatch.setattr(pdf_parser, "_render_pages", lambda fb: [FakeRP()])
    seen = {}

    def fake_sync(fb, fn, override=None):
        seen["override"] = override
        return [{"category": "text", "content": {"markdown": "START→요청→END"}, "page": 0}]

    import parse_service.parsers.ocr as ocr_mod
    monkeypatch.setattr(ocr_mod, "ocr_elements_sync", fake_sync)
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    from parse_service.parsers.ocr import prompts
    assert seen["override"] == (prompts.DIAGRAM_SYSTEM_PROMPT, prompts.DIAGRAM_USER_PROMPT)
    assert res.kind == "pages"
