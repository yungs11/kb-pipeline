"""Region 데이터 모델 (SoT §6.3)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..textutil import range_a1

REGION_TYPES = (
    "flat_table",
    "multi_header_table",
    "hierarchical_table",
    "matrix_table",
    "hierarchical_matrix",
    "form",
    "key_value_block",
    "note_block",
    "code_mapping_table",
    "report_section",
    "unknown_table",
)


@dataclass
class Region:
    id: str
    sheet: str
    min_row: int
    max_row: int
    min_col: int
    max_col: int

    region_type: str = "unknown_table"
    role: str = "body"  # body | title | note | metadata

    title: Optional[str] = None
    title_range: Optional[str] = None
    header_rows: List[int] = field(default_factory=list)
    body_rows: List[int] = field(default_factory=list)
    footer_rows: List[int] = field(default_factory=list)

    # 분류기/헤더 탐지기가 채우는 보조 정보
    features: Dict[str, float] = field(default_factory=dict)
    # 매트릭스/계층 파싱용 컬럼 역할 (header detection 또는 override 가 채움)
    hierarchy_cols: List[int] = field(default_factory=list)   # row label/계층 컬럼
    matrix_cols: Dict[int, str] = field(default_factory=dict)  # col index -> flatten된 컬럼 헤더명
    metadata_cols: Dict[int, str] = field(default_factory=dict)  # col index -> 합의/수신/비고 등

    confidence: float = 0.5
    warnings: List[str] = field(default_factory=list)

    @property
    def range_a1(self) -> str:
        return range_a1(self.min_row, self.min_col, self.max_row, self.max_col)

    @property
    def row_count(self) -> int:
        return self.max_row - self.min_row + 1

    @property
    def col_count(self) -> int:
        return self.max_col - self.min_col + 1

    def contains(self, row: int, col: int) -> bool:
        return self.min_row <= row <= self.max_row and self.min_col <= col <= self.max_col
