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
              atomic_markers: list | None = None,
              page_spans: list | None = None,
              pages: list | None = None,
              methods: list | None = None,
              skip_scoring: bool = False,
              llm_regex_pattern: str | None = None) -> dict:
        """Delegate to adaptive_chunk ASYNC job and return its R1 result dict.

        Submits ``POST /chunk/jobs`` (atomic_markers forwarded inside ``options``)
        then polls ``GET /chunk/jobs/{id}`` until terminal. On success returns the
        raw R1 ``result`` (the facade normalizes it); on failure/timeout raises.

        ``page_spans`` (``[{page_number, char_start, char_end}]``, char offsets in
        ``text`` = enriched_content) and the optional ``pages``
        (``[{page_number, markdown}]``) are additive: when supplied they are placed
        in the job body so adaptive can attribute a ``chunk_pages`` to every chunk
        (and join the page method). When omitted the body is unchanged (regression).

        Chunk-method selection (passthrough to ``options``; adaptive_chunk owns the
        validation/semantics):
          * ``methods`` — restrict the chunker to these method keys
            (``recursive_1100``/``recursive_600``/``page``/``llm_regex``/``semantic``);
            ``None`` = auto (every method competes, then best is scored/selected).
          * ``skip_scoring`` — when ``True`` the hub skips the scoring competition
            and uses the single given method directly (requires exactly one method).
          * ``llm_regex_pattern`` — a user-supplied regex; the hub uses it verbatim
            instead of generating one via the LLM (requires ``methods==['llm_regex']``).

        These three fields are placed in ``options`` ONLY when non-default
        (``methods is not None`` / ``skip_scoring is True`` / ``llm_regex_pattern is
        not None``). With all defaults (auto) the body is byte-identical to the
        legacy request, so the auto path is unchanged (regression / backward compat).
        """
        if atomic_markers is None:
            atomic_markers = MODAL_ATOMIC_MARKERS

        # 1) submit async job — no sync size cap; slow llm_regex/semantic safe.
        options: dict = {"atomic_markers": atomic_markers}
        # method-selection passthrough — omit defaults so auto stays byte-identical.
        if methods is not None:
            options["methods"] = methods
        if skip_scoring:
            options["skip_scoring"] = skip_scoring
        if llm_regex_pattern is not None:
            options["llm_regex_pattern"] = llm_regex_pattern
        body: dict = {
            "text": text,
            "doc_name": doc_name,
            "options": options,
        }
        if page_spans is not None:
            body["page_spans"] = page_spans
        if pages is not None:
            body["pages"] = pages
        r = self.http.post(
            f"{self.base}/chunk/jobs",
            json=body,
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
