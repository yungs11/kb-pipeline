"""PDF 문서수준 라우팅 — triage 버킷 집계로 ODL / VL / Paddle gateway 결정.

실측 근거(2026-07-14, 신탁/약관 문서 페이지별 triage 신호 덤프):
- 디지털 페이지(네이티브 텍스트 있음)는 triage 싼 신호(char_count/image_coverage)로
  텍스트/차트를 **구분 가능**(LLM_NEEDED=텍스트+큰이미지, TEXT_ONLY=순수텍스트).
- 스캔 페이지(char=0)는 전부 통짜 래스터라 신호가 동일(image_coverage≈0/1 고정) →
  "텍스트냐 순서도냐"를 **싼 신호로 구분 불가**. 픽셀 안을 봐야(=layout) 알 수 있음.

라우팅(문서수준, 2026-07-15 확정, 2026-08-06 레인/비율 env화):
- 차트/그림 페이지 비율 ≥ KBP_GATE_VL_RATIO(0.5) — **스캔 여부 무관** → KBP_GATE_VL_LANE
    (기본 vl, 페이지별 in-process VL(qwen) — 차트/순서도 중심 문서는 페이지 전체를 VL 이
    읽는 게 최선).
- 스캔 페이지 존재(OCR_NEEDED, 위 비율 미달) → KBP_GATE_OCR_LANE(기본 paddle_gw, PaddleOCR-VL
    게이트웨이, GPU 전체 파이프라인). 실패 시 ODL/in-process VL 폴백.
- 그 외(디지털 텍스트, 차트 소수) → KBP_GATE_DEFAULT_LANE(기본 odl, 기존 빠른 경로; 다이어그램
    페이지는 VL 서술 보충).

레인 env 값은 {"odl","vl","paddle_gw"} 화이트리스트로 검증한다 — 모르는 값이면 경고 로그 +
그 변수 고유 기본값 폴백(잘못된 env 하나가 파싱을 죽이면 안 됨). 이 보장 덕분에 소비자
(`parse_service/parsers/pdf/__init__.py`)의 `decision.lane == "vl"`/`"paddle_gw"` 리터럴
비교는 수정 없이 그대로 동작한다 — env 로 재배선해도 `decision.lane` 은 항상 그 세 리터럴
중 하나이기 때문이다.

paddle_gw 는 KBP_PADDLE_OCR_GATEWAY_URL, vl 은 MODEL_API_URL(in-process VL) 필요.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from parse_service.parsers.pdf.triage import triage_document, Bucket, PageSignals

log = logging.getLogger("kb_pipeline.parse_service.parsers.pdf.gate")

_VALID_LANES = frozenset({"odl", "vl", "paddle_gw"})


def _resolve_lane(env_name: str, default: str) -> str:
    """레인 env 문자열 검증 — 화이트리스트 밖이면 경고 + 그 변수 고유 기본값 폴백."""
    val = os.environ.get(env_name)
    if not val:
        return default
    if val not in _VALID_LANES:
        log.warning("%s=%r 은 유효한 레인이 아님(odl/vl/paddle_gw) — 기본값 %r 사용",
                    env_name, val, default)
        return default
    return val


def _resolve_vl_ratio() -> float:
    """KBP_GATE_VL_RATIO — float 파싱 실패도 레인 문자열과 동일하게 경고+기본값 폴백한다.

    triage_document() 예외 처리(아래 except)와 **별개의 try/except**다 — 같은 catch 에
    두면 이 파싱 실패가 "triage 실패"로 오분류돼 page_signals 까지 통째로 비게 된다.
    """
    raw = os.environ.get("KBP_GATE_VL_RATIO")
    if not raw:
        return 0.5
    try:
        return float(raw)
    except (ValueError, TypeError):
        log.warning("KBP_GATE_VL_RATIO=%r 파싱 실패 — 기본값 0.5 사용", raw)
        return 0.5


@dataclass(frozen=True)
class RouteDecision:
    lane: str                 # "odl" | "vl" | "paddle_gw"
    # 다이어그램(순서도/차트) 페이지 번호(1-based) — ODL 레인이 페이지 단위 VL 서술 보충에 사용.
    diagram_pages: tuple = ()
    # 페이지별 triage 신호(읽기전용, 순수 추가) — 2026-08-06 페이지별 판정 로그(이미지 파서
    # 고도화 준비)가 소비. triage_document() 가 예외로 실패한 경로만 비워둔다(sigs 를 못 얻음).
    page_signals: tuple[PageSignals, ...] = ()


_ODL = RouteDecision(lane="odl")


def decide_route(pdf_bytes: bytes) -> RouteDecision:
    try:
        sigs = triage_document(pdf_bytes)
    except Exception:  # noqa: BLE001 — triage 페이지반복 예외(암호화/손상)는 삼켜 ODL 로(가용성)
        return _ODL
    buckets = [s.bucket for s in sigs if s.bucket != Bucket.SKIP]
    total = len(buckets)
    page_signals = tuple(sigs)
    if total == 0:
        # 전부 빈 페이지/열기 실패 — sigs 는 이미 확보했으므로 page_signals 는 채운다.
        # lane 은 리터럴 "odl" 고정(KBP_GATE_DEFAULT_LANE 적용 안 함) — 분석할 신호가
        # 없는 축퇴 케이스를 vl/paddle_gw 로 보내는 건 의미가 없고 위험하다(의도적 비대칭,
        # "임계치 도달 시 어느 레인" 요구사항의 대상이 아님). 구현 시 대칭을 맞추려 하지 말 것.
        return RouteDecision(lane="odl", page_signals=page_signals)
    n_ocr = buckets.count(Bucket.OCR_NEEDED)
    n_llm = buckets.count(Bucket.LLM_NEEDED)
    # 다이어그램(순서도/차트) 페이지 — ODL 라우팅 시 페이지 단위 VL 서술 보충 대상.
    diagram_pages = tuple(s.page_number for s in sigs if getattr(s, "is_diagram", False))

    vl_ratio = _resolve_vl_ratio()
    # ① 차트/그림 페이지 비율이 높으면 — 스캔 여부 무관 — 문서 전체 VL 레인(페이지별 in-process VL).
    #    차트/순서도 중심 문서는 페이지 전체를 VL 이 읽는 게 최선(2026-07-15 결정, hybrid 대체).
    if n_llm > 0 and (n_llm / total) >= vl_ratio:
        return RouteDecision(lane=_resolve_lane("KBP_GATE_VL_LANE", "vl"),
                             page_signals=page_signals)

    # ② 스캔 페이지(네이티브 텍스트 없음)가 하나라도 있으면 → PaddleOCR-VL 게이트웨이(GPU).
    #    layout+VL+표 조립 전부 게이트웨이 서버 — parse-svc 로컬 의존 0. 실패 시 ODL/VL 폴백.
    #    diagram_pages 전달 — 게이트웨이는 순서도를 이미지 참조로만 내므로(서술 없음)
    #    paddle_gw 레인도 해당 페이지에 VL 서술을 보충한다(2026-07-15, 소유권 p4 실측).
    if n_ocr > 0:
        return RouteDecision(lane=_resolve_lane("KBP_GATE_OCR_LANE", "paddle_gw"),
                             diagram_pages=diagram_pages, page_signals=page_signals)

    # ③ 디지털 텍스트(+차트/다이어그램 소수) → ODL. 다이어그램 페이지는 ODL 레인이 VL 서술 보충.
    return RouteDecision(lane=_resolve_lane("KBP_GATE_DEFAULT_LANE", "odl"),
                         diagram_pages=diagram_pages, page_signals=page_signals)
