"""PDF 문서수준 게이트 — triage 버킷 집계로 ODL / VL / Paddle gateway 라우팅."""
import pytest
from parse_service.parsers.pdf import gate
from parse_service.parsers.pdf.triage import PageSignals, Bucket


def _sig(bucket):
    s = PageSignals(page_number=1, width=600, height=800)
    s.bucket = bucket
    return s


T, L, O, S = Bucket.TEXT_ONLY, Bucket.LLM_NEEDED, Bucket.OCR_NEEDED, Bucket.SKIP


def _sigp(bucket, pno, **kw):
    """페이지 번호·추가 신호를 지정하는 `_sig` — 페이지수준 앵커용."""
    s = PageSignals(page_number=pno, width=600, height=800)
    s.bucket = bucket
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def _sigs(*buckets):
    return [_sigp(b, i) for i, b in enumerate(buckets, start=1)]


@pytest.mark.parametrize("buckets,lane", [
    # 순수 디지털 텍스트 → ODL
    ([T, T], "odl"),
    ([T, S], "odl"),
    ([S], "odl"),
    ([], "odl"),
    # **문서수준 `vl` 레인은 삭제됐다**(2026-08-04) — 그림 비율만 보고 문서 전체를 VL 로
    # 넘겨 표를 깨뜨리던 경로다(KIS 11p: 표 테두리 curve=350 오탐 → 전 페이지 재전사).
    # 이제 문서수준 `lane` 은 **스캔 페이지 유무로만** 갈리고, 차트/그림은 페이지수준
    # `page_lanes` 에서 `vl` 로 처리된다(아래 페이지수준 앵커).
    ([L, L], "odl"),
    ([T, L], "odl"),
    ([L], "odl"),
    ([O, L], "paddle_gw"),                  # 스캔이 있으면 문서수준은 paddle_gw
    ([O, L, L], "paddle_gw"),
    # 스캔 페이지 존재(OCR_NEEDED) → paddle_gw(게이트웨이)
    ([O, O], "paddle_gw"),
    ([O, S], "paddle_gw"),
    ([T, O], "paddle_gw"),                  # 혼합(디지털+스캔)
    ([O, T, L], "paddle_gw"),               # 차트 1/3 < 0.5 → 스캔 우선
    # 차트 소수 + 스캔 없음 → 문서수준 ODL(해당 페이지만 page_lanes 에서 vl)
    ([T, T, L], "odl"),
    ([T, T, T, L], "odl"),
])
def test_decide_route(monkeypatch, buckets, lane):
    monkeypatch.setattr(gate, "triage_document", lambda b: [_sig(x) for x in buckets])
    d = gate.decide_route(b"%PDF")
    assert d.lane == lane


def test_triage_exception_falls_back_to_odl(monkeypatch):
    def boom(b):
        raise RuntimeError("corrupt page iteration")
    monkeypatch.setattr(gate, "triage_document", boom)
    d = gate.decide_route(b"%PDF")
    assert d.lane == "odl"


def test_odl_route_carries_diagram_pages(monkeypatch):
    """다이어그램 페이지(LLM_NEEDED+is_diagram)가 비율 미달로 ODL 라우팅될 때
    diagram_pages 로 전달돼야(ODL 레인 VL 보충용). 정의서 패턴: 15p 중 1p 순서도."""
    sigs = []
    for n in range(1, 7):                     # 6p: p5 만 다이어그램
        s = _sig(T if n != 5 else L)
        s.page_number = n
        if n == 5:
            s.is_diagram = True
        sigs.append(s)
    monkeypatch.setattr(gate, "triage_document", lambda b: sigs)
    d = gate.decide_route(b"%PDF")
    assert d.lane == "odl"                    # 1/6 < 0.5 → ODL
    assert d.diagram_pages == (5,)


def test_scan_route_has_no_diagram_pages(monkeypatch):
    """스캔 문서(paddle_gw 라우팅)는 diagram_pages 불필요(게이트웨이가 전 페이지 VL 처리)."""
    monkeypatch.setattr(gate, "triage_document", lambda b: [_sig(O)])
    d = gate.decide_route(b"%PDF")
    assert d.lane == "paddle_gw" and d.diagram_pages == ()


def test_paddle_gw_route_carries_diagram_pages(monkeypatch):
    """스캔 문서에 디지털 다이어그램 페이지가 섞이면(소유권 p4 패턴) paddle_gw 라우팅에도
    diagram_pages 전달 — 게이트웨이가 순서도를 이미지참조로만 내므로 VL 보충 필요."""
    sigs = []
    for n, b in [(1, O), (2, T), (3, T), (4, L), (5, T), (6, T), (7, T)]:
        s = _sig(b); s.page_number = n
        if n == 4:
            s.is_diagram = True
        sigs.append(s)
    monkeypatch.setattr(gate, "triage_document", lambda b: sigs)
    d = gate.decide_route(b"%PDF")
    assert d.lane == "paddle_gw"      # 스캔 존재, 차트 1/7 < 0.5
    assert d.diagram_pages == (4,)


# ── 레인/비율 env화(2026-08-06, 이미지 파서 고도화 준비) ────────────────────────

def test_page_signals_filled_for_normal_routes(monkeypatch):
    """정상 라우팅(비-빈문서)도 page_signals 를 채워 반환한다(§C 로깅 소비 대상)."""
    sigs = [_sig(T), _sig(T)]
    monkeypatch.setattr(gate, "triage_document", lambda b: sigs)
    d = gate.decide_route(b"%PDF")
    assert d.page_signals == tuple(sigs)


@pytest.mark.parametrize("buckets", [[S], []])
def test_page_signals_filled_even_when_total_zero(monkeypatch, buckets):
    """전부 SKIP/빈 페이지(total==0)도 sigs 는 이미 확보했으므로 page_signals 를 채운다
    (싱글턴 `_ODL` 재사용 금지 — §B 핵심 규칙). lane 은 그대로 "odl" 리터럴."""
    sigs = [_sig(b) for b in buckets]
    monkeypatch.setattr(gate, "triage_document", lambda b: sigs)
    d = gate.decide_route(b"%PDF")
    assert d.lane == "odl"
    assert d.page_signals == tuple(sigs)


def test_triage_exception_has_no_page_signals(monkeypatch):
    """triage_document 자체가 예외로 실패한 경로만 page_signals=()(공유 싱글턴)로 남는다."""
    def boom(b):
        raise RuntimeError("corrupt page iteration")
    monkeypatch.setattr(gate, "triage_document", boom)
    d = gate.decide_route(b"%PDF")
    assert d.page_signals == ()


def test_ocr_lane_env_override(monkeypatch):
    monkeypatch.setenv("KBP_GATE_OCR_LANE", "vl")
    monkeypatch.setattr(gate, "triage_document", lambda b: [_sig(O)])
    d = gate.decide_route(b"%PDF")
    assert d.lane == "vl"
def test_invalid_lane_env_warns_and_falls_back(monkeypatch, caplog):
    """모르는 레인 값이면 경고 + 그 변수 고유 기본값 폴백 — 잘못된 env 하나가 파싱을 죽이면 안 된다.

    2026-08-12: `KBP_GATE_VL_LANE`/`KBP_GATE_DEFAULT_LANE` 은 문서수준 vl 레인 삭제로
    소비자가 사라졌다(§6A — 선언은 유지, 주석으로 무효 명시). 살아남은 `_resolve_lane`
    소비자는 **`KBP_GATE_OCR_LANE` 하나뿐**이고, 그것은 폐쇄망 탈출구라 계속 지킨다.
    """
    import logging
    monkeypatch.setenv("KBP_GATE_OCR_LANE", "bogus")
    monkeypatch.setattr(gate, "triage_document", lambda b: _sigs(O, T))
    with caplog.at_level(logging.WARNING, logger=gate.log.name):
        d = gate.decide_route(b"%PDF")
    assert d.lane == "paddle_gw", "화이트리스트 밖 → 기본값 폴백"
    assert d.page_lanes == ((1, "paddle_gw"), (2, "odl")), "페이지수준에도 같은 폴백"
    assert any("유효한 레인이 아님" in r.message for r in caplog.records)


# ══════════════════════════════════════════════════════════════════════════════
# 페이지수준 필드 앵커 — scan-lane 판 `test_pdf_gate.py` 에서 **승계**(2026-08-12)
#
# `page_lanes`/`narrate_pages`/`total_pages` 의 **유일한 게이트-수준 커버리지**다.
# 정본을 HEAD 로 골랐으므로 명시적으로 이식하지 않으면 병합본에서 0건이 되는데,
# 그건 pytest 가 못 잡는다(테스트가 없으면 실패도 없다).
#
# 단언은 `LLM_NEEDED → vl`(Phase 2a 사용자 결정)에 맞춰 갱신했다.
# ══════════════════════════════════════════════════════════════════════════════
def test_page_lanes_maps_each_bucket(monkeypatch):
    """SKIP→skip / OCR_NEEDED→ocr_lane / **LLM_NEEDED→vl** / TEXT_ONLY→odl, 번호 보존."""
    monkeypatch.setattr(gate, "triage_document", lambda b: _sigs(T, S, O, L))
    d = gate.decide_route(b"%PDF")
    assert d.page_lanes == ((1, "odl"), (2, "skip"), (3, "paddle_gw"), (4, "vl"))
    assert d.total_pages == 4


def test_narrate_pages_excludes_vl_lane_pages(monkeypatch):
    """`narrate_pages` 는 **vl 레인이 아닌** is_diagram 페이지다.

    개명·의도 재기술(2026-08-12): 옛 이름은 `..._is_diagram_not_llm_needed` 였고
    "LLM_NEEDED 상위집합이 아니다" 를 지켰다. 그런데 `LLM_NEEDED → vl` 이 되면서
    is_diagram 페이지는 **전부 vl 레인**이 되고, vl 레인은 PAGE_HYBRID 가 순서도
    흐름서술을 이미 포함하므로 서술 보충 대상에서 빠져야 한다 — 안 그러면 그 페이지에
    **VL 을 두 번** 부르고 블록이 중복된다.

    결과적으로 `narrate_pages` 는 거의 항상 빈 튜플이고 `_supplement_diagram_pages` 는
    사실상 미발동이다(§9 D11 — 코드 제거는 Phase 4).
    """
    sigs = [_sigp(L, 1, is_diagram=True), _sigp(L, 2), _sigp(T, 3)]
    monkeypatch.setattr(gate, "triage_document", lambda b: sigs)
    d = gate.decide_route(b"%PDF")
    assert d.diagram_pages == (1,), "is_diagram 자체는 그대로 기록된다"
    assert d.page_lanes == ((1, "vl"), (2, "vl"), (3, "odl"))
    assert d.narrate_pages == (), "vl 레인 페이지는 서술 보충 대상이 아니다(VL 2회 호출 방지)"


def test_page_level_fields_do_not_change_doc_lane(monkeypatch):
    """페이지수준 필드는 문서수준 `lane`/`diagram_pages`/`ocr_pages` 판정을 바꾸지 않는다."""
    sigs = [_sigp(O, 1), _sigp(T, 2), _sigp(L, 3, is_diagram=True)]
    monkeypatch.setattr(gate, "triage_document", lambda b: sigs)
    d = gate.decide_route(b"%PDF")
    assert d.lane == "paddle_gw"          # 스캔 존재
    assert d.diagram_pages == (3,) and d.ocr_pages == (1,)
    assert d.page_lanes == ((1, "paddle_gw"), (2, "odl"), (3, "vl"))


def test_chart_heavy_document_is_not_transcribed_wholesale(monkeypatch):
    """차트/그림이 많아도 **문서 전체 재전사는 없다** — 페이지 단위로만 vl 로 간다.

    개명·의도 재기술(2026-08-12): 옛 이름은 `..._goes_to_odl_not_vl` 이었고 문서수준
    lane 이 odl 임을 단언했다. 지키려던 회귀는 **KIS 11p 처럼 표가 많은 문서가 표 테두리
    curve=350 을 순서도로 오탐당해 통째로 VL 재전사되며 표가 깨지던 것**이다.
    그 회귀는 지금도 막힌다 — 문서수준 vl 레인이 없으므로 `lane` 은 여전히 odl 이고,
    재전사는 **그 페이지에만** 국한된다. 이름을 의도에 맞게 바꿨다.
    """
    monkeypatch.setattr(gate, "triage_document",
                        lambda b: [_sigp(L, 1, is_diagram=True), _sigp(L, 2, is_diagram=True)])
    d = gate.decide_route(b"%PDF")
    assert d.lane == "odl", "문서수준 vl 레인은 없다 — 문서 통째 재전사 금지"
    assert d.page_lanes == ((1, "vl"), (2, "vl")), "재전사는 페이지 단위로만"
    assert d.narrate_pages == (), "vl 레인이라 서술 보충 중복 없음"


def test_gate_failure_returns_empty_page_level_fields(monkeypatch):
    """triage 예외 — `sigs` 자체를 못 얻으므로 페이지수준 필드가 전부 비는 게 맞다."""
    def boom(b):
        raise RuntimeError("pymupdf 부재")
    monkeypatch.setattr(gate, "triage_document", boom)
    d = gate.decide_route(b"%PDF")
    assert d.lane == "odl" and d.page_lanes == () and d.total_pages == 0


def test_all_skip_document_still_fills_page_lanes(monkeypatch):
    """**전-SKIP 문서도 `page_lanes`/`total_pages` 를 채운다**(조기 return 회귀 앵커).

    싱글턴 `_ODL` 을 재사용하거나 `page_signals` 만 채우면 이 둘이 기본값 `()`/`0` 으로
    남는다. 그러면 `_parse_routed` 의 병합이 `lanes.get(n, "odl")` 기본값으로 새고,
    그 페이지들이 thin 판정을 받아 **전량 VL 전사 호출**을 받는다(비용·품질 양쪽 손해).
    """
    monkeypatch.setattr(gate, "triage_document", lambda b: _sigs(S, S, S))
    d = gate.decide_route(b"%PDF")
    assert d.lane == "odl"
    assert d.page_lanes == ((1, "skip"), (2, "skip"), (3, "skip"))
    assert d.total_pages == 3
    assert len(d.page_signals) == 3
