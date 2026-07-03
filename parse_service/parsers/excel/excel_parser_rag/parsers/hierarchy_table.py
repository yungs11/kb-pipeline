"""HierarchyTableParser — 매트릭스가 아닌 계층표 파서 (SoT §13, §17.3, §17.4).

공용 계층 헬퍼도 이 모듈에 둔다 (MatrixTableParser 가 재사용):
- HierarchyTracker  : 항목 번호 패턴 + 컬럼 위치 fallback 기반 hierarchy stack (SoT §13.4)
- detect_item_text  : hierarchy_cols 에서 항목 텍스트 추출 (raw 우선, 병합 logical 보조, SoT §13.5)
- SectionCollector  : 최상위 path 단위 section_summary 집계 (SoT §17.2)
"""

from __future__ import annotations

import re
from collections import Counter, OrderedDict
from typing import Any, Dict, List, Optional, Sequence, Tuple, TYPE_CHECKING

from openpyxl.utils import get_column_letter

from ..chunking.chunk_schema import RagChunk
from ..textutil import infer_numbering_level, is_note_text, is_total_text, one_line
from .base import BaseRegionParser, ParseContext
from .flat_table import body_rows_of, cell_text, flatten_headers, merged_text, region_row_text

if TYPE_CHECKING:
    from ..canvas.sheet_canvas import SheetCanvas
    from ..detection.region import Region

# '5-1.', '5-2.' 같은 복합 번호 섹션 라벨 — 'N.' 섹션과 동급의 최상위 (SoT §13.3 보강).
# textutil.infer_numbering_level 은 'N.' 만 level 0 으로 보므로, 'N-M.' 이 직전의
# '가./나.' 깊이에 끌려 들어가 잘못 중첩되는 것을 여기서 막는다.
_COMPOUND_TOP_NUMBER_RE = re.compile(r"^\d+(?:\s*-\s*\d+)+\s*\.")


def item_numbering_level(value: Any) -> Optional[int]:
    """항목 번호 패턴 레벨. 'N-M.' 복합 번호는 'N.' 과 같은 최상위(0)로 본다."""
    t = one_line(value)
    if not t:
        return None
    if _COMPOUND_TOP_NUMBER_RE.match(t):
        return 0
    return infer_numbering_level(t)


# ---------------------------------------------------------------------------
# 계층 헬퍼
# ---------------------------------------------------------------------------

class HierarchyTracker:
    """항목 번호 패턴(우선) + 컬럼 위치(fallback) 으로 hierarchy stack 을 유지한다.

    stack 항목을 (level, text) 쌍으로 들고 있어 레벨이 비연속(0 → 3)이어도
    sibling 끼리 잘못 중첩되지 않는다.
    """

    def __init__(self, hierarchy_cols: Sequence[int]):
        self.hier_cols = list(hierarchy_cols)
        self._items: List[Tuple[int, str]] = []
        self._last_col: Optional[int] = None
        self._last_level: int = -1
        self._col_levels: Dict[int, int] = {}

    def infer_level(self, text: str, col: int) -> int:
        level = item_numbering_level(text)
        if level is not None:
            return level
        # 번호 패턴이 없으면 컬럼 위치로 추정 (SoT §13.2)
        if self._last_col is not None:
            if col > self._last_col:
                return self._last_level + 1
            if col == self._last_col:
                return max(self._last_level, 0)
            remembered = self._col_levels.get(col)
            if remembered is not None:
                return remembered
        if col in self.hier_cols:
            return self.hier_cols.index(col)
        return (self._last_level + 1) if self._last_col is not None else 0

    def push(self, text: str, col: int) -> List[str]:
        level = max(0, self.infer_level(text, col))
        while self._items and self._items[-1][0] >= level:
            self._items.pop()
        self._items.append((level, text))
        self._last_col = col
        self._last_level = level
        self._col_levels[col] = level
        return self.path

    @property
    def path(self) -> List[str]:
        return [text for _, text in self._items]

    @property
    def top(self) -> str:
        return self._items[0][1] if self._items else ""

    @property
    def last_item(self) -> str:
        return self._items[-1][1] if self._items else ""


def detect_item_text(
    canvas: "SheetCanvas", row: int, hier_cols: Sequence[int], tracker: HierarchyTracker
) -> Tuple[str, Optional[int], bool]:
    """행에서 항목 텍스트를 찾는다 → (text, col, came_from_merged_cell).

    raw 값 우선, 없으면 세로/블록 병합 logical 값 보조 (SoT §13.5).
    병합 logical 이 현재 stack 최상단과 같으면 같은 항목의 연속 행으로 보고
    text="" + came_from_merged_cell=True 를 반환한다.
    """
    for c in hier_cols:
        text = cell_text(canvas.get_cell(row, c))
        if text:
            return text, c, False
    for c in hier_cols:
        cell = canvas.get_cell(row, c)
        logical = merged_text(cell, ("vertical", "block"))
        if not logical:
            continue
        if logical == tracker.last_item:
            return "", c, True  # 직전 항목의 병합 연장
        return logical, c, True
    return "", None, False


class SectionCollector:
    """최상위 path 단위 section_summary 집계 (SoT §17.2)."""

    def __init__(self) -> None:
        self._sections: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()

    def _get(self, name: str, row: int) -> Dict[str, Any]:
        sec = self._sections.get(name)
        if sec is None:
            sec = {"rows": 0, "children": set(), "axes": Counter(), "min_row": row, "max_row": row}
            self._sections[name] = sec
        sec["min_row"] = min(sec["min_row"], row)
        sec["max_row"] = max(sec["max_row"], row)
        return sec

    def touch(self, name: str, row: int) -> None:
        if name:
            self._get(name, row)

    def add_data_row(self, name: str, row: int, axes: Sequence[str] = ()) -> None:
        if not name:
            return
        sec = self._get(name, row)
        sec["rows"] += 1
        for axis in axes:
            sec["axes"][axis] += 1

    def add_child(self, name: str, row: int, child: str) -> None:
        if not name or not child or child == name:
            return
        self._get(name, row)["children"].add(child)

    def build_chunks(
        self,
        parser: BaseRegionParser,
        region: "Region",
        canvas: "SheetCanvas",
        ctx: ParseContext,
        unit_label: str = "데이터 행",
        confidence: float = 0.86,
    ) -> List[RagChunk]:
        chunks: List[RagChunk] = []
        for name, sec in self._sections.items():
            chunk = parser.new_chunk(
                region, canvas, ctx, "section_summary", min_row=sec["min_row"], max_row=sec["max_row"]
            )
            top_axes = [axis for axis, _ in sec["axes"].most_common(3)]
            chunk.path = [name]
            chunk.fields = {
                "섹션": name,
                "데이터행수": sec["rows"],
                "하위항목수": len(sec["children"]),
                "주요열축": top_axes,
            }
            text = (
                f"{ctx.document_title}의 {canvas.sheet_name} 시트에서 '{name}' 섹션은 "
                f"{sec['rows']}개의 {unit_label}과 {len(sec['children'])}개의 하위 항목을 포함한다."
            )
            if top_axes:
                text += f" 주요 열축은 {', '.join(top_axes)}이다."
            chunk.content_text = text
            chunk.quality = {"confidence": confidence}
            chunks.append(chunk)
        return chunks


# ---------------------------------------------------------------------------
# HierarchyTableParser
# ---------------------------------------------------------------------------

class HierarchyTableParser(BaseRegionParser):
    """계층표: 행마다 table_row(path 포함) / hierarchy_node / note + table_summary."""

    name = "hierarchy_table"
    region_types = ("hierarchical_table",)

    row_confidence = 0.88
    node_confidence = 0.82
    note_confidence = 0.82
    total_confidence = 0.85
    summary_confidence = 0.9

    def parse(self, region: "Region", canvas: "SheetCanvas", ctx: ParseContext) -> List[RagChunk]:
        headers = flatten_headers(region, canvas)
        hier_cols = list(region.hierarchy_cols) or [region.min_col]
        meta_cols = {int(c): str(n) for c, n in region.metadata_cols.items()}
        value_cols = [
            c for c in range(region.min_col, region.max_col + 1)
            if c not in hier_cols
        ]
        tracker = HierarchyTracker(hier_cols)
        sections = SectionCollector()
        body_chunks: List[RagChunk] = []
        stats = {"rows": 0, "nodes": 0, "notes": 0, "totals": 0}

        for r in body_rows_of(region, canvas):
            if not canvas.row_has_content(r, region.min_col, region.max_col):
                continue
            item_text, item_col, from_merge = detect_item_text(canvas, r, hier_cols, tracker)

            if item_text and is_note_text(item_text):
                body_chunks.append(self._note_chunk(region, canvas, ctx, r, item_text, tracker.path))
                stats["notes"] += 1
                sections.touch(tracker.top, r)
                continue

            values = self._row_values(canvas, r, value_cols, headers, meta_cols)

            if item_text and is_total_text(item_text):
                body_chunks.append(self._total_chunk(region, canvas, ctx, r, item_text, values, tracker.path))
                stats["totals"] += 1
                continue

            path = tracker.path
            if item_text:
                path = tracker.push(item_text, item_col if item_col is not None else hier_cols[0])

            if values:
                body_chunks.append(
                    self._row_chunk(region, canvas, ctx, r, path, values, from_merge)
                )
                stats["rows"] += 1
                sections.add_data_row(tracker.top, r, axes=list(values.keys()))
                if len(path) > 1:
                    sections.add_child(tracker.top, r, path[-1])
            elif item_text:
                body_chunks.append(self._hierarchy_chunk(region, canvas, ctx, r, path))
                stats["nodes"] += 1
                sections.touch(tracker.top, r)
                if len(path) > 1:
                    sections.add_child(tracker.top, r, path[-1])

        for r in region.footer_rows:
            text = region_row_text(canvas, region, r)
            if text:
                body_chunks.append(self._note_chunk(region, canvas, ctx, r, text, []))
                stats["notes"] += 1

        chunks: List[RagChunk] = [self._table_summary(region, canvas, ctx, headers, stats)]
        chunks.extend(body_chunks)
        chunks.extend(sections.build_chunks(self, region, canvas, ctx))
        return chunks

    # --- 내부 ---------------------------------------------------------------

    def _row_values(
        self,
        canvas: "SheetCanvas",
        row: int,
        value_cols: List[int],
        headers: Dict[int, str],
        meta_cols: Dict[int, str],
    ) -> Dict[str, str]:
        values: Dict[str, str] = {}
        for c in value_cols:
            cell = canvas.get_cell(row, c)
            value = cell_text(cell) or merged_text(cell, ("vertical",))
            if not value:
                continue
            key = meta_cols.get(c) or headers.get(c) or get_column_letter(c)
            values.setdefault(key, value)
        return values

    def _row_chunk(
        self,
        region: "Region",
        canvas: "SheetCanvas",
        ctx: ParseContext,
        row: int,
        path: List[str],
        values: Dict[str, str],
        from_merge: bool,
    ) -> RagChunk:
        chunk = self.new_chunk(region, canvas, ctx, "table_row", min_row=row, max_row=row)
        path_text = " > ".join(path) if path else (chunk.title or "")
        chunk.path = list(path)
        chunk.fields = {"항목": path[-1] if path else "", "경로": path_text, **values}
        chunk.facts = [{"predicate": k, "value": v} for k, v in values.items()]
        sentences = ", ".join(f"{k}는 {v}" for k, v in values.items())
        chunk.content_text = (
            f"{ctx.document_title}의 {canvas.sheet_name} 시트에서 '{path_text}' 항목은 "
            f"다음 값을 가진다: {sentences}. 원본 위치는 {chunk.range}이다."
        )
        chunk.metadata["came_from_merged_cell"] = from_merge
        chunk.quality = {"confidence": self.row_confidence}
        return chunk

    def _hierarchy_chunk(
        self, region: "Region", canvas: "SheetCanvas", ctx: ParseContext, row: int, path: List[str]
    ) -> RagChunk:
        chunk = self.new_chunk(region, canvas, ctx, "hierarchy_node", min_row=row, max_row=row)
        path_text = " > ".join(path)
        chunk.path = list(path)
        chunk.fields = {"항목": path[-1] if path else "", "경로": path_text}
        chunk.content_text = (
            f"{ctx.document_title}의 {canvas.sheet_name} 시트에서 '{path_text}' 항목은 "
            f"하위 항목을 포함하는 상위 항목이다."
        )
        chunk.quality = {"confidence": self.node_confidence}
        return chunk

    def _note_chunk(
        self,
        region: "Region",
        canvas: "SheetCanvas",
        ctx: ParseContext,
        row: int,
        text: str,
        path: List[str],
    ) -> RagChunk:
        chunk = self.new_chunk(region, canvas, ctx, "note", min_row=row, max_row=row)
        related = " > ".join(path) if path else (chunk.title or ctx.document_title)
        chunk.path = list(path) if path else ([chunk.title] if chunk.title else [])
        chunk.fields = {"주석": text}
        chunk.content_text = f"{ctx.document_title}의 {related} 관련 주석: {text}"
        chunk.quality = {"confidence": self.note_confidence}
        return chunk

    def _total_chunk(
        self,
        region: "Region",
        canvas: "SheetCanvas",
        ctx: ParseContext,
        row: int,
        item_text: str,
        values: Dict[str, str],
        path: List[str],
    ) -> RagChunk:
        chunk = self.new_chunk(region, canvas, ctx, "total_row", min_row=row, max_row=row)
        chunk.path = list(path) + [item_text]
        chunk.fields = {"항목": item_text, **values}
        chunk.facts = [{"predicate": k, "value": v} for k, v in values.items()]
        sentences = ", ".join(f"{k}는 {v}" for k, v in values.items())
        chunk.content_text = (
            f"{ctx.document_title}의 {canvas.sheet_name} 시트에서 '{item_text}' 행은 합계 행이다"
            + (f": {sentences}." if sentences else ".")
        )
        chunk.metadata["is_total"] = True
        chunk.quality = {"confidence": self.total_confidence}
        return chunk

    def _table_summary(
        self,
        region: "Region",
        canvas: "SheetCanvas",
        ctx: ParseContext,
        headers: Dict[int, str],
        stats: Dict[str, int],
    ) -> RagChunk:
        summary = self.new_chunk(region, canvas, ctx, "table_summary")
        cols = [headers[c] for c in sorted(headers)][:12]
        summary.path = [summary.title] if summary.title else []
        summary.fields = {
            "범위": region.range_a1,
            "데이터행수": stats["rows"],
            "상위항목수": stats["nodes"],
            "주석수": stats["notes"],
            "컬럼": cols,
        }
        col_text = f" 값 컬럼은 {', '.join(cols)}이다." if cols else ""
        summary.content_text = (
            f"{ctx.document_title}의 {canvas.sheet_name} 시트에 있는 '{summary.title}' 표는 "
            f"계층형 표로, 총 {stats['rows']}개의 데이터 행과 {stats['nodes']}개의 상위 항목, "
            f"{stats['notes']}개의 주석을 포함한다.{col_text}"
        )
        summary.quality = {"confidence": self.summary_confidence}
        return summary
