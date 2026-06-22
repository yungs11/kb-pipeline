"""Thin HTTP client for the adaptive_chunk chunking hub (:18060).

The facade ``/chunk`` endpoint uses this client to delegate chunking to the
adaptive_chunk service while keeping the hub hidden behind the facade contract.

ASYNC path: the hub's synchronous ``POST /chunk`` has a token size cap (413 for
large inputs) and, once the slow methods are enabled (``llm_regex`` via an LLM,
``semantic`` via a reranker), a single chunking can take minutes. So we submit an
async job (``POST /chunk/jobs``) and poll ``GET /chunk/jobs/{id}`` until terminal
— no size cap, no sync timeout. The facade ``chunk()`` call still blocks until the
job finishes (the caller's timeout must cover it; kb_pipeline_timeout=600s).

The facade always forwards the modal atomic markers (``MODAL_ATOMIC_MARKERS``) so
the hub keeps each ``〈MODAL…〈/MODAL〉`` span as a single atomic chunk (marker-aware
chunking). adaptive_chunk does not know modal *semantics* — it is told only "these
spans are atomic" — so the markers are passed generically, inside ``options``
(``ChunkOptionsModel.atomic_markers``; NOT a top-level field).

The job ``result`` is the hub's R1 shape::

    {method_selected, scores, methods_compared, chunks:[{doc_name, chunk_index,
     chunk_text, chunk_pages, titles_context, chunk_len}], timing_ms}

Normalization of that shape into the facade contract lives in ``service/app.py``.
"""
from __future__ import annotations

import time

import httpx

#: Modal markers (U+3008/U+3009) passed to adaptive_chunk as atomic regions. JSON
#: cannot carry tuples, so each pair is a 2-element list ``[open, close]``.
MODAL_ATOMIC_MARKERS = [["〈MODAL", "〈/MODAL〉"]]

#: Terminal job states (adaptive_chunk service/jobs.py JobStatus).
_OK = "succeeded"
_FAIL = ("failed", "cancelled")


class AdaptiveChunkClient:
    def __init__(self, base_url: str, timeout: float = 600.0,
                 poll_timeout: float = 1800.0, poll_interval: float = 3.0):
        self.base = base_url.rstrip("/")
        self.http = httpx.Client(timeout=timeout)
        self.poll_timeout = poll_timeout
        self.poll_interval = poll_interval

    def chunk(self, *, text: str, doc_name: str,
              atomic_markers: list | None = None) -> dict:
        """Delegate to adaptive_chunk ASYNC job and return its R1 result dict.

        Submits ``POST /chunk/jobs`` (atomic_markers forwarded inside ``options``)
        then polls ``GET /chunk/jobs/{id}`` until terminal. On success returns the
        raw R1 ``result`` (the facade normalizes it); on failure/timeout raises.
        """
        if atomic_markers is None:
            atomic_markers = MODAL_ATOMIC_MARKERS

        # 1) submit async job — no sync size cap; slow llm_regex/semantic safe.
        r = self.http.post(
            f"{self.base}/chunk/jobs",
            json={
                "text": text,
                "doc_name": doc_name,
                "options": {"atomic_markers": atomic_markers},
            },
        )
        r.raise_for_status()
        job_id = (r.json() or {}).get("job_id")
        if not job_id:
            raise RuntimeError("adaptive_chunk POST /chunk/jobs returned no job_id")

        # 2) poll until a terminal job state.
        deadline = time.monotonic() + self.poll_timeout
        while True:
            t = self.http.get(f"{self.base}/chunk/jobs/{job_id}")
            t.raise_for_status()
            tj = t.json() or {}
            status = (tj.get("status") or "").lower()
            if status == _OK:
                return tj.get("result") or {}
            if status in _FAIL:
                raise RuntimeError(
                    f"adaptive_chunk job {status}: {tj.get('error')}"
                )
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"adaptive_chunk job poll timeout after "
                    f"{self.poll_timeout:.0f}s (last={status})"
                )
            time.sleep(self.poll_interval)
