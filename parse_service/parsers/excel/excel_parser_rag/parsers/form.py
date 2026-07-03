"""FormParser — 신청서/품의서 등 key-value 문서 파서 (SoT §15).

탐지 패턴:
1. 단일 셀 "키: 값"
2. 라벨 셀 + 오른쪽 값 셀 (가로 병합 건너뛰기 위해 최대 3칸 탐색)
3. 라벨 셀 + 바로 아래 값 셀
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Set, Tuple, TYPE_CHECKING

from ..chunking.chunk_schema import RagChunk
from ..textutil import is_marker_value, is_note_text, is_total_text, one_line
from .base import BaseRegionParser, ParseContext
from .flat_table import cell_text

if TYPE_CHECKING:
    from ..canvas.sheet_canvas import SheetCanvas
    from ..detection.region import Region

# "키: 값" 단일 셀 패턴 (키는 20자 이하)
_KV_RE = re.compile(r"^\s*([^:：]{1,20}?)\s*[:：]\s*(.+)$", re.S)
_MAX_LABEL_LEN = 15


class FormParser(BaseRegionParser):
    name = "form"
    region_types = ("form", "key_value_block")

    field_confidence = 0.85
    summary_confidence = 0.85

    def parse(self, region: "Region", canvas: "SheetCanvas", ctx: ParseContext) -> List[RagChunk]:
        pairs = self._detect_pairs(region, canvas)
        chunks: List[RagChunk] = []

        for p in pairs:
            chunk = self.new_chunk(
                region, canvas, ctx, "form_field",
                min_row=p["min_row"], max_row=p["max_row"],
                min_col=p["min_col"], max_col=p["max_col"],
            )
            chunk.path = ([chunk.title] if chunk.title else []) + [p["name"]]
            chunk.fields = {"field_name": p["name"], "field_value": p["value"]}
            chunk.facts = [{"predicate": p["name"], "value": p["value"]}]
            chunk.content_text = (
                f"{ctx.document_title}에서 '{p['name']}' 항목의 값은 '{p['value']}'이다."
            )
            chunk.quality = {"confidence": self.field_confidence}
            chunks.append(chunk)

        chunks.insert(0, self._form_summary(region, canvas, ctx, pairs))
        return chunks

    # --- 내부 ---------------------------------------------------------------

    def _looks_like_label(self, text: str) -> bool:
        if not text or len(text) > _MAX_LABEL_LEN:
            return False
        if is_marker_value(text) or is_note_text(text) or is_total_text(text):
            return False
        if re.fullmatch(r"[\d\s.,%/\-:]+", text):  # 순수 숫자/날짜류는 라벨 아님
            return False
        return True

    def _detect_pairs(self, region: "Region", canvas: "SheetCanvas") -> List[Dict[str, Any]]:
        pairs: List[Dict[str, Any]] = []
        consumed: Set[Tuple[int, int]] = set()

        for r in range(region.min_row, region.max_row + 1):
            for c in range(region.min_col, region.max_col + 1):
                if (r, c) in consumed:
                    continue
                text = cell_text(canvas.get_cell(r, c))
                if not text:
                    continue

                # 1) "키: 값" 단일 셀
                m = _KV_RE.match(text)
                if m and one_line(m.group(2)):
                    pairs.append({
                        "name": one_line(m.group(1)),
                        "value": one_line(m.group(2)),
                        "min_row": r, "max_row": r, "min_col": c, "max_col": c,
                    })
                    continue

                if not self._looks_like_label(text):
                    continue
                name = text.rstrip(":： ").strip()
                if not name:
                    continue

                # 2) 오른쪽 값 셀 (가로 병합 간격 고려, 최대 3칸)
                found = False
                for nc in range(c + 1, min(c + 3, region.max_col) + 1):
                    nt = cell_text(canvas.get_cell(r, nc))
                    if not nt:
                        continue
                    if not nt.endswith((":", "：")):
                        pairs.append({
                            "name": name, "value": nt,
                            "min_row": r, "max_row": r, "min_col": c, "max_col": nc,
                        })
                        consumed.add((r, nc))
                        found = True
                    break  # 첫 비어있지 않은 셀에서 종료 (라벨이면 포기)
                if found:
                    continue

                # 3) 바로 아래 값 셀
                if r + 1 <= region.max_row:
                    bt = cell_text(canvas.get_cell(r + 1, c))
                    if bt and not self._looks_like_label(bt):
                        pairs.append({
                            "name": name, "value": bt,
                            "min_row": r, "max_row": r + 1, "min_col": c, "max_col": c,
                        })
                        consumed.add((r + 1, c))
        return pairs

    def _form_summary(
        self, region: "Region", canvas: "SheetCanvas", ctx: ParseContext, pairs: List[Dict[str, Any]]
    ) -> RagChunk:
        summary = self.new_chunk(region, canvas, ctx, "form_summary")
        fields: Dict[str, Any] = {}
        for p in pairs:
            fields.setdefault(p["name"], p["value"])
        summary.path = [summary.title] if summary.title else []
        summary.fields = fields
        summary.facts = [{"predicate": k, "value": v} for k, v in fields.items()]
        if fields:
            sentences = ", ".join(f"{k}는 {v}" for k, v in list(fields.items())[:10])
            summary.content_text = (
                f"{ctx.document_title}의 {canvas.sheet_name} 시트에 있는 '{summary.title}' 문서는 "
                f"양식(form) 문서이다. 주요 항목: {sentences}."
            )
        else:
            summary.content_text = (
                f"{ctx.document_title}의 {canvas.sheet_name} 시트 '{summary.title}' 영역은 "
                f"양식(form) 문서로 분류되었으나 key-value 항목이 탐지되지 않았다."
            )
            summary.quality = {"confidence": 0.5}
        if "confidence" not in summary.quality:
            summary.quality = {"confidence": self.summary_confidence}
        summary.metadata["field_count"] = len(fields)
        return summary
