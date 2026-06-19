from service.parsing import parse_to_markdown, ParseError, _route


def test_route_uses_recommended_parser():
    assert _route("a.pptx") == "structural"
    assert _route("a.xlsx") == "markitdown"
    assert _route("a.pdf") == "structural"


def test_parse_dispatches_and_returns_markdown(monkeypatch):
    monkeypatch.setattr("service.parsing._parse_structural", lambda b, f, **k: "## H\n<table><tr><td>x</td></tr></table>")
    out = parse_to_markdown(b"bytes", "doc.pptx", ocr_url="http://x", excel_url="http://y")
    assert "<table>" in out and "## H" in out


def test_parse_error_propagates(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("parser down")
    monkeypatch.setattr("service.parsing._parse_structural", boom)
    try:
        parse_to_markdown(b"b", "doc.pdf", ocr_url="http://x", excel_url="http://y")
        assert False
    except ParseError:
        pass
