"""Hybrid (dense cosine + sparse dot) search over a LocalHybridStore index.

⚠ 복구 재구성 파일 (RECONSTRUCTED 2026-06-18).
   원본 삭제분을 `cli._run_search` 의 호출 계약에 맞춰 재작성했다::

       search_index(index_path, query, *, embedder, top_k=10,
                    dense_weight=0.35, sparse_weight=0.65,
                    filter_sheet=None, filter_chunk_type=None) -> list[dict]

   dense 점수는 코사인 유사도, sparse 점수는 공통 토큰 가중 내적으로 계산하고
   `dense_weight`/`sparse_weight` 로 가중합한다.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from .local_store import LocalHybridStore
from .models import EmbeddingVector


def _cosine(a: List[float], b: List[float]) -> float:
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    dot = sum(a[i] * b[i] for i in range(n))
    na = math.sqrt(sum(v * v for v in a))
    nb = math.sqrt(sum(v * v for v in b))
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (na * nb)


def _sparse_dot(a: Dict[str, float], b: Dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    return sum(weight * b.get(token, 0.0) for token, weight in a.items())


def search_index(
    index_path: Union[str, Path],
    query: str,
    *,
    embedder: Any,
    top_k: int = 10,
    dense_weight: float = 0.35,
    sparse_weight: float = 0.65,
    filter_sheet: Optional[str] = None,
    filter_chunk_type: Optional[str] = None,
) -> List[Dict[str, Any]]:
    store = LocalHybridStore(index_path)
    query_vec: EmbeddingVector = embedder.embed_query(query)

    scored: List[Dict[str, Any]] = []
    for rec in store.all_records():
        payload = rec.payload or {}
        if filter_sheet is not None and payload.get("sheet") != filter_sheet:
            continue
        if filter_chunk_type is not None and payload.get("chunk_type") != filter_chunk_type:
            continue
        dense_score = _cosine(query_vec.dense, rec.dense)
        sparse_score = _sparse_dot(query_vec.sparse, rec.sparse)
        score = dense_weight * dense_score + sparse_weight * sparse_score
        scored.append(
            {
                "id": rec.id,
                "score": score,
                "dense_score": dense_score,
                "sparse_score": sparse_score,
                "text": rec.text,
                "payload": payload,
            }
        )

    scored.sort(key=lambda item: item["score"], reverse=True)
    if top_k and top_k > 0:
        return scored[:top_k]
    return scored
