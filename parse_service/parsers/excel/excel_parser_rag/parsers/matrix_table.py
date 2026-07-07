"""MatrixTableParser — 매트릭스/계층 매트릭스 표 파서 (SoT §13, §14, §17.5).

행축(계층 path) × 열축(헤더) 구조의 표를 다음 chunk 들로 풀어낸다.

- matrix_fact      : marker/값이 있는 본문 셀 1개당 1개 (SoT §14.2)
- table_row        : 값/메타데이터가 있는 행 1개당 1개 (metadata_cols 포함)
- hierarchy_node   : 항목은 있지만 값이 전혀 없는 행 (SoT §17.4)
- note             : ※/단, 등 주석 행 (SoT §16) — 현재 path 에 attach
- total_row        : 합계 행
- section_summary  : 최상위 path 단위 (SoT §17.2)
- table_summary    : region 전체 1개 (SoT §17.1)

전결표 등 도메인 특화는 plugin(DelegationRulePlugin)이 이 클래스를 재사용한다.
fact_chunk_type / row_chunk_type 클래스 속성을 바꿔 chunk_type 만 치환할 수 있다.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from openpyxl.utils import get_column_letter

from ..chunking.chunk_schema import RagChunk
from ..markerutil import is_ambiguous_marker_cell, is_marker_cell, normalize_marker_cell
from ..textutil import (
    is_note_text,
    is_total_text,
    marker_label_ko,
    one_line,
)
from .base import BaseRegionParser, ParseContext
from .flat_table import body_rows_of, cell_text, flatten_headers, merged_text, region_row_text
from .hierarchy_table import ColHierarchyTracker, HierarchyTracker, SectionCollector, detect_item_text

if TYPE_CHECKING:
    from ..canvas.sheet_canvas import SheetCanvas
    from ..detection.region import Region


def expand_codes(value: str, code_map: Dict[str, str]) -> List[Dict[str, str]]:
    """'법(준감), 내' 같은 약어 문자열을 ctx.code_map 으로 확장한다 (SoT §20.2).

    괄호는 중첩 없이 안/밖을 분리해 각각 약어 후보로 본다.
    확장에 성공한 토큰만 [{"raw": 원문, "expanded": 확장명}] 으로 반환.
    """
    out: List[Dict[str, str]] = []
    if not value or not code_map:
        return out
    for token in re.split(r"[,\n/]+", value):
        token = token.strip().strip("{}").strip()
        if not token:
            continue
        inner = re.findall(r"\(([^()]*)\)", token)
        outer = re.sub(r"\([^()]*\)", "", token).strip()
        candidates = ([outer] if outer else []) + [i.strip() for i in inner if i.strip()]
        expanded = [code_map[c] for c in candidates if c in code_map]
        if expanded:
            out.append({"raw": token, "expanded": " / ".join(expanded)})
    return out


class MatrixTableParser(BaseRegionParser):
    """matrix_table / hierarchical_matrix 파서. 클래스명 고정 (plugin 이 import)."""

    name = "matrix_table"
    region_types = ("matrix_table", "hierarchical_matrix")

    # plugin 이 서브클래스에서 치환할 수 있는 chunk_type
    fact_chunk_type = "matrix_fact"
    row_chunk_type = "table_row"

    fact_confidence = 0.9
    ambiguous_fact_confidence = 0.8
    row_confidence = 0.92
    node_confidence = 0.82
    note_confidence = 0.82
    decision_note_confidence = 0.9
    total_confidence = 0.85
    summary_confidence = 0.9
    section_confidence = 0.86

    # ------------------------------------------------------------------ parse
    def parse(self, region: "Region", canvas: "SheetCanvas", ctx: ParseContext) -> List[RagChunk]:
        hier_cols, matrix_cols, meta_cols = self._resolve_axes(region, canvas)
        tracker = ColHierarchyTracker(hier_cols)
        sections = SectionCollector()
        body_chunks: List[RagChunk] = []
        stats = {"fact_rows": 0, "facts": 0, "nodes": 0, "notes": 0, "totals": 0, "ambiguous": 0}

        for r in body_rows_of(region, canvas):
            if not canvas.row_has_content(r, region.min_col, region.max_col):
                continue
            item_text, item_col, from_merge = detect_item_text(canvas, r, hier_cols, tracker)

            # --- note 행 (SoT §16): 현재 path 에 attach. 병합 marker 상속 금지 ---
            if item_text and is_note_text(item_text):
                values = self._collect_values(canvas, r, matrix_cols, allow_merged=False)
                meta_values = self._collect_meta(canvas, r, meta_cols, allow_merged=False)
                body_chunks.append(
                    self._note_chunk(region, canvas, ctx, r, item_text, tracker.path, values, meta_values)
                )
                stats["notes"] += 1
                sections.touch(tracker.top, r)
                continue

            values = self._collect_values(canvas, r, matrix_cols, allow_merged=True)
            meta_values = self._collect_meta(canvas, r, meta_cols, allow_merged=True)

            # --- 합계 행 ---
            if item_text and is_total_text(item_text):
                body_chunks.append(
                    self._total_chunk(region, canvas, ctx, r, item_text, values, meta_values, tracker.path)
                )
                stats["totals"] += 1
                continue

            # 다중열: 이 행의 모든 계층열 raw 값을 얕은→깊은(sorted) 순서로 각 열 체인에 push.
            # 병합 continuation(빈 raw)·note·total 은 push 안 함(체인 오염/부모 붕괴 방지).
            for c in sorted(hier_cols):
                t = cell_text(canvas.get_cell(r, c))
                if t and not is_note_text(t) and not is_total_text(t):
                    tracker.push(c, t)
            path = tracker.path

            if values or meta_values:
                meta_fields = self._meta_fields(meta_values, ctx)
                body_chunks.append(
                    self._row_chunk(region, canvas, ctx, r, path, values, meta_values, meta_fields, from_merge)
                )
                for v in values:
                    body_chunks.append(
                        self._fact_chunk(region, canvas, ctx, r, path, v, meta_fields, from_merge)
                    )
                    if v["ambiguous"]:
                        stats["ambiguous"] += 1
                stats["fact_rows"] += 1
                stats["facts"] += len(values)
                sections.add_data_row(tracker.top, r, axes=[v["header"] for v in values])
                if len(path) > 1:
                    sections.add_child(tracker.top, r, path[-1])
            elif item_text:
                # 항목은 있지만 marker/값이 전혀 없는 행 (SoT §17.4)
                body_chunks.append(self._hierarchy_chunk(region, canvas, ctx, r, path))
                stats["nodes"] += 1
                sections.touch(tracker.top, r)
                if len(path) > 1:
                    sections.add_child(tracker.top, r, path[-1])

        # --- footer 행 → note ---
        for r in region.footer_rows:
            text = region_row_text(canvas, region, r)
            if text:
                body_chunks.append(self._note_chunk(region, canvas, ctx, r, text, [], [], {}))
                stats["notes"] += 1

        chunks: List[RagChunk] = [
            self._table_summary(region, canvas, ctx, matrix_cols, meta_cols, stats)
        ]
        chunks.extend(body_chunks)
        chunks.extend(
            sections.build_chunks(self, region, canvas, ctx, unit_label="값 행", confidence=self.section_confidence)
        )
        return chunks

    # ---------------------------------------------------------------- 축 결정
    def _resolve_axes(self, region: "Region", canvas: "SheetCanvas"):
        """region 의 hierarchy/matrix/metadata 컬럼을 확정한다. 비어 있으면 방어적 추정."""
        matrix_cols = {int(c): str(n) for c, n in region.matrix_cols.items()}
        meta_cols = {int(c): str(n) for c, n in region.metadata_cols.items()}
        hier_cols = list(region.hierarchy_cols)

        if not matrix_cols:
            headers = flatten_headers(region, canvas, use_region_cols=False)
            sample = body_rows_of(region, canvas)[:200]
            for c in range(region.min_col, region.max_col + 1):
                if c in meta_cols or c in hier_cols:
                    continue
                markers = sum(
                    1 for r in sample
                    if is_marker_cell(canvas.get_cell(r, c), cell_text(canvas.get_cell(r, c)))
                )
                if markers >= 1:
                    matrix_cols[c] = headers.get(c) or get_column_letter(c)

        if not hier_cols:
            first_axis_col = min(matrix_cols) if matrix_cols else region.max_col + 1
            hier_cols = [
                c for c in range(region.min_col, min(first_axis_col - 1, region.max_col) + 1)
                if c not in meta_cols
            ]
            if not hier_cols:
                hier_cols = [region.min_col]
        return hier_cols, matrix_cols, meta_cols

    # ------------------------------------------------------------- 값 수집
    def _collect_values(
        self, canvas: "SheetCanvas", row: int, matrix_cols: Dict[int, str], allow_merged: bool
    ) -> List[Dict[str, Any]]:
        """matrix_cols 중 비어 있지 않은 셀만 (SoT §33.1 — 빈 셀은 fact 미생성)."""
        out: List[Dict[str, Any]] = []
        for c in sorted(matrix_cols):
            cell = canvas.get_cell(row, c)
            text = cell_text(cell)
            from_merge = False
            if not text and allow_merged:
                text = merged_text(cell, ("vertical",))
                from_merge = bool(text)
            if not text:
                continue
            out.append(
                {
                    "col": c,
                    "header": matrix_cols[c],
                    "value": text,
                    # 숫자 0 등 수치 값은 marker 정규화('해당') 대상이 아니다 (SoT §8.1)
                    "normalized": normalize_marker_cell(cell, text),
                    "ambiguous": is_ambiguous_marker_cell(cell, text),
                    "from_merge": from_merge,
                }
            )
        return out

    def _collect_meta(
        self, canvas: "SheetCanvas", row: int, meta_cols: Dict[int, str], allow_merged: bool
    ) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        for c in sorted(meta_cols):
            cell = canvas.get_cell(row, c)
            text = cell_text(cell)
            if not text and allow_merged:
                text = merged_text(cell, ("vertical",))
            if text:
                out.append({"col": c, "name": meta_cols[c], "value": text})
        return out

    def _meta_fields(self, meta_values: List[Dict[str, str]], ctx: ParseContext) -> Dict[str, Any]:
        """metadata_cols 값 + 약어 확장 (fields["합의_확장"] 등) — SoT §20.2."""
        fields: Dict[str, Any] = {}
        for m in meta_values:
            fields[m["name"]] = m["value"]
            expanded = expand_codes(m["value"], ctx.code_map)
            if expanded:
                fields[f"{m['name']}_확장"] = expanded
        return fields

    # ------------------------------------------------------------- chunk 생성
    @staticmethod
    def _value_label(v: Dict[str, Any]) -> str:
        return marker_label_ko(v["normalized"]) if v["normalized"] else v["value"]

    def _fact_chunk(
        self,
        region: "Region",
        canvas: "SheetCanvas",
        ctx: ParseContext,
        row: int,
        path: List[str],
        v: Dict[str, Any],
        meta_fields: Dict[str, Any],
        from_merge: bool,
    ) -> RagChunk:
        chunk = self.new_chunk(
            region, canvas, ctx, self.fact_chunk_type,
            min_row=row, max_row=row, min_col=v["col"], max_col=v["col"],
        )
        path_text = " > ".join(path) if path else (chunk.title or f"행 {row}")
        chunk.path = list(path)
        chunk.fields = {
            "행축": path_text,
            "열축": v["header"],
            "값": v["value"],
            "정규화값": v["normalized"] or "",
            **meta_fields,
        }
        chunk.facts = [
            {
                "subject": path_text,
                "predicate": v["header"],
                "object": v["normalized"] or v["value"],
                "raw_value": v["value"],
            }
        ]
        chunk.content_text = (
            f"{ctx.document_title}의 {canvas.sheet_name} 시트에서 '{path_text}' 항목은 "
            f"'{v['header']}'에 대해 '{self._value_label(v)}'이다."
        )
        chunk.metadata["is_decision_row"] = True
        chunk.metadata["came_from_merged_cell"] = from_merge or v["from_merge"]
        if v["ambiguous"]:
            chunk.metadata["ambiguous_marker"] = True  # 'O'/'0' 모호 marker (SoT §8.1)
        chunk.quality = {
            "confidence": self.ambiguous_fact_confidence if v["ambiguous"] else self.fact_confidence
        }
        return chunk

    def _row_chunk(
        self,
        region: "Region",
        canvas: "SheetCanvas",
        ctx: ParseContext,
        row: int,
        path: List[str],
        values: List[Dict[str, Any]],
        meta_values: List[Dict[str, str]],
        meta_fields: Dict[str, Any],
        from_merge: bool,
    ) -> RagChunk:
        chunk = self.new_chunk(region, canvas, ctx, self.row_chunk_type, min_row=row, max_row=row)
        path_text = " > ".join(path) if path else (chunk.title or f"행 {row}")
        chunk.path = list(path)
        chunk.fields = {"항목": path[-1] if path else "", "경로": path_text}
        for v in values:
            chunk.fields.setdefault(v["header"], v["value"])
        chunk.fields.update(meta_fields)
        chunk.facts = [
            {"predicate": v["header"], "value": v["normalized"] or v["value"], "raw_value": v["value"]}
            for v in values
        ] + [{"predicate": m["name"], "value": m["value"]} for m in meta_values]

        segments = [f"'{v['header']}'은(는) '{self._value_label(v)}'" for v in values]
        segments += [f"{m['name']}은(는) '{m['value']}'" for m in meta_values]
        chunk.content_text = (
            f"{ctx.document_title}의 {canvas.sheet_name} 시트에서 '{path_text}' 항목: "
            + ", ".join(segments) + "이다."
        )
        chunk.metadata["is_decision_row"] = True
        chunk.metadata["has_special_values"] = bool(meta_values)
        chunk.metadata["came_from_merged_cell"] = from_merge
        if any(v["ambiguous"] for v in values):
            chunk.metadata["ambiguous_marker"] = True
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
        chunk.metadata["is_decision_row"] = False
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
        values: List[Dict[str, Any]],
        meta_values: Any,
    ) -> RagChunk:
        meta_values = meta_values or []
        chunk = self.new_chunk(region, canvas, ctx, "note", min_row=row, max_row=row)
        related = " > ".join(path) if path else (chunk.title or ctx.document_title)
        chunk.path = list(path) if path else ([chunk.title] if chunk.title else [])
        chunk.fields = {"주석": text}
        for v in values:
            chunk.fields.setdefault(v["header"], v["value"])
        chunk.fields.update(self._meta_fields(list(meta_values), ctx))
        chunk.content_text = f"{ctx.document_title}의 {related} 관련 주석: {text}"
        is_decision = bool(values or meta_values)
        chunk.metadata["is_decision_row"] = is_decision
        chunk.quality = {
            "confidence": self.decision_note_confidence if is_decision else self.note_confidence
        }
        return chunk

    def _total_chunk(
        self,
        region: "Region",
        canvas: "SheetCanvas",
        ctx: ParseContext,
        row: int,
        item_text: str,
        values: List[Dict[str, Any]],
        meta_values: List[Dict[str, str]],
        path: List[str],
    ) -> RagChunk:
        chunk = self.new_chunk(region, canvas, ctx, "total_row", min_row=row, max_row=row)
        chunk.path = list(path) + [item_text]
        chunk.fields = {"항목": item_text}
        for v in values:
            chunk.fields.setdefault(v["header"], v["value"])
        chunk.fields.update(self._meta_fields(meta_values, ctx))
        chunk.facts = [{"predicate": v["header"], "value": v["value"]} for v in values]
        sentences = ", ".join(f"{v['header']}는 {v['value']}" for v in values)
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
        matrix_cols: Dict[int, str],
        meta_cols: Dict[int, str],
        stats: Dict[str, int],
    ) -> RagChunk:
        summary = self.new_chunk(region, canvas, ctx, "table_summary")
        axes = [matrix_cols[c] for c in sorted(matrix_cols)]
        metas = [meta_cols[c] for c in sorted(meta_cols)]
        summary.path = [summary.title] if summary.title else []
        summary.fields = {
            "범위": region.range_a1,
            "값행수": stats["fact_rows"],
            "fact수": stats["facts"],
            "상위항목수": stats["nodes"],
            "주석수": stats["notes"],
            "열축": axes,
            "메타데이터열": metas,
        }
        text = (
            f"{ctx.document_title}의 {canvas.sheet_name} 시트에 있는 '{summary.title}' 표는 "
            f"행축 항목과 열축({', '.join(axes) if axes else '없음'})으로 구성된 매트릭스형 표이다. "
            f"총 {stats['fact_rows']}개의 값 행과 {stats['nodes']}개의 상위 항목, "
            f"{stats['notes']}개의 주석을 포함한다."
        )
        if metas:
            text += f" 보조 열로 {', '.join(metas)}이(가) 있다."
        summary.content_text = text
        if stats["ambiguous"]:
            summary.metadata["ambiguous_marker_count"] = stats["ambiguous"]
        summary.quality = {"confidence": self.summary_confidence}
        return summary

