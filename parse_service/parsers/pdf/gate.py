"""PDF 문서수준 라우팅 — triage 버킷 집계로 ODL vs MinerU 레인 + parse_method 결정.

설계: docs/superpowers/specs/2026-07-13-mineru-pdf-integration-design.md §3.1·§4.3

보증 범위: "네이티브 텍스트 페이지는 텍스트를 잃지 않는다". 스캔 페이지(OCR_NEEDED)가
하나라도 있으면 parse_method='ocr' 강제 — 'auto' 로 두면 MinerU 문서수준 classify 가
'txt' 판정 시 그 스캔 페이지 텍스트가 유실(2026-07-07 버그 재발).
"""
from __future__ import annotations

from dataclasses import dataclass

from parse_service.parsers.pdf.triage import triage_document, Bucket


@dataclass(frozen=True)
class RouteDecision:
    lane: str                 # "odl" | "mineru"
    parse_method: str | None  # "ocr" | "auto" | None(=odl)


_ODL = RouteDecision(lane="odl", parse_method=None)


def decide_route(pdf_bytes: bytes) -> RouteDecision:
    try:
        sigs = triage_document(pdf_bytes)
    except Exception:  # noqa: BLE001 — triage 페이지반복 예외(암호화/손상)는 삼켜 ODL 로(가용성)
        return _ODL
    buckets = {s.bucket for s in sigs if s.bucket != Bucket.SKIP}
    # 비어있지 않은 페이지가 전부 순수 텍스트(또는 전부 빈/열기실패) → ODL 레인
    if not buckets or buckets == {Bucket.TEXT_ONLY}:
        return _ODL
    # 스캔 페이지(네이티브 텍스트 없음)가 하나라도 있으면 'ocr' 강제 —
    # 'auto'로 두면 MinerU 문서수준 classify='txt' 판정 시 그 스캔 텍스트 유실.
    if Bucket.OCR_NEEDED in buckets:
        return RouteDecision(lane="mineru", parse_method="ocr")
    # OCR_NEEDED 없이 LLM_NEEDED 만(스캔 없는 텍스트+이미지) → 'auto' 안전(모든 페이지 네이티브 텍스트 보유)
    return RouteDecision(lane="mineru", parse_method="auto")
