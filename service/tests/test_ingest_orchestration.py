"""Facade ``POST /ingest`` — end-to-end orchestration (parse→chunk→insert).

The v2 ``/ingest`` is the one-shot path for consumers that don't want to drive the
phases themselves: it calls the three capabilities in order (parse-svc → chunk hub
→ edgequake passthrough insert) and returns the stable
``{document_id, chunk_count, status, chunking_selection}`` contract — including the
REAL chunking selection rationale (method_selected/scores/methods_compared) so the
one-shot path is not lossier than the step-by-step path.
"""
from fastapi.testclient import TestClient

from service.app import (
    app, get_parse_client, get_adaptive_chunk, get_edgequake,
)
from service.adaptive_chunk import MODAL_ATOMIC_MARKERS


EQ_WS = "99999999-9999-9999-9999-999999999999"


class FakeParseClient:
    def __init__(self):
        self.calls = []

    def parse(self, *, file_bytes, filename, content_type=None):
        self.calls.append({"filename": filename, "content_type": content_type})
        return {
            "enriched_content": "## H\nbody\n〈MODAL id=\"T1\" type=\"table\"〉d\np〈/MODAL〉",
            "n_blocks": 2,
            "modal_spans": [{"id": "T1", "type": "table", "char_range": [12, 50]}],
        }


class FakeAdaptiveChunk:
    def __init__(self):
        self.calls = []

    def chunk(self, *, text, doc_name, atomic_markers):
        self.calls.append({"text": text, "doc_name": doc_name,
                           "atomic_markers": atomic_markers})
        return {
            "method_selected": "semantic",
            "scores": {"sc": 0.9, "avg": 0.8},
            "methods_compared": [
                {"method": "semantic", "avg": 0.8, "selected": True},
                {"method": "recursive", "avg": 0.6, "selected": False},
            ],
            "chunks": [
                {"chunk_index": 0, "chunk_text": "body", "chunk_pages": [1],
                 "titles_context": "## H"},
                {"chunk_index": 1, "chunk_text": "〈MODAL id=\"T1\" type=\"table\"〉d\np〈/MODAL〉",
                 "chunk_pages": [1], "titles_context": "## H"},
            ],
            "timing_ms": 9.0,
        }


class FakeEq:
    def __init__(self):
        self.ensured = []
        self.inserted = None

    def ensure_workspace(self, kb_id, name, tenant_id="00000000-0000-0000-0000-000000000002"):
        self.ensured.append((kb_id, name))
        return EQ_WS

    def insert_chunks(self, *, workspace_id, tenant_id, title, chunk_texts):
        assert workspace_id == EQ_WS
        self.inserted = {"workspace_id": workspace_id, "title": title,
                         "chunk_texts": list(chunk_texts)}
        return {"document_id": "d1", "chunk_count": 2, "status": "indexed"}


def test_ingest_orchestrates_parse_chunk_insert_in_order():
    pc, ac, eq = FakeParseClient(), FakeAdaptiveChunk(), FakeEq()
    app.dependency_overrides[get_parse_client] = lambda: pc
    app.dependency_overrides[get_adaptive_chunk] = lambda: ac
    app.dependency_overrides[get_edgequake] = lambda: eq
    c = TestClient(app)

    r = c.post(
        "/ingest",
        data={"workspace_id": "kb1", "doc_id": "dc"},
        files={"file": ("doc.pdf", b"rawbytes", "application/pdf")},
    )
    assert r.status_code == 200
    j = r.json()

    # final contract: document_id/chunk_count/status + REAL selection rationale.
    assert j["document_id"] == "d1"
    assert j["chunk_count"] == 2
    assert j["status"] == "indexed"
    assert j["chunking_selection"] == {
        "method_selected": "semantic",
        "scores": {"sc": 0.9, "avg": 0.8},
        "methods_compared": [
            {"method": "semantic", "avg": 0.8, "selected": True},
            {"method": "recursive", "avg": 0.6, "selected": False},
        ],
    }

    # 1) parse-svc was called with the upload (sanitized filename).
    assert pc.calls[0]["filename"] == "doc.pdf"
    # 2) the chunk hub got the enriched content + modal atomic markers.
    assert len(ac.calls) == 1
    assert ac.calls[0]["text"].startswith("## H")
    assert ac.calls[0]["atomic_markers"] == MODAL_ATOMIC_MARKERS
    # 3) edgequake got the kb id resolved + the chunk TEXTS (not the hub schema).
    assert eq.ensured == [("kb1", "kb1")]
    assert eq.inserted["chunk_texts"] == [
        "body", "〈MODAL id=\"T1\" type=\"table\"〉d\np〈/MODAL〉",
    ]
    assert eq.inserted["title"] == "dc"

    app.dependency_overrides.clear()
