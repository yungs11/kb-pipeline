"""Facade ``POST /chunk`` — adaptive_chunk hub hidden, selection rationale normalized.

The facade ADDS VALUE (R5): it (a) hides the adaptive_chunk hub behind a stable
contract, (b) passes the modal markers as ``atomic_markers`` so the chunker keeps
each 〈MODAL…〈/MODAL〉 span atomic, and (c) normalizes the chunker's R1 chunk schema
(``chunk_text``/``chunk_pages``) into the facade contract (``text``/``pages``) while
surfacing the real selection rationale (method_selected/scores/methods_compared).
"""
from fastapi.testclient import TestClient

from service.app import app, get_adaptive_chunk, _strip_modal
from service.edgequake import _strip_modal as _strip_modal_eq
from service.adaptive_chunk import MODAL_ATOMIC_MARKERS


def test_strip_modal_removes_only_markers_preserving_content():
    """_strip_modal 은 원자경계 마커만 제거하고 내부(제목+raw table HTML+각주)는 보존한다.
    facade 두 지점(app/edgequake)의 헬퍼가 동일 동작이어야 한다."""
    s = '〈MODAL id="T1" type="table"〉제목\n<table><tr><td>x</td></tr></table>※각주〈/MODAL〉'
    expected = "제목\n<table><tr><td>x</td></tr></table>※각주"
    assert _strip_modal(s) == expected
    assert _strip_modal_eq(s) == expected               # 두 헬퍼 동일 동작
    # 마커 없는 평문은 무변경.
    assert _strip_modal("plain text") == "plain text"
    # 여러 마커.
    two = "〈MODAL id=\"a\"〉A〈/MODAL〉 mid 〈MODAL id=\"b\"〉B〈/MODAL〉"
    assert _strip_modal(two) == "A mid B"


class FakeAdaptiveChunk:
    """Records the chunk() call and returns a fixed adaptive_chunk R1 response."""

    def __init__(self):
        self.calls = []

    def chunk(self, *, text, doc_name, atomic_markers,
              page_spans=None, pages=None, blocks=None,
              methods=None, skip_scoring=False, llm_regex_pattern=None):
        self.calls.append({"text": text, "doc_name": doc_name,
                           "atomic_markers": atomic_markers,
                           "page_spans": page_spans, "pages": pages,
                           "blocks": blocks,
                           "methods": methods, "skip_scoring": skip_scoring,
                           "llm_regex_pattern": llm_regex_pattern})
        # adaptive_chunk R1 shape (runner.run_chunk): chunks carry chunk_text/chunk_pages.
        return {
            "method_selected": "semantic",
            "scores": {"sc": 0.9, "avg": 0.8},
            "methods_compared": [
                {"method": "semantic", "avg": 0.8, "metrics": {"sc": 0.9}, "selected": True},
                {"method": "recursive", "avg": 0.6, "metrics": {"sc": 0.5}, "selected": False},
            ],
            "chunks": [
                {"doc_name": "d", "chunk_index": 0, "chunk_text": "alpha",
                 "chunk_pages": [1], "titles_context": "## H", "chunk_len": 5},
                {"doc_name": "d", "chunk_index": 1, "chunk_text": "〈MODAL id=\"x\"〉TBL〈/MODAL〉",
                 "chunk_pages": [2], "titles_context": "## H2", "chunk_len": 20},
            ],
            "timing_ms": 12.3,
        }


def test_chunk_normalizes_response_and_passes_modal_markers():
    fake = FakeAdaptiveChunk()
    app.dependency_overrides[get_adaptive_chunk] = lambda: fake
    c = TestClient(app)
    body = {"enriched_content": "## H\nalpha\n〈MODAL id=\"x\"〉TBL〈/MODAL〉", "doc_name": "d"}
    r = c.post("/chunk", json=body)
    assert r.status_code == 200
    j = r.json()

    # selection rationale surfaced verbatim (real method/scores/comparison).
    assert j["method_selected"] == "semantic"
    assert j["scores"] == {"sc": 0.9, "avg": 0.8}
    assert len(j["methods_compared"]) == 2
    assert j["methods_compared"][0]["selected"] is True

    # chunks normalized: chunk_text->text, chunk_pages->pages; index/titles preserved.
    # 응답 text 는 표시싱크라 마커 스트립됨(〈MODAL…〉TBL〈/MODAL〉 → "TBL"). 내부 내용 보존.
    assert j["chunks"] == [
        {"chunk_index": 0, "text": "alpha", "titles_context": "## H", "pages": [1]},
        {"chunk_index": 1, "text": "TBL",
         "titles_context": "## H2", "pages": [2]},
    ]

    # the enriched content + modal atomic markers were forwarded to the hub.
    # 청킹 INPUT(call["text"]) 은 마커 유지 — 원자화에 필요(스트립은 응답/저장싱크만).
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["text"] == body["enriched_content"]
    assert call["doc_name"] == "d"
    assert call["atomic_markers"] == MODAL_ATOMIC_MARKERS
    # markers are exactly the modal open/close pair (U+3008/U+3009).
    assert MODAL_ATOMIC_MARKERS == [["〈MODAL", "〈/MODAL〉"]]
    # regression: no page_spans/pages in the body → forwarded as None (unchanged).
    assert call["page_spans"] is None
    assert call["pages"] is None
    # regression: no method-selection fields → auto defaults forwarded (unchanged).
    assert call["methods"] is None
    assert call["skip_scoring"] is False
    assert call["llm_regex_pattern"] is None

    app.dependency_overrides.clear()


def test_chunk_response_strips_modal_markers_but_keeps_content():
    """표시싱크 배선: 마커든 chunk_text → /chunk 응답 text 는 마커 0, 내부(제목+표+각주)
    보존. 청킹 INPUT(call["text"]) 은 마커 유지 — 미래 수정이 마커 재노출 못하게 고정."""
    fake = FakeAdaptiveChunk()

    def _resp(**kw):
        fake.calls.append(kw)
        return {
            "method_selected": "recursive", "scores": {}, "methods_compared": [],
            "chunks": [
                {"chunk_index": 0,
                 "chunk_text": '〈MODAL id="T1" type="table"〉제목줄\n<table><tr><td>y</td></tr></table>※각주줄〈/MODAL〉',
                 "chunk_pages": [1], "titles_context": None},
            ],
        }

    fake.chunk = _resp  # type: ignore[assignment]
    app.dependency_overrides[get_adaptive_chunk] = lambda: fake
    c = TestClient(app)
    body = {"enriched_content": '〈MODAL id="T1" type="table"〉제목줄〈/MODAL〉', "doc_name": "d"}
    r = c.post("/chunk", json=body)
    assert r.status_code == 200
    text = r.json()["chunks"][0]["text"]
    assert "〈MODAL" not in text and "〈/MODAL〉" not in text
    assert "제목줄" in text and "<table><tr><td>y</td></tr></table>" in text and "※각주줄" in text
    # 청킹 INPUT 은 마커 유지(원자화용).
    assert fake.calls[0]["text"] == body["enriched_content"]

    app.dependency_overrides.clear()


def test_chunk_forwards_page_spans_and_pages():
    """The facade forwards the optional ``page_spans`` (+ ``pages``) body fields to
    the adaptive hub so every chunk can be page-attributed; normalization unchanged."""
    fake = FakeAdaptiveChunk()
    app.dependency_overrides[get_adaptive_chunk] = lambda: fake
    c = TestClient(app)
    page_spans = [
        {"page_number": 1, "char_start": 0, "char_end": 5},
        {"page_number": 2, "char_start": 5, "char_end": 30},
    ]
    pages = [
        {"page_number": 1, "markdown": "## H\nalpha"},
        {"page_number": 2, "markdown": "〈MODAL id=\"x\"〉TBL〈/MODAL〉"},
    ]
    body = {
        "enriched_content": "## H\nalpha\n〈MODAL id=\"x\"〉TBL〈/MODAL〉",
        "doc_name": "d",
        "page_spans": page_spans,
        "pages": pages,
    }
    r = c.post("/chunk", json=body)
    assert r.status_code == 200
    # response normalization (chunk_pages->pages) still holds.
    j = r.json()
    assert j["chunks"][0]["pages"] == [1]
    assert j["chunks"][1]["pages"] == [2]

    # the additive fields were forwarded verbatim to the hub.
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["page_spans"] == page_spans
    assert call["pages"] == pages
    assert call["atomic_markers"] == MODAL_ATOMIC_MARKERS

    app.dependency_overrides.clear()


def test_chunk_forwards_page_spans_without_pages():
    """``page_spans`` may be sent without ``pages`` (pages stays None)."""
    fake = FakeAdaptiveChunk()
    app.dependency_overrides[get_adaptive_chunk] = lambda: fake
    c = TestClient(app)
    page_spans = [{"page_number": 1, "char_start": 0, "char_end": 10}]
    body = {"enriched_content": "## H\nalpha", "doc_name": "d",
            "page_spans": page_spans}
    r = c.post("/chunk", json=body)
    assert r.status_code == 200
    call = fake.calls[0]
    assert call["page_spans"] == page_spans
    assert call["pages"] is None

    app.dependency_overrides.clear()


def test_chunk_forwards_method_selection_fields():
    """The facade forwards ``methods``/``skip_scoring``/``llm_regex_pattern`` body
    fields to the adaptive hub verbatim (chunk-method selection passthrough, B2)."""
    fake = FakeAdaptiveChunk()
    app.dependency_overrides[get_adaptive_chunk] = lambda: fake
    c = TestClient(app)
    body = {
        "enriched_content": "## H\nalpha",
        "doc_name": "d",
        "methods": ["recursive_600"],
        "skip_scoring": True,
    }
    r = c.post("/chunk", json=body)
    assert r.status_code == 200
    call = fake.calls[0]
    assert call["methods"] == ["recursive_600"]
    assert call["skip_scoring"] is True
    assert call["llm_regex_pattern"] is None
    # other passthrough untouched.
    assert call["atomic_markers"] == MODAL_ATOMIC_MARKERS

    app.dependency_overrides.clear()


def test_chunk_forwards_llm_regex_pattern():
    """``llm_regex_pattern`` (with ``methods==['llm_regex']``) is forwarded so the
    hub uses the user-supplied regex instead of generating one via the LLM."""
    fake = FakeAdaptiveChunk()
    app.dependency_overrides[get_adaptive_chunk] = lambda: fake
    c = TestClient(app)
    body = {
        "enriched_content": "제1조 ...\n제2조 ...",
        "doc_name": "d",
        "methods": ["llm_regex"],
        "skip_scoring": True,
        "llm_regex_pattern": r"제\d+조",
    }
    r = c.post("/chunk", json=body)
    assert r.status_code == 200
    call = fake.calls[0]
    assert call["methods"] == ["llm_regex"]
    assert call["skip_scoring"] is True
    assert call["llm_regex_pattern"] == r"제\d+조"

    app.dependency_overrides.clear()


def test_adaptive_client_includes_method_fields_in_job_body():
    """AdaptiveChunkClient.chunk puts methods/skip_scoring/llm_regex_pattern into
    the /chunk/jobs ``options`` only when non-default; otherwise options is
    unchanged (byte-identical regression / backward compat)."""
    from service.adaptive_chunk import AdaptiveChunkClient

    class FakeResp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    class FakeHttp:
        def __init__(self):
            self.posts = []

        def post(self, url, *, json=None):
            self.posts.append(json)
            return FakeResp({"job_id": "j1"})

        def get(self, url):
            return FakeResp({"status": "succeeded", "result": {"chunks": []}})

    # with method fields -> present in the submit body options.
    client = AdaptiveChunkClient("http://adaptive:18060")
    client.http = FakeHttp()
    client.chunk(text="abc", doc_name="d",
                 methods=["recursive_600"], skip_scoring=True,
                 llm_regex_pattern=r"제\d+조")
    opts = client.http.posts[0]["options"]
    assert opts["methods"] == ["recursive_600"]
    assert opts["skip_scoring"] is True
    assert opts["llm_regex_pattern"] == r"제\d+조"
    assert opts["atomic_markers"] == MODAL_ATOMIC_MARKERS

    # defaults (auto) -> options carries ONLY atomic_markers (byte-identical to old).
    client2 = AdaptiveChunkClient("http://adaptive:18060")
    client2.http = FakeHttp()
    client2.chunk(text="abc", doc_name="d")
    opts2 = client2.http.posts[0]["options"]
    assert set(opts2.keys()) == {"atomic_markers"}
    assert "methods" not in opts2
    assert "skip_scoring" not in opts2
    assert "llm_regex_pattern" not in opts2

    # methods=None but skip_scoring True alone still serialized (explicit non-default).
    client3 = AdaptiveChunkClient("http://adaptive:18060")
    client3.http = FakeHttp()
    client3.chunk(text="abc", doc_name="d", methods=["page"], skip_scoring=True)
    opts3 = client3.http.posts[0]["options"]
    assert opts3["methods"] == ["page"]
    assert opts3["skip_scoring"] is True
    assert "llm_regex_pattern" not in opts3


def test_adaptive_client_includes_page_fields_in_job_body():
    """AdaptiveChunkClient.chunk puts page_spans/pages into the /chunk/jobs body
    only when supplied; otherwise the body is unchanged (regression)."""
    from service.adaptive_chunk import AdaptiveChunkClient

    class FakeResp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    class FakeHttp:
        def __init__(self):
            self.posts = []

        def post(self, url, *, json=None):
            self.posts.append(json)
            return FakeResp({"job_id": "j1"})

        def get(self, url):
            return FakeResp({"status": "succeeded", "result": {"chunks": []}})

    # with page fields -> present in the submit body.
    client = AdaptiveChunkClient("http://adaptive:18060")
    client.http = FakeHttp()
    page_spans = [{"page_number": 1, "char_start": 0, "char_end": 3}]
    pages = [{"page_number": 1, "markdown": "abc"}]
    client.chunk(text="abc", doc_name="d", page_spans=page_spans, pages=pages)
    submitted = client.http.posts[0]
    assert submitted["page_spans"] == page_spans
    assert submitted["pages"] == pages
    assert submitted["options"]["atomic_markers"] == MODAL_ATOMIC_MARKERS

    # without page fields -> keys absent (regression: body shape unchanged).
    client2 = AdaptiveChunkClient("http://adaptive:18060")
    client2.http = FakeHttp()
    client2.chunk(text="abc", doc_name="d")
    submitted2 = client2.http.posts[0]
    assert "page_spans" not in submitted2
    assert "pages" not in submitted2
    assert set(submitted2.keys()) == {"text", "doc_name", "options"}
