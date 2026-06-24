"""parse-svc ``POST /parse`` — parse→blockify→modal, returns enriched + modal spans.

parse-svc owns the heavy parsing dependencies (java/OCR/markitdown/qwen) so the
facade stays light. The ``/parse`` endpoint:
  * routes the upload through the parser (``_safe_basename`` security preserved),
  * blockifies + modal-enriches into one ``enriched_content`` string,
  * reports ``n_blocks`` and ``modal_spans:[{id, type, char_range}]`` so consumers
    know exactly where each 〈MODAL…〈/MODAL〉 atomic region sits.
"""
from fastapi.testclient import TestClient

from kb_pipeline.blockify import hybrid_to_blocks
from kb_pipeline.modal import MODAL_OPEN_PREFIX, MODAL_CLOSE


def _fake_pages_from_md(md: str, page_number: int = 1):
    """Inject a fake page parser yielding one PageDoc whose blocks carry page_idx.

    ``run_parse`` now uses the page-preserving parser (``parse_to_pages``) instead
    of ``parse_to_markdown``. Tests inject this via ``parse_pages=`` so no live
    Java/OpenDataLoader/OCR is touched.
    """
    def parse_pages(file_bytes, filename, **k):
        return [{
            "page_number": page_number,
            "blocks": hybrid_to_blocks(md, page_idx=page_number),
        }]
    return parse_pages


def _no_render(_file_bytes):
    """Inject an empty renderer so run_parse touches no PyMuPDF/minio."""
    return []


def test_run_parse_emits_timing_metrics():
    """P2 모니터링: run_parse 가 timing_metrics(parse/modal/render 단계 + 모달 LLM 분해 +
    카운터)를 additive 로 낸다 — 집계자가 파서 내부 단계 소요를 읽는다."""
    import parse_service.app as svc

    md = "## Heading\n\nbody text\n\n| a | b |\n| - | - |\n| 1 | 2 |\n"
    out = svc.run_parse(
        b"bytes", "doc.pdf",
        text_llm=lambda prompt, payload: "TABLE_DESC",
        vision_llm=None, ocr_url="http://x", excel_url="http://y",
        parse_pages=_fake_pages_from_md(md), render=_no_render, minio=None,
    )
    tm = out["timing_metrics"]
    for k in ("parse_ms", "modal_enrich_ms", "render_upload_ms"):
        assert isinstance(tm[k], float) and tm[k] >= 0.0
    assert tm["counters"]["n_blocks"] == out["n_blocks"]
    assert tm["counters"].get("table", 0) >= 1  # 표 1개 카운트
    ml = tm["modal_llm"]
    assert ml["calls"] >= 1  # 표 모달 LLM 1콜
    assert ml["by_type"]["table"]["n"] >= 1
    assert isinstance(ml["wall_ms"], float)


def test_run_parse_computes_enriched_and_modal_spans():
    """The core run_parse: parse→blockify→modal, with modal_spans located by
    exact char offset in the enriched content (id/type/char_range)."""
    import parse_service.app as svc

    # A fake page parser that yields markdown with one text para and one pipe table.
    md = "## Heading\n\nbody text\n\n| a | b |\n| - | - |\n| 1 | 2 |\n"
    # Deterministic table description (no real LLM).
    out = svc.run_parse(
        b"bytes", "doc.pdf",
        text_llm=lambda prompt, payload: "TABLE_DESC",
        vision_llm=None, ocr_url="http://x", excel_url="http://y",
        parse_pages=_fake_pages_from_md(md), render=_no_render, minio=None,
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


def test_run_parse_strips_pua_garbage():
    """OpenDataLoader 의 U+F000 깨진 글자가 enriched_content 에서 제거된다.

    PUA 제거는 이제 평탄화 전 **블록 텍스트 단계**에서 일어난다(spec §5.1.5).
    """
    import parse_service.app as svc

    md = "휴가결근 신청서\n\n\n| a | b |\n| - | - |\n| 1 | 2 |\n"
    out = svc.run_parse(
        b"x", "d.pdf",
        text_llm=lambda p, pl: "DESC", vision_llm=None,
        ocr_url="x", excel_url="y",
        parse_pages=_fake_pages_from_md(md), render=_no_render, minio=None,
    )
    assert "" not in out["enriched_content"]
    assert "휴가결근 신청서" in out["enriched_content"]


def test_modal_span_covers_absorbed_title_and_footnote():
    """제목·각주 흡수 후에도 modal_spans char_range 가 확장 span 전체를 가리킨다."""
    import json
    import parse_service.app as svc

    # text 단락 + 파이프표 + text 각주.
    md = "캡션줄\n\n| a | b |\n| - | - |\n| 1 | 2 |\n\n각주줄\n"

    out = svc.run_parse(
        b"x", "d.pdf",
        text_llm=lambda prompt, payload: json.dumps(
            {"summary": "요약", "title_count": 1, "footnote_count": 1}
        ),
        vision_llm=None, ocr_url="http://x", excel_url="http://y",
        parse_pages=_fake_pages_from_md(md), render=_no_render, minio=None,
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


# ---------------------------------------------------------------------------
# spec §7 — parse_to_pages / enrich_with_spans / render+upload / additive resp
# ---------------------------------------------------------------------------


class _FakeMinio:
    """Captures put_page_image calls; mirrors MinioStore key scheme. No network."""

    def __init__(self):
        self.puts: list[tuple[str, str, bytes]] = []

    @staticmethod
    def page_image_object_key(docs_id, page_uuid):
        return f"{docs_id}/{page_uuid}.jpeg"

    def put_page_image(self, docs_id, page_uuid, jpeg_bytes):
        self.puts.append((docs_id, page_uuid, jpeg_bytes))
        return self.page_image_object_key(docs_id, page_uuid)


class _RP:
    """Stand-in for pdf_pages.RenderedPage (page_number, jpeg, text)."""

    def __init__(self, page_number, jpeg=b"\xff\xd8jpeg\xff\xd9", text=""):
        self.page_number = page_number
        self.jpeg = jpeg
        self.text = text


def test_parse_to_pages_digital_pdf_fills_page_idx_per_page(monkeypatch):
    """디지털 PDF: OpenDataLoader 페이지별 .md(join 안 함) → 각 PageDoc 의 blocks 가
    그 페이지의 1-based page_idx 로 채워진다(spec §5.1.3 / §7-①)."""
    import parse_service.parsing as p

    page_mds = [
        "# Page One\n\nalpha body\n",
        "## Page Two\n\nbeta body\n\n| a | b |\n| - | - |\n| 1 | 2 |\n",
    ]

    # OpenDataLoader convert + per-page .md glob 을 fake 로 대체(JVM/디스크 미사용).
    # ``_parse_pdf_to_pages`` 는 함수 내부에서 ``import opendataloader_pdf`` / ``import glob``
    # 하므로 stdlib ``glob`` 모듈과 sys.modules 의 opendataloader_pdf 를 패치한다.
    import glob as _glob_mod
    import io as _io
    import sys as _sys
    import types as _t

    monkeypatch.setitem(_sys.modules, "opendataloader_pdf",
                        _t.SimpleNamespace(convert=lambda **k: None))
    monkeypatch.setattr(_glob_mod, "glob", lambda *a, **k: ["p1.md", "p2.md"])
    _files = dict(zip(["p1.md", "p2.md"], page_mds))
    real_open = open
    monkeypatch.setattr(
        "builtins.open",
        lambda f, *a, **k: (
            _io.StringIO(_files[f]) if f in _files else real_open(f, *a, **k)
        ),
    )

    pages = p.parse_to_pages(b"%PDF-1.7 fake", "doc.pdf",
                             ocr_url="http://x", excel_url="http://y")

    assert [pd["page_number"] for pd in pages] == [1, 2]
    # 각 PageDoc 의 모든 블록 page_idx == 그 PageDoc 의 page_number (1-based).
    for pd in pages:
        assert pd["blocks"], "page should produce at least one block"
        assert all(b["page_idx"] == pd["page_number"] for b in pd["blocks"])
    # page 2 에 표 블록이 있어야 한다.
    p2 = next(pd for pd in pages if pd["page_number"] == 2)
    assert any(b["type"] == "table" for b in p2["blocks"])


def test_parse_to_pages_single_image_ocr_elements(monkeypatch):
    """단일 이미지: OCR raw elements 보존 → elements_to_blocks(page=1) (spec §7-①)."""
    import parse_service.parsing as p

    # OCR(:18050) 를 호출하지 않고 raw elements 를 직접 주입.
    elements = [
        {"category": "title", "content": {"text": "Scanned Title"}},
        {"category": "paragraph", "content": {"text": "scanned body line"}},
    ]
    monkeypatch.setattr(p, "_ocr_page", lambda b, f, *, ocr_url: elements)

    pages = p.parse_to_pages(b"\xff\xd8img\xff\xd9", "scan.png",
                             ocr_url="http://x", excel_url="http://y")

    assert len(pages) == 1
    assert pages[0]["page_number"] == 1
    assert all(b["page_idx"] == 1 for b in pages[0]["blocks"])
    assert any("scanned body line" in (b.get("text") or "") for b in pages[0]["blocks"])


def test_run_parse_page_spans_align_to_enriched_content():
    """page_spans char 범위가 enriched_content 슬라이스와 정합(spec §7-②).

    두 페이지(각각 text 블록)를 주입 → page_spans 가 페이지별 [start,end) 를 정확히 가리키고
    enriched[start:end] 가 그 페이지 텍스트를 포함한다."""
    import parse_service.app as svc

    def parse_pages(file_bytes, filename, **k):
        return [
            {"page_number": 1, "blocks": [
                {"type": "text", "text": "PAGE ONE BODY", "page_idx": 1}]},
            {"page_number": 2, "blocks": [
                {"type": "text", "text": "PAGE TWO BODY", "page_idx": 2}]},
        ]

    out = svc.run_parse(
        b"x", "doc.pdf",
        text_llm=lambda p, pl: "DESC", vision_llm=None,
        ocr_url="http://x", excel_url="http://y",
        parse_pages=parse_pages, render=_no_render, minio=None,
    )

    enriched = out["enriched_content"]
    spans = {s["page_number"]: s for s in out["page_spans"]}
    assert set(spans) == {1, 2}
    s1, s2 = spans[1], spans[2]
    # 슬라이스 정합: enriched[char_start:char_end] 가 해당 페이지 본문을 포함.
    assert "PAGE ONE BODY" in enriched[s1["char_start"]:s1["char_end"]]
    assert "PAGE TWO BODY" in enriched[s2["char_start"]:s2["char_end"]]
    # 비중첩·문서순.
    assert s1["char_end"] <= s2["char_start"]
    assert s1["char_start"] == 0


def test_run_parse_renders_and_uploads_with_locked_key_scheme():
    """PDF render+upload 키가 {docs_id}/{docs_id}_{p}.jpeg 규칙(spec §7-④)."""
    import parse_service.app as svc

    fake_minio = _FakeMinio()

    def parse_pages(file_bytes, filename, **k):
        return [{"page_number": 1, "blocks": [
            {"type": "text", "text": "body", "page_idx": 1}]}]

    out = svc.run_parse(
        b"%PDF-1.7", "doc.pdf",
        text_llm=lambda p, pl: "DESC", vision_llm=None,
        ocr_url="http://x", excel_url="http://y",
        docs_id="ab12cd34ef560000",
        minio=fake_minio,
        parse_pages=parse_pages,
        render=lambda b: [_RP(1), _RP(2), _RP(3)],
    )

    assert out["docs_id"] == "ab12cd34ef560000"
    assert out["page_count"] == 3
    # pages[] keys + locked minio_object scheme.
    assert [pg["page_number"] for pg in out["pages"]] == [1, 2, 3]
    for pg in out["pages"]:
        p = pg["page_number"]
        assert pg["page_uuid"] == f"ab12cd34ef560000_{p}"
        assert pg["minio_object"] == f"ab12cd34ef560000/ab12cd34ef560000_{p}.jpeg"
    # upload called once per page with the locked page_uuid.
    assert [u[1] for u in fake_minio.puts] == [
        "ab12cd34ef560000_1", "ab12cd34ef560000_2", "ab12cd34ef560000_3",
    ]


def test_run_parse_additive_response_keys_and_alignment():
    """응답이 기존 키(enriched_content/n_blocks/modal_spans) + 신규 키(docs_id/
    page_count/pages/page_spans) 를 모두 포함하고, page_spans 가 enriched 와 정합."""
    import parse_service.app as svc

    fake_minio = _FakeMinio()

    def parse_pages(file_bytes, filename, **k):
        return [
            {"page_number": 1, "blocks": [
                {"type": "text", "text": "intro on page one", "page_idx": 1}]},
            {"page_number": 2, "blocks": [
                {"type": "text", "text": "table caption", "page_idx": 2},
                {"type": "table", "table_body": "<table><tr><td>x</td></tr></table>",
                 "table_caption": [], "page_idx": 2}]},
        ]

    out = svc.run_parse(
        b"%PDF-1.7", "doc.pdf",
        text_llm=lambda p, pl: "TBL_DESC", vision_llm=None,
        ocr_url="http://x", excel_url="http://y",
        docs_id="deadbeefdeadbeef",
        minio=fake_minio,
        parse_pages=parse_pages,
        render=lambda b: [_RP(1), _RP(2)],
    )

    # additive: all original + new keys present.
    for key in ("enriched_content", "n_blocks", "modal_spans",
                "docs_id", "page_count", "pages", "page_spans"):
        assert key in out, f"missing response key {key}"

    enriched = out["enriched_content"]
    # pages[] keys align to the docs_id/page scheme.
    assert {pg["page_number"] for pg in out["pages"]} == {1, 2}
    for pg in out["pages"]:
        assert set(pg) == {"page_number", "page_uuid", "minio_object"}

    # page_spans align to enriched_content slices.
    spans = {s["page_number"]: s for s in out["page_spans"]}
    assert set(spans) == {1, 2}
    assert "intro on page one" in enriched[spans[1]["char_start"]:spans[1]["char_end"]]
    # page 2 span covers the modal (table description / payload).
    sub2 = enriched[spans[2]["char_start"]:spans[2]["char_end"]]
    assert MODAL_OPEN_PREFIX in sub2 and MODAL_CLOSE in sub2


def test_default_docs_id_is_content_hash_prefix():
    """docs_id 폴백 = content_hash(file_bytes)[:16] = sha256 hex prefix(spec §3)."""
    import hashlib
    import parse_service.app as svc

    data = b"the quick brown fox"
    expect = hashlib.sha256(data).hexdigest()[:16]
    assert svc._default_docs_id(data) == expect

    # run_parse 가 docs_id 미전달 시 폴백을 쓴다.
    out = svc.run_parse(
        data, "doc.pdf",
        text_llm=lambda p, pl: "DESC", vision_llm=None,
        ocr_url="http://x", excel_url="http://y",
        parse_pages=lambda b, f, **k: [
            {"page_number": 1, "blocks": [
                {"type": "text", "text": "body", "page_idx": 1}]}],
        render=_no_render, minio=None,
    )
    assert out["docs_id"] == expect
