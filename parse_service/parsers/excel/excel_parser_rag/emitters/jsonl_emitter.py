"""JSONL emitter (SoT §5.2, §18) — 한 줄 = 한 chunk."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List


def write_jsonl(chunk_dicts: List[Dict[str, Any]], path: str | Path) -> None:
    """chunk dict 목록을 UTF-8 JSONL 로 기록한다 (ensure_ascii=False)."""
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fp:
        for record in chunk_dicts:
            fp.write(json.dumps(record, ensure_ascii=False, default=str))
            fp.write("\n")
