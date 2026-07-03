"""chunk 최종 보정 + stats 집계 (pipeline 계약).

- finalize_chunk(chunk, region, canvas, ctx) -> RagChunk | None
- build_stats(chunk_dicts, source_file, canvases, regions, errors) -> dict
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, TYPE_CHECKING

from ..textutil import PARSER_VERSION, stable_id
from .chunk_schema import RagChunk
from .confidence import score_quality
from .content_text_builder import build_content_text
from .keyword_extractor import extract_keywords

if TYPE_CHECKING:
    from ..canvas.sheet_canvas import SheetCanvas
    from ..detection.region import Region
    from ..parsers.base import ParseContext

# metadata.body_rows 가 과도하게 커지지 않도록 절단 (SoT §33.1)
_MAX_BODY_ROWS_IN_METADATA = 50


def _chunk_rows(chunk: RagChunk, region: "Region") -> List[int]:
    """chunk 의 source 행 범위를 region 기준으로 정수 리스트로 반환."""
    src = chunk.source or {}
    start = src.get("start_row")
    end = src.get("end_row")
    if not isinstance(start, int) or not isinstance(end, int) or end < start:
        return []
    return list(range(start, end + 1))


def _fill_source(chunk: RagChunk, canvas: "SheetCanvas", ctx: "ParseContext") -> None:
    src = chunk.source if isinstance(chunk.source, dict) else {}
    src.setdefault("file", chunk.source_file or ctx.source_file)
    src.setdefault("sheet", chunk.sheet or canvas.sheet_name)
    if chunk.range:
        src.setdefault("range", chunk.range)
    elif src.get("range"):
        chunk.range = src["range"]
    chunk.source = src


def _fill_metadata(chunk: RagChunk, region: "Region", canvas: "SheetCanvas", ctx: "ParseContext") -> None:
    md = chunk.metadata if isinstance(chunk.metadata, dict) else {}
    # parser_version 은 quality 에만 둔다 (중복 저장 금지)
    md.pop("parser_version", None)

    md.setdefault("region_id", region.id)
    md.setdefault("sheet_index", canvas.sheet_index)
    md.setdefault("workbook_title", ctx.document_title)
    md.setdefault("is_hidden_sheet", canvas.is_hidden_sheet)
    md.setdefault("contains_formula", canvas.contains_formula)

    if "header_rows" not in md:
        md["header_rows"] = list(region.header_rows)
    if "body_rows" not in md:
        rows = _chunk_rows(chunk, region)
        body_set = set(region.body_rows)
        body_rows = [r for r in rows if r in body_set] if body_set else rows
        if not body_rows and not rows:
            body_rows = list(region.body_rows)
        if len(body_rows) > _MAX_BODY_ROWS_IN_METADATA:
            md["body_row_count"] = len(body_rows)
            body_rows = body_rows[:_MAX_BODY_ROWS_IN_METADATA]
        md["body_rows"] = body_rows

    # 숨김 행/열 포함 여부 힌트 (confidence 감점/경고 근거)
    if "contains_hidden_rows" not in md and canvas.hidden_rows:
        rows = _chunk_rows(chunk, region)
        if any(r in canvas.hidden_rows for r in rows):
            md["contains_hidden_rows"] = True
    if "contains_hidden_cols" not in md and canvas.hidden_cols:
        src = chunk.source or {}
        c0, c1 = src.get("start_col"), src.get("end_col")
        if isinstance(c0, int) and isinstance(c1, int):
            if any(c in canvas.hidden_cols for c in range(c0, c1 + 1)):
                md["contains_hidden_cols"] = True
    chunk.metadata = md


def finalize_chunk(
    chunk: RagChunk,
    region: "Region",
    canvas: "SheetCanvas",
    ctx: "ParseContext",
) -> Optional[RagChunk]:
    """파서가 만든 chunk 의 누락 필드를 보정한다.

    content_text 를 만들 수 없으면 None 을 반환해 emit 을 건너뛴다.
    """
    if chunk is None:
        return None

    # --- 기본 식별 정보 ------------------------------------------------------
    chunk.source_file = chunk.source_file or ctx.source_file
    chunk.sheet = chunk.sheet or canvas.sheet_name
    chunk.region_type = chunk.region_type or region.region_type
    if not chunk.range:
        chunk.range = (chunk.source or {}).get("range") or region.range_a1

    # --- title fallback ------------------------------------------------------
    if not chunk.title:
        chunk.title = (
            region.title
            or ctx.sheet_titles.get(chunk.sheet)
            or ctx.document_title
            or None
        )

    _fill_source(chunk, canvas, ctx)
    _fill_metadata(chunk, region, canvas, ctx)

    # --- content_text --------------------------------------------------------
    if not (chunk.content_text or "").strip():
        chunk.content_text = build_content_text(chunk, region, ctx)
    if not (chunk.content_text or "").strip():
        return None  # 임베딩 불가능한 chunk 는 스킵 (SoT Rule 4)

    # --- keywords -------------------------------------------------------------
    if not chunk.keywords:
        chunk.keywords = extract_keywords(chunk, ctx)

    # --- id --------------------------------------------------------------------
    if not chunk.id:
        chunk.id = stable_id(
            chunk.source_file,
            chunk.sheet,
            chunk.chunk_type,
            chunk.range,
            *chunk.path,
        )

    # --- quality (항상 재계산 — metadata 힌트 반영) ----------------------------
    chunk.quality = score_quality(chunk, region, ctx)
    return chunk


def build_stats(
    chunk_dicts: List[Dict[str, Any]],
    source_file: str,
    canvases: List["SheetCanvas"],
    regions: List["Region"],
    errors: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """emit 결과 요약 stats (CLI stdout / report 용)."""
    chunk_type_counts: Dict[str, int] = {}
    sheet_counts: Dict[str, int] = {}
    confidences: List[float] = []
    review_required = 0

    for record in chunk_dicts:
        ct = record.get("chunk_type", "")
        chunk_type_counts[ct] = chunk_type_counts.get(ct, 0) + 1
        sheet = record.get("sheet", "")
        sheet_counts[sheet] = sheet_counts.get(sheet, 0) + 1
        quality = record.get("quality") or {}
        conf = quality.get("confidence")
        if isinstance(conf, (int, float)):
            confidences.append(float(conf))
        if quality.get("review_required"):
            review_required += 1

    region_type_counts: Dict[str, int] = {}
    for region in regions:
        rt = getattr(region, "region_type", "unknown_table")
        region_type_counts[rt] = region_type_counts.get(rt, 0) + 1

    confidence_stats: Dict[str, Any] = {
        "avg": round(sum(confidences) / len(confidences), 4) if confidences else None,
        "min": min(confidences) if confidences else None,
        "max": max(confidences) if confidences else None,
        "review_required_count": review_required,
    }

    return {
        "source_file": source_file,
        "parser_version": PARSER_VERSION,
        "record_count": len(chunk_dicts),
        "chunk_type_counts": dict(sorted(chunk_type_counts.items())),
        "sheet_counts": dict(sorted(sheet_counts.items())),
        "sheet_count": len(canvases),
        "sheets": [c.sheet_name for c in canvases],
        "region_count": len(regions),
        "region_type_counts": dict(sorted(region_type_counts.items())),
        "validation_errors": {
            "count": len(errors),
            "samples": errors[:5],
        },
        "confidence": confidence_stats,
    }
