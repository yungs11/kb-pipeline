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
    # ── Phase 2b-1 관측(2026-08-13): **전 페이지** trace. `page_verdicts` 와 **공존**한다. ──
    # 개명하지 않은 이유: `page_verdicts` 는 게이트 대상 **부분집합**이고 기존 단언 13곳이
    # **인덱스 기반**이라(`page_verdicts[0]` = "게이트 대상 중 첫째"), 전수로 바꾸면
    # 인덱스 의미가 조용히 바뀌고 그걸 고치다 단언이 허술해질 수 있다. 소비처가 4곳뿐이고
    # 외부 레포는 0건이라 급하지도 않다 — 제거는 Phase 4.
    #   [{"page_number", "bucket", "lane", "source", "attempts", "chars",
    #     "verdict", "state", "verdict_reason"}]
    page_traces: list | None = None


class ParserError(Exception):
    """파서 실패 — app.py 가 FrontError("parse_failed") 로 매핑."""
