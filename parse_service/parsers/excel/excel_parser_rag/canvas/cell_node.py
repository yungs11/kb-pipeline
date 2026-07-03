"""CellNode / CellStyle 데이터 모델 (SoT §6.1).

CellNode.features 는 canvas.feature_extractor 가 채우는 dict 이며
키는 FEATURE_KEYS 로 고정한다 (SoT §8).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from ..textutil import cell_addr

# SoT §8 — feature_extractor 가 채워야 하는 키 (모든 키가 항상 존재해야 함)
FEATURE_KEYS = (
    "has_value",
    "text_length",
    "is_numeric",
    "is_date",
    "is_formula",
    "is_marker",
    "looks_like_header",
    "looks_like_note",
    "looks_like_code",
    "indent_level",
    "merge_orientation",  # "" | "vertical" | "horizontal" | "block"
    "style_weight",
)


@dataclass
class CellStyle:
    bold: bool = False
    italic: bool = False
    font_size: Optional[float] = None
    fill_color: Optional[str] = None
    font_color: Optional[str] = None
    horizontal_alignment: Optional[str] = None
    vertical_alignment: Optional[str] = None
    border_top: bool = False
    border_bottom: bool = False
    border_left: bool = False
    border_right: bool = False
    number_format: Optional[str] = None
    indent: int = 0


@dataclass
class CellNode:
    sheet: str
    row: int
    col: int

    raw_value: Any = None
    display_value: str = ""
    normalized_value: str = ""

    formula: Optional[str] = None
    data_type: str = "empty"  # empty|str|int|float|date|bool|formula

    is_empty: bool = True
    is_merged: bool = False
    merge_range: Optional[str] = None   # "A1:A5"
    merge_anchor: Optional[str] = None  # "A1"
    logical_value: str = ""             # 병합 복원 후 의미값 (SoT §7)

    style: CellStyle = field(default_factory=CellStyle)

    hidden_row: bool = False
    hidden_col: bool = False

    features: Dict[str, Any] = field(default_factory=dict)

    @property
    def address(self) -> str:
        return cell_addr(self.row, self.col)

    @property
    def has_logical_value(self) -> bool:
        return bool(self.logical_value)

    @property
    def merge_orientation(self) -> str:
        """"" | vertical | horizontal | block (SoT §7.2)."""
        if not self.merge_range or ":" not in self.merge_range:
            return ""
        from openpyxl.utils import range_boundaries

        min_col, min_row, max_col, max_row = range_boundaries(self.merge_range)
        row_span = max_row - min_row + 1
        col_span = max_col - min_col + 1
        if row_span > 1 and col_span > 1:
            return "block"
        if row_span > col_span:
            return "vertical"
        if col_span > row_span:
            return "horizontal"
        return ""
