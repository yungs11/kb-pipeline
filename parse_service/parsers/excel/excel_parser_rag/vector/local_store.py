"""Local JSON-backed hybrid (dense + sparse) vector store.

⚠ 복구 재구성 파일 (RECONSTRUCTED 2026-06-18).
   원본 삭제분을 `ingest.py`(LocalHybridStore(path).upsert(records)->int, .save())와
   `search.py` 의 사용처에 맞춰 재작성했다. 인덱스는 단일 JSON 파일로 영속화한다.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Union

from .models import VectorRecord


class LocalHybridStore:
    """Persists VectorRecord rows to one JSON file and serves them for search.

    인덱스 JSON 스키마::

        {"dimensions": <int|null>, "count": <int>, "records": [<VectorRecord.to_dict()>, ...]}
    """

    def __init__(self, index_path: Union[str, Path]) -> None:
        self.index_path = Path(index_path)
        self.records: Dict[str, VectorRecord] = {}
        self.dimensions: Optional[int] = None
        self.load()

    def load(self) -> None:
        if not self.index_path.exists():
            return
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        if not isinstance(data, dict):
            return
        self.dimensions = data.get("dimensions")
        for row in data.get("records", []):
            if not isinstance(row, dict):
                continue
            rec = VectorRecord.from_dict(row)
            if rec.id:
                self.records[rec.id] = rec

    def upsert(self, records: Iterable[VectorRecord]) -> int:
        count = 0
        for rec in records:
            if not getattr(rec, "id", None):
                continue
            self.records[rec.id] = rec
            if rec.dense and self.dimensions is None:
                self.dimensions = len(rec.dense)
            count += 1
        return count

    def all_records(self) -> List[VectorRecord]:
        return list(self.records.values())

    def __len__(self) -> int:
        return len(self.records)

    def save(self) -> None:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "dimensions": self.dimensions,
            "count": len(self.records),
            "records": [rec.to_dict() for rec in self.records.values()],
        }
        self.index_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
