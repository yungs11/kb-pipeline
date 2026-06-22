"""Facade ``POST /chunk`` — adaptive_chunk hub hidden, selection rationale normalized.

The facade ADDS VALUE (R5): it (a) hides the adaptive_chunk hub behind a stable
contract, (b) passes the modal markers as ``atomic_markers`` so the chunker keeps
each 〈MODAL…〈/MODAL〉 span atomic, and (c) normalizes the chunker's R1 chunk schema
(``chunk_text``/``chunk_pages``) into the facade contract (``text``/``pages``) while
surfacing the real selection rationale (method_selected/scores/methods_compared).
"""
from fastapi.testclient import TestClient

from service.app import app, get_adaptive_chunk
from service.adaptive_chunk import MODAL_ATOMIC_MARKERS


class FakeAdaptiveChunk:
    """Records the chunk() call and returns a fixed adaptive_chunk R1 response."""

    def __init__(self):
        self.calls = []

    def chunk(self, *, text, doc_name, atomic_markers):
        self.calls.append({"text": text, "doc_name": doc_name,
                           "atomic_markers": atomic_markers})
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
    assert j["chunks"] == [
        {"chunk_index": 0, "text": "alpha", "titles_context": "## H", "pages": [1]},
        {"chunk_index": 1, "text": "〈MODAL id=\"x\"〉TBL〈/MODAL〉",
         "titles_context": "## H2", "pages": [2]},
    ]

    # the enriched content + modal atomic markers were forwarded to the hub.
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["text"] == body["enriched_content"]
    assert call["doc_name"] == "d"
    assert call["atomic_markers"] == MODAL_ATOMIC_MARKERS
    # markers are exactly the modal open/close pair (U+3008/U+3009).
    assert MODAL_ATOMIC_MARKERS == [["〈MODAL", "〈/MODAL〉"]]

    app.dependency_overrides.clear()
