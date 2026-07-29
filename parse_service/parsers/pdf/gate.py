"""PDF 문서수준 라우팅 — triage 버킷 집계로 ODL / VL / Paddle gateway 결정.

실측 근거(2026-07-14, 신탁/약관 문서 페이지별 triage 신호 덤프):
- 디지털 페이지(네이티브 텍스트 있음)는 triage 싼 신호(char_count/image_coverage)로
  텍스트/차트를 **구분 가능**(LLM_NEEDED=텍스트+큰이미지, TEXT_ONLY=순수텍스트).
- 스캔 페이지(char=0)는 전부 통짜 래스터라 신호가 동일(image_coverage≈0/1 고정) →
  "텍스트냐 순서도냐"를 **싼 신호로 구분 불가**. 픽셀 안을 봐야(=layout) 알 수 있음.

라우팅(문서수준, 2026-07-15 확정):
- 차트/그림 페이지 비율 ≥ KBP_GATE_VL_RATIO(0.5) — **스캔 여부 무관** → **vl** 레인
    (페이지별 in-process VL(qwen) — 차트/순서도 중심 문서는 페이지 전체를 VL 이 읽는 게 최선).
- 스캔 페이지 존재(OCR_NEEDED, 위 비율 미달) → **paddle_gw**(PaddleOCR-VL 게이트웨이, GPU 전체
    파이프라인). 실패 시 ODL/in-process VL 폴백.
- 그 외(디지털 텍스트, 차트 소수) → ODL(기존 빠른 경로; 다이어그램 페이지는 VL 서술 보충).

paddle_gw 는 KBP_PADDLE_OCR_GATEWAY_URL, vl 은 MODEL_API_URL(in-process VL) 필요.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from parse_service.parsers.pdf.triage import triage_document, Bucket

# 차트/그림 페이지(LLM_NEEDED) 비율이 이 이상이면 문서 전체 VL 레인(스캔 여부 무관),
# 미만이면 스캔유무로 paddle_gw/ODL. 큰 텍스트 문서가 그림 몇 장 때문에 통째로 VL(느림)로
# 가는 회귀를 막는 가드. 실측: 292p 약관은 LLM 22/285=0.08 → ODL 유지.
_VL_RATIO = float(os.environ.get("KBP_GATE_VL_RATIO", "0.5"))


@dataclass(frozen=True)
class RouteDecision:
    lane: str                 # "odl" | "vl" | "paddle_gw"
    # 다이어그램(순서도/차트) 페이지 번호(1-based) — ODL 레인이 페이지 단위 VL 서술 보충에 사용.
    diagram_pages: tuple = ()


_ODL = RouteDecision(lane="odl")


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

    # ① 차트/그림 페이지 비율이 높으면 — 스캔 여부 무관 — 문서 전체 VL 레인(페이지별 in-process VL).
    #    차트/순서도 중심 문서는 페이지 전체를 VL 이 읽는 게 최선(2026-07-15 결정, hybrid 대체).
    if n_llm > 0 and (n_llm / total) >= _VL_RATIO:
        return RouteDecision(lane="vl")

    # ② 스캔 페이지(네이티브 텍스트 없음)가 하나라도 있으면 → PaddleOCR-VL 게이트웨이(GPU).
    #    layout+VL+표 조립 전부 게이트웨이 서버 — parse-svc 로컬 의존 0. 실패 시 ODL/VL 폴백.
    #    diagram_pages 전달 — 게이트웨이는 순서도를 이미지 참조로만 내므로(서술 없음)
    #    paddle_gw 레인도 해당 페이지에 VL 서술을 보충한다(2026-07-15, 소유권 p4 실측).
    if n_ocr > 0:
        return RouteDecision(lane="paddle_gw", diagram_pages=diagram_pages)

    # ③ 디지털 텍스트(+차트/다이어그램 소수) → ODL. 다이어그램 페이지는 ODL 레인이 VL 서술 보충.
    return RouteDecision(lane="odl", diagram_pages=diagram_pages)
