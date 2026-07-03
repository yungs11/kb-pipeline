"""Region 유형 분류기 (SoT §10).

classify_region(region, canvas, config):
- region feature 를 계산해 region.features 에 기록
- region_type 이 이미 지정돼 있으면(override 등) 분류를 덮어쓰지 않음
- 아니면 규칙 기반 분류 결과로 region_type / confidence / warnings / role 갱신
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..config import ParserConfig
from .rules import classify_from_features, compute_region_features

if TYPE_CHECKING:
    from ..canvas.sheet_canvas import SheetCanvas
    from ..detection.region import Region


def classify_region(region: "Region", canvas: "SheetCanvas", config: ParserConfig) -> None:
    features = compute_region_features(region, canvas)
    region.features.update(features)

    if region.region_type != "unknown_table":
        # override 또는 선행 단계가 이미 지정 — 신뢰하고 유지 (SoT §34)
        region.confidence = max(region.confidence, 0.9)
        return

    region_type, confidence, warnings, role = classify_from_features(features)
    region.region_type = region_type
    region.confidence = confidence
    for w in warnings:
        if w not in region.warnings:
            region.warnings.append(w)
    if role and region.role == "body":
        region.role = role


def refine_unknown_region(region: "Region", canvas: "SheetCanvas") -> None:
    """detect_headers 이후 호출하는 2차 분류 (SoT §10/§11).

    `unknown_table` 로 남은 region 이라도 헤더 감지기가 명확한 header_rows 를
    찾았고 본문이 표 형태(텍스트 위주·marker 없음·다열·본문 2행+)이면 `flat_table`
    로 승급한다. 표 위에 붙은 제목/메타 행 때문에 `top_row_header_score` 가
    헤더 행을 놓쳐 unknown 으로 떨어지는 경우를 보정한다.

    `unknown_table` 와 `flat_table` 은 동일한 FlatTableParser 로 파싱되므로 (registry)
    chunk 출력은 그대로이고, region_type 라벨/confidence/review_required 만 정확해진다.
    이미 분류된 region(override 포함)은 건드리지 않는다.
    """
    if region.region_type != "unknown_table":
        return
    if not region.header_rows:
        return
    f = region.features or {}
    if f.get("marker_ratio", 0.0) >= 0.08:
        return  # matrix/체크표 성격 — flat 로 단정하지 않는다
    if f.get("text_ratio", 0.0) < 0.5:
        return  # 수치/기타 위주 — 표 본문으로 단정하지 않는다
    if region.col_count < 2:
        return
    if region.row_count - len(region.header_rows) < 2:
        return  # 본문 행이 부족

    region.region_type = "flat_table"
    region.confidence = max(region.confidence, 0.65)
    region.warnings = [w for w in region.warnings if w != "region_type_uncertain"]
    if "type_inferred_from_headers" not in region.warnings:
        region.warnings.append("type_inferred_from_headers")

