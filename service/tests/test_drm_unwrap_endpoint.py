"""Facade ``POST /drm/unwrap`` — 순수 프록시(잡 큐 미경유, docs/REFERENCE_DRM해제_API.md).

kb-backend 가 provider 분기 이전 신호추출 단계에서 raw 파일을 못 열 때(DRM 암호화)
재시도용으로 쓴다. `/parse` 와 달리 job 경유가 아니다 — 가벼운 호출이라 유량제어
대상이 아니다.
"""
from fastapi.testclient import TestClient

from service.app import app, get_parse_client


class FakeParseClient:
    def __init__(self, *, content=b"decrypted", raises=False):
        self.calls = []
        self._content = content
        self._raises = raises

    def unwrap_drm(self, *, file_bytes, filename):
        self.calls.append({"file_bytes": file_bytes, "filename": filename})
        if self._raises:
            raise RuntimeError("parse-svc unreachable")
        return self._content


def test_drm_unwrap_forwards_and_returns_bytes():
    fake = FakeParseClient(content=b"plaintext-pdf-bytes")
    app.dependency_overrides[get_parse_client] = lambda: fake
    try:
        c = TestClient(app)
        r = c.post("/drm/unwrap",
                   files={"file": ("a.pdf", b"drm-wrapped-bytes", "application/pdf")})
        assert r.status_code == 200
        assert r.content == b"plaintext-pdf-bytes"
        assert fake.calls[0]["file_bytes"] == b"drm-wrapped-bytes"
        assert fake.calls[0]["filename"] == "a.pdf"
    finally:
        app.dependency_overrides.clear()


def test_drm_unwrap_sanitizes_filename():
    fake = FakeParseClient()
    app.dependency_overrides[get_parse_client] = lambda: fake
    try:
        c = TestClient(app)
        c.post("/drm/unwrap", files={"file": ("../../etc/passwd", b"b", "text/plain")})
        assert fake.calls[0]["filename"] == "passwd"
    finally:
        app.dependency_overrides.clear()


def test_drm_unwrap_upstream_failure_becomes_502():
    fake = FakeParseClient(raises=True)
    app.dependency_overrides[get_parse_client] = lambda: fake
    try:
        c = TestClient(app)
        r = c.post("/drm/unwrap", files={"file": ("a.pdf", b"x", "application/pdf")})
        assert r.status_code == 502
    finally:
        app.dependency_overrides.clear()


def test_parse_client_unwrap_drm_posts_multipart():
    from service.parse_client import ParseSvcClient

    captured = {}

    class FakeResp:
        status_code = 200
        content = b"decrypted-bytes"

        def raise_for_status(self):
            pass

    class FakeHttp:
        def post(self, url, *, files=None, data=None):
            captured["url"] = url
            captured["files"] = files
            captured["data"] = data
            return FakeResp()

    client = ParseSvcClient("http://parse:19001")
    client.http = FakeHttp()
    out = client.unwrap_drm(file_bytes=b"raw", filename="a.pdf")
    assert out == b"decrypted-bytes"
    assert captured["url"] == "http://parse:19001/drm/unwrap"
    fname, fbytes, ctype = captured["files"]["file"]
    assert (fname, fbytes) == ("a.pdf", b"raw")
