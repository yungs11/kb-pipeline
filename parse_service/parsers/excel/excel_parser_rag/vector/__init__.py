"""Hybrid vector store subpackage: embeddings, local index, ingest, search.

⚠ 복구 재구성 파일 (RECONSTRUCTED 2026-06-18) — 공개 심볼 재노출용.
   같은 패키지의 `bge_m3.py`, `ingest.py` 는 트랜스크립트에서 복구된 원본이고,
   `models.py`, `local_store.py`, `search.py`, 이 `__init__.py` 는 사용처 API 기반 재작성본이다.
"""

from __future__ import annotations

from .bge_m3 import BgeM3HttpClient, EmbeddingClient, HashingEmbeddingClient
from .ingest import ingest_jsonl
from .local_store import LocalHybridStore
from .models import EmbeddingVector, VectorRecord
from .search import search_index

__all__ = [
    "EmbeddingVector",
    "VectorRecord",
    "EmbeddingClient",
    "HashingEmbeddingClient",
    "BgeM3HttpClient",
    "LocalHybridStore",
    "ingest_jsonl",
    "search_index",
]
