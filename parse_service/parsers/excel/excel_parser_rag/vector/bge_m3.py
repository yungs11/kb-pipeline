"""Embedding clients for hybrid retrieval.

`HashingEmbeddingClient` is dependency-free and deterministic for tests/local
experiments. `BgeM3HttpClient` is a thin adapter for a BGE-M3 serving endpoint
that returns dense and sparse vectors.
"""

from __future__ import annotations

import hashlib
import json
import math
import urllib.request
from typing import Any, Dict, Iterable, List, Protocol

from ..textutil import split_keywords
from .models import EmbeddingVector


class EmbeddingClient(Protocol):
    def embed_texts(self, texts: List[str]) -> List[EmbeddingVector]:
        ...

    def embed_query(self, query: str) -> EmbeddingVector:
        ...


def _md5_int(text: str) -> int:
    return int(hashlib.md5(text.encode("utf-8")).hexdigest(), 16)


def _normalize(values: List[float]) -> List[float]:
    norm = math.sqrt(sum(v * v for v in values))
    if norm <= 0:
        return values
    return [v / norm for v in values]


class HashingEmbeddingClient:
    """Small deterministic dense+sparse encoder.

    This is not a replacement for BGE-M3 quality; it provides the same shape so
    ingest/search can be tested without network/model dependencies.
    """

    def __init__(self, *, dimensions: int = 64) -> None:
        self.dimensions = max(8, int(dimensions))

    def _embed_one(self, text: str) -> EmbeddingVector:
        dense = [0.0] * self.dimensions
        sparse: Dict[str, float] = {}
        tokens = split_keywords(text, limit=256)
        if not tokens:
            return EmbeddingVector(dense=dense, sparse={})

        for token in tokens:
            h = _md5_int(token)
            idx = h % self.dimensions
            sign = 1.0 if (h >> 8) & 1 else -1.0
            dense[idx] += sign
            sparse[token] = sparse.get(token, 0.0) + 1.0

        dense = _normalize(dense)
        scale = math.sqrt(sum(v * v for v in sparse.values())) or 1.0
        sparse = {k: v / scale for k, v in sparse.items()}
        return EmbeddingVector(dense=dense, sparse=sparse)

    def embed_texts(self, texts: List[str]) -> List[EmbeddingVector]:
        return [self._embed_one(text) for text in texts]

    def embed_query(self, query: str) -> EmbeddingVector:
        return self._embed_one(query)


class BgeM3HttpClient:
    """HTTP adapter for a BGE-M3 embedding service.

    Expected request:
      POST endpoint {"texts": ["..."], "return_dense": true, "return_sparse": true}

    Accepted response shapes:
      {"data": [{"dense": [...], "sparse": {"token": weight}}]}
      {"embeddings": [{"dense_vecs": [...], "lexical_weights": {...}}]}
    """

    def __init__(self, endpoint: str, *, timeout_s: float = 60.0, batch_size: int = 16) -> None:
        self.endpoint = endpoint
        self.timeout_s = timeout_s
        self.batch_size = max(1, int(batch_size))

    def _post(self, texts: List[str]) -> Any:
        body = json.dumps(
            {"texts": texts, "return_dense": True, "return_sparse": True},
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_s) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _extract_items(response: Any) -> Iterable[Dict[str, Any]]:
        if isinstance(response, dict):
            for key in ("data", "embeddings", "results"):
                value = response.get(key)
                if isinstance(value, list):
                    return value
        if isinstance(response, list):
            return response
        raise ValueError("BGE-M3 response must contain data/embeddings/results list")

    @staticmethod
    def _parse_item(item: Dict[str, Any]) -> EmbeddingVector:
        dense = (
            item.get("dense")
            or item.get("dense_vecs")
            or item.get("dense_vector")
            or item.get("embedding")
            or []
        )
        sparse = (
            item.get("sparse")
            or item.get("lexical_weights")
            or item.get("sparse_vector")
            or {}
        )
        if isinstance(sparse, dict) and "indices" in sparse and "values" in sparse:
            sparse = {str(i): float(v) for i, v in zip(sparse["indices"], sparse["values"])}
        return EmbeddingVector(
            dense=[float(v) for v in dense],
            sparse={str(k): float(v) for k, v in dict(sparse).items()},
        )

    def embed_texts(self, texts: List[str]) -> List[EmbeddingVector]:
        vectors: List[EmbeddingVector] = []
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start : start + self.batch_size]
            if not batch:
                continue
            response = self._post(batch)
            vectors.extend(self._parse_item(item) for item in self._extract_items(response))
        return vectors

    def embed_query(self, query: str) -> EmbeddingVector:
        vectors = self.embed_texts([query])
        if not vectors:
            raise ValueError("BGE-M3 endpoint returned no embedding")
        return vectors[0]

