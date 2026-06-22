"""Facade ``POST /parse`` — routes to parse-svc, hides the parser fleet.

The facade ADDS VALUE (R5): it hides parse-svc behind the stable capability
contract, sanitizes the upload filename (no path traversal reaches the backend),
and returns the consistent ``{enriched_content, n_blocks, modal_spans}`` shape so
consumers never bind to the parsing service directly.
"""
from fastapi.testclient import TestClient

from service.app import app, get_parse_client


class FakeParseClient:
    """Records the parse() call and returns a fixed parse-svc response."""

    def __init__(self):
        self.calls = []

    def parse(self, *, file_bytes, filename, content_type=None):
        self.calls.append({"file_bytes": file_bytes, "filename": filename,
                           "content_type": content_type})
        return {
            "enriched_content": "## H\nbody\n〈MODAL id=\"T1\" type=\"table\"〉d\np〈/MODAL〉",
            "n_blocks": 2,
            "modal_spans": [{"id": "T1", "type": "table", "char_range": [12, 50]}],
        }


def test_parse_forwards_multipart_and_returns_enriched():
    fake = FakeParseClient()
    app.dependency_overrides[get_parse_client] = lambda: fake
    c = TestClient(app)
    r = c.post(
        "/parse",
        files={"file": ("doc.pdf", b"rawbytes", "application/pdf")},
    )
    assert r.status_code == 200
    j = r.json()
    assert j["enriched_content"].startswith("## H")
    assert j["n_blocks"] == 2
    assert j["modal_spans"] == [{"id": "T1", "type": "table", "char_range": [12, 50]}]

    # the raw bytes + filename were forwarded to parse-svc.
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["file_bytes"] == b"rawbytes"
    assert call["filename"] == "doc.pdf"
    app.dependency_overrides.clear()


def test_parse_sanitizes_filename_before_forwarding():
    fake = FakeParseClient()
    app.dependency_overrides[get_parse_client] = lambda: fake
    c = TestClient(app)
    r = c.post(
        "/parse",
        files={"file": ("../../etc/passwd", b"b", "text/plain")},
    )
    assert r.status_code == 200
    # traversal stripped before it ever reaches parse-svc.
    assert fake.calls[0]["filename"] == "passwd"
    app.dependency_overrides.clear()


def test_parse_client_posts_multipart(monkeypatch):
    """ParseSvcClient.parse posts multipart file+filename to parse-svc /parse."""
    from service.parse_client import ParseSvcClient

    captured = {}

    class FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"enriched_content": "x", "n_blocks": 1, "modal_spans": []}

    class FakeHttp:
        def post(self, url, *, files=None, data=None):
            captured["url"] = url
            captured["files"] = files
            captured["data"] = data
            return FakeResp()

    client = ParseSvcClient("http://parse:19001")
    client.http = FakeHttp()
    out = client.parse(file_bytes=b"rawbytes", filename="doc.pdf",
                       content_type="application/pdf")
    assert out == {"enriched_content": "x", "n_blocks": 1, "modal_spans": []}
    assert captured["url"] == "http://parse:19001/parse"
    # multipart file part carries the raw bytes + filename + content_type.
    fname, fbytes, ctype = captured["files"]["file"]
    assert fname == "doc.pdf"
    assert fbytes == b"rawbytes"
    assert ctype == "application/pdf"
    # filename also sent as a form field (parse-svc reads form ``filename``).
    assert captured["data"]["filename"] == "doc.pdf"
