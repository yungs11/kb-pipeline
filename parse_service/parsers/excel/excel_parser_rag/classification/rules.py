"""Region feature 계산 + 규칙 기반 분류 (SoT §10).

- compute_region_features(region, canvas) -> Dict[str, float]  (SoT §10.1)
- classify_from_features(features) -> (region_type, confidence, warnings, role)  (SoT §10.2)

실제 Region 갱신은 classification.region_classifier.classify_region 이 수행한다.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

from ..markerutil import is_marker_cell
from ..textutil import (
    compact,
    infer_numbering_level,
    is_marker_value,
    is_note_text,
    one_line,
)

if TYPE_CHECKING:
    from ..canvas.cell_node import CellNode
    from ..canvas.sheet_canvas import SheetCanvas
    from ..detection.region import Region

_NUMERIC_RE = re.compile(r"^-?[\d,]+(?:\.\d+)?%?$")

# key-value 의 '키' 로 인정할 최대 글자수 (compact 기준)
_KV_KEY_MAX_LEN = 8
# code_mapping 으로 보려면 키 평균 길이가 이 이하
_CODE_KEY_AVG_MAX = 6.0
# 긴 문장으로 간주하는 텍스트 길이 (note_score)
_LONG_TEXT_LEN = 60

FEATURE_NAMES = (
    "row_count",
    "col_count",
    "area",
    "occupied",
    "raw_cells",
    "density",
    "merged_cell_ratio",
    "numeric_ratio",
    "text_ratio",
    "marker_ratio",
    "border_ratio",
    "note_score",
    "avg_text_len",
    "key_value_score",
    "kv_key_len_avg",
    "left_col_hierarchy_score",
    "top_row_header_score",
    "header_style_ratio_top_rows",
    "est_header_depth",
    "horizontal_title_merge",
    "code_header_vocab",
    "kv_pair_coverage",
)

# 코드 매핑 표 헤더로 보는 어휘 (compact 후 완전 일치)
_CODE_HEADER_VOCAB = {
    "약어", "약칭", "코드", "코드명", "코드값", "부호", "기호",
    "정식명칭", "명칭", "의미", "설명", "code", "abbr",
}


def _cell_text(cell: Optional["CellNode"]) -> str:
    if cell is None:
        return ""
    v = cell.logical_value or cell.normalized_value or cell.display_value
    if not v and cell.raw_value is not None:
        v = cell.raw_value
    return one_line(v)


def _is_numeric_cell(cell: Optional["CellNode"], text: str) -> bool:
    if cell is not None and cell.data_type in ("int", "float"):
        return True
    return bool(_NUMERIC_RE.match(compact(text)))


def _merge_col_span(cell: "CellNode") -> int:
    rng = cell.merge_range
    if not rng or ":" not in rng:
        return 1
    from openpyxl.utils import range_boundaries

    try:
        c0, _r0, c1, _r1 = range_boundaries(rng)
        return int(c1) - int(c0) + 1
    except Exception:
        return 1


def compute_region_features(region: "Region", canvas: "SheetCanvas") -> Dict[str, float]:
    """SoT §10.1 region feature dict. 값은 전부 float."""
    rc, cc = region.row_count, region.col_count
    area = rc * cc
    feats: Dict[str, float] = {name: 0.0 for name in FEATURE_NAMES}
    feats["row_count"] = float(rc)
    feats["col_count"] = float(cc)
    feats["area"] = float(area)
    feats["kv_key_len_avg"] = 99.0

    # (r, c, cell, text, is_raw)
    occupied: List[Tuple[int, int, "CellNode", str, bool]] = []
    for (r, c), cell in canvas.cells.items():
        if not region.contains(r, c):
            continue
        text = _cell_text(cell)
        raw = not cell.is_empty
        if not text and not raw:
            continue
        occupied.append((r, c, cell, text, raw))
    if not occupied:
        return feats

    occ = len(occupied)
    feats["occupied"] = float(occ)
    feats["density"] = occ / float(area) if area else 0.0
    feats["merged_cell_ratio"] = sum(1 for _r, _c, cell, _t, _raw in occupied if cell.is_merged) / float(occ)

    raw_items = [(r, c, cell, text) for r, c, cell, text, raw in occupied if raw]
    nraw = len(raw_items)
    feats["raw_cells"] = float(nraw)
    if nraw == 0:
        return feats

    markers = numeric = textual = bordered = 0
    note_hits = 0
    text_lens: List[int] = []
    for _r, _c, cell, text in raw_items:
        st = cell.style
        if st.border_top or st.border_bottom or st.border_left or st.border_right:
            bordered += 1
        # 숫자 0 등 수치 셀은 marker 가 아니라 수치로 센다 (SoT §8.1)
        if is_marker_cell(cell, text):
            markers += 1
            continue
        if _is_numeric_cell(cell, text):
            numeric += 1
        elif text:
            textual += 1
            text_lens.append(len(text))
            if is_note_text(text) or len(text) > _LONG_TEXT_LEN:
                note_hits += 1
    feats["marker_ratio"] = markers / float(nraw)
    feats["numeric_ratio"] = numeric / float(nraw)
    feats["text_ratio"] = textual / float(nraw)
    feats["border_ratio"] = bordered / float(nraw)
    feats["note_score"] = note_hits / float(textual) if textual else 0.0
    feats["avg_text_len"] = (sum(text_lens) / float(len(text_lens))) if text_lens else 0.0

    # --- 행 단위 인덱스 -----------------------------------------------------------
    rows_map: Dict[int, Dict[int, Tuple["CellNode", str]]] = {}
    for r, c, cell, text in raw_items:
        if text:
            rows_map.setdefault(r, {})[c] = (cell, text)
    content_rows = sorted(rows_map)

    # 상단 행 스타일 비율 (최대 3개 content row)
    top_rows = content_rows[:3]
    styled = total_top = 0
    for r in top_rows:
        for _c, (cell, _t) in rows_map[r].items():
            total_top += 1
            if cell.style.bold or cell.style.fill_color:
                styled += 1
    feats["header_style_ratio_top_rows"] = styled / float(total_top) if total_top else 0.0

    # 첫 '밀집' content row 의 헤더 점수 (0..1)
    dense_min = max(2, int(0.3 * cc))
    for r in content_rows:
        cells_in_row = rows_map[r]
        if len(cells_in_row) < dense_min:
            continue
        n = float(len(cells_in_row))
        bold = sum(1 for cell, _t in cells_in_row.values() if cell.style.bold) / n
        center = sum(
            1 for cell, _t in cells_in_row.values()
            if cell.style.horizontal_alignment in ("center", "centerContinuous")
        ) / n
        fillc = sum(1 for cell, _t in cells_in_row.values() if cell.style.fill_color) / n
        nonnum = sum(1 for cell, t in cells_in_row.values() if not _is_numeric_cell(cell, t)) / n
        feats["top_row_header_score"] = 0.45 * bold + 0.2 * center + 0.15 * fillc + 0.2 * nonnum
        break

    # est_header_depth — 스타일 기반 상단 연속 헤더 행 깊이 추정
    feats["est_header_depth"] = float(_estimate_header_depth(region, rows_map, cc))

    # horizontal_title_merge — 상단 3개 content row 의 최대 가로 병합 폭 비율
    hmax = 0.0
    for r in top_rows:
        for _c, (cell, _t) in rows_map[r].items():
            if cell.merge_orientation in ("horizontal", "block"):
                hmax = max(hmax, _merge_col_span(cell) / float(cc))
    feats["horizontal_title_merge"] = hmax

    # key_value_score — 인접 컬럼 (짧은 키, 값) 쌍 비율
    kv_hits = 0
    key_lens: List[int] = []
    for r in content_rows:
        colmap = rows_map[r]
        for c in sorted(colmap):
            right = colmap.get(c + 1)
            if right is None:
                continue
            key = compact(colmap[c][1])
            if not key or len(key) > _KV_KEY_MAX_LEN:
                continue
            if is_marker_value(key) or _NUMERIC_RE.match(key):
                continue
            kv_hits += 1
            key_lens.append(len(key))
            break
    feats["key_value_score"] = kv_hits / float(len(content_rows)) if content_rows else 0.0
    if key_lens:
        feats["kv_key_len_avg"] = sum(key_lens) / float(len(key_lens))

    # kv_pair_coverage — 텍스트 셀 중 (짧은 키, 값) 인접 쌍에 속하는 비율.
    # code_mapping 표는 행이 거의 전부 2열 쌍(쌍 블록 복수 가능)으로 구성되고,
    # 일반 flat table 은 쌍 밖에 남는 데이터 컬럼이 있어 coverage 가 낮다.
    pair_cells = 0
    total_cells = 0
    for r in content_rows:
        colmap = rows_map[r]
        cols = sorted(colmap)
        total_cells += len(cols)
        idx = 0
        while idx < len(cols):
            c = cols[idx]
            partner = cols[idx + 1] if idx + 1 < len(cols) and cols[idx + 1] in (c + 1, c + 2) else None
            key = compact(colmap[c][1])
            if len(key) > _KV_KEY_MAX_LEN:
                # 'CPO(개인정보보호책임자)' 같은 약어(설명) 키는 괄호를 떼고 길이 판정
                stripped = re.sub(r"\([^()]{1,24}\)$", "", key)
                if stripped and len(stripped) <= _KV_KEY_MAX_LEN:
                    key = stripped
            if (
                partner is not None
                and key
                and len(key) <= _KV_KEY_MAX_LEN
                and not is_marker_value(key)
                and not _NUMERIC_RE.match(key)
            ):
                pair_cells += 2
                idx += 2
            else:
                idx += 1
    feats["kv_pair_coverage"] = pair_cells / float(total_cells) if total_cells else 0.0

    # left_col_hierarchy_score — 왼쪽 컬럼들의 항목 번호 패턴 + 세로 병합
    feats["left_col_hierarchy_score"] = _left_hierarchy_score(region, raw_items)

    # code_header_vocab — 첫 content row 에 약어/코드/정식명칭 류 헤더 존재 여부
    if content_rows:
        first_texts = {compact(t).lower() for _cell, t in rows_map[content_rows[0]].values()}
        if first_texts & {v.lower() for v in _CODE_HEADER_VOCAB}:
            feats["code_header_vocab"] = 1.0
    return feats


def _estimate_header_depth(
    region: "Region", rows_map: Dict[int, Dict[int, Tuple["CellNode", str]]], cc: int
) -> int:
    depth = 0
    skipped = 0
    sparse_limit = max(1, int(0.25 * cc))
    for r in range(region.min_row, min(region.min_row + 9, region.max_row) + 1):
        colmap = rows_map.get(r)
        if not colmap:
            if depth:
                break
            skipped += 1
            if skipped > 6:
                break
            continue
        n = float(len(colmap))
        if depth == 0 and len(colmap) <= sparse_limit and skipped <= 6:
            skipped += 1
            continue
        bold = sum(1 for cell, _t in colmap.values() if cell.style.bold) / n
        numbering = any(infer_numbering_level(t) is not None for _cell, t in colmap.values())
        marker = any(is_marker_cell(cell, t) for cell, t in colmap.values())
        if bold >= 0.5 and not numbering and not marker and len(colmap) >= 2:
            depth += 1
            if depth >= 5:
                break
        else:
            break
    return depth


def _left_hierarchy_score(
    region: "Region", raw_items: List[Tuple[int, int, "CellNode", str]]
) -> float:
    by_col: Dict[int, List[Tuple["CellNode", str]]] = {}
    left_cols = set(range(region.min_col, min(region.min_col + 5, region.max_col + 1)))
    for _r, c, cell, text in raw_items:
        if c in left_cols and text and not is_marker_cell(cell, text):
            by_col.setdefault(c, []).append((cell, text))
    best = 0.0
    for c, items in by_col.items():
        if len(items) < 3:
            continue
        hits = sum(1 for _cell, t in items if infer_numbering_level(t) is not None)
        vmerged = sum(1 for cell, _t in items if cell.merge_orientation == "vertical")
        score = hits / float(len(items)) + 0.3 * (vmerged / float(len(items)))
        best = max(best, min(1.0, score))
    return best


def classify_from_features(f: Dict[str, float]) -> Tuple[str, float, List[str], str]:
    """SoT §10.2 분류 규칙. 반환: (region_type, confidence, warnings, role)."""
    warnings: List[str] = []
    rc = f.get("row_count", 0.0)
    cc = f.get("col_count", 0.0)

    # 제목 전용 영역 (큰 가로 병합 + 소수의 raw 셀)
    if (
        rc <= 3
        and f.get("raw_cells", 0.0) <= 4
        and f.get("marker_ratio", 0.0) == 0.0
        and (
            f.get("horizontal_title_merge", 0.0) >= 0.5
            or (f.get("raw_cells", 0.0) <= 2 and f.get("text_ratio", 0.0) >= 0.99)
        )
    ):
        return "report_section", 0.7, warnings, "title"

    marker = f.get("marker_ratio", 0.0)
    hier = f.get("left_col_hierarchy_score", 0.0)
    kv = f.get("key_value_score", 0.0)

    if marker >= 0.08 and hier >= 0.25:
        return "hierarchical_matrix", min(0.95, 0.85 + 0.2 * marker), warnings, ""
    if marker >= 0.12:
        return "matrix_table", 0.8, warnings, ""
    if f.get("note_score", 0.0) >= 0.55 and marker < 0.05:
        return "note_block", 0.7, warnings, ""
    # form — 전폭 제목 병합 + key-value 쌍 반복 + 숫자 적음 (SoT §10.2 form: row repetition 약함)
    if (
        kv >= 0.5
        and marker < 0.05
        and f.get("horizontal_title_merge", 0.0) >= 0.7
        and rc <= 20
        and f.get("kv_pair_coverage", 0.0) >= 0.75
        and f.get("numeric_ratio", 0.0) <= 0.3
    ):
        return "form", 0.72, warnings, ""
    if f.get("est_header_depth", 0.0) >= 2 and rc >= f.get("est_header_depth", 0.0) + 2:
        return "multi_header_table", 0.75, warnings, ""
    if hier >= 0.4 and rc >= 4 and cc >= 2:
        return "hierarchical_table", 0.8, warnings, ""
    # code_mapping_table — 약어/코드 헤더 어휘가 명시된 2~4열 매핑 표 (SoT §10.2)
    if (
        f.get("code_header_vocab", 0.0) >= 1.0
        and kv >= 0.5
        and marker < 0.05
        and 2 <= cc <= 4
        and f.get("kv_key_len_avg", 99.0) <= _CODE_KEY_AVG_MAX
        and f.get("numeric_ratio", 0.0) <= 0.05
    ):
        return "code_mapping_table", 0.8, warnings, ""
    # 헤더 1행 + 본문 1행 이상이면 flat_table (헤더+1행 표가 kv fallback 으로 새지 않게 rc>=2)
    if f.get("top_row_header_score", 0.0) >= 0.5 and rc >= 2 and cc >= 2:
        return "flat_table", 0.72, warnings, ""
    # code_mapping fallback — 헤더 어휘가 없으면 (짧은 키, 값) 쌍 '구조'가 표를 지배하고
    # 수치가 없어야만 인정한다. 일반 flat table(명단/실적 등)이 코드표로 오인되어
    # 사실을 지어내는 것을 방지 (SoT §10.2 code_mapping_table: 2~4열 '매핑' 구조).
    if (
        kv >= 0.5
        and marker < 0.05
        and 2 <= cc <= 8
        and f.get("kv_key_len_avg", 99.0) <= _CODE_KEY_AVG_MAX
        and f.get("numeric_ratio", 0.0) <= 0.05
        and f.get("kv_pair_coverage", 0.0) >= 0.8
    ):
        return "code_mapping_table", 0.78, warnings, ""
    # 스타일 없는 헤더의 단순 flat table — 밀집 직사각형 그리드 + marker 없음 +
    # 첫 밀집 행이 비숫자 위주 (SoT §10.2 flat_table: 상단 1행 헤더 + 열 유형 반복)
    if (
        rc >= 3
        and cc >= 3
        and marker == 0.0
        and f.get("density", 0.0) >= 0.6
        and f.get("merged_cell_ratio", 0.0) <= 0.2
        and f.get("top_row_header_score", 0.0) >= 0.15
    ):
        return "flat_table", 0.6, warnings, ""
    if kv >= 0.4 and cc <= 6:
        return "key_value_block", 0.65, warnings, ""
    if kv >= 0.25 and rc <= 40 and cc <= 6:
        return "form", 0.6, warnings, ""
    if cc <= 3 and f.get("text_ratio", 0.0) >= 0.8 and f.get("avg_text_len", 0.0) >= 25:
        return "report_section", 0.55, warnings, ""

    warnings.append("region_type_uncertain")
    return "unknown_table", 0.4, warnings, ""

