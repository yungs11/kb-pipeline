"""PDF **페이지수준** 라우팅 — triage 버킷을 페이지마다 레인으로 매핑한다(Plan B, 2026-08-04).

실측 근거(2026-07-14, 신탁/약관 문서 페이지별 triage 신호 덤프):
- 디지털 페이지(네이티브 텍스트 있음)는 triage 싼 신호(char_count/image_coverage)로
  텍스트/차트를 **구분 가능**(LLM_NEEDED=텍스트+큰이미지, TEXT_ONLY=순수텍스트).
- 스캔 페이지(char=0)는 전부 통짜 래스터라 신호가 동일(image_coverage≈0/1 고정) →
  "텍스트냐 순서도냐"를 **싼 신호로 구분 불가**. 픽셀 안을 봐야(=layout) 알 수 있음.

페이지별 레인:
- SKIP                     → skip       (ODL md 있으면 블록화, 없으면 빈 blocks. VL 미호출)
- OCR_NEEDED               → paddle_gw  (PaddleOCR-VL 게이트웨이 + layout 기반 hybrid)
- TEXT_ONLY / LLM_NEEDED   → odl        (ODL 이 표·텍스트 보존. 빈약하면 그 페이지만 VL 전사)

**문서수준 `vl` 레인은 삭제**(2026-08-04). 그림 비율(`KBP_GATE_VL_RATIO`)만 보고 문서 전체를
VL 로 넘기던 경로인데, KIS(11p)처럼 표가 많은 문서가 표 테두리 벡터선(curve=350)을 순서도로
오탐당해 통째로 VL 재전사되며 표 구조가 깨졌다. 이제 그런 문서는 페이지마다 odl 로 간다.

paddle_gw 는 KBP_PADDLE_OCR_GATEWAY_URL, 페이지 VL 전사는 MODEL_API_URL 필요.
"""
from __future__ import annotations

from dataclasses import dataclass

from parse_service.parsers.pdf.triage import triage_document, Bucket

@dataclass(frozen=True)
class RouteDecision:
    lane: str                 # "odl" | "paddle_gw" — 하위호환 요약값(B-5 는 page_lanes 를 본다)
    # 다이어그램(순서도/차트) 페이지 번호(1-based) — ODL 레인이 페이지 단위 VL 서술 보충에 사용.
    diagram_pages: tuple = ()
    # 스캔 페이지 번호(1-based, bucket == OCR_NEEDED). paddle_gw 레인의 layout 기반 hybrid 처리
    # 대상을 **실제 스캔 페이지로 한정**하는 데 쓴다(Plan A §A0, 2026-08-02).
    #   gate 는 스캔 페이지가 1장만 있어도 문서 전체를 paddle_gw 로 보내므로(아래 ②) pages 에는
    #   네이티브 텍스트 페이지가 섞인다. hybrid 판정의 면적 임계는 스캔 페이지에서만 실측했기에
    #   그 대상을 좁히지 않으면 근거 없는 적용이 된다. **lane 판정에는 전혀 관여하지 않는 순수 추가.**
    ocr_pages: tuple = ()
    # ── Plan B-1 (2026-08-04): 페이지수준 라우팅용 **순수 추가 필드**. ────────────────
    # 아직 아무도 소비하지 않는다 — B-5 에서 `_parse_routed` 가 페이지별로 병합할 때 쓴다.
    # `lane` 판정 로직에는 전혀 관여하지 않으므로 기존 동작이 그대로 유지된다.
    #   page_lanes    : ((1-based pno, "skip"|"odl"|"paddle_gw"), …)
    #                   SKIP→skip / OCR_NEEDED→paddle_gw / TEXT_ONLY·LLM_NEEDED→odl.
    #                   frozen dataclass 라 dict 대신 tuple — 소비 측에서 dict(...) 로 변환한다.
    #   narrate_pages : is_diagram 페이지(= 현행 diagram_pages 와 같은 의미). 서술 보충 대상.
    #                   `bucket == LLM_NEEDED` 로 잡으면 혼합 콘텐츠(텍스트+큰 이미지) 페이지까지
    #                   포함하는 상위집합이 되어 순서도가 아닌 페이지에 DIAGRAM 프롬프트가 붙는다.
    #   total_pages   : SKIP 포함 전체 페이지 수(= len(sigs)). page_lanes 의 상한.
    page_lanes: tuple = ()
    narrate_pages: tuple = ()
    total_pages: int = 0


_ODL = RouteDecision(lane="odl")


def _page_lane(bucket) -> str:
    """페이지 bucket → 레인. B-5 의 페이지수준 병합이 소비한다."""
    if bucket == Bucket.SKIP:
        return "skip"
    if bucket == Bucket.OCR_NEEDED:
        return "paddle_gw"
    return "odl"          # TEXT_ONLY / LLM_NEEDED


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
    # 스캔 페이지 — paddle_gw 레인의 hybrid 처리 대상 한정용(§A0). diagram_pages 와는
    # 상호배타적이다(triage: is_diagram 은 has_native_text 분기 안에서만, OCR_NEEDED 는 그 밖에서만).
    ocr_pages = tuple(s.page_number for s in sigs if s.bucket == Bucket.OCR_NEEDED)
    # B-1: 페이지수준 필드(순수 추가 — 아래 lane 판정에는 쓰이지 않는다).
    page_lanes = tuple((s.page_number, _page_lane(s.bucket)) for s in sigs)
    narrate_pages = diagram_pages          # is_diagram 페이지 — 의미가 같다
    total_pages = len(sigs)
    _extra = dict(diagram_pages=diagram_pages, ocr_pages=ocr_pages,
                  page_lanes=page_lanes, narrate_pages=narrate_pages,
                  total_pages=total_pages)

    # `lane` 은 **하위호환용 요약값**이다(B-5 의 `_parse_routed` 는 page_lanes 만 본다).
    #   스캔 페이지가 있으면 paddle_gw, 없으면 odl.
    # 문서수준 `vl` 레인은 삭제했다 — 그림 비율만 보고 문서 전체를 VL 로 넘겨 표를 깨뜨리던
    # 경로다(KIS 11p: 표 테두리 curve=350 이 순서도로 오탐 → 전 페이지 VL 재전사).
    lane = "paddle_gw" if n_ocr > 0 else "odl"
    return RouteDecision(lane=lane, **_extra)
