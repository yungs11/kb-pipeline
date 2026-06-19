"""kb_pipeline — front-end stages of the KB pipeline.

W0 Blockify (blockify.py): hybrid "markdown + inline HTML table" -> block list,
plus VLM/OCR ``elements[]`` mapping to the same block schema (SoT 5.1).

W2 Modal enrichment (modal.py): walk blocks, describe table/image/equation blocks
with injected LLM callables, inline each as one atomic 〈MODAL〉…〈/MODAL〉 marker
into a single enriched content string (SoT 3.3 / 3.4).
"""

from .blockify import hybrid_to_blocks, elements_to_blocks
from .modal import enrich, MODAL_OPEN_PREFIX, MODAL_CLOSE

__all__ = [
    "hybrid_to_blocks",
    "elements_to_blocks",
    "enrich",
    "MODAL_OPEN_PREFIX",
    "MODAL_CLOSE",
]
