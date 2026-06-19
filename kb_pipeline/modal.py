"""W2 Modal enrichment — inline modal-block LLM descriptions (SoT 3.3 / 3.4).

Walk blocks in document order, produce a single enriched content string:
  * text blocks pass through as plain markdown
  * table / equation blocks -> text_llm(prompt, payload) description
  * image blocks            -> vision_llm(img_path, prompt) description
Each modal is inlined as ONE ATOMIC marker.

EXACT modal marker (single source of truth — producer here and the W1 Rust
consumer MUST use byte-identical markers). The angle-bracket chars are
U+3008 〈 (open) and U+3009 〉 (close):

    〈MODAL id="X" type="table|image|equation"〉{description}\n{payload}〈/MODAL〉

The marker is closed with:  〈/MODAL〉
"""

from __future__ import annotations

from typing import Callable

# U+3008 / U+3009 — byte-identical with the Rust consumer.
_LANGLE = "〈"  # 〈
_RANGLE = "〉"  # 〉

#: Literal prefix that opens a modal marker (before id/type attributes).
MODAL_OPEN_PREFIX = f"{_LANGLE}MODAL"
#: Literal closing marker.
MODAL_CLOSE = f"{_LANGLE}/MODAL{_RANGLE}"

_TABLE_PROMPT = (
    "Describe this table in natural language for retrieval. "
    "Summarize what it contains and its key rows/columns."
)
_EQUATION_PROMPT = (
    "Describe this equation in natural language: what it expresses and its variables."
)
_IMAGE_PROMPT = (
    "Describe this image/figure in natural language for retrieval."
)


def _open_marker(modal_id: str, modal_type: str) -> str:
    return f'{MODAL_OPEN_PREFIX} id="{modal_id}" type="{modal_type}"{_RANGLE}'


def _wrap(modal_id: str, modal_type: str, description: str, payload: str) -> str:
    """Build one atomic 〈MODAL …〉{description}\n{payload}〈/MODAL〉 span."""
    return (
        f"{_open_marker(modal_id, modal_type)}"
        f"{description}\n{payload}"
        f"{MODAL_CLOSE}"
    )


def enrich(
    blocks: list[dict],
    *,
    text_llm: Callable[[str, str], str] | None,
    vision_llm: Callable[[str, str], str] | None,
) -> tuple[str, list[str]]:
    """Enrich blocks into a single content string + ordered modal ids.

    :param text_llm: ``(prompt, payload) -> description`` for table/equation.
    :param vision_llm: ``(img_path, prompt) -> description`` for image.
    :returns: ``(enriched_content, modal_ids)``.
    :raises ValueError: if a modal of a kind appears but its callable is None.
    """
    segments: list[str] = []
    modal_ids: list[str] = []
    counters = {"table": 0, "image": 0, "equation": 0}

    for block in blocks:
        btype = block.get("type")

        if btype == "text":
            text = block.get("text", "")
            if text:
                segments.append(text)
            continue

        if btype == "table":
            if text_llm is None:
                raise ValueError(
                    "table block encountered but text_llm is None; "
                    "a text LLM callable is required to describe tables."
                )
            counters["table"] += 1
            modal_id = f"T{counters['table']}"
            payload = block.get("table_body", "")
            description = text_llm(_TABLE_PROMPT, payload)
            segments.append(_wrap(modal_id, "table", description, payload))
            modal_ids.append(modal_id)
            continue

        if btype == "equation":
            if text_llm is None:
                raise ValueError(
                    "equation block encountered but text_llm is None; "
                    "a text LLM callable is required to describe equations."
                )
            counters["equation"] += 1
            modal_id = f"E{counters['equation']}"
            payload = block.get("latex", "")
            description = text_llm(_EQUATION_PROMPT, payload)
            segments.append(_wrap(modal_id, "equation", description, payload))
            modal_ids.append(modal_id)
            continue

        if btype == "image":
            if vision_llm is None:
                raise ValueError(
                    "image block encountered but vision_llm is None; "
                    "a vision LLM callable is required to describe images."
                )
            counters["image"] += 1
            modal_id = f"I{counters['image']}"
            img_path = block.get("img_path", "")
            description = vision_llm(img_path, _IMAGE_PROMPT)
            segments.append(_wrap(modal_id, "image", description, img_path))
            modal_ids.append(modal_id)
            continue

        # Unknown block type: ignore (front-end is lenient).

    enriched_content = "\n\n".join(segments)
    return enriched_content, modal_ids
