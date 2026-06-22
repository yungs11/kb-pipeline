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


def test_strip_pua_removes_private_use_chars():
    from parse_service.app import _strip_pua
    assert _strip_pua("휴가규정(개정)") == "휴가규정(개정)"
    assert _strip_pua("섞임") == "섞임"
    assert _strip_pua("normal text 한글") == "normal text 한글"


def test_run_parse_strips_pua_garbage(monkeypatch):
    """OpenDataLoader 의 U+F000 깨진 글자가 enriched_content 에서 제거된다."""
    import parse_service.app as svc

    md = "휴가결근 신청서\n\n\n| a | b |\n| - | - |\n| 1 | 2 |\n"
    monkeypatch.setattr(svc, "parse_to_markdown", lambda b, f, **k: md)
    out = svc.run_parse(
        b"x", "d.pdf",
        text_llm=lambda p, pl: "DESC", vision_llm=None,
        ocr_url="x", excel_url="y",
    )
    assert "" not in out["enriched_content"]
    assert "휴가결근 신청서" in out["enriched_content"]


def test_modal_span_covers_absorbed_title_and_footnote(monkeypatch):
    """제목·각주 흡수 후에도 modal_spans char_range 가 확장 span 전체를 가리킨다."""
    import json
    import parse_service.app as svc

    # text 단락 + 파이프표 + text 각주.
    md = "캡션줄\n\n| a | b |\n| - | - |\n| 1 | 2 |\n\n각주줄\n"
    monkeypatch.setattr(svc, "parse_to_markdown", lambda b, f, **k: md)

    out = svc.run_parse(
        b"x", "d.pdf",
        text_llm=lambda prompt, payload: json.dumps(
            {"summary": "요약", "title_count": 1, "footnote_count": 1}
        ),
        vision_llm=None, ocr_url="http://x", excel_url="http://y",
    )
    enriched = out["enriched_content"]
    spans = out["modal_spans"]
    assert len(spans) == 1
    start, end = spans[0]["char_range"]
    sub = enriched[start:end]
    assert sub.startswith(MODAL_OPEN_PREFIX) and sub.endswith(MODAL_CLOSE)
    assert "요약" in sub          # 요약이 span 안
    assert "각주줄" in sub         # 흡수된 각주가 span 안
    # 흡수된 각주는 enriched 전체에서 1회만(외부 중복 0)
    assert enriched.count("각주줄") == 1


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
