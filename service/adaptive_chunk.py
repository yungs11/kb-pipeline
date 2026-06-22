"""Thin HTTP client for the adaptive_chunk chunking hub (:18060).

The facade ``/chunk`` endpoint uses this client to delegate chunking to the
adaptive_chunk service while keeping the hub hidden behind the facade contract.

The facade always forwards the modal atomic markers (``MODAL_ATOMIC_MARKERS``) so
the hub keeps each ``〈MODAL…〈/MODAL〉`` span as a single atomic chunk (marker-aware
chunking, adaptive_chunk Task 1.1). adaptive_chunk does not know modal *semantics*
— it is told only "these spans are atomic" — so the markers are passed generically.

The hub's ``POST /chunk`` returns the R1 shape::

    {method_selected, scores, methods_compared, chunks:[{doc_name, chunk_index,
     chunk_text, chunk_pages, titles_context, chunk_len}], timing_ms}

Normalization of that shape into the facade contract lives in ``service/app.py``.
"""
from __future__ import annotations

import httpx

#: Modal markers (U+3008/U+3009) passed to adaptive_chunk as atomic regions. JSON
#: cannot carry tuples, so each pair is a 2-element list ``[open, close]``.
MODAL_ATOMIC_MARKERS = [["〈MODAL", "〈/MODAL〉"]]


class AdaptiveChunkClient:
    def __init__(self, base_url: str, timeout: float = 600.0):
        self.base = base_url.rstrip("/")
        self.http = httpx.Client(timeout=timeout)

    def chunk(self, *, text: str, doc_name: str,
              atomic_markers: list | None = None) -> dict:
        """Delegate to adaptive_chunk ``POST /chunk`` and return its R1 response dict.

        ``atomic_markers`` (defaulting to the modal pair) is forwarded so the hub
        preserves marked spans as atomic chunks. The raw R1 response is returned
        unchanged; the facade normalizes it for its own contract.
        """
        if atomic_markers is None:
            atomic_markers = MODAL_ATOMIC_MARKERS
        r = self.http.post(
            f"{self.base}/chunk",
            json={
                "text": text,
                "doc_name": doc_name,
                "atomic_markers": atomic_markers,
            },
        )
        r.raise_for_status()
        return r.json() or {}
