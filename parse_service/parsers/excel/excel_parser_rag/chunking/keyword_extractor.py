"""keywords 생성기 (SoT §20).

BM25/hybrid search 용 키워드를 path/title/sheet/fields/facts/파일명에서 추출한다.
약어는 ctx.code_map 으로 확장명을 병기한다.
"""

from __future__ import annotations

import re
from typing import Any, List, TYPE_CHECKING

from ..textutil import one_line, split_keywords

if TYPE_CHECKING:
    from ..chunking.chunk_schema import RagChunk
    from ..parsers.base import ParseContext

MAX_KEYWORDS = 60


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return " ".join(one_line(v) for v in value)
    if isinstance(value, dict):
        return " ".join(f"{one_line(k)} {one_line(v)}" for k, v in value.items())
    return one_line(value)


def _file_stem_words(source_file: str) -> str:
    stem = re.sub(r"\.[A-Za-z0-9]+$", "", one_line(source_file))
    return re.sub(r"[._-]+", " ", stem)


def extract_keywords(chunk: "RagChunk", ctx: "ParseContext") -> List[str]:
    """chunk 의 검색 키워드 목록 (순서 보존, 중복 제거, 최대 MAX_KEYWORDS)."""
    texts: List[str] = []

    # 1) path 구성 요소 (가장 구체적인 검색 신호)
    texts.extend(one_line(p) for p in (chunk.path or []))

    # 2) fields key/value
    for key, value in (chunk.fields or {}).items():
        texts.append(one_line(key))
        texts.append(_stringify(value))

    # 3) facts value (+ 이미 확장된 약어)
    for fact in chunk.facts or []:
        if not isinstance(fact, dict):
            continue
        for fkey in ("value", "object", "subject", "predicate"):
            if fact.get(fkey):
                texts.append(one_line(fact[fkey]))
        for exp in fact.get("expanded") or []:
            if isinstance(exp, dict):
                texts.append(one_line(exp.get("raw")))
                texts.append(one_line(exp.get("expanded")))

    # 4) 표 제목 / 시트명 / 파일명 주요 단어
    texts.append(one_line(chunk.title))
    texts.append(one_line(chunk.sheet))
    texts.append(_file_stem_words(chunk.source_file or ctx.source_file))

    out: List[str] = []
    seen = set()

    def _add(token: str) -> None:
        if token and token not in seen and len(out) < MAX_KEYWORDS:
            seen.add(token)
            out.append(token)

    for text in texts:
        if not text:
            continue
        for token in split_keywords(text):
            _add(token)
        if len(out) >= MAX_KEYWORDS:
            break

    # 5) 약어 확장 병기 (SoT §20.2 — 약어 원문과 확장명)
    code_map = ctx.code_map or {}
    if code_map and len(out) < MAX_KEYWORDS:
        for token in list(out):
            expanded = code_map.get(token)
            if expanded:
                for exp_token in split_keywords(expanded):
                    _add(exp_token)
            if len(out) >= MAX_KEYWORDS:
                break

    return out
