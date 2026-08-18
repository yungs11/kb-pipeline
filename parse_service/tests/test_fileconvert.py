"""tools/fileconvert — 변환 API 클라이언트(docs/API_FILECONVERT_AGENT.md).

seam 은 `httpx.MockTransport` 다. 모듈 내부 함수 monkeypatch(test_paddle_gw 관례)로는
request 헤더가 관측되지 않아 "POST 에만 Authorization" 계약을 검증할 수 없다.
"""
import httpx
import pytest

from parse_service.tools import ToolError, fileconvert

PDF = b"%PDF-1.4\nbody"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("KBP_FILECONVERT_URL", "http://gw/api/fileconvert/agent/tool")
    monkeypatch.setenv("KBP_FILECONVERT_TOKEN", "T0KEN")
    monkeypatch.delenv("KBP_FILECONVERT_TIMEOUT", raising=False)
    yield
    fileconvert._transport = None


def _wire(monkeypatch, handler):
    seen: list[httpx.Request] = []

    def wrapped(request):
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(fileconvert, "_transport", httpx.MockTransport(wrapped))
    return seen


def _ok(request):
    if request.url.path.endswith("/convert-sync"):
        return httpx.Response(200, json={"success": True, "cnvId": 621,
                                         "fileName": "a.pdf", "message": "완료"})
    return httpx.Response(200, content=PDF)


def test_converts_and_downloads(monkeypatch):
    seen = _wire(monkeypatch, _ok)
    assert fileconvert.convert_to_pdf(b"PPTX", "a.pptx") == PDF
    post, get = seen
    assert post.method == "POST" and post.url.path.endswith("/convert-sync")
    assert get.method == "GET" and get.url.path.endswith("/download/621")


def test_auth_header_on_post_only(monkeypatch):
    """명세 §2.2 — 변환에만 인증, 다운로드는 인증 불필요."""
    seen = _wire(monkeypatch, _ok)
    fileconvert.convert_to_pdf(b"PPTX", "a.pptx")
    post, get = seen
    assert post.headers.get("authorization") == "Bearer T0KEN"
    assert "authorization" not in get.headers


def test_url_unset_raises_without_http(monkeypatch):
    """하드코딩 기본 URL 금지 — 미설정이면 HTTP 를 때리지 않고 즉시 실패한다."""
    monkeypatch.delenv("KBP_FILECONVERT_URL", raising=False)
    seen = _wire(monkeypatch, _ok)
    with pytest.raises(ToolError):
        fileconvert.convert_to_pdf(b"PPTX", "a.pptx")
    assert seen == []


def test_http200_with_errorcode_is_failure(monkeypatch):
    """명세 §2.4 — 오류가 HTTP 200 으로 온다. 상태코드로 판정하면 통과해버린다."""
    _wire(monkeypatch, lambda r: httpx.Response(
        200, json={"errorCode": "E000001", "errorMsg": "필수 파일이 첨부되지 않았습니다.",
                   "data": None}))
    with pytest.raises(ToolError, match="E000001"):
        fileconvert.convert_to_pdf(b"", "a.pptx")


def test_unsupported_ext_422(monkeypatch):
    _wire(monkeypatch, lambda r: httpx.Response(
        422, json={"success": False, "cnvId": None, "message": "변환 제출 실패: 400"}))
    with pytest.raises(ToolError, match="변환 제출 실패"):
        fileconvert.convert_to_pdf(b"x", "a.odt")


def test_download_not_pdf_raises(monkeypatch):
    def h(request):
        if request.url.path.endswith("/convert-sync"):
            return httpx.Response(200, json={"success": True, "cnvId": 9})
        return httpx.Response(500, json={"errorCode": "E000007"})   # 명세 §3.2.3
    _wire(monkeypatch, h)
    with pytest.raises(ToolError, match="PDF"):
        fileconvert.convert_to_pdf(b"PPTX", "a.pptx")


def test_pdf_with_preamble_accepted(monkeypatch):
    """헤더 앞 preamble 이 붙은 PDF — PyMuPDF·ODL 은 연다. startswith 로 죽이면 안 된다."""
    def h(request):
        if request.url.path.endswith("/convert-sync"):
            return httpx.Response(200, json={"success": True, "cnvId": 3})
        return httpx.Response(200, content=b"junk\r\n" + PDF)
    _wire(monkeypatch, h)
    assert fileconvert.convert_to_pdf(b"PPTX", "a.pptx").endswith(b"body")


def test_token_not_in_error_message(monkeypatch):
    _wire(monkeypatch, lambda r: httpx.Response(200, json={"errorCode": "E1", "errorMsg": "x"}))
    with pytest.raises(ToolError) as ei:
        fileconvert.convert_to_pdf(b"x", "a.pptx")
    assert "T0KEN" not in str(ei.value)


@pytest.mark.parametrize("filename,expected", [
    ("a.hwp", False), ("A.HWP", False), ("a.hwpx", False), ("a.docx", False),
    ("a.doc", True), ("a.ppt", True), ("a.pptx", True),
    ("a.pdf", False), ("a.xlsx", False), ("a.png", False), ("a.txt", False),
    ("a.odt", False), ("upload", False), ("a.tar.gz", False),
])
def test_needs_convert(filename, expected):
    assert fileconvert.needs_convert(filename) is expected


@pytest.mark.parametrize("filename,expected", [
    ("a.hwp", "a.pdf"), ("A.HWP", "A.pdf"), ("a.tar.gz", "a.tar.pdf"),
    ("upload", "upload.pdf"), ("_.hwp", "_.pdf"),
    ("a.", "a..pdf"),        # ext_of("a.") == "" → else 가지. 마지막 확장자만 보므로 무해.
])
def test_swap_ext_pdf(filename, expected):
    assert fileconvert.swap_ext_pdf(filename) == expected
