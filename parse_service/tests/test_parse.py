"""parse-svc ``POST /parse`` — parse→blockify→modal, returns enriched + modal spans.

parse-svc owns the heavy parsing dependencies (java/OCR/markitdown/qwen) so the
facade stays light. The ``/parse`` endpoint:
  * routes the upload through the parser (``_safe_basename`` security preserved),
  * blockifies + modal-enriches into one ``enriched_content`` string,
  * reports ``n_blocks`` and ``modal_spans:[{id, type, char_range}]`` so consumers
    know exactly where each 〈MODAL…〈/MODAL〉 atomic region sits.
"""
from fastapi.testclient import TestClient

from kb_pipeline.modal import MODAL_OPEN_PREFIX, MODAL_CLOSE


def test_run_parse_computes_enriched_and_modal_spans(monkeypatch):
    """The core run_parse: parse→blockify→modal, with modal_spans located by
    exact char offset in the enriched content (id/type/char_range)."""
    import parse_service.app as svc

    # A fake parse that yields markdown with one text para and one pipe table.
    md = "## Heading\n\nbody text\n\n| a | b |\n| - | - |\n| 1 | 2 |\n"
    monkeypatch.setattr(svc, "parse_to_markdown", lambda b, f, **k: md)
    # Deterministic table description (no real LLM).
    out = svc.run_parse(
        b"bytes", "doc.pdf",
        text_llm=lambda prompt, payload: "TABLE_DESC",
        vision_llm=None, ocr_url="http://x", excel_url="http://y",
    )

    enriched = out["enriched_content"]
    assert out["n_blocks"] >= 2  # at least the text para + the table block
    spans = out["modal_spans"]
    assert len(spans) == 1
    span = spans[0]
    assert span["type"] == "table"
    assert span["id"]  # modal id present (e.g. "T1")
    # char_range points exactly at the 〈MODAL…〈/MODAL〉 substring in enriched.
    start, end = span["char_range"]
    sub = enriched[start:end]
    assert sub.startswith(MODAL_OPEN_PREFIX)
    assert sub.endswith(MODAL_CLOSE)
    assert "TABLE_DESC" in sub


def test_parse_endpoint_returns_contract(monkeypatch):
    """POST /parse (multipart) -> {enriched_content, n_blocks, modal_spans}."""
    import parse_service.app as svc

    monkeypatch.setattr(
        svc, "run_parse",
        lambda data, filename, **k: {
            "enriched_content": "## H\nbody",
            "n_blocks": 2,
            "modal_spans": [{"id": "T1", "type": "table", "char_range": [10, 30]}],
        },
    )
    c = TestClient(svc.app)
    r = c.post(
        "/parse",
        files={"file": ("doc.pdf", b"bytes", "application/pdf")},
        data={"filename": "doc.pdf"},
    )
    assert r.status_code == 200
    j = r.json()
    assert j["enriched_content"] == "## H\nbody"
    assert j["n_blocks"] == 2
    assert j["modal_spans"] == [{"id": "T1", "type": "table", "char_range": [10, 30]}]


def test_parse_endpoint_uses_safe_basename(monkeypatch):
    """The upload filename is sanitized (no path traversal) before parsing."""
    import parse_service.app as svc

    seen = {}

    def fake_run_parse(data, filename, **k):
        seen["filename"] = filename
        return {"enriched_content": "x", "n_blocks": 1, "modal_spans": []}

    monkeypatch.setattr(svc, "run_parse", fake_run_parse)
    c = TestClient(svc.app)
    r = c.post(
        "/parse",
        files={"file": ("../../etc/passwd", b"b", "text/plain")},
        data={"filename": "../../etc/passwd"},
    )
    assert r.status_code == 200
    # traversal stripped to a safe basename.
    assert seen["filename"] == "passwd"


def test_healthz():
    import parse_service.app as svc

    c = TestClient(svc.app)
    r = c.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
