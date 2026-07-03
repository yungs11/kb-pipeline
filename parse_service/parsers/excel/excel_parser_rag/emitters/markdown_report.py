"""파싱 결과 markdown report (SoT §5.2 보조 출력)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List


def _table(headers: List[str], rows: List[List[Any]]) -> List[str]:
    lines = ["| " + " | ".join(headers) + " |"]
    lines.append("|" + "|".join("---" for _ in headers) + "|")
    for row in rows:
        lines.append("| " + " | ".join(str(v) for v in row) + " |")
    return lines


def _truncate(text: Any, limit: int = 160) -> str:
    t = str(text or "").replace("\n", " ").strip()
    return t if len(t) <= limit else t[: limit - 1] + "…"


def write_report(
    chunk_dicts: List[Dict[str, Any]],
    stats: Dict[str, Any],
    path: str | Path,
    sample_size: int = 12,
) -> None:
    """사람이 읽는 파싱 리포트를 markdown 으로 기록한다."""
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    lines: List[str] = []
    lines.append(f"# Excel Parser-RAG Report — {stats.get('source_file', '')}")
    lines.append("")
    lines.append(f"- parser_version: `{stats.get('parser_version', '')}`")
    lines.append(f"- 총 chunk 수: **{stats.get('record_count', len(chunk_dicts))}**")
    lines.append(f"- 시트 수: {stats.get('sheet_count', '')}")
    lines.append(f"- region 수: {stats.get('region_count', '')}")
    conf = stats.get("confidence") or {}
    lines.append(
        f"- confidence: avg={conf.get('avg')} / min={conf.get('min')} / max={conf.get('max')}"
        f" / review_required={conf.get('review_required_count')}"
    )
    lines.append("")

    # chunk_type 분포
    lines.append("## Chunk Type 분포")
    lines.append("")
    type_counts = stats.get("chunk_type_counts") or {}
    lines.extend(_table(["chunk_type", "count"], [[k, v] for k, v in type_counts.items()]))
    lines.append("")

    # 시트별 분포
    lines.append("## 시트별 chunk 수")
    lines.append("")
    sheet_counts = stats.get("sheet_counts") or {}
    lines.extend(_table(["sheet", "count"], [[k, v] for k, v in sheet_counts.items()]))
    lines.append("")

    # region 분포
    lines.append("## Region Type 분포")
    lines.append("")
    region_counts = stats.get("region_type_counts") or {}
    lines.extend(_table(["region_type", "count"], [[k, v] for k, v in region_counts.items()]))
    lines.append("")

    # validation errors
    errors = stats.get("validation_errors") or {}
    lines.append("## Schema Validation")
    lines.append("")
    lines.append(f"- 위반 chunk 수: {errors.get('count', 0)}")
    for sample in errors.get("samples") or []:
        lines.append(f"  - `{sample.get('id')}` ({sample.get('chunk_type')}): {sample.get('violations')}")
    lines.append("")

    # review_required / warnings 샘플
    flagged = [c for c in chunk_dicts if (c.get("quality") or {}).get("review_required")]
    lines.append("## Review Required")
    lines.append("")
    lines.append(f"- 검수 필요 chunk: {len(flagged)}개")
    for record in flagged[:sample_size]:
        quality = record.get("quality") or {}
        lines.append(
            f"  - `{record.get('id')}` [{record.get('chunk_type')}] {record.get('sheet')}!{record.get('range')}"
            f" conf={quality.get('confidence')} warnings={quality.get('warnings')}"
        )
    lines.append("")

    # 샘플 chunk
    lines.append(f"## 샘플 Chunk (최대 {sample_size}개)")
    lines.append("")
    for record in chunk_dicts[:sample_size]:
        quality = record.get("quality") or {}
        lines.append(f"### {record.get('chunk_type')} — {record.get('sheet')}!{record.get('range')}")
        lines.append("")
        lines.append(f"- id: `{record.get('id')}`")
        lines.append(f"- path: {' > '.join(record.get('path') or [])}")
        lines.append(f"- confidence: {quality.get('confidence')}")
        lines.append(f"- content_text: {_truncate(record.get('content_text'), 200)}")
        lines.append("")

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
