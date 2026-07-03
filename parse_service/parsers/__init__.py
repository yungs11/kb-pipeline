"""도메인 파서 공용 계약 — 각 parsers/<도메인>.parse() 는 RouteResult 를 반환한다."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RouteResult:
    kind: str                    # "pages" | "chunks"
    chunk_needed: bool
    pages: list | None = None    # PageDoc[] = [{"page_number", "blocks"}]
    chunks: list | None = None   # facade 청크 스키마 [{"chunk_index","text","titles_context","pages"}]


class ParserError(Exception):
    """파서 실패 — app.py 가 FrontError("parse_failed") 로 매핑."""
