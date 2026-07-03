"""Cell feature extraction (SoT §8).

pipeline.py 가 고정한 시그니처:

- extract_cell_features(canvas) -> None

merge_normalizer 실행 이후에 호출되어야 한다 (logical_value 기반 판정).
FEATURE_KEYS 의 모든 키를 채운다 — 다운스트림이 features[key] 직접 접근을
해도 안전하도록 canvas 에 저장된 모든 셀에 대해 채운다.
"""

from __future__ import annotations

import re

from ..textutil import is_marker_value, is_note_text, looks_like_code
from .cell_node import FEATURE_KEYS, CellNode
from .sheet_canvas import SheetCanvas

_NUMERIC_TEXT_RE = re.compile(r"-?[\d,]*\d(\.\d+)?\s*%?")
_LEADING_WS_RE = re.compile(r"^[ \t　]+")

# style_weight 가중치 (bold/fill/border 가중합, 0~1)
_W_BOLD = 0.4
_W_FILL = 0.3
_W_BORDER = 0.3  # 4방향 모두 있을 때 만점


def _is_numeric(cell: CellNode, text: str) -> bool:
    if cell.data_type in ("int", "float"):
        return True
    if not text:
        return False
    return bool(_NUMERIC_TEXT_RE.fullmatch(text))


def _indent_level(cell: CellNode) -> int:
    """style.indent + 선행 공백 기반 들여쓰기 레벨."""
    level = int(cell.style.indent or 0)
    raw = cell.raw_value
    if isinstance(raw, str):
        match = _LEADING_WS_RE.match(raw)
        if match:
            spaces = 0
            for ch in match.group(0):
                spaces += 2 if ch in ("\t", "　") else 1
            level += spaces // 2
    return level


def _style_weight(cell: CellNode) -> float:
    style = cell.style
    weight = 0.0
    if style.bold:
        weight += _W_BOLD
    if style.fill_color:
        weight += _W_FILL
    borders = sum(
        1 for b in (style.border_top, style.border_bottom, style.border_left, style.border_right) if b
    )
    weight += _W_BORDER * (borders / 4.0)
    return round(min(1.0, weight), 4)


def _looks_like_header(cell: CellNode, text: str, is_numeric: bool, is_marker: bool) -> bool:
    """헤더 후보 휴리스틱: bold or fill or 짧은 비숫자 텍스트.

    행 단위 최종 판정은 detection.header_detector 가 종합한다 — 여기서는 셀 신호만.
    """
    if not text or is_numeric or is_marker:
        return False
    if is_note_text(text):
        return False
    if cell.style.bold or cell.style.fill_color:
        return True
    return len(text) <= 12 and "\n" not in cell.display_value


def _extract_one(cell: CellNode) -> None:
    text = cell.logical_value or cell.normalized_value
    is_numeric = _is_numeric(cell, text)
    # 숫자 0 등 수치 값은 marker 후보가 아니다 (SoT §8.1 — 'O' 문자만 혼동 대상)
    is_marker = bool(text) and not is_numeric and is_marker_value(text)

    cell.features.update(
        {
            "has_value": bool(text) or not cell.is_empty,
            "text_length": len(text),
            "is_numeric": is_numeric,
            "is_date": cell.data_type == "date",
            "is_formula": cell.formula is not None or cell.data_type == "formula",
            "is_marker": is_marker,
            "looks_like_header": _looks_like_header(cell, text, is_numeric, is_marker),
            "looks_like_note": bool(text) and is_note_text(text),
            "looks_like_code": bool(text) and looks_like_code(text),
            "indent_level": _indent_level(cell),
            "merge_orientation": cell.merge_orientation,
            "style_weight": _style_weight(cell),
        }
    )


def extract_cell_features(canvas: SheetCanvas) -> None:
    """canvas 에 저장된 모든 셀의 features 를 FEATURE_KEYS 전체로 채운다 (SoT §8)."""
    for cell in canvas.cells.values():
        _extract_one(cell)
        # FEATURE_KEYS 계약 보증 (누락 키 방지)
        for key in FEATURE_KEYS:
            cell.features.setdefault(key, None)
