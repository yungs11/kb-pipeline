"""NoteParser — region 전체가 주석 블록인 경우 (SoT §10.2 note_block, §16).

행 단위로 텍스트를 모으되, ※/주)/단, 등 note prefix 로 시작하는 행에서
새 문단을 시작하고, prefix 없는 후속 행은 직전 문단에 이어붙인다.
"""

from __future__ import annotations

from typing import Any, Dict, List, TYPE_CHECKING

from ..chunking.chunk_schema import RagChunk
from ..textutil import is_note_text
from .base import BaseRegionParser, ParseContext
from .flat_table import region_row_text

if TYPE_CHECKING:
    from ..canvas.sheet_canvas import SheetCanvas
    from ..detection.region import Region


class NoteParser(BaseRegionParser):
    name = "note"
    region_types = ("note_block",)

    note_confidence = 0.82

    def parse(self, region: "Region", canvas: "SheetCanvas", ctx: ParseContext) -> List[RagChunk]:
        paragraphs: List[Dict[str, Any]] = []
        current: Dict[str, Any] = {}

        for r in range(region.min_row, region.max_row + 1):
            text = region_row_text(canvas, region, r)
            if not text:
                current = {}  # 빈 행 = 문단 경계
                continue
            if not current or is_note_text(text):
                current = {"lines": [text], "min_row": r, "max_row": r}
                paragraphs.append(current)
            else:
                current["lines"].append(text)
                current["max_row"] = r

        chunks: List[RagChunk] = []
        related = region.title or ctx.sheet_titles.get(canvas.sheet_name) or ctx.document_title
        for p in paragraphs:
            note_text = " ".join(p["lines"])
            chunk = self.new_chunk(region, canvas, ctx, "note", min_row=p["min_row"], max_row=p["max_row"])
            chunk.path = [related] if related else []
            chunk.fields = {"주석": note_text}
            chunk.content_text = f"{ctx.document_title}의 {related} 관련 주석: {note_text}"
            chunk.metadata["related_region_id"] = region.id
            chunk.quality = {"confidence": self.note_confidence}
            chunks.append(chunk)
        return chunks
