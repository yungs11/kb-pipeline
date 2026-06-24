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

    def parse(self, *, file_bytes, filename, content_type=None, docs_id=None):
        self.calls.append({"file_bytes": file_bytes, "filename": filename,
                           "content_type": content_type, "docs_id": docs_id})
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
    # docs_id NOT supplied → not in the form (parse-svc derives the fallback).
    assert "docs_id" not in captured["data"]


def test_parse_client_forwards_docs_id_form_field():
    """ParseSvcClient.parse sends ``docs_id`` as a form field when supplied so the
    page-image MinIO keys agree between parse-svc and the orchestrator."""
    from service.parse_client import ParseSvcClient

    captured = {}

    class FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            # additive page fields passed through unchanged.
            return {
                "enriched_content": "x", "n_blocks": 1, "modal_spans": [],
                "docs_id": "ab12cd34ef56ab78", "page_count": 2,
                "pages": [
                    {"page_number": 1, "page_uuid": "ab12cd34ef56ab78_1",
                     "minio_object": "ab12cd34ef56ab78/ab12cd34ef56ab78_1.jpeg"},
                ],
                "page_spans": [{"page_number": 1, "char_start": 0, "char_end": 10}],
            }

    class FakeHttp:
        def post(self, url, *, files=None, data=None):
            captured["data"] = data
            return FakeResp()

    client = ParseSvcClient("http://parse:19001")
    client.http = FakeHttp()
    out = client.parse(file_bytes=b"raw", filename="doc.pdf",
                       content_type="application/pdf",
                       docs_id="ab12cd34ef56ab78")
    assert captured["data"]["docs_id"] == "ab12cd34ef56ab78"
    assert captured["data"]["filename"] == "doc.pdf"
    # passthrough: page fields surface unchanged from parse-svc.
    assert out["docs_id"] == "ab12cd34ef56ab78"
    assert out["page_count"] == 2
    assert out["pages"][0]["minio_object"] == \
        "ab12cd34ef56ab78/ab12cd34ef56ab78_1.jpeg"
    assert out["page_spans"] == [{"page_number": 1, "char_start": 0, "char_end": 10}]


def test_is_excel():
    from service.app import _is_excel
    assert _is_excel("a.xlsx") and _is_excel("A.XLSM") and _is_excel("b.xls")
    assert not _is_excel("a.pdf") and not _is_excel("noext")


def test_parse_routes_excel_to_excel_client(monkeypatch):
    from fastapi.testclient import TestClient
    import service.app as svc

    class _FakeExcel:
        def parse_chunks(self, *, file_bytes, filename):
            return [{"chunk_index": 0, "text": "셀A", "titles_context": ["시트1"], "pages": []}]

    svc.app.dependency_overrides[svc.get_excel_client] = lambda: _FakeExcel()
    try:
        c = TestClient(svc.app)
        r = c.post("/parse", files={"file": ("book.xlsx", b"PK\x03\x04", "application/octet-stream")},
                   data={})
        assert r.status_code == 200
        j = r.json()
        assert j["chunk_strategy"] == "excel_rag_parser"
        assert j["chunks"][0]["text"] == "셀A"
        assert j["modal_spans"] == []
    finally:
        svc.app.dependency_overrides.pop(svc.get_excel_client, None)


def test_parse_routes_nonexcel_to_parse_svc(monkeypatch):
    from fastapi.testclient import TestClient
    import service.app as svc

    class _FakeParse:
        def parse(self, *, file_bytes, filename, content_type=None, docs_id=None):
            return {"enriched_content": "본문", "n_blocks": 1, "modal_spans": []}

    svc.app.dependency_overrides[svc.get_parse_client] = lambda: _FakeParse()
    try:
        c = TestClient(svc.app)
        r = c.post("/parse", files={"file": ("doc.pdf", b"%PDF", "application/pdf")}, data={})
        assert r.status_code == 200
        j = r.json()
        assert "chunks" not in j and j["enriched_content"] == "본문"
    finally:
        svc.app.dependency_overrides.pop(svc.get_parse_client, None)


def test_parse_forwards_docs_id_and_returns_page_fields():
    """The facade forwards the optional ``docs_id`` form to parse-svc and passes
    the additive page fields (docs_id/page_count/pages/page_spans) back unchanged."""

    class _PageParse:
        def __init__(self):
            self.calls = []

        def parse(self, *, file_bytes, filename, content_type=None, docs_id=None):
            self.calls.append({"docs_id": docs_id, "filename": filename})
            return {
                "enriched_content": "본문", "n_blocks": 1, "modal_spans": [],
                "docs_id": docs_id, "page_count": 3,
                "pages": [
                    {"page_number": 1, "page_uuid": "deadbeef_1",
                     "minio_object": "deadbeef/deadbeef_1.jpeg"},
                    {"page_number": 2, "page_uuid": "deadbeef_2",
                     "minio_object": "deadbeef/deadbeef_2.jpeg"},
                    {"page_number": 3, "page_uuid": "deadbeef_3",
                     "minio_object": "deadbeef/deadbeef_3.jpeg"},
                ],
                "page_spans": [
                    {"page_number": 1, "char_start": 0, "char_end": 40},
                    {"page_number": 2, "char_start": 40, "char_end": 90},
                ],
            }

    pp = _PageParse()
    app.dependency_overrides[get_parse_client] = lambda: pp
    try:
        c = TestClient(app)
        r = c.post(
            "/parse",
            files={"file": ("doc.pdf", b"%PDF", "application/pdf")},
            data={"docs_id": "deadbeef00000000"},
        )
        assert r.status_code == 200
        j = r.json()
        # docs_id form reached the parse client.
        assert pp.calls[0]["docs_id"] == "deadbeef00000000"
        # additive page fields surfaced verbatim.
        assert j["docs_id"] == "deadbeef00000000"
        assert j["page_count"] == 3
        assert len(j["pages"]) == 3
        assert j["pages"][0]["minio_object"] == "deadbeef/deadbeef_1.jpeg"
        assert j["page_spans"][1] == {"page_number": 2, "char_start": 40, "char_end": 90}
    finally:
        app.dependency_overrides.clear()


def test_parse_without_docs_id_forwards_none():
    """When no ``docs_id`` form field is sent, the facade forwards ``docs_id=None``
    so parse-svc derives its own fallback (key-rule agreement still holds)."""
    fake = FakeParseClient()
    app.dependency_overrides[get_parse_client] = lambda: fake
    try:
        c = TestClient(app)
        r = c.post("/parse", files={"file": ("doc.pdf", b"raw", "application/pdf")})
        assert r.status_code == 200
        assert fake.calls[0]["docs_id"] is None
    finally:
        app.dependency_overrides.clear()


def test_parse_excel_branch_has_no_page_fields():
    """The Excel branch (Feature 1) is untouched: it never returns page fields and
    never forwards docs_id to a parse client."""
    import service.app as svc

    class _FakeExcel:
        def parse_chunks(self, *, file_bytes, filename):
            return [{"chunk_index": 0, "text": "셀A", "titles_context": ["시트1"], "pages": []}]

    class _SpyParse:
        def __init__(self):
            self.calls = []

        def parse(self, *, file_bytes, filename, content_type=None, docs_id=None):
            self.calls.append(docs_id)
            return {"enriched_content": "x", "n_blocks": 0, "modal_spans": []}

    spy = _SpyParse()
    svc.app.dependency_overrides[svc.get_excel_client] = lambda: _FakeExcel()
    svc.app.dependency_overrides[svc.get_parse_client] = lambda: spy
    try:
        c = TestClient(svc.app)
        r = c.post(
            "/parse",
            files={"file": ("book.xlsx", b"PK\x03\x04", "application/octet-stream")},
            data={"docs_id": "shouldbeignored0"},
        )
        assert r.status_code == 200
        j = r.json()
        # Excel response shape is unchanged — no page fields injected.
        assert j["chunk_strategy"] == "excel_rag_parser"
        for key in ("docs_id", "page_count", "pages", "page_spans"):
            assert key not in j, f"Excel branch must not emit page field {key!r}"
        # the parse client was never invoked for the Excel upload.
        assert spy.calls == []
    finally:
        svc.app.dependency_overrides.pop(svc.get_excel_client, None)
        svc.app.dependency_overrides.pop(svc.get_parse_client, None)
