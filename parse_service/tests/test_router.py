"""router — 확장자 → 도메인 5분기. 변환은 run_parse 가 한다(여기서 하지 않는다)."""
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
    ("a.csv", "excel"),      # 2026-08-11 엑셀 레인으로 이동(행 레코드 청크)
    ("a.txt", "text"), ("a.md", "text"), ("a.xml", "text"),
    ("a.html", "html"), ("A.HTM", "html"),   # 형변환 API 미경유(2026-08-11)
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


def test_html_does_not_hit_convert_api():
    """html 은 형변환 API 를 타지 않고 자체 레인으로 간다."""
    from parse_service.tools import fileconvert

    assert fileconvert.needs_convert("a.html") is False
    assert fileconvert.needs_convert("a.htm") is False


def test_office_still_converts():
    """형변환 API 대상은 hwp/office 만 남는다(축소는 했지만 없애지 않았다)."""
    from parse_service.tools import fileconvert

    for name in ("a.hwp", "a.hwpx", "a.doc", "a.docx", "a.ppt", "a.pptx"):
        assert fileconvert.needs_convert(name) is True


def test_parsers_table_covers_every_domain():
    """domain_of 에 분기를 추가하고 _PARSERS 키를 빠뜨리면 런타임 KeyError 로
    해당 레인 전체가 죽는다 — domain_of 단언만으로는 못 잡는다."""
    assert set(router._PARSERS) == {"pdf", "excel", "ocr", "html", "text"}


def test_blockless_xml_fails_loudly():
    """속성 전용 XML export 는 텍스트 노드가 없어 blocks=0 이다(실측). 가드가 없으면
    enriched_content="" 로 200 이 나가 조용한 빈 적재가 된다 — 편입 전 `%PDF` 가드가
    parse_failed 로 크게 죽던 것보다 나쁜 실패 유형이다."""
    from parse_service.parsers import ParserError

    assert router.domain_of("a.xml") == "text"      # 텍스트 레인에 진입한 뒤
    with pytest.raises(ParserError):                # 가드가 발화해야 한다
        router.route(b'<?xml version="1.0"?><root><item id="1"/></root>', "a.xml",
                     ocr_url="", excel_url="")


def test_xml_with_text_still_parses():
    rr = router.route('<?xml version="1.0"?><root><item>내용</item></root>'.encode("utf-8"),
                      "a.xml", ocr_url="", excel_url="")
    assert rr.kind == "pages"
    assert any("내용" in b.get("text", "") for b in rr.pages[0]["blocks"])


def test_route_dispatches_html_end_to_end():
    rr = router.route("<html><body><p>가</p></body></html>".encode("utf-8"), "a.html",
                      ocr_url="", excel_url="")
    assert rr.kind == "pages" and rr.chunk_needed is True
    assert rr.pages[0]["blocks"]
