"""kb_pipeline — front-end stages of the KB pipeline.

W0 Blockify (blockify.py): hybrid "markdown + inline HTML table" -> block list,
plus VLM/OCR ``elements[]`` mapping to the same block schema (SoT 5.1).

W2 Modal enrichment (modal.py): walk blocks, describe table/image/equation blocks
with injected LLM callables, inline each as one atomic 〈MODAL〉…〈/MODAL〉 marker
into a single enriched content string (SoT 3.3 / 3.4).

W3 Community reports (community.py): read the AGE graph for a workspace, Louvain-
partition into communities, render each as Entities/Relationships CSV, run the
ported GraphRAG community-report prompt through qwen (OpenRouter), and upsert into
public.community_reports; plus map-reduce global_query over the stored reports.
"""

from .blockify import hybrid_to_blocks, elements_to_blocks
from .modal import enrich, MODAL_OPEN_PREFIX, MODAL_CLOSE
from .community import (
    fetch_graph,
    build_communities,
    generate_report,
    store_reports,
    build_workspace_communities,
    global_query,
    make_openrouter_llm,
)

__all__ = [
    "hybrid_to_blocks",
    "elements_to_blocks",
    "enrich",
    "MODAL_OPEN_PREFIX",
    "MODAL_CLOSE",
    # W3 community-report pipeline
    "fetch_graph",
    "build_communities",
    "generate_report",
    "store_reports",
    "build_workspace_communities",
    "global_query",
    "make_openrouter_llm",
]
