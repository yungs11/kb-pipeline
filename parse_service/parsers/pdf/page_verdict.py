"""GW 페이지 판정 — 3-way triage contract + terminal quarantine.

**v1 정책: `ESCALATE_VL` 은 contract 로만 존재하고 절대 반환되지 않는다.**
실측 반증(2026-08-11, `V1_DECISION.md` conditional join): hard-fail 신호는
**"VL 이 도움이 되는 페이지" 의 반대**를 고른다 — gate 가 발화한 페이지에서 VL rescue 0,
날조 2. 확인된 rescue 6건은 전부 gate 가 놓친 페이지에서 나왔다. 그래서 GW→VL escalation
execution path 를 v1 에 만들지 않는다(그걸 만들면 GPU 를 써서 날조를 유입시킨다).
`SOFT_RISK` trigger 가 실데이터로 검증되면 그때 한 줄 활성화한다(v1.1).

설계 원칙: **애매하면 보존.** false quarantine 보다 silent deletion 이 더 나쁘다.

판정 우선순위(고정) — 위에서부터 매기고, 매겨지면 아래는 보지 않는다:
    ENGINE_ERROR → DEGEN_COLLAPSE → CJK_CONTAM → EMPTY(일반/diagram) → EMPTY_SKIPPED

mutation 은 **반드시 2-phase** 다(`apply_gw_page_gate` docstring 참조).
"""
from __future__ import annotations

import logging
import os
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from enum import Enum

from parse_service.parsers.degen_filter import (
    PageAssessment, apply_assessment, assess_page, visible_chars,
)

log = logging.getLogger("kb_pipeline.parse_service.parsers.pdf.page_verdict")

_HAN_RE = re.compile(r"[一-鿿]")
_HANGUL_RE = re.compile(r"[가-힣]")


class Verdict(str, Enum):
    ACCEPT_GW = "accept_gw"
    ESCALATE_VL = "escalate_vl"     # contract 전용 — v1 정책은 이 값을 반환하지 않는다
    QUARANTINE = "quarantine"
    ENGINE_ERROR = "engine_error"   # 판정이 아니라 엔진 사고 — PageState 와 1:1


class PageState(str, Enum):
    OK = "ok"                                   # 색인 대상
    QUARANTINED_FAILURE = "quarantined_failure"  # 파서 실패 — blocks 를 비운다
    ENGINE_ERROR = "engine_error"                # 엔진 사고 — 별도 카운터, blocks 비움
    EMPTY_SKIPPED = "empty_skipped"              # 원래 빈 페이지 — **blocks 보존(색인 유지)**


#: blocks 를 비우는(= 색인에서 빼는) 상태. EMPTY_SKIPPED 는 포함하지 않는다 —
#: 실측에서 이 상태가 된 2건이 둘 다 USABLE 라벨 페이지였다(43자/35자, 저잉크).
#: 내용이 50자 미만이라 색인해도 무해하고, 빼면 게이트가 스스로 "silent deletion 이 더
#: 나쁘다" 는 원칙을 위반한다.
_MUTATING_STATES = frozenset({PageState.QUARANTINED_FAILURE, PageState.ENGINE_ERROR})


@dataclass(frozen=True)
class PageVerdict:
    page_number: object
    verdict: Verdict
    state: PageState
    reason: str = ""
    signals: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["verdict"] = self.verdict.value
        d["state"] = self.state.value
        return d


# ───────────────────────────────────────────────────────────────── env (call-time)

def _f(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("%s=%r 파싱 실패 — 기본값 %s", name, raw, default)
        return default


def _i(name: str, default: int) -> int:
    return int(_f(name, default))


def gate_enabled() -> bool:
    return os.environ.get("KBP_GW_GATE", "1") != "0"


# ───────────────────────────────────────────────────────────────── 신호

def page_chars(page: dict) -> int:
    """페이지의 보이는 문자 수(태그·공백 제외) — degen_filter 와 동일 정의."""
    total = 0
    for b in (page or {}).get("blocks") or []:
        body = b.get("table_body") if b.get("type") == "table" else b.get("text")
        total += visible_chars(body or "")
    return total


def cjk_signal(page: dict) -> tuple[int, float]:
    """(한자 수, 한자/(한자+한글) 비율)."""
    buf = []
    for b in (page or {}).get("blocks") or []:
        body = b.get("table_body") if b.get("type") == "table" else b.get("text")
        if body:
            buf.append(body)
    text = "".join(buf)
    han = len(_HAN_RE.findall(text))
    hangul = len(_HANGUL_RE.findall(text))
    denom = han + hangul
    return han, (han / denom if denom else 0.0)


def page_ink(file_bytes: bytes, page_number: int) -> float | None:
    """페이지 잉크량 = **어두운 픽셀 비율**(dpi100 GRAY, v<160).

    측정 하네스(`qual_sample.py`)와 **비트 단위로 같은 정의**여야 임계 0.05 가 유효하다.
    numpy 를 쓰지 않는다 — `requirements.txt` 에 없어서 **컨테이너에서만 ImportError** 로
    죽는다(호스트 dev venv 엔 있어 로컬에서 재현되지 않는 유형).

    EMPTY 후보 페이지에만 지연 호출한다(코퍼스 15만 페이지에 렌더를 돌리지 않는다).
    실패하면 None — 호출부는 None 이면 EMPTY 를 hard fail 로 보지 않는다(보존 우선).
    """
    try:
        import pymupdf as fitz
    except ImportError:  # pragma: no cover
        try:
            import fitz  # type: ignore
        except Exception:  # noqa: BLE001
            return None
    doc = None
    try:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        if not (1 <= page_number <= doc.page_count):
            return None
        pix = doc[page_number - 1].get_pixmap(dpi=100, alpha=False,
                                              colorspace=fitz.csGRAY)
        samples = pix.samples
        if not samples:
            return None
        return sum(1 for v in samples if v < 160) / len(samples)
    except Exception:  # noqa: BLE001 — ink 실패가 파싱을 깨면 안 된다
        log.exception("ink 계산 실패 (page %s)", page_number)
        return None
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:  # noqa: BLE001
                pass


# ───────────────────────────────────────────────────────────────── 판정(phase 1)

def judge_page(page: dict, assessment: PageAssessment, *,
               is_diagram: bool, ink_fn) -> PageVerdict:
    """페이지 1장 판정. **부작용 없음** — blocks 를 건드리지 않는다.

    `ink_fn` 은 EMPTY 후보에서만 호출되는 지연 콜러블(`() -> float | None`).
    """
    pno = (page or {}).get("page_number")
    chars_before = assessment.chars_before
    chars_after = page_chars(page)
    hard_rules = sorted({r for b in assessment.hard_blocks for r in b.rules})
    soft_rules = sorted({r for b in assessment.soft_blocks for r in b.rules})
    sig = {"chars_before": chars_before, "chars_after": chars_after,
           "hard_rules": hard_rules, "soft_rules": soft_rules,
           "hard_removed": len(assessment.hard_blocks), "is_diagram": is_diagram}

    def _v(verdict, state, reason, **extra):
        sig.update(extra)
        return PageVerdict(page_number=pno, verdict=verdict, state=state,
                           reason=reason, signals=sig)

    # 1. 엔진 사고 — 판정이 아니다. 최우선(저잉크일 때 EMPTY_SKIPPED 로 새면 안 된다).
    if (page or {}).get("status") == "error":
        return _v(Verdict.ENGINE_ERROR, PageState.ENGINE_ERROR,
                  f"게이트웨이 오류: {(page or {}).get('error') or '알 수 없음'}")

    # 2. DEGEN_COLLAPSE — HARD 제거 후 대부분이 사라졌다.
    #    chars_before 하한이 없으면 62자짜리 페이지가 T3 오탐 1발로 통째 격리된다.
    ratio_max = _f("KBP_GW_DEGEN_SURVIVE_RATIO", 0.5)
    min_before = _i("KBP_GW_DEGEN_MIN_CHARS", 500)
    if (assessment.hard_blocks and chars_before >= min_before
            and chars_after / max(chars_before, 1) < ratio_max):
        return _v(Verdict.QUARANTINE, PageState.QUARANTINED_FAILURE,
                  f"퇴화 붕괴: {chars_before}자 → {chars_after}자 "
                  f"(HARD {len(assessment.hard_blocks)}블록 제거, 규칙 {','.join(hard_rules)})",
                  survive_ratio=round(chars_after / max(chars_before, 1), 4))

    # 3. CJK_CONTAM — 한자 오염. 문서 단위 가드는 phase 1 종료 후 되돌린다.
    han, han_ratio = cjk_signal(page)
    sig.update(cjk_count=han, cjk_ratio=round(han_ratio, 4))
    if han >= _i("KBP_GW_CJK_MIN", 30) and han_ratio >= _f("KBP_GW_CJK_RATIO", 0.50):
        return _v(Verdict.QUARANTINE, PageState.QUARANTINED_FAILURE,
                  f"한자 오염: 한자 {han}자, 비율 {han_ratio:.2f}")

    # 4 / 4'. EMPTY — diagram 페이지는 행 4 **대신** 4' 를 적용한다(추가가 아니다).
    #    게이트가 _supplement_diagram_pages 뒤에 있어 chars_after 가 VL 서술 기준이므로,
    #    VL 이 성공했지만 간결하게 응답한 페이지가 일반 임계(50자)에서 잘못 잡힌다.
    #    VL 이 1자라도 냈으면 성공으로 본다.
    min_chars = _i("KBP_GW_MIN_CHARS", 50)
    empty_hit = (chars_after == 0) if is_diagram else (chars_after < min_chars)
    if empty_hit:
        ink = ink_fn()
        sig["ink"] = None if ink is None else round(ink, 4)
        blank_max = _f("KBP_GW_BLANK_INK_MAX", 0.05)
        if ink is None:
            # 보존 우선 — 잉크를 모르면 "빈 페이지" 와 "OCR 실패" 를 가를 수 없다.
            return _v(Verdict.ACCEPT_GW, PageState.OK, "빈 출력이나 ink 측정 실패 — 보존")
        if ink >= blank_max:
            return _v(Verdict.QUARANTINE, PageState.QUARANTINED_FAILURE,
                      f"빈 출력({chars_after}자)인데 잉크 있음(ink={ink:.4f}) — OCR 실패")
        # 5. 종이가 실제로 비었다 — 실패가 아니고 색인에서 빼지도 않는다.
        return _v(Verdict.ACCEPT_GW, PageState.EMPTY_SKIPPED,
                  f"빈 페이지(ink={ink:.4f}) — 내용 없음")

    return _v(Verdict.ACCEPT_GW, PageState.OK, "")


# ────────────────────────────────────────────────── 2-phase 진입점

def apply_gw_page_gate(pages: list, file_bytes: bytes, *,
                       diagram_pages: tuple = ()) -> list[PageVerdict]:
    """GW 페이지 게이트. **반드시 2-phase 다.**

    phase 1 (판정, 부작용 없음)
        - 전 페이지 verdict 확정
        - 문서 단위 CJK 가드 되돌림 반영
        - ink 렌더·fitz open 등 **실패 가능 연산은 전부 여기**(try/except → ink=None)
    phase 2 (변형, 예외 없음)
        - 최종 state 가 QUARANTINED_FAILURE / ENGINE_ERROR 인 페이지만 blocks=[] (순수 대입)

    **단일 패스로 구현하면 안 된다.** 판정 즉시 비우면 문서 단위 CJK 가드가 ACCEPT_GW 로
    되돌리는 시점에 blocks 가 이미 비어 있어 `verdict == ACCEPT_GW` 인데 `blocks == []` 가
    된다 — 국한문혼용 문서 전체가 "통과" 판정인 채로 색인에서 사라진다. phase 2 에 예외
    가능 연산을 두지 않는 것도 같은 이유다(중간 예외 → 일부 페이지만 비워진 부분 mutation).
    """
    pages = pages or []
    if not gate_enabled():
        return [PageVerdict(page_number=p.get("page_number"), verdict=Verdict.ACCEPT_GW,
                            state=PageState.OK, reason="KBP_GW_GATE=0 — 게이트 비활성")
                for p in pages]

    diagram = set(diagram_pages or ())

    # ── phase 1 — 판정만. HARD 제거는 여기서 일어난다(degen 안전화의 소유 범위).
    verdicts: list[PageVerdict] = []
    for page in pages:
        assessment = assess_page(page)          # 순수
        apply_assessment(page, assessment)       # HARD 만 제거 + SOFT 관측 로그
        pno = page.get("page_number")
        verdicts.append(judge_page(
            page, assessment,
            is_diagram=pno in diagram,
            ink_fn=lambda p=pno: page_ink(file_bytes, p) if isinstance(p, int) else None,
        ))

    verdicts = _apply_cjk_document_guard(verdicts)

    # ── phase 2 — 순수 대입만. 예외를 던질 수 있는 연산을 여기 두지 않는다.
    by_page = {v.page_number: v for v in verdicts}
    for page in pages:
        v = by_page.get(page.get("page_number"))
        if v is not None and v.state in _MUTATING_STATES:
            page["blocks"] = []

    _log_summary(verdicts)
    return verdicts


def _apply_cjk_document_guard(verdicts: list[PageVerdict]) -> list[PageVerdict]:
    """문서 전체가 국한문혼용이면 CJK 판정을 되돌린다.

    "오염은 페이지 국소 현상" 이라는 전제를 명시적으로 검증하는 장치다. 구 등기부·제적등본·
    1980~90년대 계약서/정관은 페이지당 한자 30자·비율 0.30 을 전 페이지에서 넘긴다.
    quarantine 이 terminal 이므로 오탐 1건이 **문서 1건 손실**이 된다.

    최소 페이지 수 조건이 없으면 1~2페이지 문서에서 가드가 자기 자신을 무력화한다.
    ⚠️ 이 가드는 표본에 국한문혼용 문서가 0건이라 **미검증**이다(D12).
    """
    total = len(verdicts)
    if total < _i("KBP_GW_CJK_DOC_MIN_PAGES", 3):
        return verdicts
    cjk_hits = [v for v in verdicts if v.state is PageState.QUARANTINED_FAILURE
                and v.reason.startswith("한자 오염")]
    if not cjk_hits or (len(cjk_hits) / total) < _f("KBP_GW_CJK_DOC_RATIO", 0.30):
        return verdicts
    log.warning("CJK 문서 가드 발동 — %d/%d 페이지가 한자 오염 판정 → 한자 문서로 보고 되돌림",
                len(cjk_hits), total)
    reverted = set(id(v) for v in cjk_hits)
    return [
        PageVerdict(page_number=v.page_number, verdict=Verdict.ACCEPT_GW, state=PageState.OK,
                    reason="한자 문서(문서 단위 가드) — CJK 판정 되돌림", signals=v.signals)
        if id(v) in reverted else v
        for v in verdicts
    ]


def _log_summary(verdicts: list[PageVerdict]) -> None:
    states = Counter(v.state for v in verdicts)
    reasons = Counter(v.reason.split(":")[0] for v in verdicts
                      if v.state is PageState.QUARANTINED_FAILURE)
    quarantined = states[PageState.QUARANTINED_FAILURE]
    total = len(verdicts)
    log.info("paddle_gw 게이트: 총 %d p / ok %d / quarantine %d (%s) / ENGINE_ERROR %d / "
             "EMPTY_SKIPPED %d",
             total, states[PageState.OK], quarantined,
             ", ".join(f"{k} {n}" for k, n in reasons.items()) or "-",
             states[PageState.ENGINE_ERROR], states[PageState.EMPTY_SKIPPED])
    if quarantined:
        log.warning("paddle_gw quarantine %d/%d 페이지 — 사유: %s", quarantined, total,
                    ", ".join(f"{k} {n}" for k, n in reasons.items()))
    if total and quarantined == total:
        log.warning("paddle_gw **전 페이지 quarantine**(%d/%d) — 이 문서는 빈 결과로 나간다"
                    "(enriched_content='', n_blocks=0, HTTP 200). 사유: %s",
                    quarantined, total,
                    ", ".join(f"{k} {n}" for k, n in reasons.items()))
