"""Occupied region 탐지 (SoT §9).

non-empty(logical 포함) 셀 그리드에서 빈 행/열 boundary 와 gap tolerance 기반
connected component 로 의미 있는 직사각형 영역을 찾는다.

규약:
- config.sheet_overrides[sheet].regions 가 있으면 자동 탐지 대신 override 로 Region 생성
  (region_type/header_rows/hierarchy_cols/matrix_cols/metadata_cols 를 미리 채움.
   metadata_cols 는 {이름: col} → {col: 이름} 으로 뒤집어 저장)
- ignore_ranges 내 셀은 occupied 그리드에서 제외
- 본표(테두리 있는 주 영역) 아래로 멀리 떨어진 무테두리 고아 잔재는 제거
  (위임전결표 시트2 rows 25~837 / Index 시트 잔재 대응)
"""

from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple, TYPE_CHECKING

from openpyxl.utils import range_boundaries

from ..config import ParserConfig, SheetOverride
from .region import Region

if TYPE_CHECKING:
    from ..canvas.sheet_canvas import SheetCanvas

Box = List[int]  # [min_row, max_row, min_col, max_col]

# --- 잔재(remnant) 판정 파라미터 ----------------------------------------------
_REMNANT_MIN_PRIMARY_BORDER = 0.3   # 주 영역이 이만큼 테두리를 가질 때만 잔재 필터 동작
_REMNANT_MIN_PRIMARY_CELLS = 20
_REMNANT_MIN_GAP_ROWS = 2           # 주 영역 하단에서 이보다 멀리 떨어져 있어야 잔재 후보
_REMNANT_MIN_GAP_COLS = 2           # 주 영역 옆(열 방향)으로 이보다 멀리 떨어진 조각도 잔재 후보
# 잔재 후보라도 아래 조건을 모두 만족하는 '응집된 블록'이면 유지 (예: Index 시트 추가 약어 블록)
_REMNANT_KEEP_MIN_CELLS = 12
_REMNANT_KEEP_MIN_DENSITY = 0.35


def detect_regions(canvas: "SheetCanvas", config: ParserConfig) -> List[Region]:
    """시트 캔버스에서 Region 목록을 탐지한다 (행 순서 정렬, id={sheet}_region{n})."""
    override = config.sheet_overrides.get(canvas.sheet_name)
    if override is not None and override.regions:
        return _regions_from_overrides(canvas, override)

    occupied = _occupied_coords(canvas, override)
    if not occupied:
        return []

    gap_r = max(0, int(config.gap_tolerance_row))
    gap_c = max(0, int(config.gap_tolerance_col))
    boxes = _connected_boxes(occupied, gap_r, gap_c)
    boxes = _merge_overlapping(boxes)
    boxes = _merge_adjacent(boxes)
    boxes.sort(key=lambda b: (b[0], b[2]))

    profiles = [_box_profile(canvas, occupied, box) for box in boxes]
    kept, dropped_count, primary_pos = _filter_remnants(boxes, profiles)

    regions: List[Region] = []
    for n, (box, profile) in enumerate(kept, start=1):
        region = Region(
            id=f"{canvas.sheet_name}_region{n}",
            sheet=canvas.sheet_name,
            min_row=box[0],
            max_row=box[1],
            min_col=box[2],
            max_col=box[3],
        )
        region.features["detected_cells"] = float(profile["cells"])
        region.features["detected_density"] = float(profile["density"])
        region.features["detected_border_ratio"] = float(profile["border_ratio"])
        regions.append(region)

    if dropped_count and 0 <= primary_pos < len(regions):
        regions[primary_pos].warnings.append(f"remnant_regions_dropped:{dropped_count}")
    return regions


# --- override -----------------------------------------------------------------

def _regions_from_overrides(canvas: "SheetCanvas", override: SheetOverride) -> List[Region]:
    regions: List[Region] = []
    n = 0
    for ov in override.regions:
        try:
            c0, r0, c1, r1 = range_boundaries(ov.range)
        except Exception:
            continue
        n += 1
        region = Region(
            id=f"{canvas.sheet_name}_region{n}",
            sheet=canvas.sheet_name,
            min_row=int(r0),
            max_row=int(r1),
            min_col=int(c0),
            max_col=int(c1),
        )
        if ov.region_type:
            region.region_type = ov.region_type
        if ov.header_rows:
            region.header_rows = [int(r) for r in ov.header_rows]
        if ov.hierarchy_cols:
            region.hierarchy_cols = [int(c) for c in ov.hierarchy_cols]
        if ov.matrix_cols:
            # 이름은 header_detector 가 flatten 으로 채운다
            region.matrix_cols = {int(c): "" for c in ov.matrix_cols}
        if ov.metadata_cols:
            # {이름: col} → {col: 이름}
            region.metadata_cols = {int(col): str(name) for name, col in ov.metadata_cols.items()}
        region.confidence = 0.9
        regions.append(region)
    return regions


# --- occupied grid ------------------------------------------------------------

def _occupied_coords(canvas: "SheetCanvas", override: Optional[SheetOverride]) -> Set[Tuple[int, int]]:
    ignored: List[Tuple[int, int, int, int]] = []
    if override is not None:
        for rng in override.ignore_ranges:
            try:
                c0, r0, c1, r1 = range_boundaries(rng)
            except Exception:
                continue
            ignored.append((int(r0), int(r1), int(c0), int(c1)))

    out: Set[Tuple[int, int]] = set()
    for (r, c), cell in canvas.cells.items():
        if not (cell.has_logical_value or not cell.is_empty):
            continue
        if any(r0 <= r <= r1 and c0 <= c <= c1 for (r0, r1, c0, c1) in ignored):
            continue
        out.add((r, c))
    return out


# --- connected components (gap tolerant) ---------------------------------------

def _connected_boxes(occupied: Set[Tuple[int, int]], gap_r: int, gap_c: int) -> List[Box]:
    """gap tolerance 이내(빈 행 gap_r개, 빈 열 gap_c개)면 같은 component 로 연결."""
    reach_r, reach_c = gap_r + 1, gap_c + 1
    offsets = [
        (dr, dc)
        for dr in range(-reach_r, reach_r + 1)
        for dc in range(-reach_c, reach_c + 1)
        if (dr, dc) != (0, 0)
    ]
    todo = set(occupied)
    boxes: List[Box] = []
    while todo:
        seed = todo.pop()
        stack = [seed]
        r0 = r1 = seed[0]
        c0 = c1 = seed[1]
        while stack:
            r, c = stack.pop()
            r0, r1 = min(r0, r), max(r1, r)
            c0, c1 = min(c0, c), max(c1, c)
            for dr, dc in offsets:
                nb = (r + dr, c + dc)
                if nb in todo:
                    todo.discard(nb)
                    stack.append(nb)
        boxes.append([r0, r1, c0, c1])
    return boxes


def _boxes_overlap(a: Box, b: Box) -> bool:
    return not (a[1] < b[0] or b[1] < a[0] or a[3] < b[2] or b[3] < a[2])


def _union(a: Box, b: Box) -> Box:
    return [min(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), max(a[3], b[3])]


def _merge_overlapping(boxes: List[Box]) -> List[Box]:
    changed = True
    while changed:
        changed = False
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                if _boxes_overlap(boxes[i], boxes[j]):
                    boxes[i] = _union(boxes[i], boxes[j])
                    del boxes[j]
                    changed = True
                    break
            if changed:
                break
    return boxes


def _should_merge_vertically(a: Box, b: Box) -> bool:
    """SoT §9.5 — 1~2행 이하 간격 + 열 범위 공유 + 한쪽이 제목/주석 후보(1~2행)."""
    top, bot = (a, b) if a[0] <= b[0] else (b, a)
    gap = bot[0] - top[1] - 1
    if gap < 0 or gap > 2:
        return False
    ov = min(top[3], bot[3]) - max(top[2], bot[2]) + 1
    if ov <= 0:
        return False
    width = min(top[3] - top[2], bot[3] - bot[2]) + 1
    if width <= 0 or ov / width < 0.6:
        return False
    return (top[1] - top[0] + 1 <= 2) or (bot[1] - bot[0] + 1 <= 2)


def _merge_adjacent(boxes: List[Box]) -> List[Box]:
    changed = True
    while changed:
        changed = False
        boxes.sort(key=lambda b: (b[0], b[2]))
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                if _should_merge_vertically(boxes[i], boxes[j]):
                    boxes[i] = _union(boxes[i], boxes[j])
                    del boxes[j]
                    changed = True
                    break
            if changed:
                break
    return _merge_overlapping(boxes)


# --- 잔재 필터 ------------------------------------------------------------------

def _box_profile(canvas: "SheetCanvas", occupied: Set[Tuple[int, int]], box: Box) -> Dict[str, float]:
    r0, r1, c0, c1 = box
    cells = 0
    raw = 0
    bordered = 0
    for (r, c) in occupied:
        if r0 <= r <= r1 and c0 <= c <= c1:
            cells += 1
            cell = canvas.cells.get((r, c))
            if cell is not None and not cell.is_empty:
                raw += 1
                st = cell.style
                if st.border_top or st.border_bottom or st.border_left or st.border_right:
                    bordered += 1
    area = (r1 - r0 + 1) * (c1 - c0 + 1)
    return {
        "cells": float(cells),
        "raw": float(raw),
        "border_ratio": (bordered / raw) if raw else 0.0,
        "density": (cells / area) if area else 0.0,
    }


def _filter_remnants(
    boxes: List[Box], profiles: List[Dict[str, float]]
) -> Tuple[List[Tuple[Box, Dict[str, float]]], int, int]:
    """본표(테두리 있는 주 영역) 바깥에 흩어진 고아 조각(잔재)을 제거.

    - 잔재 후보: 주 영역 하단으로 떨어져 있거나, 행은 겹치되 열 방향으로 떨어진 조각
    - 단, 충분히 응집된 블록(셀 수/밀도/2열 이상)은 독립 표로 보고 유지
    - 주 영역 자체가 테두리 없는 시트(일반 평문 시트)는 필터를 적용하지 않음

    반환: (남은 (box, profile) 목록, 제거 수, 남은 목록에서 주 영역 위치)
    """
    pairs = list(zip(boxes, profiles))
    if not pairs:
        return [], 0, -1
    primary_idx = max(range(len(pairs)), key=lambda i: profiles[i]["cells"])
    p_prof = profiles[primary_idx]
    p_box = boxes[primary_idx]

    if p_prof["border_ratio"] < _REMNANT_MIN_PRIMARY_BORDER or p_prof["cells"] < _REMNANT_MIN_PRIMARY_CELLS:
        return pairs, 0, primary_idx

    kept: List[Tuple[Box, Dict[str, float]]] = []
    dropped = 0
    primary_pos = -1
    for i, (box, prof) in enumerate(pairs):
        if i == primary_idx:
            primary_pos = len(kept)
            kept.append((box, prof))
            continue
        below = box[0] > p_box[1] + _REMNANT_MIN_GAP_ROWS
        rows_overlap = not (box[1] < p_box[0] or box[0] > p_box[1])
        beside = rows_overlap and (
            box[2] > p_box[3] + _REMNANT_MIN_GAP_COLS or box[3] < p_box[2] - _REMNANT_MIN_GAP_COLS
        )
        coherent = (
            prof["cells"] >= _REMNANT_KEEP_MIN_CELLS
            and prof["density"] >= _REMNANT_KEEP_MIN_DENSITY
            and (box[3] - box[2] + 1) >= 2
        )
        if (below or beside) and not coherent:
            dropped += 1
            continue
        kept.append((box, prof))
    return kept, dropped, primary_pos
