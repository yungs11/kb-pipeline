"""RagChunk — 최종 JSONL 한 줄 (SoT §6.5, §18) + schema validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..textutil import PARSER_VERSION

CHUNK_TYPES = (
    "table_summary",
    "section_summary",
    "table_row",
    "hierarchy_node",
    "matrix_fact",
    "form_field",
    "form_summary",
    "code_mapping",
    "note",
    "total_row",
    "delegation_rule",       # DelegationRulePlugin (SoT §14.4)
    "unsupported_artifact",
)

REQUIRED_FIELDS = (
    "id",
    "source_file",
    "sheet",
    "range",
    "chunk_type",
    "region_type",
    "title",
    "path",
    "fields",
    "facts",
    "content_text",
    "keywords",
    "source",
    "metadata",
    "quality",
)


@dataclass
class RagChunk:
    id: str = ""
    source_file: str = ""
    sheet: str = ""
    range: str = ""
    chunk_type: str = ""
    region_type: str = ""
    title: Optional[str] = None

    path: List[str] = field(default_factory=list)
    fields: Dict[str, Any] = field(default_factory=dict)
    facts: List[Dict[str, Any]] = field(default_factory=list)
    content_text: str = ""
    keywords: List[str] = field(default_factory=list)

    source: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    quality: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "source_file": self.source_file,
            "sheet": self.sheet,
            "range": self.range,
            "chunk_type": self.chunk_type,
            "region_type": self.region_type,
            "title": self.title,
            "path": self.path,
            "fields": self.fields,
            "facts": self.facts,
            "content_text": self.content_text,
            "keywords": self.keywords,
            "source": self.source,
            "metadata": self.metadata,
            "quality": self.quality,
        }


def validate_chunk_schema(chunk: Dict[str, Any]) -> List[str]:
    """SoT §18 필수 필드/§21.3 review 조건 검증. 위반 목록을 반환 (빈 리스트 = 통과)."""
    errors: List[str] = []
    for key in REQUIRED_FIELDS:
        if key not in chunk:
            errors.append(f"missing field: {key}")
    if errors:
        return errors

    if not chunk["id"]:
        errors.append("id is empty")
    if chunk["chunk_type"] not in CHUNK_TYPES:
        errors.append(f"unknown chunk_type: {chunk['chunk_type']}")
    if not chunk["content_text"] or not str(chunk["content_text"]).strip():
        errors.append("content_text is empty")
    src = chunk["source"]
    if not isinstance(src, dict):
        errors.append("source must be dict")
    else:
        for key in ("file", "sheet", "range"):
            if not src.get(key):
                errors.append(f"source.{key} missing")
    quality = chunk["quality"]
    if not isinstance(quality, dict) or "confidence" in quality and not isinstance(quality["confidence"], (int, float)):
        errors.append("quality.confidence must be numeric")
    elif "confidence" not in quality:
        errors.append("quality.confidence missing")
    else:
        conf = quality["confidence"]
        if not (0.0 <= conf <= 1.0):
            errors.append(f"quality.confidence out of range: {conf}")
        if quality.get("parser_version") != PARSER_VERSION:
            errors.append("quality.parser_version missing/mismatch")
        if "review_required" not in quality:
            errors.append("quality.review_required missing")
    if not isinstance(chunk["path"], list):
        errors.append("path must be list")
    if not isinstance(chunk["keywords"], list):
        errors.append("keywords must be list")
    return errors
