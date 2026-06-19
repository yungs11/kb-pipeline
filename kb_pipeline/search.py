"""W5 Unified search over the edgequake knowledge base.

Two retrieval modes, one router:

  * ``local_search``  — a *specific-fact* lookup. Delegates to edgequake's
    ``POST /api/v1/query`` in ``hybrid`` mode, scoped to a single workspace via
    the SAME ``x-workspace-id`` / ``x-tenant-id`` header mechanism that the
    ingest path used (``TenantContext::from_headers``). edgequake performs the
    vector+graph merge server-side and returns a grounded answer plus the cited
    chunks/entities.

  * ``global_search`` — a *whole-corpus* question ("summarize", "what topics").
    Delegates to W3 ``kb_pipeline.community.global_query`` (community-report
    map-reduce). If no reports exist yet for the workspace, they are built first
    via ``community.build_workspace_communities``.

  * ``route``         — a cheap classifier deciding local vs global. Heuristic
    cue words first; an optional tiny ``llm`` call only as a tie-breaker. Both
    are injectable so the dispatcher can be unit-tested with no network.

  * ``unified_search`` — route → local_search | global_search →
    ``{mode, answer, sources, workspace_id}``.

Workspace isolation is enforced at the application level: the workspace header
constrains edgequake retrieval to that workspace's physical vector table
(``eq_eq_default_ws_<short8>_vectors``), and community reports are stored /
queried with a ``workspace_id`` filter. A ws-A fact asked while scoped to ws-B
must therefore NOT surface ws-A content.
"""

from __future__ import annotations

import os
import re
from typing import Callable, Optional

from . import community

#: An LLM callable: (system_prompt, user_prompt) -> completion text.
LLM = Callable[[str, str], str]

# --------------------------------------------------------------------------- #
# Defaults — mirror community.py so a standalone job needs no wiring.
# --------------------------------------------------------------------------- #
DEFAULT_DSN = community.DEFAULT_DSN
DEFAULT_MODEL = community.DEFAULT_MODEL
DEFAULT_ENV_PATH = community.DEFAULT_ENV_PATH

#: edgequake API base. The query endpoint is ``{base}/api/v1/query``.
DEFAULT_EDGEQUAKE_URL = "http://localhost:8080"

#: The Default tenant both prep workspaces live under (see prep notes / STEP 3).
DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000002"

#: HTTP timeout for the (real-qwen) edgequake query call. qwen extraction is
#: slow; a single hybrid query with generation can take tens of seconds.
DEFAULT_QUERY_TIMEOUT = 180.0

# --------------------------------------------------------------------------- #
# 1. local_search — edgequake hybrid query scoped to one workspace.
# --------------------------------------------------------------------------- #
def local_search(
    question: str,
    workspace_id: str,
    *,
    tenant_id: str = DEFAULT_TENANT_ID,
    base_url: str = DEFAULT_EDGEQUAKE_URL,
    mode: str = "hybrid",
    max_results: Optional[int] = None,
    timeout: float = DEFAULT_QUERY_TIMEOUT,
    http_post: Optional[Callable] = None,
) -> dict:
    """Run a workspace-scoped hybrid query against edgequake.

    The workspace is selected with the ``x-workspace-id`` header (and the
    matching ``x-tenant-id``), exactly as ``TenantContext::from_headers``
    expects — this is the SAME mechanism the ingest path used, so retrieval is
    confined to that workspace's vector table. The vector+graph merge happens
    edgequake-side; we just relay the answer and the cited sources.

    :param http_post: optional injectable ``(url, json, headers, timeout) ->
        response`` for tests (defaults to ``httpx.post``). The response object
        must expose ``.raise_for_status()`` and ``.json()``.
    :returns: ``{answer, sources, mode, workspace_id, stats, raw}`` where
        ``sources`` is the list of cited chunk/entity references.
    """
    url = base_url.rstrip("/") + "/api/v1/query"
    payload = {
        "query": question,
        "mode": mode,
        "include_references": True,
    }
    if max_results is not None:
        payload["max_results"] = max_results

    headers = {
        "Content-Type": "application/json",
        "x-workspace-id": workspace_id,
        "x-tenant-id": tenant_id,
    }

    if http_post is None:
        import httpx

        def http_post(url, json, headers, timeout):  # noqa: A002 - mirror httpx
            return httpx.post(url, json=json, headers=headers, timeout=timeout)

    resp = http_post(url=url, json=payload, headers=headers, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()

    return {
        "answer": data.get("answer", "") or "",
        "sources": data.get("sources", []) or [],
        "mode": data.get("mode", mode) or mode,
        "workspace_id": workspace_id,
        "stats": data.get("stats", {}) or {},
        "raw": data,
    }


# --------------------------------------------------------------------------- #
# 2. global_search — community-report map-reduce (W3), build-if-missing.
# --------------------------------------------------------------------------- #
def _reports_exist(workspace_id: str, dsn: str, *, level: int = 0) -> bool:
    """Return True if at least one community report is stored for the workspace.

    A missing ``community_reports`` table (UndefinedTable) counts as "no reports"
    rather than an error, so a first-time global query transparently builds them.
    """
    import psycopg

    try:
        with psycopg.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM public.community_reports "
                "WHERE workspace_id = %s AND level = %s LIMIT 1",
                (workspace_id, level),
            )
            return cur.fetchone() is not None
    except psycopg.errors.UndefinedTable:
        return False
    except psycopg.Error:
        # Be conservative: if we cannot tell, assume none and let the build run.
        return False


def global_search(
    question: str,
    workspace_id: str,
    *,
    llm: LLM,
    dsn: str = DEFAULT_DSN,
    top_k: int = 5,
    level: int = 0,
    resolution: float = 1.0,
    build_if_missing: bool = True,
) -> dict:
    """Whole-corpus QA via W3 community-report map-reduce.

    If reports for ``workspace_id`` are missing and ``build_if_missing`` is set,
    ``community.build_workspace_communities`` is run first (fetch graph →
    Louvain → per-community report → store), then ``community.global_query``
    does the map-reduce over the stored reports.

    :returns: ``{answer, sources, mode, workspace_id}`` where ``sources`` is the
        list of community ids that contributed to the synthesized answer.
    """
    if build_if_missing and not _reports_exist(workspace_id, dsn, level=level):
        community.build_workspace_communities(
            workspace_id,
            llm=llm,
            dsn=dsn,
            resolution=resolution,
            level=level,
        )

    result = community.global_query(
        question,
        workspace_id,
        llm=llm,
        dsn=dsn,
        top_k=top_k,
        level=level,
    )
    return {
        "answer": result.get("answer", "") or "",
        "sources": result.get("communities_used", []) or [],
        "mode": "global",
        "workspace_id": workspace_id,
    }


# --------------------------------------------------------------------------- #
# 3. route — cheap local/global classifier (heuristic + optional tiny qwen).
# --------------------------------------------------------------------------- #
#: Global cue words. Presence of any of these strongly implies a whole-corpus,
#: summary-style question rather than a single-fact lookup.
GLOBAL_CUES = (
    "요약",
    "전체",
    "종류",
    "핵심",
    "개요",
    "무엇을 다루",
    "주제",
    "전반",
    "통틀어",
    "정리해",
    "한눈에",
    "summary",
    "summarize",
    "overview",
    "overall",
    "main topic",
    "key topics",
)

_ROUTE_SYSTEM = (
    "You are a query router for a document QA system. Classify the user's "
    "question as exactly one word: GLOBAL if it asks for a whole-document "
    "summary / overview / list of themes; LOCAL if it asks for a specific fact, "
    "number, clause, or detail. Reply with only GLOBAL or LOCAL."
)


def route(
    question: str,
    *,
    llm: Optional[LLM] = None,
) -> str:
    """Classify a question as ``"local"`` or ``"global"``.

    Strategy (cheap-first):
      1. If any global cue word appears in the question → ``"global"`` (no LLM).
      2. Else, if an ``llm`` is injected, ask it for a one-word GLOBAL/LOCAL
         verdict and honor a clear GLOBAL.
      3. Otherwise default to ``"local"`` (specific-fact lookup is the common
         case and the safer, cheaper default).

    The ``llm`` is optional + injectable so unit tests run with no network.
    """
    q = (question or "").lower()
    for cue in GLOBAL_CUES:
        if cue.lower() in q:
            return "global"

    if llm is not None:
        try:
            verdict = llm(_ROUTE_SYSTEM, question).strip().upper()
        except Exception:
            verdict = ""
        if "GLOBAL" in verdict:
            return "global"
        if "LOCAL" in verdict:
            return "local"

    return "local"


# --------------------------------------------------------------------------- #
# 4. unified_search — route → local|global → uniform envelope.
# --------------------------------------------------------------------------- #
def unified_search(
    question: str,
    workspace_id: str,
    *,
    llm: LLM,
    dsn: str = DEFAULT_DSN,
    tenant_id: str = DEFAULT_TENANT_ID,
    base_url: str = DEFAULT_EDGEQUAKE_URL,
    top_k: int = 5,
    level: int = 0,
    local_fn: Callable = local_search,
    global_fn: Callable = global_search,
    route_fn: Callable = route,
) -> dict:
    """Route a question then dispatch to local or global search.

    ``local_fn`` / ``global_fn`` / ``route_fn`` are injectable so the dispatch
    logic is unit-testable without any network or DB. The returned envelope is
    uniform across both modes: ``{mode, answer, sources, workspace_id}``.
    """
    mode = route_fn(question, llm=llm)

    if mode == "global":
        result = global_fn(
            question,
            workspace_id,
            llm=llm,
            dsn=dsn,
            top_k=top_k,
            level=level,
        )
    else:
        result = local_fn(
            question,
            workspace_id,
            tenant_id=tenant_id,
            base_url=base_url,
        )

    return {
        "mode": mode,
        "answer": result.get("answer", "") or "",
        "sources": result.get("sources", []) or [],
        "workspace_id": workspace_id,
    }


# --------------------------------------------------------------------------- #
# 5. CLI / smoke entrypoint.
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="W5 unified search.")
    parser.add_argument("question")
    parser.add_argument("--workspace-id", default=community.WORKSPACE_ID)
    parser.add_argument("--tenant-id", default=DEFAULT_TENANT_ID)
    parser.add_argument("--base-url", default=DEFAULT_EDGEQUAKE_URL)
    parser.add_argument("--dsn", default=DEFAULT_DSN)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--env-path", default=DEFAULT_ENV_PATH)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument(
        "--mode",
        choices=["auto", "local", "global"],
        default="auto",
        help="force a mode, or 'auto' to let route() decide.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    llm = community.make_openrouter_llm(model=args.model, env_path=args.env_path)

    if args.mode == "auto":
        result = unified_search(
            args.question,
            args.workspace_id,
            llm=llm,
            dsn=args.dsn,
            tenant_id=args.tenant_id,
            base_url=args.base_url,
            top_k=args.top_k,
        )
    elif args.mode == "global":
        result = global_search(
            args.question, args.workspace_id, llm=llm, dsn=args.dsn, top_k=args.top_k
        )
        result["mode"] = "global"
    else:
        result = local_search(
            args.question,
            args.workspace_id,
            tenant_id=args.tenant_id,
            base_url=args.base_url,
        )
        result["mode"] = "local"

    print(f"[mode] {result['mode']}")
    print(f"[sources] {result.get('sources')}")
    print(f"[answer]\n{result['answer']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
