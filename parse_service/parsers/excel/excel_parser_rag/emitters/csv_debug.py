"""디버그용 CSV emitter (SoT §5.2 normalized csv optional)."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Dict, List

_COLUMNS = [
    "id",
    "chunk_type",
    "region_type",
    "sheet",
    "range",
    "title",
    "path",
    "content_text",
    "fields",
    "confidence",
    "review_required",
    "warnings",
    "keywords",
]


def write_debug_csv(chunk_dicts: List[Dict[str, Any]], path: str | Path) -> None:
    """chunk 핵심 필드를 평탄화한 디버그 CSV 를 기록한다 (UTF-8 BOM — 엑셀 호환)."""
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(_COLUMNS)
        for record in chunk_dicts:
            quality = record.get("quality") or {}
            writer.writerow(
                [
                    record.get("id", ""),
                    record.get("chunk_type", ""),
                    record.get("region_type", ""),
                    record.get("sheet", ""),
                    record.get("range", ""),
                    record.get("title") or "",
                    " > ".join(str(p) for p in (record.get("path") or [])),
                    record.get("content_text", ""),
                    json.dumps(record.get("fields") or {}, ensure_ascii=False, default=str),
                    quality.get("confidence", ""),
                    quality.get("review_required", ""),
                    "; ".join(str(w) for w in (quality.get("warnings") or [])),
                    ", ".join(str(k) for k in (record.get("keywords") or [])),
                ]
            )
