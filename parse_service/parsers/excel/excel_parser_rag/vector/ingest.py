"""JSONL chunk ingest for hybrid vector stores."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List

from .bge_m3 import EmbeddingClient, HashingEmbeddingClient
from .local_store import LocalHybridStore
from .models import VectorRecord


def _read_jsonl(path: str | Path) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as fp:
        for line_no, line in enumerate(fp, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                item = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at line {line_no}: {exc}") from exc
            if isinstance(item, dict):
                records.append(item)
    return records


def chunk_embedding_text(chunk: Dict[str, Any]) -> str:
    metadata = chunk.get("metadata") or {}
    return (
        str(metadata.get("embedding_text") or "").strip()
        or str(metadata.get("core_text") or "").strip()
        or str(chunk.get("content_text") or "").strip()
    )


def chunk_payload(chunk: Dict[str, Any]) -> Dict[str, Any]:
    source = chunk.get("source") or {}
    quality = chunk.get("quality") or {}
    metadata = chunk.get("metadata") or {}
    return {
        "id": chunk.get("id"),
        "source_file": chunk.get("source_file"),
        "sheet": chunk.get("sheet") or source.get("sheet"),
        "range": chunk.get("range") or source.get("range"),
        "chunk_type": chunk.get("chunk_type"),
        "region_type": chunk.get("region_type"),
        "title": chunk.get("title"),
        "path": chunk.get("path") or [],
        "fields": chunk.get("fields") or {},
        "keywords": chunk.get("keywords") or [],
        "quality": {
            "confidence": quality.get("confidence"),
            "review_required": quality.get("review_required"),
        },
        "metadata": {
            "workbook_title": metadata.get("workbook_title"),
            "core_text": metadata.get("core_text"),
        },
    }


def build_vector_records(
    chunks: Iterable[Dict[str, Any]],
    embedder: EmbeddingClient,
) -> List[VectorRecord]:
    chunk_list = [chunk for chunk in chunks if chunk_embedding_text(chunk)]
    texts = [chunk_embedding_text(chunk) for chunk in chunk_list]
    vectors = embedder.embed_texts(texts)
    if len(vectors) != len(chunk_list):
        raise ValueError(f"embedding count mismatch: chunks={len(chunk_list)} vectors={len(vectors)}")

    records: List[VectorRecord] = []
    for chunk, text, vector in zip(chunk_list, texts, vectors):
        records.append(
            VectorRecord(
                id=str(chunk.get("id") or ""),
                text=text,
                dense=vector.dense,
                sparse=vector.sparse,
                payload=chunk_payload(chunk),
            )
        )
    return [record for record in records if record.id]


def ingest_jsonl(
    jsonl_path: str | Path,
    index_path: str | Path,
    *,
    embedder: EmbeddingClient | None = None,
) -> Dict[str, Any]:
    embedder = embedder or HashingEmbeddingClient()
    chunks = _read_jsonl(jsonl_path)
    records = build_vector_records(chunks, embedder)
    store = LocalHybridStore(index_path)
    upserted = store.upsert(records)
    store.save()
    return {
        "jsonl_path": str(jsonl_path),
        "index_path": str(index_path),
        "input_chunks": len(chunks),
        "indexed_records": len(records),
        "upserted": upserted,
    }

