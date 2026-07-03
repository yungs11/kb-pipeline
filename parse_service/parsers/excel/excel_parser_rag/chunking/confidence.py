"""confidence scoring (SoT §21).

region detection/classification, header 명확성, path 존재, content_text 품질,
marker 모호성, 숨김 행/열 포함 여부를 가중평균하고 review_required 를 판정한다.
"""

from __future__ import annotations

from typing import Any, Dict, List, TYPE_CHECKING

from ..textutil import PARSER_VERSION

if TYPE_CHECKING:
    from ..chunking.chunk_schema import RagChunk
    from ..detection.region import Region
    from ..parsers.base import ParseContext

# path 가 있어야 자연스러운 chunk_type
_PATH_EXPECTED_TYPES = {
    "table_row",
    "matrix_fact",
    "hierarchy_node",
    "section_summary",
    "delegation_rule",
    "note",
    "total_row",
}

# 헤더 개념이 약한 region_type (form/주석/코드표 등)
_HEADERLESS_REGION_TYPES = {
    "form",
    "key_value_block",
    "note_block",
    "code_mapping_table",
    "report_section",
}

# 가중치 (SoT §21.1)
_WEIGHTS = {
    "region": 0.30,    # region detection + classification
    "header": 0.20,    # header 명확성
    "path": 0.20,      # path 복원
    "content": 0.20,   # content_text 품질
    "structure": 0.10, # 구조 분류 자체의 신뢰
}


def _header_score(chunk: "RagChunk", region: "Region") -> float:
    if region.region_type in _HEADERLESS_REGION_TYPES:
        return 0.8
    if region.header_rows:
        return 1.0
    if chunk.metadata.get("header_rows"):
        return 0.9
    return 0.4


def _path_score(chunk: "RagChunk") -> float:
    has_path = bool([p for p in (chunk.path or []) if str(p).strip()])
    if has_path:
        return 1.0
    return 0.5 if chunk.chunk_type in _PATH_EXPECTED_TYPES else 0.9


def _content_score(chunk: "RagChunk") -> float:
    text = (chunk.content_text or "").strip()
    if not text:
        return 0.0
    length = len(text)
    if length < 15:
        return 0.6
    if length > 600:
        return 0.8  # 너무 긴 문장은 임베딩 품질 저하 (SoT §33.2)
    return 1.0


def _structure_score(chunk: "RagChunk", region: "Region") -> float:
    if region.region_type == "unknown_table":
        return 0.3
    if chunk.chunk_type == "unsupported_artifact":
        return 0.4
    return 1.0


def score_quality(chunk: "RagChunk", region: "Region", ctx: "ParseContext") -> Dict[str, Any]:
    """SoT §21 — quality dict 반환.

    반환: {"confidence": float(소수 2자리), "review_required": bool,
           "warnings": [...], "parser_version": PARSER_VERSION}
    """
    metadata = chunk.metadata or {}
    warnings: List[str] = []

    region_score = max(0.0, min(1.0, float(region.confidence)))
    header_score = _header_score(chunk, region)
    path_score = _path_score(chunk)
    content_score = _content_score(chunk)
    structure_score = _structure_score(chunk, region)

    confidence = (
        region_score * _WEIGHTS["region"]
        + header_score * _WEIGHTS["header"]
        + path_score * _WEIGHTS["path"]
        + content_score * _WEIGHTS["content"]
        + structure_score * _WEIGHTS["structure"]
    )

    # --- 감점 (SoT §21.2) + 파서가 metadata 에 남긴 힌트 반영 ----------------
    if metadata.get("ambiguous_marker"):
        confidence -= 0.10
        warnings.append("ambiguous marker (O/o/0) detected")
    if metadata.get("came_from_merged_cell"):
        confidence -= 0.03
    if metadata.get("contains_hidden_rows") or metadata.get("contains_hidden_cols") or metadata.get("contains_hidden"):
        confidence -= 0.05
        warnings.append("chunk includes hidden rows/cols")
    if metadata.get("path_uncertain"):
        confidence -= 0.08
        warnings.append("hierarchy path reconstruction uncertain")
    if metadata.get("missing_row_label"):
        confidence -= 0.08
        warnings.append("matrix value without row label")
    if metadata.get("missing_column_axis"):
        confidence -= 0.08
        warnings.append("marker without column axis")

    if header_score < 0.5:
        warnings.append("header rows unclear")
    if path_score < 1.0 and chunk.chunk_type in _PATH_EXPECTED_TYPES:
        warnings.append("path missing for row-level chunk")
    if content_score == 0.0:
        warnings.append("content_text empty")

    # region 수준 경고는 그대로 승계 (소폭 감점)
    region_warnings = [str(w) for w in (region.warnings or []) if str(w).strip()]
    confidence -= min(0.10, 0.02 * len(region_warnings))

    confidence = max(0.05, min(0.99, confidence))
    confidence = round(confidence, 2)

    # --- review_required (SoT §21.3) ----------------------------------------
    source_range = (chunk.source or {}).get("range") or chunk.range
    review_required = (
        confidence < 0.7
        or region.region_type == "unknown_table"
        or header_score < 0.5
        or not (chunk.content_text or "").strip()
        or not source_range
    )

    # 중복 제거 (순서 보존)
    merged_warnings: List[str] = []
    seen = set()
    for w in region_warnings + warnings:
        if w not in seen:
            seen.add(w)
            merged_warnings.append(w)

    return {
        "confidence": confidence,
        "review_required": bool(review_required),
        "warnings": merged_warnings,
        "parser_version": PARSER_VERSION,
    }

