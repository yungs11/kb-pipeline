"""제목 탐지 (SoT §9 title attach, §18.4 workbook_title).

- attach_title(region, canvas, config): region 위쪽 5행 이내 또는 region 상단의
  제목 후보(가로 병합 + bold/큰 글씨/제목 어휘)를 찾아 region.title / title_range 설정.
- extract_document_title(canvases, config): 문서 대표 제목 추출 (첫 시트 우선).
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple, TYPE_CHECKING

from openpyxl.utils import range_boundaries

from ..config import ParserConfig
from ..textutil import infer_numbering_level, is_note_text, one_line

if TYPE_CHECKING:
    from ..canvas.cell_node import CellNode
    from ..canvas.sheet_canvas import SheetCanvas
    from ..detection.region import Region

# "<별표1>", "[붙임2]" 같은 라벨 — 제목 본문이 아님
_LABEL_RE = re.compile(r"^[<\[(（【].{0,12}[>\])）】]$")
# "(개정 : 2026.04.17.)" 같은 단독 괄호 주석
_PAREN_NOTE_RE = re.compile(r"^\(.{0,30}\)$")

_TITLE_HINT_TERMS = (
    "기준표", "기준", "현황", "목록", "일람", "요약", "리스트", "계획",
    "결과", "보고", "명세", "내역", "매트릭스", "표",
)

_TITLE_SCORE_MIN = 2.0
_LOOKBACK_ROWS = 5      # region 위쪽 몇 행까지 제목을 찾을지
_INNER_SCAN_ROWS = 5    # region 내부 상단 몇 행까지 제목을 찾을지


def _cell_text(cell: "CellNode") -> str:
    v = cell.normalized_value or cell.display_value or cell.logical_value
    if not v and cell.raw_value is not None:
        v = cell.raw_value
    return one_line(v)


def _merge_span_cols(cell: "CellNode", canvas: "SheetCanvas") -> Tuple[int, int]:
    """셀이 속한 병합 범위의 (min_col, max_col). 병합 없으면 자기 자신."""
    rng = cell.merge_range
    if not rng:
        for mr in canvas.merged_ranges:
            try:
                c0, r0, c1, r1 = range_boundaries(mr)
            except Exception:
                continue
            if r0 <= cell.row <= r1 and c0 <= cell.col <= c1:
                return int(c0), int(c1)
        return cell.col, cell.col
    try:
        c0, _r0, c1, _r1 = range_boundaries(rng)
        return int(c0), int(c1)
    except Exception:
        return cell.col, cell.col


def _anchor_texts(canvas: "SheetCanvas", row: int, c_lo: int, c_hi: int) -> List[Tuple["CellNode", str]]:
    """해당 행의 raw 값 보유 anchor 셀 + 텍스트 목록 (병합 멤버 제외)."""
    out: List[Tuple["CellNode", str]] = []
    lo = max(1, c_lo)
    hi = min(canvas.max_col if canvas.max_col else c_hi, c_hi)
    for c in range(lo, hi + 1):
        cell = canvas.cells.get((row, c))
        if cell is None or cell.is_empty:
            continue
        if cell.is_merged and cell.merge_anchor and cell.merge_anchor != cell.address:
            continue
        text = _cell_text(cell)
        if not text:
            continue
        out.append((cell, text))
    return out


def _title_score(cell: "CellNode", text: str, width: int, canvas: "SheetCanvas") -> float:
    """제목 후보 점수. 0 이면 후보 아님."""
    if not text:
        return 0.0
    if _LABEL_RE.match(text) or _PAREN_NOTE_RE.match(text):
        return 0.0
    if is_note_text(text):
        return 0.0

    score = 0.0
    c0, c1 = _merge_span_cols(cell, canvas)
    span = c1 - c0 + 1
    if span >= max(3, int(width * 0.5)):
        score += 2.0
    elif span >= 2:
        score += 1.0
    if cell.style.bold:
        score += 1.5
    if cell.style.font_size and cell.style.font_size >= 12:
        score += 0.5
    if cell.style.horizontal_alignment in ("center", "centerContinuous"):
        score += 0.5
    if any(term in text for term in _TITLE_HINT_TERMS):
        score += 1.0
    if len(text) >= 6:
        score += 0.5
    if infer_numbering_level(text) is not None:
        score -= 2.0
    return score


def attach_title(region: "Region", canvas: "SheetCanvas", config: ParserConfig) -> None:
    """region 위쪽/상단에서 제목 후보를 찾아 region.title / title_range 를 채운다."""
    if region.title:
        return
    width = region.col_count
    best: Optional[Tuple[float, int, "CellNode", str]] = None  # (score, row, cell, text)

    def consider(cell: "CellNode", text: str) -> None:
        nonlocal best
        s = _title_score(cell, text, width, canvas)
        if s < _TITLE_SCORE_MIN:
            return
        # 동점이면 region 에 더 가까운(아래쪽) 행 우선
        if best is None or s > best[0] or (s == best[0] and cell.row > best[1]):
            best = (s, cell.row, cell, text)

    # 1) region 위쪽 _LOOKBACK_ROWS 행
    for row in range(max(1, region.min_row - _LOOKBACK_ROWS), region.min_row):
        for cell, text in _anchor_texts(canvas, row, region.min_col - 1, region.max_col + 1):
            consider(cell, text)

    # 2) region 내부 상단 — 본문/헤더(한 행에 anchor 3개 이상)가 시작되기 전까지만
    last_inner = min(region.min_row + _INNER_SCAN_ROWS - 1, region.max_row)
    for row in range(region.min_row, last_inner + 1):
        anchors = _anchor_texts(canvas, row, region.min_col, region.max_col)
        if len(anchors) >= 3:
            break
        if any(infer_numbering_level(t) is not None for _, t in anchors):
            break
        for cell, text in anchors:
            if len(anchors) >= 2:
                # anchor 2개 이상인 행은 헤더 행("항목 | 지급액")일 수 있음 —
                # 가로 병합(2칸 이상) 또는 제목 어휘가 있는 셀만 제목 후보로 인정
                c0, c1 = _merge_span_cols(cell, canvas)
                if (c1 - c0 + 1) < 2 and not any(term in text for term in _TITLE_HINT_TERMS):
                    continue
            consider(cell, text)

    if best is None:
        return
    _score, _row, cell, text = best
    region.title = text
    region.title_range = cell.merge_range or cell.address


def extract_document_title(canvases: List["SheetCanvas"], config: ParserConfig) -> str:
    """모든 캔버스 상단에서 문서 대표 제목 추출 (첫 시트 우선)."""
    if config.document_title:
        return config.document_title
    for canvas in canvases:
        if not canvas.cells:
            continue
        best: Optional[Tuple[float, str]] = None
        max_scan = min(8, canvas.max_row)
        for row in range(1, max_scan + 1):
            for cell, text in _anchor_texts(canvas, row, 1, canvas.max_col):
                s = _title_score(cell, text, canvas.max_col or 1, canvas)
                if s >= _TITLE_SCORE_MIN and (best is None or s > best[0]):
                    best = (s, text)
        if best is not None:
            return best[1]
    return ""
