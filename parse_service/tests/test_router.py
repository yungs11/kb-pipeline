"""router — 확장자 → 도메인 4분기. 변환은 run_parse 가 한다(여기서 하지 않는다)."""
import pytest

from parse_service import router


@pytest.mark.parametrize("filename,expected", [
    ("a.pdf", "pdf"),
    ("a.hwp", "pdf"),        # 변환 후 pdf 파서 — run_parse 가 .pdf 로 바꿔 보낸다
    ("a.docx", "pdf"),       # kordoc 레인 제거(2026-08-06)
    ("a.pptx", "pdf"),       # 구 gotenberg 레인 제거
    ("A.HWP", "pdf"),        # 대소문자 무시
    ("a.xlsx", "excel"), ("a.xls", "excel"),
    ("a.png", "ocr"), ("a.jpeg", "ocr"),
    ("a.txt", "text"), ("a.csv", "text"), ("a.md", "text"),
    ("upload", "pdf"),       # 확장자 없음 → pdf 도메인(%PDF 가드는 run_parse 에)
])
def test_domain_of(filename, expected):
    assert router.domain_of(filename) == expected


def test_route_dispatches_to_domain_parser(monkeypatch):
    seen = {}

    def fake(fb, fn, **kw):
        seen["fn"] = fn
        return "RR"

    monkeypatch.setitem(router._PARSERS, "pdf", fake)
    assert router.route(b"%PDF", "a.pdf", ocr_url="", excel_url="") == "RR"
    assert seen["fn"] == "a.pdf"


def test_text_lane_decodes_cp949():
    """cp949 평문이 mojibake 없이 블록화된다(utf-16 을 앞에 두면 조용히 깨진다)."""
    rr = router._text_parse("휴가규정 제1조".encode("cp949"), "a.txt")
    assert "휴가규정" in " ".join(b.get("text", "") for b in rr.pages[0]["blocks"])


def test_text_lane_empty_raises():
    from parse_service.parsers import ParserError
    with pytest.raises(ParserError):
        router._text_parse(b"   ", "a.txt")
