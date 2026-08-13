"""PDF **페이지수준** 라우팅 — triage 버킷을 페이지마다 레인으로 매핑한다(Plan B, 2026-08-04).

실측 근거(2026-07-14, 신탁/약관 문서 페이지별 triage 신호 덤프):
- 디지털 페이지(네이티브 텍스트 있음)는 triage 싼 신호(char_count/image_coverage)로
  텍스트/차트를 **구분 가능**(LLM_NEEDED=텍스트+큰이미지, TEXT_ONLY=순수텍스트).
- 스캔 페이지(char=0)는 전부 통짜 래스터라 신호가 동일(image_coverage≈0/1 고정) →
  "텍스트냐 순서도냐"를 **싼 신호로 구분 불가**. 픽셀 안을 봐야(=layout) 알 수 있음.

페이지별 레인 (2026-08-12 Phase 2a — `LLM_NEEDED → vl` 확정):
- SKIP        → skip        (ODL md 있으면 블록화, 없으면 빈 blocks. VL 미호출)
- OCR_NEEDED  → `KBP_GATE_OCR_LANE`(기본 paddle_gw)  게이트웨이 + layout 기반 hybrid
- LLM_NEEDED  → vl          가로형·다이어그램·세로형 혼합콘텐츠 **전부**. 페이지 전체를 VL 이 전사
- TEXT_ONLY   → odl         ODL 이 표·텍스트 보존. 빈약하면 그 페이지만 VL 전사

`is_landscape` 는 **여기서 보지 않는다** — `triage.py` 가 이미 가로형을 `LLM_NEEDED` 로
마킹하므로(`KBP_TRIAGE_LANDSCAPE_TO_LLM`, 기본 1) `LLM_NEEDED → vl` 하나면 따라온다.

**문서수준 `vl` 레인은 삭제**(2026-08-04). 그림 비율(`KBP_GATE_VL_RATIO`)만 보고 문서 전체를
VL 로 넘기던 경로인데, KIS(11p)처럼 표가 많은 문서가 표 테두리 벡터선(curve=350)을 순서도로
오탐당해 통째로 VL 재전사되며 표 구조가 깨졌다. 이제 그런 문서는 **페이지마다** 갈린다.

paddle_gw 는 KBP_PADDLE_OCR_GATEWAY_URL, 페이지 VL 전사는 MODEL_API_URL 필요.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from parse_service.parsers.pdf.triage import triage_document, Bucket, PageSignals

log = logging.getLogger("kb_pipeline.parse_service.parsers.pdf.gate")

_VALID_LANES = frozenset({"odl", "vl", "paddle_gw"})


def _resolve_lane(env_name: str, default: str) -> str:
    """레인 env 문자열 검증 — 화이트리스트 밖이면 경고 + 그 변수 고유 기본값 폴백.

    살아 있는 소비자는 `KBP_GATE_OCR_LANE` 하나다(§6B). 이것은 **게이트웨이 없는 폐쇄망에서
    스캔 레인을 끄는 탈출구**다. `=vl` 로 두면 스캔 페이지가 GW 대신 VL 전사로 간다.
    이 스위치가 살아 있어야:
      · `scripts/airgap/verify-bundle.sh:106/115` 의 두 가드가 발화한다(레인 켰는데 URL 공란 = 오설정)
      · `docs/airgap-onsite-checklist.md` 와 `scripts/run-parse-svc.sh` 의 안내가 유효하다
    지우려면 대체 신호와 현장 `.env` 마이그레이션을 같은 작업에서 해야 한다 → Phase 4.
    """
    val = os.environ.get(env_name)
    if not val:
        return default
    if val not in _VALID_LANES:
        log.warning("%s=%r 은 유효한 레인이 아님(odl/vl/paddle_gw) — 기본값 %r 사용",
                    env_name, val, default)
        return default
    return val


@dataclass(frozen=True)
class RouteDecision:
    lane: str                 # "odl" | "paddle_gw" — 하위호환 요약값(B-5 는 page_lanes 를 본다)
    # 다이어그램(순서도/차트) 페이지 번호(1-based) — ODL 레인이 페이지 단위 VL 서술 보충에 사용.
    diagram_pages: tuple = ()
    # 스캔 페이지 번호(1-based, bucket == OCR_NEEDED). paddle_gw 레인의 layout 기반 hybrid 처리
    # 대상을 **실제 스캔 페이지로 한정**하는 데 쓴다(Plan A §A0, 2026-08-02).
    ocr_pages: tuple = ()
    # 페이지별 triage 신호(읽기전용, 순수 추가) — 2026-08-06 페이지별 판정 로그가 소비.
    # triage_document() 가 예외로 실패한 경로만 비워둔다(sigs 를 못 얻음).
    page_signals: tuple[PageSignals, ...] = ()
    # ── Plan B-1: 페이지수준 라우팅 필드. `_parse_routed` 가 페이지별 병합에 쓴다. ──
    #   page_lanes    : ((1-based pno, "skip"|"odl"|"vl"|"paddle_gw"), …)
    #   narrate_pages : is_diagram 이면서 **vl 레인이 아닌** 페이지. 서술 보충 대상.
    #                   vl 레인 페이지는 PAGE_HYBRID 가 순서도 흐름서술을 이미 포함하므로
    #                   빼지 않으면 VL 2회 호출 + 중복 블록이 된다.
    #   total_pages   : SKIP 포함 전체 페이지 수(= len(sigs)). page_lanes 의 상한.
    page_lanes: tuple = ()
    narrate_pages: tuple = ()
    total_pages: int = 0


def _page_lane(sig, *, ocr_lane: str) -> str:
    """페이지 bucket → 레인.

    `ocr_lane` 은 호출부에서 **한 번** 읽어 넘긴다 — 페이지마다 env 를 읽으면 문서 중간에
    값이 바뀌는 비결정 경로가 생긴다.
    """
    if sig.bucket == Bucket.SKIP:
        return "skip"
    if sig.bucket == Bucket.OCR_NEEDED:
        return ocr_lane            # 탈출구: `=vl` 이면 스캔 페이지도 VL 전사로(§6B)
    if sig.bucket == Bucket.LLM_NEEDED:
        return "vl"                # 가로형 · 다이어그램 · 세로형 혼합콘텐츠 전부
    return "odl"                   # TEXT_ONLY


def decide_route(pdf_bytes: bytes) -> RouteDecision:
    # 레인 env 는 **루프 밖에서 1회**만 읽는다(위 `_page_lane` docstring 참조).
    ocr_lane = _resolve_lane("KBP_GATE_OCR_LANE", "paddle_gw")
    try:
        sigs = triage_document(pdf_bytes)
    except Exception:  # noqa: BLE001 — triage 페이지반복 예외(암호화/손상)는 삼켜 ODL 로(가용성)
        # ⚠️ 싱글턴 `_ODL` 재사용 금지 — 조기 return 도 페이지수준 필드를 채워야 한다.
        #    여기서는 sigs 자체를 못 얻었으므로 전부 빈 값이 맞다.
        return RouteDecision(lane="odl", page_signals=(), page_lanes=(), total_pages=0)

    page_signals = tuple(sigs)
    total_pages = len(sigs)
    page_lanes = tuple((s.page_number, _page_lane(s, ocr_lane=ocr_lane)) for s in sigs)

    buckets = [s.bucket for s in sigs if s.bucket != Bucket.SKIP]
    if len(buckets) == 0:
        # 전부 빈 페이지/열기 실패 — sigs 는 확보했으므로 페이지수준 필드를 **전부** 채운다.
        # ⚠️ `page_lanes`/`total_pages` 를 비우면 `_parse_routed` 가 `lanes.get(n, "odl")`
        #    기본값으로 새고, 그 페이지가 thin 판정을 받아 **전량 VL 전사 호출**을 받는다.
        # lane 은 리터럴 "odl" 고정 — 분석할 신호가 없는 축퇴 케이스를 vl/paddle_gw 로
        # 보내는 건 의미가 없고 위험하다(의도적 비대칭. 대칭을 맞추려 하지 말 것).
        return RouteDecision(lane="odl", page_signals=page_signals,
                             page_lanes=page_lanes, total_pages=total_pages)

    n_ocr = buckets.count(Bucket.OCR_NEEDED)
    # 다이어그램(순서도/차트) 페이지 — ODL 라우팅 시 페이지 단위 VL 서술 보충 대상.
    diagram_pages = tuple(s.page_number for s in sigs if getattr(s, "is_diagram", False))
    # 스캔 페이지 — paddle_gw 레인의 hybrid 처리 대상 한정용(§A0).
    ocr_pages = tuple(s.page_number for s in sigs if s.bucket == Bucket.OCR_NEEDED)
    # vl 레인 페이지는 PAGE_HYBRID 가 순서도 흐름서술을 포함하므로 narrate 대상에서 뺀다.
    # `LLM_NEEDED → vl` 이라 실제로는 거의 항상 빈 튜플이 된다(→ `_supplement_diagram_pages`
    # 는 이 배선에서 사실상 미발동. 코드 제거는 Phase 4).
    _lane_of = dict(page_lanes)
    narrate_pages = tuple(n for n in diagram_pages if _lane_of.get(n) != "vl")

    # `lane` 은 **하위호환용 요약값**이다(`_parse_routed` 는 page_lanes 만 본다).
    #   스캔 페이지가 있으면 ocr_lane, 없으면 odl. 탈출구(`=vl`)가 문서수준에도 반영된다.
    lane = ocr_lane if n_ocr > 0 else "odl"
    return RouteDecision(lane=lane, diagram_pages=diagram_pages, ocr_pages=ocr_pages,
                         page_signals=page_signals, page_lanes=page_lanes,
                         narrate_pages=narrate_pages, total_pages=total_pages)
