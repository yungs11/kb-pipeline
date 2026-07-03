"""파서 공통 인터페이스 (SoT §6.4 ParsedObject, §22 Plugin 과 짝).

모든 region 파서는 BaseRegionParser 를 상속하고
parse(region, canvas, ctx) -> list[RagChunk] 를 구현한다.

파서가 채워야 하는 것: chunk_type, region_type, title, path, fields, facts,
range/source(start_row 등), metadata 일부, (선택) content_text.
content_text/keywords/quality/id 의 최종 보정은 chunking.chunk_factory.finalize_chunk 가 수행한다.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, TYPE_CHECKING

from ..chunking.chunk_schema import RagChunk
from ..config import ParserConfig
from ..textutil import range_a1

if TYPE_CHECKING:
    from ..canvas.sheet_canvas import SheetCanvas
    from ..detection.region import Region


@dataclass
class ParseContext:
    """파싱 전 과정에서 공유되는 문서 수준 컨텍스트."""

    source_file: str            # 파일명 (경로 제외)
    document_title: str         # 문서 제목 (예: "위임전결 기준표(2026.04.17. 개정)")
    config: ParserConfig
    code_map: Dict[str, str] = field(default_factory=dict)  # 약어 -> 의미 (Index 시트 등)
    plugins: List[Any] = field(default_factory=list)         # ParserPlugin 인스턴스 목록
    sheet_titles: Dict[str, str] = field(default_factory=dict)  # sheet_name -> 시트별 표 제목


@dataclass
class ParsedObject:
    """Region 구조화 파싱의 중간 결과 (SoT §6.4). 파서 내부용 — 최종 출력은 RagChunk."""

    id: str = ""
    source_file: str = ""
    sheet: str = ""
    range: str = ""
    object_type: str = ""
    title: Optional[str] = None
    records: List[Dict[str, Any]] = field(default_factory=list)
    facts: List[Dict[str, Any]] = field(default_factory=list)
    fields: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.5


class BaseRegionParser(ABC):
    """모든 region 파서의 베이스."""

    name: str = "base"
    region_types: Sequence[str] = ()

    @abstractmethod
    def parse(self, region: "Region", canvas: "SheetCanvas", ctx: ParseContext) -> List[RagChunk]:
        ...

    # --- 공용 헬퍼 ----------------------------------------------------------
    def new_chunk(
        self,
        region: "Region",
        canvas: "SheetCanvas",
        ctx: ParseContext,
        chunk_type: str,
        *,
        min_row: Optional[int] = None,
        max_row: Optional[int] = None,
        min_col: Optional[int] = None,
        max_col: Optional[int] = None,
    ) -> RagChunk:
        """region 기반 chunk 뼈대 생성 — source range 자동 구성 (SoT Rule 3)."""
        r0 = min_row if min_row is not None else region.min_row
        r1 = max_row if max_row is not None else region.max_row
        c0 = min_col if min_col is not None else region.min_col
        c1 = max_col if max_col is not None else region.max_col
        rng = range_a1(r0, c0, r1, c1)
        return RagChunk(
            source_file=ctx.source_file,
            sheet=canvas.sheet_name,
            range=rng,
            chunk_type=chunk_type,
            region_type=region.region_type,
            title=region.title or ctx.sheet_titles.get(canvas.sheet_name) or ctx.document_title,
            source={
                "file": ctx.source_file,
                "sheet": canvas.sheet_name,
                "range": rng,
                "start_row": r0,
                "end_row": r1,
                "start_col": c0,
                "end_col": c1,
            },
            metadata={
                "region_id": region.id,
                "sheet_index": canvas.sheet_index,
            },
        )
