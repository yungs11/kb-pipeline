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
    monkeypatch.setattr(pdf_parser, "_render_pages", lambda fb, page_numbers=None, **k: [FakeRP()])
    monkeypatch.setattr(pdf_parser, "_ocr_elements_for_pages",
                        lambda jobs, ocr_url=None, **k: [
                            [{"category": "text", "content": {"markdown": "ocr text"}, "page": 0}]
                            for _ in jobs])
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

    monkeypatch.setattr(pdf_parser, "_render_pages", lambda fb, page_numbers=None, **k: [FakeRP()])
    called = {}

    def fake_ocr(jobs, ocr_url=None, **k):
        called["ocr"] = True
        return [[{"category": "text", "content": {"markdown": "vl 추출 내용"}, "page": 0}]
                for _ in jobs]

    monkeypatch.setattr(pdf_parser, "_ocr_elements_for_pages", fake_ocr)
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert called.get("ocr"), "이미지참조/빈표 페이지는 VL(OCR) 로 라우팅돼야 한다"
    assert res.pages[1]["blocks"], "VL 보충 블록이 있어야 한다"
    assert res.pages[0]["blocks"], "digital 페이지(p1)는 그대로 ODL 블록"


def test_odl_tool_error_falls_back_to_vl(monkeypatch):
    """ODL 프로세스 실패는 **문서 실패가 아니라 VL 폴백**이다(Plan B-5, 사용자 확정 2026-08-04).

    이전에는 `ToolError → ParserError` 로 문서 전체가 실패했다. 자바/ODL 은 세팅 전제이므로
    그 실패는 예외 상황이고, 그때 문서를 통째로 버리는 것보다 VL 로 읽는 편이 낫다.
    """
    from parse_service.tools import ToolError

    def boom(fb, fn):
        raise ToolError("no md")

    class FakeRP:
        page_number, jpeg = 1, b"jpegbytes"

    monkeypatch.setattr(pdf_parser, "_page_markdowns", boom)
    monkeypatch.setattr(pdf_parser, "_render_pages",
                        lambda fb, page_numbers=None, **k: [FakeRP()])
    monkeypatch.setattr(pdf_parser, "_ocr_elements_for_pages",
                        lambda jobs, ocr_url=None, **k: [
                            [{"category": "text",
                              "content": {"markdown": "VL 로 살린 내용"}, "page": 0}]
                            for _ in jobs])
    res = pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="http://ocr")
    assert res.kind == "pages"
    texts = " ".join(b.get("text") or "" for p in res.pages for b in p["blocks"])
    assert "VL 로 살린 내용" in texts


def test_odl_tool_error_in_delegated_path_still_raises(monkeypatch):
    """`_odl_lane` 위임 경로(정합 가드)는 현행 계약 유지 — ToolError → ParserError."""
    from parse_service.tools import ToolError

    def boom(fb, fn):
        raise ToolError("no md")

    monkeypatch.setattr(pdf_parser, "_page_markdowns", boom)
    with pytest.raises(ParserError):
        pdf_parser._odl_lane(b"%PDF", "a.pdf", ocr_url="http://ocr")
