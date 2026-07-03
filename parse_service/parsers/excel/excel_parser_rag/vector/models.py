"""Data models for the hybrid vector store.

⚠ 복구 재구성 파일 (RECONSTRUCTED 2026-06-18).
   원본이 `rm -rf` 로 삭제되었고 Claude 트랜스크립트/zip 백업/디스크/서버 메모리
   어디에도 내용이 남아있지 않아(원본 바이트 복구 불가, FileVault 로 카빙 불가),
   사용처(`bge_m3.py`, `ingest.py`, `search.py`, `cli.py`)의 API 계약에 맞춰
   동작 동등하게 재작성했다. 필드/메서드 시그니처는 사용처와 일치하지만
   세부 구현은 원본과 다를 수 있다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class EmbeddingVector:
    """Dense + sparse embedding pair produced by an EmbeddingClient.

    사용처: bge_m3.py 가 `EmbeddingVector(dense=[...], sparse={...})` 로 생성하고
    ingest.py 가 `vector.dense`, `vector.sparse` 로 읽는다.
    """

    dense: List[float] = field(default_factory=list)
    sparse: Dict[str, float] = field(default_factory=dict)


@dataclass
class VectorRecord:
    """A single indexed chunk: embeddings + retrieval payload.

    사용처: ingest.py 가 `VectorRecord(id=, text=, dense=, sparse=, payload=)` 로 생성.
    """

    id: str
    text: str
    dense: List[float] = field(default_factory=list)
    sparse: Dict[str, float] = field(default_factory=dict)
    payload: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "dense": list(self.dense),
            "sparse": dict(self.sparse),
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "VectorRecord":
        return cls(
            id=str(data.get("id") or ""),
            text=str(data.get("text") or ""),
            dense=[float(v) for v in (data.get("dense") or [])],
            sparse={str(k): float(v) for k, v in (data.get("sparse") or {}).items()},
            payload=data.get("payload") or {},
        )
