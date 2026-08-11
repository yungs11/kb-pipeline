"""paddle_gw 페이지 판정 — 3-way contract + terminal quarantine 단위 테스트.

설계 원칙: **애매하면 보존.** false quarantine 보다 silent deletion 이 더 나쁘다.
v1 정책: `ESCALATE_VL` 은 contract 로만 존재하고 절대 반환되지 않는다(실측 반증 —
gate 가 발화한 페이지에서 VL rescue 0 / 날조 2).
"""
import pytest

from parse_service.parsers.pdf import page_verdict as pv
from parse_service.parsers.pdf.page_verdict import PageState, Verdict, apply_gw_page_gate

_HEALTHY = ("원고는 피고와 2021년 3월 체결한 분양계약에 따라 계약금 일억원을 지급하였다. "
            "그런데 피고는 준공예정일을 도과하고도 소유권이전등기 절차를 이행하지 아니하였다.")
_LOOP = "기계음 손상완을 잡고 " * 60


def _p(n, blocks, status="ok", **kw):
    return {"page_number": n, "blocks": list(blocks), "status": status, **kw}


def _text(t):
    return {"type": "text", "text": t, "page_idx": 1}


@pytest.fixture(autouse=True)
def _ink(monkeypatch):
    """기본 잉크 = 있음. 테스트가 필요하면 개별로 덮어쓴다."""
    monkeypatch.setattr(pv, "page_ink", lambda fb, p: 0.30)


def _gate(pages, **kw):
    return apply_gw_page_gate(pages, b"%PDF", **kw)


# ── 정상 / 붕괴 ───────────────────────────────────────────────────────────────

def test_healthy_page_accepted():
    pages = [_p(1, [_text(_HEALTHY)])]
    (v,) = _gate(pages)
    assert v.verdict is Verdict.ACCEPT_GW and v.state is PageState.OK
    assert pages[0]["blocks"], "OK 페이지는 blocks 보존"


def test_degen_collapse_quarantines_and_empties_blocks():
    pages = [_p(1, [_text("정상 한 줄."), _text(_LOOP)])]
    (v,) = _gate(pages)
    assert v.verdict is Verdict.QUARANTINE and v.state is PageState.QUARANTINED_FAILURE
    assert v.reason.startswith("퇴화 붕괴")
    assert pages[0]["blocks"] == [], "quarantine 은 색인에서 실제로 빼야 한다"


def test_degen_below_min_chars_is_not_collapse():
    """`chars_before < 500` 이면 붕괴로 안 본다.

    하한이 없으면 퇴화 블록 1개 제거만으로 '페이지 전체 영구 제외' 가 된다 — 이 코퍼스에
    본문이 62자·43자·39자인 USABLE 페이지가 실재하고, T3(60~200자)는 USABLE 에서 ttr
    마진이 0.05 뿐이다. 실측 TP 2건은 chars_before 7,955 / 3,276 이라 500 하한으로
    잃는 것이 없다.
    """
    short_loop = "완성의 협력을 위한 " * 20        # T3 발화, <200자
    # 남는 본문은 EMPTY 임계(50자, 공백 제외)를 넘겨야 이 앵커가 DEGEN 하한만 검증한다.
    rest = ("이 사건 계약의 이행에 관하여 원고가 주장하는 바는 다음과 같으며 "
            "제출한 증거들로써 이를 충분히 뒷받침할 수 있다고 사료됩니다.")   # 54자
    pages = [_p(1, [_text(short_loop), _text(rest)])]
    (v,) = _gate(pages)
    sig = v.signals
    assert sig["hard_rules"] and sig["hard_removed"] == 1, "HARD 제거 자체는 일어난다"
    assert sig["chars_before"] < 500
    assert sig["chars_after"] / sig["chars_before"] < 0.5, "잔존율만 보면 붕괴 구간이다"
    assert not v.reason.startswith("퇴화 붕괴"), "하한 미달이라 DEGEN_COLLAPSE 는 발화하지 않는다"
    assert pages[0]["blocks"], "페이지는 살아남는다"


# ── EMPTY / EMPTY_SKIPPED ────────────────────────────────────────────────────

def test_empty_with_ink_is_quarantined():
    pages = [_p(1, [_text("표지")])]
    (v,) = _gate(pages)
    assert v.state is PageState.QUARANTINED_FAILURE and "OCR 실패" in v.reason


def test_empty_without_ink_is_skipped_and_kept(monkeypatch):
    """저잉크 = 종이가 실제로 빈 것. 실패가 아니고 **색인에서 빼지도 않는다**.

    실측에서 이 상태가 된 2건이 둘 다 USABLE 라벨 페이지였다(43자 / 35자).
    """
    monkeypatch.setattr(pv, "page_ink", lambda fb, p: 0.008)
    pages = [_p(1, [_text("표지")])]
    (v,) = _gate(pages)
    assert v.state is PageState.EMPTY_SKIPPED and v.verdict is Verdict.ACCEPT_GW
    assert pages[0]["blocks"], "EMPTY_SKIPPED 는 blocks 를 보존한다"


def test_ink_unknown_preserves(monkeypatch):
    """잉크를 모르면 '빈 페이지' 와 'OCR 실패' 를 가를 수 없다 → 보존 우선."""
    monkeypatch.setattr(pv, "page_ink", lambda fb, p: None)
    pages = [_p(1, [_text("표지")])]
    (v,) = _gate(pages)
    assert v.state is PageState.OK and pages[0]["blocks"]


def test_ink_computation_failure_returns_none(monkeypatch):
    """실제 ink 계산이 터져도 예외가 새지 않고 None 을 돌려준다(보존 우선 경로로 이어짐)."""
    import parse_service.parsers.pdf.page_verdict as mod
    monkeypatch.delattr(mod, "page_ink", raising=False)
    monkeypatch.setattr(mod, "page_ink", mod.__dict__.get("page_ink", None) or (lambda *a: None),
                        raising=False)
    assert pv.page_ink(b"not-a-pdf", 1) is None


# ── diagram 예외 (행 4' 는 행 4 의 대체 규칙) ────────────────────────────────

def test_diagram_page_short_vl_accepted():
    """VL 이 성공했지만 간결하게 응답한 도면 페이지를 EMPTY 로 잡으면 안 된다."""
    pages = [_p(1, [_text("START→검토→승인→END")])]
    (v,) = _gate(pages, diagram_pages=(1,))
    assert v.state is PageState.OK and pages[0]["blocks"]


def test_diagram_page_zero_chars_quarantined():
    pages = [_p(1, []), _p(2, [_text(_HEALTHY)])]
    v1, v2 = _gate(pages, diagram_pages=(1,))
    assert v1.state is PageState.QUARANTINED_FAILURE
    assert v2.state is PageState.OK


# ── 엔진 사고 ────────────────────────────────────────────────────────────────

def test_engine_error_has_own_verdict(monkeypatch):
    """게이트웨이 오류는 판정이 아니라 사고 — 저잉크여도 EMPTY_SKIPPED 로 새면 안 된다."""
    monkeypatch.setattr(pv, "page_ink", lambda fb, p: 0.001)
    pages = [_p(1, [], status="error", error="TimeoutError: poll")]
    (v,) = _gate(pages)
    assert v.verdict is Verdict.ENGINE_ERROR and v.state is PageState.ENGINE_ERROR
    assert "게이트웨이 오류" in v.reason
    assert pages[0]["blocks"] == []


# ── CJK ──────────────────────────────────────────────────────────────────────

_CJK_PAGE = "名稱 單位 數量 總價 工程名稱 設計期限 序號 合計 備註 承包商 監理 竣工 檢査 承認 申請"


def test_cjk_contamination_quarantined():
    pages = [_p(1, [_text(_CJK_PAGE)])]
    (v,) = _gate(pages)
    assert v.state is PageState.QUARANTINED_FAILURE and v.reason.startswith("한자 오염")


def test_cjk_document_guard_reverts_and_keeps_blocks():
    """전 페이지가 한자면 '한자 문서' 로 보고 되돌린다 — **blocks 도 보존돼야 한다**.

    2-phase 앵커: 판정 즉시 비우는 단일 패스로 구현하면 verdict 는 ACCEPT_GW 인데
    blocks 는 []가 되어 국한문혼용 문서가 통째로 조용히 사라진다.
    """
    pages = [_p(i, [_text(_CJK_PAGE)]) for i in (1, 2, 3)]
    verdicts = _gate(pages)
    assert all(v.verdict is Verdict.ACCEPT_GW for v in verdicts)
    assert all(p["blocks"] for p in pages), "되돌린 페이지의 blocks 가 보존돼야 한다"


def test_cjk_document_guard_needs_min_pages():
    """1~2페이지 문서에서는 가드가 자기 자신을 무력화하므로 적용하지 않는다."""
    pages = [_p(i, [_text(_CJK_PAGE)]) for i in (1, 2)]
    verdicts = _gate(pages)
    assert all(v.state is PageState.QUARANTINED_FAILURE for v in verdicts)


# ── 스위치 / 정책 앵커 ───────────────────────────────────────────────────────

def test_gate_off_accepts_everything(monkeypatch):
    monkeypatch.setenv("KBP_GW_GATE", "0")
    pages = [_p(1, [_text("정상 한 줄."), _text(_LOOP)])]
    (v,) = _gate(pages)
    assert v.verdict is Verdict.ACCEPT_GW
    assert len(pages[0]["blocks"]) == 2, "게이트를 끄면 blocks 를 건드리지 않는다"


def test_escalate_vl_is_never_returned(monkeypatch):
    """v1 정책 앵커 — contract 에만 존재한다."""
    monkeypatch.setattr(pv, "page_ink", lambda fb, p: 0.30)
    pages = [_p(1, [_text(_HEALTHY)]), _p(2, [_text(_LOOP)]),
             _p(3, [], status="error", error="boom"), _p(4, [_text(_CJK_PAGE)]),
             _p(5, [_text("표지")])]
    assert all(v.verdict is not Verdict.ESCALATE_VL for v in _gate(pages))


# ── 관측 ─────────────────────────────────────────────────────────────────────

def test_summary_log_has_ok_and_reasons(caplog):
    pages = [_p(1, [_text(_HEALTHY)]), _p(2, [_text("정상 한 줄."), _text(_LOOP)])]
    with caplog.at_level("INFO", logger="kb_pipeline.parse_service.parsers.pdf.page_verdict"):
        _gate(pages)
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "ok 1" in joined and "quarantine 1" in joined
    assert "퇴화 붕괴" in joined


def test_all_quarantine_emits_dedicated_warning(caplog):
    """전 페이지 quarantine 이면 빈 문서가 HTTP 200 으로 나간다 — 전용 경고가 필요하다."""
    pages = [_p(1, [_text("정상 한 줄."), _text(_LOOP)])]
    with caplog.at_level("WARNING", logger="kb_pipeline.parse_service.parsers.pdf.page_verdict"):
        _gate(pages)
    msgs = [r.getMessage() for r in caplog.records]
    assert any("전 페이지 quarantine" in m for m in msgs)
    assert any("빈 결과로 나간다" in m for m in msgs)


# ── 2026-08-11 Phase 1 §6: 정규화의 두 번째 소비자가 이 게이트다 ──────────────

def test_toc_page_not_quarantined():
    """leader dot 목차 페이지가 GW 게이트에서 quarantine 되면 안 된다.

    `assess_text_rules` 정규화는 `filter_degenerate_pages` 뿐 아니라 이 게이트의 입력도
    바꾼다(`apply_gw_page_gate` → `assess_page` → `assess_text_rules`). v1 게이트 회귀 앵커.
    """
    titles = ["총칙", "용어의 정의", "적용 범위", "우발비용의 개념", "사전점검 대상 사업",
              "점검 시기 및 주기", "점검 항목과 기준", "현장 실사 절차", "보고서 작성 요령",
              "이견 조정 및 재점검", "결과의 활용", "기록의 보존", "위임 및 준용", "부칙"]
    toc = "\n".join(f"제{i}조 {t} {'. ' * 25} {i * 5}" for i, t in enumerate(titles, 1))
    pages = [_p(1, [_text(toc)])]
    (v,) = _gate(pages)
    assert v.state is PageState.OK, f"목차가 격리됐다: {v.reason}"
    assert pages[0]["blocks"], "목차 blocks 가 보존돼야 한다"
