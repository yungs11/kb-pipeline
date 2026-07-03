"""Compact retrieval text for row-level chunks.

`content_text` stays a natural-language explanation.  This module builds a
shorter row record for embedding/sparse retrieval so long rows do not dilute
important field names and values.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Tuple, TYPE_CHECKING

from ..textutil import one_line

if TYPE_CHECKING:
    from ..chunking.chunk_schema import RagChunk

ROW_LEVEL_CHUNK_TYPES = {
    "table_row",
    "total_row",
    "form_field",
    "code_mapping",
    "matrix_fact",
    "delegation_rule",
}

MAX_CORE_FIELDS = 8
MAX_VALUE_CHARS = 140
MAX_CORE_TEXT_CHARS = 900

_INTERNAL_FIELD_KEYS = {"경로"}
_ROW_ID_KEYS = {"항목", "행축", "field_name", "필드명"}


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return ", ".join(one_line(v) for v in value if one_line(v))
    if isinstance(value, dict):
        return ", ".join(f"{one_line(k)}={one_line(v)}" for k, v in value.items() if one_line(v))
    return one_line(value)


def _clip(value: str, limit: int = MAX_VALUE_CHARS) -> str:
    text = one_line(value)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _is_mostly_numeric(value: str) -> bool:
    compact = re.sub(r"\s+", "", value)
    if not compact:
        return False
    numeric_chars = sum(1 for ch in compact if ch.isdigit() or ch in ".,:%-/#")
    return numeric_chars / float(len(compact)) >= 0.8


def _field_score(key: str, value: str, original_index: int) -> Tuple[float, int]:
    """Higher score means the field is more useful for retrieval.

    The scoring is intentionally structural, not domain-specific: compact text
    fields and row identifiers outrank long prose and pure numbers.
    """
    score = 0.0
    if key in _ROW_ID_KEYS:
        score += 2.0
    if 2 <= len(value) <= 80:
        score += 1.2
    elif len(value) <= MAX_VALUE_CHARS:
        score += 0.6
    else:
        score -= 0.6
    if _is_mostly_numeric(value):
        score -= 0.8
    if any(ch.isalpha() for ch in value) or re.search(r"[가-힣]", value):
        score += 0.5
    if len(key) <= 20:
        score += 0.2
    return score, -original_index


def _ordered_core_fields(fields: Dict[str, Any], limit: int = MAX_CORE_FIELDS) -> List[Tuple[str, str]]:
    candidates: List[Tuple[float, int, str, str]] = []
    for idx, (raw_key, raw_value) in enumerate(fields.items()):
        key = one_line(raw_key)
        if not key or key in _INTERNAL_FIELD_KEYS:
            continue
        value = _stringify(raw_value)
        if not value:
            continue
        score, tie = _field_score(key, value, idx)
        candidates.append((score, tie, key, _clip(value)))

    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    selected = candidates[:limit]

    # Restore source field order after selection so the row record remains easy
    # to scan and comparable to the worksheet.
    selected.sort(key=lambda item: -item[1])
    return [(key, value) for _score, _tie, key, value in selected]


def _path_text(path: Iterable[Any]) -> str:
    return " > ".join(one_line(part) for part in path if one_line(part))


def build_row_core_text(chunk: "RagChunk") -> str:
    """Build a compact `key:value; ... -- sheet` retrieval record."""
    if chunk.chunk_type not in ROW_LEVEL_CHUNK_TYPES:
        return ""

    parts: List[Tuple[str, str]] = []
    sheet = one_line(chunk.sheet)
    title = one_line(chunk.title)
    path = _path_text(chunk.path or [])

    if title:
        parts.append(("title", _clip(title, 80)))
    if path and path != title:
        parts.append(("path", _clip(path, 180)))

    parts.extend(_ordered_core_fields(chunk.fields or {}))

    if not parts:
        return ""

    text = "; ".join(f"{key}: {value}" for key, value in parts if value)
    if sheet:
        text = f"{text} -- {sheet}"
    if chunk.range:
        text = f"{text} [{chunk.range}]"
    if len(text) > MAX_CORE_TEXT_CHARS:
        text = text[: MAX_CORE_TEXT_CHARS - 1].rstrip() + "…"
    return text

