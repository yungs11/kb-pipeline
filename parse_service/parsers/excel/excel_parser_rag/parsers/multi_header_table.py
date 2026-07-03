"""MultiHeaderTableParser — 다단 헤더 표 파서 (SoT §10.2 multi_header_table, §11.4).

flatten_headers 가 region.header_rows 의 모든 행을 결합해 컬럼명을 만들기 때문에
행 단위 파싱 로직은 FlatTableParser 와 동일하다. 다단 헤더 특성(헤더 깊이)만
metadata 로 추가 기록한다.
"""

from __future__ import annotations

from typing import List, TYPE_CHECKING

from ..chunking.chunk_schema import RagChunk
from .base import ParseContext
from .flat_table import FlatTableParser

if TYPE_CHECKING:
    from ..canvas.sheet_canvas import SheetCanvas
    from ..detection.region import Region


class MultiHeaderTableParser(FlatTableParser):
    name = "multi_header_table"
    region_types = ("multi_header_table",)

    def parse(self, region: "Region", canvas: "SheetCanvas", ctx: ParseContext) -> List[RagChunk]:
        chunks = super().parse(region, canvas, ctx)
        depth = len(region.header_rows)
        for chunk in chunks:
            chunk.metadata.setdefault("header_rows", list(region.header_rows))
            chunk.metadata.setdefault("header_depth", depth)
        return chunks
