"""Footer 탐지 (SoT §12.2, §16).

region 하단의 '명확한' note/합계 블록만 footer_rows 로 분리한다.
본문 중간에 흩어진 ※ 주석 행은 건드리지 않는다 (계층 파서가 본문 위치에서 처리).

footer 로 인정하는 조건:
- region body 의 맨 아래에서 연속된 note/total 행 블록이고,
- (a) 블록 전체가 합계(total) 행이거나
- (b) 블록 위가 빈 행으로 분리되어 본문과 명확히 떨어져 있는 경우
"""

from __future__ import annotations

from typing import List, Optional, Tuple, TYPE_CHECKING

from ..config import ParserConfig
from ..markerutil import is_marker_cell
from ..textutil import is_note_text, is_total_text, one_line

if TYPE_CHECKING:
    from ..canvas.cell_node import CellNode
    from ..canvas.sheet_canvas import SheetCanvas
    from ..detection.region import Region

_MAX_FOOTER_ROWS = 15


def _cell_text(cell: Optional["CellNode"]) -> str:
    if cell is None:
        return ""
    v = cell.logical_value or cell.normalized_value or cell.display_value
    if not v and cell.raw_value is not None:
        v = cell.raw_value
    return one_line(v)


def _row_kind(region: "Region", canvas: "SheetCanvas", row: int) -> str:
    """행 분류: "note" | "total" | "body"."""
    texts: List[str] = []
    has_marker = False
    for c in range(region.min_col, region.max_col + 1):
        cell = canvas.cells.get((row, c))
        if cell is None or cell.is_empty:
            continue
        t = _cell_text(cell)
        if not t:
            continue
        if is_marker_cell(cell, t):
            has_marker = True
            continue
        texts.append(t)
    if not texts:
        return "body"
    if any(is_total_text(t) for t in texts):
        return "total"
    if not has_marker and is_note_text(texts[0]):
        return "note"
    return "body"


def detect_footers(region: "Region", canvas: "SheetCanvas", config: ParserConfig) -> None:
    if region.footer_rows or not region.body_rows:
        return

    body = sorted(region.body_rows)
    block: List[Tuple[int, str]] = []
    for r in reversed(body):
        kind = _row_kind(region, canvas, r)
        if kind in ("note", "total") and len(block) < _MAX_FOOTER_ROWS:
            block.insert(0, (r, kind))
        else:
            break
    if not block or len(block) >= len(body):
        return

    rows = [r for r, _ in block]
    kinds = {k for _, k in block}
    top = rows[0]

    all_total = kinds == {"total"}
    above = top - 1
    separated = (
        above >= region.min_row
        and above not in set(body)
        and above not in set(region.header_rows)
        and not canvas.row_has_content(above, region.min_col, region.max_col)
    )
    if not (all_total or separated):
        # SoT §12.1 — region 맨 아래의 '순수 note 행' 연속 구간은 body 에서 제외.
        # (본문과 붙은 합계 행은 body 에 남겨 파서가 total_row 로 처리하게 둔다)
        tail: List[int] = []
        for r, kind in reversed(block):
            if kind == "note":
                tail.insert(0, r)
            else:
                break
        if not tail or len(tail) >= len(body):
            return
        rows = tail

    region.footer_rows = rows
    row_set = set(rows)
    region.body_rows = [r for r in body if r not in row_set]
