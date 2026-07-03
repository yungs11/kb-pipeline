"""ApprovalMatrixPlugin — 일반 권한/승인 매트릭스 도메인 플러그인 (SoT §22.2, 선택적).

전결표 외의 권한표/역할표/접근권한표를 대상으로 하며,
MatrixTableParser 출력에 '권한 보유 주체' 의미를 보강해 통과시킨다.
DelegationRulePlugin(우선순위 10)이 매칭되는 region 은 그쪽이 가져간다.
"""

from __future__ import annotations

from typing import List, TYPE_CHECKING

from ..chunking.chunk_schema import RagChunk
from ..parsers.base import ParseContext
from ..textutil import compact, one_line
from .base import ParserPlugin

if TYPE_CHECKING:
    from ..canvas.sheet_canvas import SheetCanvas
    from ..detection.region import Region

APPROVAL_KEYWORDS = ("권한", "승인", "결재", "역할", "담당", "접근", "R&R")
# 전결표 키워드가 있으면 DelegationRulePlugin 영역이므로 양보
_DELEGATION_KEYWORDS = ("전결", "위임전결")


class ApprovalMatrixPlugin(ParserPlugin):
    name = "approval_matrix"
    priority = 5

    def match(self, region: "Region", canvas: "SheetCanvas") -> float:
        if not region.matrix_cols:
            return 0.0
        texts: List[str] = [one_line(region.title)]
        texts.extend(one_line(v) for v in region.matrix_cols.values())
        texts.extend(one_line(v) for v in region.metadata_cols.values())
        for row in region.header_rows:
            for cell in canvas.iter_row(row, region.min_col, region.max_col):
                texts.append(
                    one_line(cell.normalized_value)
                    or one_line(cell.display_value)
                    or one_line(cell.logical_value)
                )
        haystack = compact(" ".join(t for t in texts if t))
        if any(kw in haystack for kw in _DELEGATION_KEYWORDS):
            return 0.0
        if any(compact(kw) in haystack for kw in APPROVAL_KEYWORDS):
            return 0.8
        return 0.0

    def parse(self, region: "Region", canvas: "SheetCanvas", ctx: ParseContext) -> List[RagChunk]:
        # 타 모듈(parsers.matrix_table)은 지연 import
        from ..parsers.matrix_table import MatrixTableParser

        chunks = MatrixTableParser().parse(region, canvas, ctx)
        for chunk in chunks:
            if chunk.chunk_type != "matrix_fact":
                continue
            fields = chunk.fields or {}
            col_axis = one_line(fields.get("열축")) or one_line(fields.get("column_axis"))
            if col_axis and "권한주체" not in fields:
                fields["권한주체"] = col_axis
            fields.setdefault("열축의미", "권한주체")
            chunk.fields = fields
        return chunks
