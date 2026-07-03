"""SheetCanvas — 시트 전체를 보존하는 2D 캔버스 (SoT §6.2).

빈 셀은 저장하지 않되 get_cell 이 항상 CellNode 를 돌려준다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Set, Tuple

from .cell_node import CellNode


@dataclass
class SheetCanvas:
    workbook_name: str
    sheet_name: str
    sheet_index: int = 0
    max_row: int = 0
    max_col: int = 0
    cells: Dict[Tuple[int, int], CellNode] = field(default_factory=dict)
    merged_ranges: List[str] = field(default_factory=list)
    hidden_rows: Set[int] = field(default_factory=set)
    hidden_cols: Set[int] = field(default_factory=set)
    is_hidden_sheet: bool = False
    contains_formula: bool = False

    # 내부 캐시
    _row_counts: Dict[int, int] = field(default_factory=dict, repr=False)
    _col_counts: Dict[int, int] = field(default_factory=dict, repr=False)

    def put_cell(self, cell: CellNode) -> None:
        self.cells[(cell.row, cell.col)] = cell
        self.max_row = max(self.max_row, cell.row)
        self.max_col = max(self.max_col, cell.col)
        self._row_counts.clear()
        self._col_counts.clear()

    def get_cell(self, row: int, col: int) -> CellNode:
        cell = self.cells.get((row, col))
        if cell is None:
            cell = CellNode(
                sheet=self.sheet_name,
                row=row,
                col=col,
                hidden_row=row in self.hidden_rows,
                hidden_col=col in self.hidden_cols,
            )
        return cell

    def get_range(self, min_row: int, min_col: int, max_row: int, max_col: int) -> List[List[CellNode]]:
        return [
            [self.get_cell(r, c) for c in range(min_col, max_col + 1)]
            for r in range(min_row, max_row + 1)
        ]

    def non_empty_cells(self) -> List[CellNode]:
        return [c for c in self.cells.values() if c.has_logical_value or not c.is_empty]

    def iter_row(self, row: int, min_col: int = 1, max_col: int | None = None) -> Iterable[CellNode]:
        last = max_col if max_col is not None else self.max_col
        for c in range(min_col, last + 1):
            yield self.get_cell(row, c)

    def _ensure_counts(self) -> None:
        if self._row_counts or not self.cells:
            return
        for (r, c), cell in self.cells.items():
            if cell.has_logical_value or not cell.is_empty:
                self._row_counts[r] = self._row_counts.get(r, 0) + 1
                self._col_counts[c] = self._col_counts.get(c, 0) + 1

    def row_density(self, row: int) -> float:
        self._ensure_counts()
        if self.max_col == 0:
            return 0.0
        return self._row_counts.get(row, 0) / self.max_col

    def col_density(self, col: int) -> float:
        self._ensure_counts()
        if self.max_row == 0:
            return 0.0
        return self._col_counts.get(col, 0) / self.max_row

    def row_has_content(self, row: int, min_col: int = 1, max_col: int | None = None) -> bool:
        last = max_col if max_col is not None else self.max_col
        for c in range(min_col, last + 1):
            cell = self.cells.get((row, c))
            if cell is not None and (cell.has_logical_value or not cell.is_empty):
                return True
        return False
