"""전체 파이프라인 오케스트레이션 (SoT §4, §35).

이 모듈이 각 하위 모듈의 공개 함수 시그니처를 고정한다:

- loaders.workbook_loader.load_workbook_for_parsing(path, config) -> (data_wb, formula_wb|None)
- loaders.workbook_loader.should_skip_sheet(ws, config) -> bool
- loaders.xlsx_loader.build_sheet_canvas(ws, formula_ws, workbook_name, sheet_index, config) -> SheetCanvas
- canvas.merge_normalizer.normalize_merged_cells(canvas) -> None
- canvas.feature_extractor.extract_cell_features(canvas) -> None
- detection.region_detector.detect_regions(canvas, config) -> list[Region]
- detection.title_detector.attach_title(region, canvas, config) -> None
- detection.title_detector.extract_document_title(canvases, config) -> str
- classification.region_classifier.classify_region(region, canvas, config) -> None
- detection.header_detector.detect_headers(region, canvas, config) -> None
- detection.footer_detector.detect_footers(region, canvas, config) -> None
- parsers.registry.select_parser(region, canvas, ctx) -> 파서 또는 플러그인 (parse(region, canvas, ctx) 제공)
- parsers.code_mapping.build_code_map(region_canvas_pairs) -> dict[str, str]
- chunking.chunk_factory.finalize_chunk(chunk, region, canvas, ctx) -> RagChunk | None
- chunking.chunk_factory.build_stats(chunk_dicts, source_file, canvases, regions, errors) -> dict

규약:
- override(config.sheet_overrides)로 region_type/header_rows 등이 미리 지정된 Region 은
  classify_region/detect_headers 가 덮어쓰지 않는다.
- 모든 chunk 는 emit 전에 validate_chunk_schema 를 통과해야 한다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .canvas.feature_extractor import extract_cell_features
from .canvas.merge_normalizer import normalize_merged_cells
from .canvas.sheet_canvas import SheetCanvas
from .chunking.chunk_factory import build_stats, finalize_chunk
from .chunking.chunk_schema import CHUNK_TYPES, RagChunk, validate_chunk_schema
from .classification.region_classifier import classify_region, refine_unknown_region
from .config import ParserConfig
from .detection.footer_detector import detect_footers
from .detection.header_detector import detect_headers
from .detection.region import Region
from .detection.region_detector import detect_regions
from .detection.title_detector import attach_title, extract_document_title
from .loaders.workbook_loader import load_workbook_for_parsing, should_skip_sheet
from .loaders.xlsx_loader import build_sheet_canvas
from .parsers.base import ParseContext
from .parsers.code_mapping import build_code_map
from .parsers.registry import select_parser
from .plugins import default_plugins


def build_canvases(input_file: Path, config: ParserConfig) -> List[SheetCanvas]:
    data_wb, formula_wb = load_workbook_for_parsing(input_file, config)
    canvases: List[SheetCanvas] = []
    for idx, ws in enumerate(data_wb.worksheets):
        if should_skip_sheet(ws, config):
            continue
        formula_ws = formula_wb[ws.title] if formula_wb is not None and ws.title in formula_wb.sheetnames else None
        canvas = build_sheet_canvas(
            ws,
            formula_ws,
            workbook_name=Path(input_file).name,
            sheet_index=idx,
            config=config,
        )
        normalize_merged_cells(canvas)
        extract_cell_features(canvas)
        canvases.append(canvas)
    return canvases


def detect_and_classify(canvases: List[SheetCanvas], config: ParserConfig) -> List[Tuple[Region, SheetCanvas]]:
    pairs: List[Tuple[Region, SheetCanvas]] = []
    for canvas in canvases:
        for region in detect_regions(canvas, config):
            attach_title(region, canvas, config)
            classify_region(region, canvas, config)
            detect_headers(region, canvas, config)
            refine_unknown_region(region, canvas)
            detect_footers(region, canvas, config)
            pairs.append((region, canvas))
    return pairs


def parse_excel_for_rag(
    input_file: str | Path,
    config: Optional[ParserConfig] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """엑셀 1개 파일 → (chunk dict 리스트, stats dict)."""
    config = config or ParserConfig()
    input_path = Path(input_file)
    source_file = input_path.name

    canvases = build_canvases(input_path, config)
    region_pairs = detect_and_classify(canvases, config)

    document_title = config.document_title or extract_document_title(canvases, config) or input_path.stem
    code_map = build_code_map(
        [(r, c) for r, c in region_pairs if r.region_type == "code_mapping_table"]
    )

    ctx = ParseContext(
        source_file=source_file,
        document_title=document_title,
        config=config,
        code_map=code_map,
        plugins=default_plugins(),
        sheet_titles={},
    )
    for region, canvas in region_pairs:
        if region.title and canvas.sheet_name not in ctx.sheet_titles:
            ctx.sheet_titles[canvas.sheet_name] = region.title

    all_chunks: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    type_counts: Dict[str, int] = {}

    for region, canvas in region_pairs:
        parser = select_parser(region, canvas, ctx)
        if parser is None:
            continue
        chunks: List[RagChunk] = parser.parse(region, canvas, ctx)
        for chunk in chunks:
            finalized = finalize_chunk(chunk, region, canvas, ctx)
            if finalized is None:
                continue
            record = finalized.to_dict()

            # chunk_profiles 필터 (플러그인 고유 타입은 항상 통과)
            ct = record["chunk_type"]
            if ct in CHUNK_TYPES and config.chunk_profiles and ct not in config.chunk_profiles:
                if ct in ("delegation_rule", "unsupported_artifact"):
                    pass
                else:
                    continue
            if config.max_chunks_per_type is not None and type_counts.get(ct, 0) >= config.max_chunks_per_type:
                continue
            if config.min_confidence and record["quality"].get("confidence", 0.0) < config.min_confidence:
                continue

            violations = validate_chunk_schema(record)
            if violations:
                errors.append({"id": record.get("id"), "chunk_type": ct, "violations": violations})
                continue
            type_counts[ct] = type_counts.get(ct, 0) + 1
            all_chunks.append(record)

    stats = build_stats(all_chunks, source_file, canvases, [r for r, _ in region_pairs], errors)
    return all_chunks, stats
