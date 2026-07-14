"""PDF 문서수준 라우팅 — triage 버킷 집계로 ODL / MinerU(pipeline·hybrid) 결정.

설계: docs/superpowers/specs/2026-07-13-mineru-pdf-integration-design.md §3.1·§4.3

실측 근거(2026-07-14, 신탁/약관 문서 페이지별 triage 신호 덤프):
- 디지털 페이지(네이티브 텍스트 있음)는 triage 싼 신호(char_count/image_coverage)로
  텍스트/차트를 **구분 가능**(LLM_NEEDED=텍스트+큰이미지, TEXT_ONLY=순수텍스트).
- 스캔 페이지(char=0)는 전부 통짜 래스터라 신호가 동일(image_coverage≈0/1 고정) →
  "텍스트냐 순서도냐"를 **싼 신호로 구분 불가**. 픽셀 안을 봐야(=layout) 알 수 있음.

라우팅(문서수준):
- 스캔 페이지 존재(OCR_NEEDED)  → **paddle_gw**(PaddleOCR-VL 게이트웨이, GPU 전체 파이프라인).
    실측(신탁 3p): 48s vs MinerU pipeline(CPU) 181s. 실패 시 parse() 가 ODL/in-process VL 폴백
    (사용자 결정 2026-07-15: MinerU 는 스캔 폴백 체인에서 제외).
- 스캔 없음 + 차트/그림 페이지 비율 높음(LLM_NEEDED) → MinerU **hybrid**(원격 VL 품질).
- 그 외(순수 디지털 텍스트, 차트 소수) → ODL(기존 빠른 경로; 다이어그램 페이지는 VL 서술 보충).

paddle_gw 는 KBP_PADDLE_OCR_GATEWAY_URL, hybrid 는 MINERU_VLM_SERVER_URL 필요.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from parse_service.parsers.pdf.triage import triage_document, Bucket

# 스캔 없는 디지털 문서에서 차트/그림 페이지(LLM_NEEDED) 비율이 이 이상이면 hybrid(VL 품질),
# 미만이면 ODL(텍스트 위주 → 빠른 ODL + 그림은 modal-enrich VL). 큰 디지털 문서가 그림 몇 장
# 때문에 통째로 hybrid(느림)로 가는 회귀를 막는 가드. 실측: 292p 약관은 LLM 22/285=0.08.
_HYBRID_RATIO = float(os.environ.get("KBP_GATE_HYBRID_RATIO", "0.5"))

_HYBRID_BACKEND = "hybrid-http-client"


@dataclass(frozen=True)
class RouteDecision:
    lane: str                 # "odl" | "mineru"
    backend: str | None       # None(odl) | "pipeline" | "hybrid-http-client"
    parse_method: str | None  # None(odl) | "ocr" | "auto"
    # 다이어그램(순서도/차트) 페이지 번호(1-based) — ODL 레인이 페이지 단위 VL 서술 보충에 사용.
    diagram_pages: tuple = ()


_ODL = RouteDecision(lane="odl", backend=None, parse_method=None)


def decide_route(pdf_bytes: bytes) -> RouteDecision:
    try:
        sigs = triage_document(pdf_bytes)
    except Exception:  # noqa: BLE001 — triage 페이지반복 예외(암호화/손상)는 삼켜 ODL 로(가용성)
        return _ODL
    buckets = [s.bucket for s in sigs if s.bucket != Bucket.SKIP]
    total = len(buckets)
    if total == 0:
        return _ODL  # 전부 빈 페이지/열기 실패
    n_ocr = buckets.count(Bucket.OCR_NEEDED)
    n_llm = buckets.count(Bucket.LLM_NEEDED)
    # 다이어그램(순서도/차트) 페이지 — ODL 라우팅 시 페이지 단위 VL 서술 보충 대상.
    diagram_pages = tuple(s.page_number for s in sigs if getattr(s, "is_diagram", False))

    # 스캔 페이지(네이티브 텍스트 없음)가 하나라도 있으면 → PaddleOCR-VL 게이트웨이(GPU).
    # layout+VL+표 조립 전부 게이트웨이 서버 — parse-svc 로컬 의존 0. 실패 시 ODL/VL 폴백.
    if n_ocr > 0:
        return RouteDecision(lane="paddle_gw", backend=None, parse_method=None)

    # 스캔 없음. 차트/그림 페이지 비율이 높으면 → hybrid(원격 VL 품질). 아니면 → ODL.
    if n_llm > 0 and (n_llm / total) >= _HYBRID_RATIO:
        return RouteDecision(lane="mineru", backend=_HYBRID_BACKEND, parse_method="auto")

    # 순수 디지털 텍스트(+차트/다이어그램 소수) → ODL. 다이어그램 페이지는 ODL 레인이 VL 서술 보충.
    return RouteDecision(lane="odl", backend=None, parse_method=None,
                         diagram_pages=diagram_pages)
