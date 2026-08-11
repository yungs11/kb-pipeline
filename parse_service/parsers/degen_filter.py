"""퇴화(무한반복) 블록 — **증거 생성기**. 삭제는 확실한 것(HARD)에만 한다.

VLM(qwen/PaddleOCR-VL/게이트웨이)이 어려운 페이지에서 같은 구절을 무한 반복 생성하는
퇴화(degenerate loop)가 관측됨(2026-07-15). LLM 없이 싼 통계 신호로 검출한다.

**2026-08-11 재설계 — detect → decide → mutate 분리.**
이전 버전은 판정 즉시 블록을 삭제했고, 그 결과 **정상 법인등기부 표가 R4(TTR)에 걸려
production 에서 삭제되고 있었다**(실측: 죽림현대 p18 1,526자 / 시흥 장현지구 p52 1,673자,
둘 다 사람 대조 USABLE). 등기부 표는 같은 날짜·등기원인·문구 반복이 **정상**이라 lexical
diversity 가 원래 낮다. 낮은 TTR 은 "루프일 수도 있음"이라는 **약한 feature** 이지
"삭제해도 됨"이라는 근거가 아니다.

  검색 recall 이 목표라면 **false quarantine 보다 silent deletion 이 더 나쁘다.**

그래서 규칙을 두 등급으로 나눈다.

  HARD (삭제/격리 근거)   표 R1(압축비) · R2(지배 셀값) · 텍스트 T1/T2/T3
  SOFT (관측만, 삭제 금지) 표 R3(산재반복) · R4(단어 다양성)

근거(regression set 9/9, 2026-08-11 실측): **R3·R4 가 단독으로 잡는 TP 가 하나도 없다** —
R3/R4 를 넣게 만든 픽스처(`SCATTER`/`LOW_TTR`)조차 R1 이 잡는다. 반면 R4 는 실측 FP 2건의
**단독 원인**이다. 텍스트 규칙은 60p 에서 TP 5 / observed FP 0 이라 손대지 않는다.

env:
  KBP_DEGEN_COMPRESS_MAX  표 R1 임계(기본 0.16). **호출 시점에 읽는다** — 모듈 상수로 두면
                          import 시 1회 고정돼 monkeypatch 도 폐쇄망 재기동도 안 먹는다.
                          텍스트 T1 은 `_COMPRESS_MAX` 를 그대로 쓴다(무변경 원칙).
  KBP_DEGEN_SOFT_RULES    SOFT 로 강등할 규칙(기본 "R3,R4"). 값이 **"none"** 이면 전 규칙
                          HARD = 구동작 복원(되돌림 손잡이). 빈 문자열은 in-process 전용 —
                          compose 의 `${VAR:-default}` 가 빈 값에도 기본값을 대입하므로
                          폐쇄망에서는 센티널로 쓸 수 없다.

불변식(회귀 방지 — 어기면 지금 살아남는 블록이 새로 삭제된다):
  A. `_TABLE_MIN_CELLS=20` 게이트는 R2/R3/R4 에 그대로 유지한다(R1 만 셀 수 무관).
  B. R1 은 `len(joined) >= _MIN_LEN(200)` 일 때만 comp 를 계산하고 그 외 **comp=1.0 고정**.
     이 값이 R3(`comp < 0.36`)의 입력이다.
"""
from __future__ import annotations

import logging
import os
import re
import zlib
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum

log = logging.getLogger("kb_pipeline.parse_service.parsers.degen_filter")

_MIN_LEN = 200            # 이보다 짧으면 판정 보류
_COMPRESS_MAX = 0.16      # 압축비 이하 = 퇴화 (정상 한국어는 0.4+)
_NGRAM = 5                # 지배 구절 단위(단어)
_NGRAM_MIN_COUNT = 8      # 최소 반복 횟수
_NGRAM_MIN_COVER = 0.35   # 지배 구절이 전체 단어의 이 비율 이상 차지


_SHORT_MIN = 60           # 짧은 루프 판정 하한(이 미만은 완전 보류)
_SHORT_G3_COVER = 0.5     # 60-200자: 3-gram 지배 점유율(실관측 퇴화 0.71 vs 정상 최대 0.21)
_SHORT_TTR_MAX = 0.45     # 60-200자: 단어 다양성 하한(실관측 퇴화 0.33 vs 정상 최저 0.73)

_CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

_TABLE_MIN_CELLS = 20        # 이보다 작은 표는 판정 보류(정상 소형 표 오검 방지)
_TABLE_DOMINANT_RATIO = 0.6  # 동일 셀 값이 전체(유의미 셀)의 이 비율 이상 = 퇴화
_TABLE_CELL_MIN_LEN = 2      # 'O'/'X'/숫자 등 1글자 체크셀은 정당한 반복 → 분모·분자에서 제외

_DEFAULT_SOFT_RULES = "R3,R4"


class Severity(str, Enum):
    NONE = "none"
    SOFT = "soft"    # 애매 — 관측만 한다. **절대 삭제하지 않는다.**
    HARD = "hard"    # 확실 — 삭제/격리 근거


@dataclass(frozen=True)
class BlockAssessment:
    """블록 1개의 판정 근거. 무엇이 왜 몇 자였는지 전부 보존한다."""
    index: int
    kind: str                                   # "text" | "table"
    severity: Severity
    rules: tuple[str, ...] = ()                 # ("R1","R4") — 걸린 규칙 전부
    chars: int = 0                              # 원래 몇 자였는지(태그·공백 제외)
    stats: dict = field(default_factory=dict)   # comp / dom / ttr / top5 실측값

    @property
    def is_hard(self) -> bool:
        return self.severity is Severity.HARD


@dataclass(frozen=True)
class PageAssessment:
    page_number: object
    blocks: tuple[BlockAssessment, ...] = ()
    chars_before: int = 0
    chars_hard: int = 0
    chars_soft: int = 0

    @property
    def hard_blocks(self) -> tuple[BlockAssessment, ...]:
        return tuple(b for b in self.blocks if b.is_hard)

    @property
    def soft_blocks(self) -> tuple[BlockAssessment, ...]:
        return tuple(b for b in self.blocks if b.severity is Severity.SOFT)


def _table_compress_max() -> float:
    """표 R1 임계 — **호출 시점에** env 를 읽는다(모듈 상수 고정 금지)."""
    raw = os.environ.get("KBP_DEGEN_COMPRESS_MAX")
    if not raw:
        return _COMPRESS_MAX
    try:
        return float(raw)
    except ValueError:
        log.warning("KBP_DEGEN_COMPRESS_MAX=%r 파싱 실패 — 기본값 %s 사용", raw, _COMPRESS_MAX)
        return _COMPRESS_MAX


def _soft_rules() -> frozenset[str]:
    """SOFT 로 강등할 규칙 집합.

    `none` **만** 센티널이다(= 공집합 = 전 규칙 HARD = 구동작 복원).
    미설정과 **빈 값은 기본값(R3,R4)** 으로 본다 — 두 이유로 그래야 한다.
      ① compose 의 `${VAR:-R3,R4}` 가 빈 값에도 기본값을 대입하므로, in-process 만
         빈 값을 "전 규칙 HARD" 로 읽으면 호스트와 컨테이너 동작이 갈린다(실측 확인).
      ② 빈 값에 "삭제가 늘어나는 쪽"을 배정하면 실수로 비웠을 때 안전화가 조용히 풀린다.
         애매하면 보존이 원칙이다.
    """
    raw = (os.environ.get("KBP_DEGEN_SOFT_RULES") or "").strip()
    if not raw:
        raw = _DEFAULT_SOFT_RULES
    if raw.lower() == "none":
        return frozenset()
    return frozenset(r.strip().upper() for r in raw.split(",") if r.strip())


def _severity_for(rules: tuple[str, ...]) -> Severity:
    """규칙 하나라도 HARD 면 HARD. 전부 SOFT 목록에 있으면 SOFT."""
    if not rules:
        return Severity.NONE
    soft = _soft_rules()
    return Severity.SOFT if all(r in soft for r in rules) else Severity.HARD


def visible_chars(text: str) -> int:
    """태그·공백을 뺀 문자 수. `chars` 의 단일 정의 — 구현자가 고르면 안 된다.

    마크업 포함으로 세면 화성 p86 의 잔존율이 0.177 대신 0.116 이 되어 gate 임계가
    조용히 달라진다(v3 이 "17,425자 보존"으로 5.4배 과장했던 것도 같은 원인).
    """
    if not text:
        return 0
    return len(_WS_RE.sub("", _TAG_RE.sub("", text)))


def _block_body(block: dict) -> tuple[str, str]:
    """(kind, 판정 대상 문자열)."""
    if (block or {}).get("type") == "table":
        return "table", (block.get("table_body") or "")
    return "text", ((block or {}).get("text") or "")


# ─────────────────────────────────────────────────────────── 규칙 판정(순수)

def assess_text_rules(text: str) -> tuple[tuple[str, ...], dict]:
    """텍스트 블록에서 발화한 규칙과 실측 통계. 삭제하지 않는다."""
    stats: dict = {}
    if not text or len(text) < _SHORT_MIN:
        return (), stats
    words = text.split()

    # T3: 짧은 텍스트(60-200자) — 강한 신호만(3-gram 지배 or 극저 다양성).
    if len(text) < _MIN_LEN:
        n = len(words)
        if n >= 9:
            g3 = Counter(tuple(words[i:i + 3]) for i in range(n - 2))
            top3 = g3.most_common(1)[0][1] * 3 / n
            ttr = len(set(words)) / n
            stats.update(top3=round(top3, 4), ttr=round(ttr, 4), short=True)
            if top3 >= _SHORT_G3_COVER or ttr <= _SHORT_TTR_MAX:
                return ("T3",), stats
        return (), stats

    hits: list[str] = []
    raw = text.encode("utf-8")
    comp = len(zlib.compress(raw, 6)) / len(raw)
    stats["comp"] = round(comp, 4)
    if comp < _COMPRESS_MAX:            # T1 — 텍스트 임계는 env 화하지 않는다(무변경 원칙)
        hits.append("T1")

    if len(words) >= _NGRAM * 6:        # T2 — 지배 구절(단어 5-gram) 과다 반복
        grams = Counter(tuple(words[i:i + _NGRAM]) for i in range(len(words) - _NGRAM + 1))
        top_count = grams.most_common(1)[0][1]
        cover = (top_count * _NGRAM) / len(words)
        stats.update(top5_count=top_count, top5_cover=round(cover, 4))
        if top_count >= _NGRAM_MIN_COUNT and cover >= _NGRAM_MIN_COVER:
            hits.append("T2")

    return tuple(hits), stats


def assess_table_rules(table_body: str) -> tuple[tuple[str, ...], dict]:
    """표 블록에서 발화한 규칙과 실측 통계. 삭제하지 않는다.

    R1 셀 연결 텍스트 압축비 < KBP_DEGEN_COMPRESS_MAX (셀 수 무관)
    R2 지배 셀값 ≥ 0.6                      (유의미셀 20+)
    R3 dom ≥ 0.35 AND comp < 0.36           (유의미셀 20+)
    R4 단어 다양성 ttr ≤ 0.45                (유의미셀 20+, 단어 30+)
    """
    stats: dict = {}
    if not table_body:
        return (), stats
    cells = [_TAG_RE.sub("", c).strip() for c in _CELL_RE.findall(table_body)]
    joined = " ".join(c for c in cells if c)
    stats.update(cells=len(cells), joined=len(joined))

    hits: list[str] = []
    # 불변식 B — joined 가 짧으면 comp 를 계산하지 않고 1.0 으로 고정한다(R3 의 입력).
    if len(joined) >= _MIN_LEN:
        raw = joined.encode("utf-8")
        comp = len(zlib.compress(raw, 6)) / len(raw)
        if comp < _table_compress_max():
            hits.append("R1")
    else:
        comp = 1.0
    stats["comp"] = round(comp, 4)

    meaningful = [c for c in cells if len(c) >= _TABLE_CELL_MIN_LEN]
    stats["meaningful"] = len(meaningful)
    # 불변식 A — 소형 표에서는 R2/R3/R4 를 평가하지 않는다(정상 소형 반복표 보호).
    if len(meaningful) < _TABLE_MIN_CELLS:
        return tuple(hits), stats

    dom = Counter(meaningful).most_common(1)[0][1] / len(meaningful)
    stats["dom"] = round(dom, 4)
    if dom >= _TABLE_DOMINANT_RATIO:
        hits.append("R2")
    if dom >= 0.35 and comp < 0.36:
        hits.append("R3")

    words = joined.split()
    if len(words) >= 30:
        ttr = len(set(words)) / len(words)
        stats.update(words=len(words), ttr=round(ttr, 4))
        if ttr <= 0.45:
            hits.append("R4")

    return tuple(hits), stats


# ─────────────────────────────────────────────── 하위호환 진입점(시그니처 불변)

def is_degenerate_text(text: str) -> bool:
    """퇴화 텍스트인지. 확신 없으면 False(오검 방지 우선)."""
    return bool(assess_text_rules(text)[0])


def is_degenerate_table(table_body: str) -> bool:
    """퇴화 표인지(SOFT 규칙 포함 — 판정 자체는 그대로다).

    **삭제 여부는 이걸로 정하지 않는다** — `filter_degenerate_pages` 는 HARD 만 지운다.
    """
    return bool(assess_table_rules(table_body)[0])


# ────────────────────────────────────────────────── detect → decide → mutate

def assess_page(page: dict) -> PageAssessment:
    """**순수 함수 — `page` 를 변경하지 않는다.** 판정 근거만 만든다."""
    blocks = (page or {}).get("blocks") or []
    out: list[BlockAssessment] = []
    total = hard = soft = 0
    for i, b in enumerate(blocks):
        kind, body = _block_body(b)
        rules, stats = (assess_table_rules(body) if kind == "table"
                        else assess_text_rules(body))
        n = visible_chars(body)
        sev = _severity_for(rules)
        total += n
        if sev is Severity.HARD:
            hard += n
        elif sev is Severity.SOFT:
            soft += n
        out.append(BlockAssessment(index=i, kind=kind, severity=sev,
                                   rules=rules, chars=n, stats=stats))
    return PageAssessment(page_number=(page or {}).get("page_number"),
                          blocks=tuple(out), chars_before=total,
                          chars_hard=hard, chars_soft=soft)


def apply_assessment(page: dict, assessment: PageAssessment) -> int:
    """**HARD 만** 제거(제자리). 제거 수 반환. SOFT 는 보존하고 관측 로그만 남긴다."""
    blocks = (page or {}).get("blocks") or []
    drop = {b.index for b in assessment.hard_blocks}
    for b in assessment.soft_blocks:
        log.info("degen SOFT 관측(보존) page=%s idx=%d kind=%s rules=%s chars=%d %s",
                 assessment.page_number, b.index, b.kind, ",".join(b.rules), b.chars, b.stats)
    for b in assessment.hard_blocks:
        log.warning("degenerate block removed page=%s idx=%d kind=%s rules=%s chars=%d %s",
                    assessment.page_number, b.index, b.kind, ",".join(b.rules), b.chars, b.stats)
    if drop:
        page["blocks"] = [b for i, b in enumerate(blocks) if i not in drop]
    return len(drop)


def filter_degenerate_pages(pages: list) -> int:
    """pages[{page_number, blocks}] 에서 **HARD 퇴화 블록만** 제거(제자리). 제거 수 반환.

    시그니처·반환형은 이전과 같다. 동작 변화는 **"SOFT 만 걸린 블록을 더 이상 삭제하지
    않는다"** 뿐이다(호출부: pdf/__init__.py, ocr/__init__.py — 무변경).
    """
    removed = 0
    for page in pages or []:
        removed += apply_assessment(page, assess_page(page))
    return removed
