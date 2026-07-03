"""병합 셀 logical value 복원 (SoT §7).

pipeline.py 가 고정한 시그니처:

- normalize_merged_cells(canvas) -> None

원칙 (SoT Rule 2):
- 병합 범위의 anchor(좌상단) 값을 범위 내 모든 셀의 logical_value 로 전파한다.
- raw_value 는 그대로 유지한다.
- 비병합 셀의 logical_value 는 자신의 normalized_value 다.
- 병합 범위 내 빈 셀도 CellNode 가 canvas 에 존재해야 한다 (없으면 생성해 put_cell).
"""

from __future__ import annotations

from openpyxl.utils import range_boundaries

from ..textutil import cell_addr
from .sheet_canvas import SheetCanvas


def normalize_merged_cells(canvas: SheetCanvas) -> None:
    """canvas 의 모든 셀에 logical_value 를 채우고 병합 메타데이터를 설정한다."""
    # 1) 기본값: 모든 셀의 logical_value = 자신의 normalized_value
    for cell in canvas.cells.values():
        cell.logical_value = cell.normalized_value

    # 2) 병합 범위마다 anchor 값을 전파
    for merge_range in canvas.merged_ranges:
        try:
            min_col, min_row, max_col, max_row = range_boundaries(merge_range)
        except ValueError:
            continue
        if None in (min_col, min_row, max_col, max_row):
            continue

        anchor_key = (min_row, min_col)
        anchor = canvas.cells.get(anchor_key)
        if anchor is None:
            # used range 밖이거나 로더가 잘라낸 경우 — 빈 anchor 생성
            anchor = canvas.get_cell(min_row, min_col)
            canvas.put_cell(anchor)
        anchor_value = anchor.normalized_value
        anchor_addr = cell_addr(min_row, min_col)

        for row in range(min_row, max_row + 1):
            for col in range(min_col, max_col + 1):
                node = canvas.cells.get((row, col))
                if node is None:
                    node = canvas.get_cell(row, col)
                    canvas.put_cell(node)
                node.is_merged = True
                node.merge_range = merge_range
                node.merge_anchor = anchor_addr
                node.logical_value = anchor_value
