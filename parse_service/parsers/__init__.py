"""도메인 파서 공용 계약 — 각 parsers/<도메인>.parse() 는 RouteResult 를 반환한다."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RouteResult:
    kind: str                    # "pages" | "chunks"
    chunk_needed: bool
    pages: list | None = None    # PageDoc[] = [{"page_number", "blocks"}]
    chunks: list | None = None   # facade 청크 스키마 [{"chunk_index","text","titles_context","pages"}]
    gate_summary: dict | None = None  # excel-gate 요약 (엑셀 경로만 채움; 그 외 None)
    page_verdicts: list | None = None  # paddle_gw 페이지 판정 (그 레인만 채움; 그 외 None)


class ParserError(Exception):
    """파서 실패 — app.py 가 FrontError("parse_failed") 로 매핑."""
