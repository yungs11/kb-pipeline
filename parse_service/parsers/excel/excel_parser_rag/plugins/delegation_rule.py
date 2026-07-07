"""DelegationRulePlugin — 위임전결표 도메인 플러그인 (SoT §14.4, §22.2).

기본 MatrixTableParser 출력을 그대로 통과시키되:
- matrix_fact 에 "전결권자" 의미를 부여하고
- 같은 행의 fact 들을 묶어 row 단위 delegation_rule chunk 를 추가 생성한다.

합의/수신 같은 메타데이터 컬럼 값의 약어는 ctx.code_map 으로 확장 병기한다.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

from openpyxl.utils import range_boundaries

from ..chunking.chunk_schema import RagChunk
from ..markerutil import is_ambiguous_marker_cell, is_marker_cell
from ..parsers.base import ParseContext
from ..parsers.hierarchy_table import item_numbering_level
from ..textutil import (
    compact,
    is_note_text,
    one_line,
    range_a1,
)
from .base import ParserPlugin

if TYPE_CHECKING:
    from ..canvas.cell_node import CellNode
    from ..canvas.sheet_canvas import SheetCanvas
    from ..detection.region import Region

# 전결표 계열 키워드 (SoT §14.4 contains_keywords)
DELEGATION_KEYWORDS = ("전결", "위임전결", "합의", "수신", "전결권자")


def _cell_text(cell: "CellNode") -> str:
    """셀의 표시 텍스트 (raw 우선, 병합 logical 보조)."""
    return (
        one_line(cell.normalized_value)
        or one_line(cell.display_value)
        or one_line(cell.raw_value)
        or one_line(cell.logical_value)
    )


def expand_codes(value: str, code_map: Dict[str, str]) -> List[Dict[str, str]]:
    """합의/수신 값의 약어 토큰을 {raw, expanded} 목록으로 변환.

    예: "기,{법(준감)},내" → 기/법/준감/내 각각을 code_map 으로 확장.
    """
    value = one_line(value)
    if not value:
        return []
    tokens: List[str] = []
    for part in re.split(r"[,，/\n]+", value):
        part = part.strip().strip("{}")
        if not part:
            continue
        # 괄호 안 약어는 별도 토큰으로 분리 (중첩 안전)
        inners = re.findall(r"\(([^()]*)\)", part)
        outer = re.sub(r"\([^()]*\)", "", part).strip().strip("()")
        if outer:
            tokens.append(outer)
        for inner in inners:
            inner = inner.strip()
            if inner:
                tokens.append(inner)
    result: List[Dict[str, str]] = []
    seen = set()
    for token in tokens:
        if token in seen:
            continue
        seen.add(token)
        result.append({"raw": token, "expanded": code_map.get(token, "")})
    return result


def _row_key(chunk: RagChunk) -> int:
    """청크의 원본 엑셀 행(정렬용). source.start_row 우선, 없으면 metadata.excel_row, 그래도 없으면 0."""
    src = chunk.source or {}
    r = src.get("start_row")
    if isinstance(r, int):
        return r
    r = (chunk.metadata or {}).get("excel_row")
    return r if isinstance(r, int) else 0


class DelegationRulePlugin(ParserPlugin):
    name = "delegation_rule"
    priority = 10

    # ------------------------------------------------------------------ match
    def match(self, region: "Region", canvas: "SheetCanvas") -> float:
        if not region.matrix_cols:
            return 0.0
        texts: List[str] = [one_line(region.title)]
        texts.extend(one_line(v) for v in region.matrix_cols.values())
        texts.extend(one_line(v) for v in region.metadata_cols.values())
        for row in region.header_rows:
            for cell in canvas.iter_row(row, region.min_col, region.max_col):
                texts.append(_cell_text(cell))
        haystack = compact(" ".join(t for t in texts if t))
        if any(kw in haystack for kw in DELEGATION_KEYWORDS):
            return 1.0
        return 0.0

    # ------------------------------------------------------------------ parse
    def parse(self, region: "Region", canvas: "SheetCanvas", ctx: ParseContext) -> List[RagChunk]:
        # 타 모듈(parsers.matrix_table)은 지연 import — 범용 파서 위에 얹는 구조 (SoT §14.4)
        from ..parsers.matrix_table import MatrixTableParser

        base_chunks = MatrixTableParser().parse(region, canvas, ctx)

        out: List[RagChunk] = []
        row_paths: Dict[int, List[str]] = {}
        note_rows: set[int] = set()

        for chunk in base_chunks:
            if chunk.chunk_type == "matrix_fact":
                self._annotate_matrix_fact(chunk)
            row = self._single_row_of(chunk)
            if row is not None:
                if chunk.chunk_type == "note":
                    note_rows.add(row)
                path = self._item_path(chunk)
                if path and len(path) >= len(row_paths.get(row, [])):
                    row_paths[row] = path
            out.append(chunk)

        out.extend(
            self._build_delegation_rows(region, canvas, ctx, row_paths, note_rows)
        )
        # delegation_rule 만 형제 병합; 비-delegation 청크는 merge_sibling_rules 가 통과시킴.
        from ..chunking.sibling_merger import merge_sibling_rules

        config = getattr(ctx, "config", None)
        max_chars = getattr(config, "delegation_merge_max_chars", 1100) if config is not None else 1100
        merged = merge_sibling_rules(out, max_chars=max_chars)
        # 엑셀 위→아래 행 순서로 안정정렬. 기존엔 base_chunks(note 등) 뒤에 delegation_rows 를 append 해
        # [note 블록][delegation 블록]으로 뒤섞였음. table_summary=맨앞, section_summary=맨뒤,
        # 그 외 body(note/delegation_rule/matrix_fact/table_row/hierarchy_node/total_row)는 start_row 행순.
        # 병합은 정렬 전 완료 → 형제-run 로직 무영향. 병합 그룹 내부 note 는 그룹 뒤로(블록단위 인터리브).
        _cls = {"table_summary": 0, "section_summary": 2}
        return sorted(merged, key=lambda c: (_cls.get(c.chunk_type, 1), _row_key(c)))

    # ------------------------------------------------------- matrix_fact 보강
    def _annotate_matrix_fact(self, chunk: RagChunk) -> None:
        """matrix_fact 의 열축에 '전결권자' 의미를 부여한다."""
        fields = chunk.fields or {}
        col_axis = (
            one_line(fields.get("열축"))
            or one_line(fields.get("column_axis"))
        )
        if col_axis and "전결권자" not in fields:
            fields["전결권자"] = col_axis
        fields.setdefault("열축의미", "전결권자")
        chunk.fields = fields

    # ----------------------------------------------------------------- helpers
    @staticmethod
    def _single_row_of(chunk: RagChunk) -> Optional[int]:
        src = chunk.source or {}
        start, end = src.get("start_row"), src.get("end_row")
        if isinstance(start, int) and start == end:
            return start
        return None

    @staticmethod
    def _item_path(chunk: RagChunk) -> List[str]:
        """chunk.path 에서 표 제목 prefix 를 제거한 항목 경로."""
        path = [one_line(p) for p in (chunk.path or []) if one_line(p)]
        if len(path) > 1 and chunk.title and path[0] == one_line(chunk.title):
            path = path[1:]
        return path

    def _hierarchy_cols(self, region: "Region") -> List[int]:
        if region.hierarchy_cols:
            return list(region.hierarchy_cols)
        if region.matrix_cols:
            first_matrix = min(region.matrix_cols)
            return [c for c in range(region.min_col, first_matrix)]
        return [region.min_col]

    def _body_rows(self, region: "Region") -> List[int]:
        if region.body_rows:
            return sorted(region.body_rows)
        header_max = max(region.header_rows) if region.header_rows else region.min_row - 1
        return [r for r in range(max(region.min_row, header_max + 1), region.max_row + 1)]

    def _row_item_text(self, region: "Region", canvas: "SheetCanvas", row: int) -> Tuple[str, Optional[int], bool]:
        """행의 항목 텍스트. (text, col, came_from_merged_cell)"""
        for col in self._hierarchy_cols(region):
            cell = canvas.get_cell(row, col)
            raw = one_line(cell.normalized_value) or one_line(cell.display_value) or one_line(cell.raw_value)
            if raw:
                return raw, col, False
        for col in self._hierarchy_cols(region):
            cell = canvas.get_cell(row, col)
            logical = one_line(cell.logical_value)
            if logical:
                return logical, col, True
        return "", None, False

    def _is_section_banner_row(self, region: "Region", canvas: "SheetCanvas", row: int) -> bool:
        """전폭(≥80%) 병합 배너 행인가.

        '1. 경영 관리'(A5:H5) 같은 섹션 제목이 병합으로 모든 열에 퍼져 비고/역할 열에
        echo 되면 '비고=배너텍스트' delegation 청크가 잘못 발행된다. 이런 행은 섹션
        제목이므로 규칙 행으로 발행하지 않는다.
        """
        width = region.max_col - region.min_col + 1
        if width < 3:
            return False
        for mr in canvas.merged_ranges:
            try:
                c0, r0, c1, r1 = range_boundaries(mr)
            except Exception:
                continue
            if r0 <= row <= r1 and (c1 - c0 + 1) >= 0.8 * width:
                return True
        return False

    # -------------------------------------------------- delegation_rule 생성
    def _build_delegation_rows(
        self,
        region: "Region",
        canvas: "SheetCanvas",
        ctx: ParseContext,
        row_paths: Dict[int, List[str]],
        note_rows: set,
    ) -> List[RagChunk]:
        chunks: List[RagChunk] = []
        stack: List[str] = []  # MatrixTableParser 경로가 없을 때의 fallback 계층 스택
        title = region.title or ctx.sheet_titles.get(canvas.sheet_name) or ctx.document_title
        matrix_cols = sorted(region.matrix_cols.items())
        metadata_cols = sorted(region.metadata_cols.items())
        hierarchy_cols = self._hierarchy_cols(region)

        for row in self._body_rows(region):
            # --- 경로 추적 (emit 여부와 무관하게 매 행 갱신) ----------------
            known_path = row_paths.get(row)
            item, item_col, came_from_merge = self._row_item_text(region, canvas, row)
            row_is_note = bool(item) and is_note_text(item)

            if known_path:
                stack = list(known_path)
                path = list(known_path)
            elif item and not row_is_note and not came_from_merge:
                level = item_numbering_level(item)
                if level is None:
                    level = hierarchy_cols.index(item_col) if item_col in hierarchy_cols else len(stack)
                level = min(level, len(stack))
                stack = stack[:level] + [item]
                path = list(stack)
            elif stack:
                path = list(stack)
                came_from_merge = True
            elif item and not row_is_note:
                stack = [item]
                path = list(stack)
            else:
                path = []

            if row in note_rows or row_is_note:
                continue  # note 행은 note chunk 가 담당
            if self._is_section_banner_row(region, canvas, row):
                continue  # 전폭 병합 섹션배너 — 규칙 행 아님(비고 echo 차단). stack 은 위에서 갱신됨

            approvers: List[str] = []
            ambiguous = False
            for col, label in matrix_cols:
                cell = canvas.get_cell(row, col)
                value = _cell_text(cell)
                if value and is_marker_cell(cell, value):
                    approvers.append(one_line(label))
                    if is_ambiguous_marker_cell(cell, value):
                        ambiguous = True

            specials: Dict[str, str] = {}
            for col, label in metadata_cols:
                cell = canvas.get_cell(row, col)
                value = _cell_text(cell)
                if not value:
                    continue
                # 병합 헤더가 본문으로 내려온 echo 는 제외
                if compact(value) in (compact(label), "전결권자"):
                    continue
                specials[one_line(label)] = value

            if not approvers and not specials:
                continue
            if not path:
                path = [f"행 {row}"]

            chunks.append(
                self._make_rule_chunk(
                    region, canvas, ctx, row, path, approvers, specials,
                    title=title, ambiguous=ambiguous, came_from_merge=came_from_merge,
                )
            )
        return chunks

    def _make_rule_chunk(
        self,
        region: "Region",
        canvas: "SheetCanvas",
        ctx: ParseContext,
        row: int,
        path: List[str],
        approvers: List[str],
        specials: Dict[str, str],
        *,
        title: Optional[str],
        ambiguous: bool,
        came_from_merge: bool,
    ) -> RagChunk:
        path_text = " > ".join(path)
        rng = range_a1(row, region.min_col, row, region.max_col)

        fields: Dict[str, Any] = {
            "항목": path[-1] if path else "",
            "경로": path_text,
            "전결권자": approvers,
        }
        facts: List[Dict[str, Any]] = [
            {"predicate": "전결권자", "value": a} for a in approvers
        ]
        extras: List[str] = []
        for label, value in specials.items():
            fields[label] = value
            expanded = expand_codes(value, ctx.code_map)
            facts.append({"predicate": label, "value": value, "expanded": expanded})
            names = [e["expanded"] for e in expanded if e["expanded"]]
            display = value + (f" [{', '.join(names)}]" if names else "")
            extras.append(f"{label}: {display}")

        base = f"{ctx.document_title}의 {canvas.sheet_name} 시트에서 '{path_text}' 항목"
        if approvers:
            content = f"{base}의 전결권자는 {', '.join(approvers)}이다."
        else:
            content = f"{base}의 " + ", ".join(
                f"{label}는 {value}이다" for label, value in specials.items()
            ) + "."
        if extras:
            content += " (" + ", ".join(extras) + ")"

        return RagChunk(
            source_file=ctx.source_file,
            sheet=canvas.sheet_name,
            range=rng,
            chunk_type="delegation_rule",
            region_type=region.region_type,
            title=title,
            path=list(path),
            fields=fields,
            facts=facts,
            content_text=content,
            source={
                "file": ctx.source_file,
                "sheet": canvas.sheet_name,
                "range": rng,
                "start_row": row,
                "end_row": row,
                "start_col": region.min_col,
                "end_col": region.max_col,
            },
            metadata={
                "region_id": region.id,
                "sheet_index": canvas.sheet_index,
                "excel_row": row,
                "is_decision_row": True,
                "has_approver": bool(approvers),
                "has_special_values": bool(specials),
                "ambiguous_marker": ambiguous,
                "came_from_merged_cell": came_from_merge,
            },
        )
