"""파서 설정 (SoT §5.1 입력 옵션, §34 Config Override)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_CHUNK_PROFILES = [
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
]


@dataclass
class RegionOverride:
    """특정 시트의 특정 범위에 대한 수동 지정 (SoT §34)."""

    range: str
    region_type: Optional[str] = None
    header_rows: Optional[List[int]] = None
    hierarchy_cols: Optional[List[int]] = None
    matrix_cols: Optional[List[int]] = None
    metadata_cols: Dict[str, int] = field(default_factory=dict)


@dataclass
class SheetOverride:
    ignore_ranges: List[str] = field(default_factory=list)
    regions: List[RegionOverride] = field(default_factory=list)
    skip: bool = False


@dataclass
class ParserConfig:
    language: str = "ko"
    parse_hidden_rows: bool = False
    parse_hidden_sheets: bool = False
    formula_mode: str = "cached_value"  # cached_value | formula_text
    max_cells_per_sheet: int = 500_000
    gap_tolerance_row: int = 1
    gap_tolerance_col: int = 1
    max_header_depth: int = 5
    min_confidence: float = 0.0  # 이 미만 chunk는 emit 시 drop (0 = drop 없음)
    max_chunks_per_type: Optional[int] = None
    chunk_profiles: List[str] = field(default_factory=lambda: list(DEFAULT_CHUNK_PROFILES))
    emit_debug: bool = False
    sheet_overrides: Dict[str, SheetOverride] = field(default_factory=dict)
    document_title: Optional[str] = None  # 미지정 시 자동 추출
    delegation_merge_max_chars: int = 1100  # delegation_rule 형제 병합 한도(문자). 0=비활성
    numbering_merge_max_chars: int = 1100   # kordoc 십진번호(WBS) 병합 한도(문자). 0=비활성
    row_content_max_chars: int = 3000       # table_row content_text 상한(문자). 넓은 행 짤림 방지
    row_embedding_max_chars: int = 4000     # table_row core/embedding_text 상한(문자, BGE-M3 8192토큰 내)

    # 백엔드 (SoT 통합설계). "kordoc" = kordoc .md 기반 (기본), "openpyxl" = 기존 region 파서
    backend: str = "kordoc"
    kordoc_md_path: Optional[str] = None   # 단일 .md 직접 지정
    kordoc_md_dir: Optional[str] = None    # <stem>.md 를 찾을 디렉토리
    kordoc_bin: Optional[str] = None       # kordoc CLI (없으면 md 자동생성 불가 → md 필수)
    kordoc_md_out: Optional[str] = None    # 자동생성 md 저장 위치 (기본 임시디렉토리)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ParserConfig":
        cfg = cls()
        overrides = data.pop("sheet_overrides", {}) or {}
        for key, value in data.items():
            if hasattr(cfg, key):
                setattr(cfg, key, value)
        for sheet_name, ov in overrides.items():
            regions = [
                RegionOverride(
                    range=r["range"],
                    region_type=r.get("region_type"),
                    header_rows=r.get("header_rows"),
                    hierarchy_cols=r.get("hierarchy_cols"),
                    matrix_cols=r.get("matrix_cols"),
                    metadata_cols=r.get("metadata_cols", {}) or {},
                )
                for r in (ov.get("regions") or [])
            ]
            cfg.sheet_overrides[sheet_name] = SheetOverride(
                ignore_ranges=ov.get("ignore_ranges", []) or [],
                regions=regions,
                skip=bool(ov.get("skip", False)),
            )
        return cfg

