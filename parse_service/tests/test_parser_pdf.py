"""parsers/pdf — 페이지 보존 + 스캔페이지 OCR 보충 + chunk_needed=True."""
import pytest
from parse_service.parsers import RouteResult, ParserError
from parse_service.parsers import pdf as pdf_parser


def test_digital_pdf_pages(monkeypatch):
    monkeypatch.setattr(pdf_parser, "_page_markdowns",
                        lambda fb, fn: ["# p1 text", "# p2 text"])
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert isinstance(res, RouteResult)
    assert res.kind == "pages" and res.chunk_needed is True
    assert [p["page_number"] for p in res.pages] == [1, 2]
    assert res.pages[0]["blocks"][0]["page_idx"] == 1


def test_scanned_page_gets_ocr(monkeypatch):
    # p2 가 빈 md → 렌더+OCR 보충 경로. 렌더/OCR 를 fake 로.
    monkeypatch.setattr(pdf_parser, "_page_markdowns", lambda fb, fn: ["# p1", "   "])
    class FakeRP:  # render_pdf_pages 반환 원소 흉내
        page_number, jpeg = 2, b"jpegbytes"
    monkeypatch.setattr(pdf_parser, "_render_pages", lambda fb: [FakeRP()])
    monkeypatch.setattr(pdf_parser, "_ocr_elements_for_page",
                        lambda jpeg, name, ocr_url: [
                            {"category": "text", "content": {"markdown": "ocr text"}, "page": 0}
                        ])
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert res.pages[1]["page_number"] == 2
    assert res.pages[1]["blocks"], "OCR 보충 블록이 있어야"
    assert res.pages[1]["blocks"][0]["page_idx"] == 2


def test_digital_text_len_ignores_tags_and_image_refs():
    """digital 판정은 실 텍스트로 — 이미지 참조/HTML 태그/공백은 안 센다."""
    from parse_service.parsers.pdf import _digital_text_len
    assert _digital_text_len("![image 1](<doc(우발)_images/imageFile1.png>)") == 0
    assert _digital_text_len("<table>\n  <tr><td> </td></tr>\n</table>") == 0
    assert _digital_text_len("<td>실제내용</td>") == 4       # 표 안 실텍스트는 센다
    assert _digital_text_len("# 제목 본문 텍스트") >= 4


def test_scanned_page_with_imagerefs_and_empty_tables_gets_ocr(monkeypatch):
    """ODL 이 스캔 페이지에 **이미지 참조 + 빈 표 구조**를 non-empty markdown 으로 내도
    VL(OCR) 로 라우팅돼야 한다. raw 길이로 digital 판정하면 VL 을 스킵해 표/텍스트가
    빈 채 나오던 버그(2026-07-07)."""
    scanned_md = (
        "![image 1](<doc(우발)_images/imageFile1.png>)\n\n"
        "<table>\n  <tr><th> </th><th> </th></tr>\n  <tr><td> </td><td> </td></tr>\n</table>"
    )
    monkeypatch.setattr(pdf_parser, "_page_markdowns",
                        lambda fb, fn: ["# p1 실제 텍스트", scanned_md])

    class FakeRP:
        page_number, jpeg = 2, b"jpegbytes"

    monkeypatch.setattr(pdf_parser, "_render_pages", lambda fb: [FakeRP()])
    called = {}

    def fake_ocr(jpeg, name, ocr_url):
        called["ocr"] = True
        return [{"category": "text", "content": {"markdown": "vl 추출 내용"}, "page": 0}]

    monkeypatch.setattr(pdf_parser, "_ocr_elements_for_page", fake_ocr)
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert called.get("ocr"), "이미지참조/빈표 페이지는 VL(OCR) 로 라우팅돼야 한다"
    assert res.pages[1]["blocks"], "VL 보충 블록이 있어야 한다"
    assert res.pages[0]["blocks"], "digital 페이지(p1)는 그대로 ODL 블록"


def test_parse_routes_by_triage(monkeypatch):
    """triage 버킷대로 라우팅 — text=ODL, llm/ocr=VL, skip=제외."""
    from parse_service.parsers import pdf as pdf_parser
    from parse_service.parsers.pdf import triage as triage_mod

    # p1=text(ODL), p2=llm(VL), p3=ocr(VL), p4=skip
    monkeypatch.setattr(pdf_parser, "_page_markdowns",
                        lambda fb, fn: ["# 본문 텍스트", "표페이지", "스캔페이지", "빈페이지"])

    def fake_triage(fb, **kw):
        def mk(n, bucket):
            s = triage_mod.PageSignals(page_number=n, width=595.0, height=842.0)
            s.bucket = bucket
            s.reason = bucket.name
            return s
        return [
            mk(0, triage_mod.Bucket.TEXT_ONLY),
            mk(1, triage_mod.Bucket.LLM_NEEDED),
            mk(2, triage_mod.Bucket.OCR_NEEDED),
            mk(3, triage_mod.Bucket.SKIP),
        ]
    monkeypatch.setattr(pdf_parser, "triage_document", fake_triage)

    class FakeRP:
        def __init__(self, n):
            self.page_number, self.jpeg = n, b"jpeg"
    monkeypatch.setattr(pdf_parser, "_render_pages",
                        lambda fb: [FakeRP(2), FakeRP(3)])
    vl_calls = []
    def fake_ocr(jpeg, name, ocr_url):
        vl_calls.append(name)
        return [{"category": "text", "content": {"markdown": "vl 내용"}, "page": 0}]
    monkeypatch.setattr(pdf_parser, "_ocr_elements_for_page", fake_ocr)

    res = pdf_parser.parse(b"%PDF-realish", "a.pdf", ocr_url="http://ocr")
    routes = {p["page_number"]: p.get("route") for p in res.pages}
    # skip(p4)은 출력에서 제외
    assert set(routes) == {1, 2, 3}
    assert routes[1] == "text"      # ODL
    assert routes[2] == "llm"       # VL
    assert routes[3] == "ocr"       # VL(seam)
    # llm/ocr 두 페이지만 VL 호출
    assert len(vl_calls) == 2
    # text 페이지는 ODL 블록(page_idx 세팅)
    p1 = next(p for p in res.pages if p["page_number"] == 1)
    assert p1["blocks"] and p1["blocks"][0]["page_idx"] == 1


def test_parse_falls_back_when_triage_empty(monkeypatch):
    """triage 가 [] (비-PDF/손상)면 기존 _digital_text_len 폴백으로 동작."""
    from parse_service.parsers import pdf as pdf_parser
    monkeypatch.setattr(pdf_parser, "_page_markdowns", lambda fb, fn: ["# 디지털 텍스트", "   "])
    monkeypatch.setattr(pdf_parser, "triage_document", lambda fb, **kw: [])

    class FakeRP:
        page_number, jpeg = 2, b"jpeg"
    monkeypatch.setattr(pdf_parser, "_render_pages", lambda fb: [FakeRP()])
    monkeypatch.setattr(pdf_parser, "_ocr_elements_for_page",
                        lambda jpeg, name, ocr_url: [{"category": "text", "content": {"markdown": "ocr"}, "page": 0}])
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    # p1 텍스트 → ODL, p2 빈 → VL 폴백
    assert res.pages[0]["blocks"][0]["page_idx"] == 1
    assert res.pages[1]["blocks"], "빈 페이지는 폴백 VL 보충"


def test_tool_error_becomes_parser_error(monkeypatch):
    from parse_service.tools import ToolError
    def boom(fb, fn):
        raise ToolError("no md")
    monkeypatch.setattr(pdf_parser, "_page_markdowns", boom)
    with pytest.raises(ParserError):
        pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
