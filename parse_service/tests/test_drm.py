"""tools/drm — DRM 해제 API 클라이언트(docs/REFERENCE_DRM해제_API.md).

seam 은 `httpx.MockTransport` 다(test_fileconvert.py 관례와 동일 이유 — 모듈 내부
함수 monkeypatch 로는 request 헤더가 관측되지 않는다).
"""
import httpx
import pytest

from parse_service.tools import ToolError, drm

PLAIN = b"%PDF-1.3\nplaintext body"
DRM_WRAPPED = b"\x9b\x20" + b"DRMONE" + b"  This Document is encrypted and protected"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("KBP_DRM_URL", "http://gw/api/drm/agent/tool")
    monkeypatch.setenv("KBP_DRM_TOKEN", "T0KEN")
    monkeypatch.delenv("KBP_DRM_TIMEOUT", raising=False)
    yield
    drm._transport = None


def _wire(monkeypatch, handler):
    seen: list[httpx.Request] = []

    def wrapped(request):
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(drm, "_transport", httpx.MockTransport(wrapped))
    return seen


def test_unpack_success(monkeypatch):
    seen = _wire(monkeypatch, lambda r: httpx.Response(200, content=PLAIN))
    assert drm.unpack(DRM_WRAPPED, "a.pdf") == PLAIN
    (post,) = seen
    assert post.method == "POST" and post.url.path.endswith("/unpack")


def test_auth_header_present(monkeypatch):
    seen = _wire(monkeypatch, lambda r: httpx.Response(200, content=PLAIN))
    drm.unpack(DRM_WRAPPED, "a.pdf")
    (post,) = seen
    assert post.headers.get("authorization") == "Bearer T0KEN"


def test_url_unset_raises_without_http(monkeypatch):
    """하드코딩 기본 URL 금지 — 미설정이면 HTTP 를 때리지 않고 즉시 실패한다."""
    monkeypatch.delenv("KBP_DRM_URL", raising=False)
    seen = _wire(monkeypatch, lambda r: httpx.Response(200, content=PLAIN))
    with pytest.raises(ToolError):
        drm.unpack(DRM_WRAPPED, "a.pdf")
    assert seen == []


@pytest.mark.parametrize("status", [400, 401, 422, 500])
def test_error_status_raises(monkeypatch, status):
    """명세 §1 응답표 — 성공만 200, 그 외는 실패로 판정한다."""
    _wire(monkeypatch, lambda r: httpx.Response(status))
    with pytest.raises(ToolError):
        drm.unpack(DRM_WRAPPED, "a.pdf")


def test_token_not_in_error_message(monkeypatch):
    _wire(monkeypatch, lambda r: httpx.Response(422))
    with pytest.raises(ToolError) as ei:
        drm.unpack(DRM_WRAPPED, "a.pdf")
    assert "T0KEN" not in str(ei.value)


def test_non_drm_fallback_passthrough(monkeypatch):
    """명세 — DRM 파일이 아니면 서버가 입력을 그대로 반환한다(폴백)."""
    _wire(monkeypatch, lambda r: httpx.Response(200, content=PLAIN))
    assert drm.unpack(PLAIN, "a.pdf") == PLAIN


@pytest.mark.parametrize("body,expected", [
    (DRM_WRAPPED, True),
    (PLAIN, False),
    (b"", False),
    (b"\x9b\x20DRMONE", True),
])
def test_is_drm(body, expected):
    assert drm.is_drm(body) is expected


def test_endpoint_unwraps_drm_file(monkeypatch):
    """`POST /drm/unwrap` — DRM 파일이면 해제된 바이트를 반환한다."""
    from fastapi.testclient import TestClient
    import parse_service.app as svc

    monkeypatch.setattr(svc.drm, "unpack", lambda fb, fn: PLAIN)
    c = TestClient(svc.app)
    r = c.post("/drm/unwrap", files={"file": ("a.pdf", DRM_WRAPPED, "application/pdf")})
    assert r.status_code == 200
    assert r.content == PLAIN


def test_endpoint_echoes_non_drm_file_without_remote_call(monkeypatch):
    """DRM 아닌 파일은 원격 호출 없이 원본을 그대로 echo 한다."""
    from fastapi.testclient import TestClient
    import parse_service.app as svc

    called = []
    monkeypatch.setattr(svc.drm, "unpack", lambda fb, fn: called.append(fn) or b"x")
    c = TestClient(svc.app)
    r = c.post("/drm/unwrap", files={"file": ("a.pdf", PLAIN, "application/pdf")})
    assert r.status_code == 200
    assert r.content == PLAIN
    assert called == []


def test_endpoint_upstream_failure_becomes_502(monkeypatch):
    from fastapi.testclient import TestClient
    import parse_service.app as svc

    def boom(fb, fn):
        raise ToolError("gateway down")
    monkeypatch.setattr(svc.drm, "unpack", boom)
    c = TestClient(svc.app)
    r = c.post("/drm/unwrap", files={"file": ("a.pdf", DRM_WRAPPED, "application/pdf")})
    assert r.status_code == 502
