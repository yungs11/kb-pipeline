"""파서 선택 registry (SoT §22, §35 select_parser).

우선순위:
1. region.role == 'title' 이고 시트에 다른 내용(표 등)이 있으면 → None
   (표 위에 붙은 제목 영역은 chunk 미생성. 단, 시트 전체가 그 텍스트 블록뿐이면
   시트가 통째로 누락되므로 일반 파서로 넘겨 최소 1개 chunk 를 남긴다 — SoT §16/§17)
2. ctx.plugins 를 priority 내림차순으로 순회, match >= PLUGIN_MATCH_THRESHOLD 면 plugin
3. region_type → 기본 파서 매핑 (없으면 FlatTableParser fallback)
"""

from __future__ import annotations

from typing import Dict, Optional, Type, TYPE_CHECKING

from ..plugins.base import PLUGIN_MATCH_THRESHOLD
from .base import BaseRegionParser, ParseContext
from .code_mapping import CodeMappingParser
from .flat_table import FlatTableParser
from .form import FormParser
from .hierarchy_table import HierarchyTableParser
from .matrix_table import MatrixTableParser
from .multi_header_table import MultiHeaderTableParser
from .note import NoteParser

if TYPE_CHECKING:
    from ..canvas.sheet_canvas import SheetCanvas
    from ..detection.region import Region

_REGION_PARSER_MAP: Dict[str, Type[BaseRegionParser]] = {
    "hierarchical_matrix": MatrixTableParser,
    "matrix_table": MatrixTableParser,
    "hierarchical_table": HierarchyTableParser,
    "multi_header_table": MultiHeaderTableParser,
    "flat_table": FlatTableParser,
    "form": FormParser,
    "key_value_block": FormParser,
    "code_mapping_table": CodeMappingParser,
    "note_block": NoteParser,
    "report_section": FlatTableParser,
    "unknown_table": FlatTableParser,
}

_instances: Dict[Type[BaseRegionParser], BaseRegionParser] = {}


def _parser_for(region_type: str) -> BaseRegionParser:
    cls = _REGION_PARSER_MAP.get(region_type, FlatTableParser)
    if cls not in _instances:
        _instances[cls] = cls()
    return _instances[cls]


def _sheet_has_content_outside(region: "Region", canvas: "SheetCanvas") -> bool:
    """region 밖에 시트의 다른 내용(표 본문 등)이 존재하는지."""
    for cell in canvas.non_empty_cells():
        if not region.contains(cell.row, cell.col):
            return True
    return False


def select_parser(region: "Region", canvas: "SheetCanvas", ctx: ParseContext):
    """region 에 맞는 파서/플러그인을 반환한다. (표에 붙은) 제목 영역은 None."""
    if region.role == "title" and _sheet_has_content_outside(region, canvas):
        return None

    plugins = sorted(ctx.plugins or [], key=lambda p: getattr(p, "priority", 0), reverse=True)
    for plugin in plugins:
        try:
            score = plugin.match(region, canvas)
        except Exception:
            continue
        if score >= PLUGIN_MATCH_THRESHOLD:
            return plugin

    return _parser_for(region.region_type)

