"""DEPRECATED shim — CGH(hierarchy_tree.merge_hierarchy_rows)로 위임.

기존 Method B(merge_wbs_rows)는 비-leaf 내부노드를 단독 방치해 부모-자식 단절을
일으켰다(사용자 실사용 불만). CGH 로 대체됐으며, 이 모듈은 호출 규약 호환을 위한
얇은 위임 래퍼만 남긴다. 신규 코드는 hierarchy_tree.merge_hierarchy_rows 를 직접 쓸 것.
"""
from __future__ import annotations

from typing import Any, Dict, List

from .hierarchy_tree import merge_hierarchy_rows


def merge_wbs_rows(chunks: List[Dict[str, Any]], *, max_chars: int = 1100,
                   min_numbered_ratio: float = 0.6) -> List[Dict[str, Any]]:
    """호환 shim → merge_hierarchy_rows(CGH)."""
    return merge_hierarchy_rows(chunks, max_chars=max_chars,
                                min_numbered_ratio=min_numbered_ratio)
