"""도메인 플러그인 인터페이스 (SoT §22).

plugin.match() 가 0.5 이상이면 해당 region 파싱을 plugin 이 가져간다.
plugin 이 없어도 기본 파서가 chunk 를 생성해야 한다 (SoT §22.2).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, TYPE_CHECKING

from ..chunking.chunk_schema import RagChunk
from ..parsers.base import ParseContext

if TYPE_CHECKING:
    from ..canvas.sheet_canvas import SheetCanvas
    from ..detection.region import Region

PLUGIN_MATCH_THRESHOLD = 0.5


class ParserPlugin(ABC):
    name: str = "plugin"
    priority: int = 0  # 높을수록 우선

    @abstractmethod
    def match(self, region: "Region", canvas: "SheetCanvas") -> float:
        """0.0~1.0 매칭 점수."""

    @abstractmethod
    def parse(self, region: "Region", canvas: "SheetCanvas", ctx: ParseContext) -> List[RagChunk]:
        ...
