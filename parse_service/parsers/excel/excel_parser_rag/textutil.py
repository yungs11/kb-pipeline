"""공유 텍스트/좌표 유틸리티.

모든 모듈이 동일한 정규화·마커·계층·키워드 규칙을 쓰도록 이 모듈에 모은다.
(SoT §8 cell feature, §13.3 항목 번호 패턴, §14.3 marker normalization, §20 keywords)
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any, List, Optional, Sequence, Tuple

from openpyxl.utils import get_column_letter

PARSER_VERSION = "excel-parser-rag-v1"

# --- SoT §14.3 marker normalization -----------------------------------------
MARKER_NORMALIZATION = {
    "○": "applicable",
    "◯": "applicable",
    "●": "applicable_primary",
    "◎": "applicable_special",
    "△": "conditional",
    "▲": "conditional",
    "×": "not_applicable",
    "X": "not_applicable",
    "✕": "not_applicable",
    "Y": "yes",
    "N": "no",
    "✓": "checked",
    "✔": "checked",
    "해당": "applicable",
    "대상": "applicable",
    "필수": "required",
    "선택": "optional",
}
# 'O'/'o'는 영문자/숫자 0과 혼동 가능 → marker로는 인정하되 confidence 감점 대상
AMBIGUOUS_MARKERS = {"O", "o", "0"}

MARKER_LABELS_KO = {
    "applicable": "해당",
    "applicable_primary": "주관",
    "applicable_special": "특별 해당",
    "conditional": "조건부 해당",
    "not_applicable": "비해당",
    "yes": "예",
    "no": "아니오",
    "checked": "체크됨",
    "required": "필수",
    "optional": "선택",
}

# --- SoT §8.2 note 판단 -------------------------------------------------------
NOTE_PREFIXES = ("※", "주)", "주:", "주.", "비고", "참고", "단,", "단서", "* ", "(단,", "(※")
NOTE_MARK_RE = re.compile(r"^[①-⑮]\s*")

# --- SoT §12.2 total row -----------------------------------------------------
TOTAL_TERMS = {"합계", "소계", "총계", "계", "total", "subtotal"}

# --- SoT §13.3 항목 번호 패턴 --------------------------------------------------
_LEVEL_PATTERNS: List[tuple] = [
    (0, re.compile(r"^(제\s*\d+\s*[장조절]|[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫ]+\s*\.|I{1,3}V?X?\s*\.|\d+\s*\.(?!\d))")),
    (1, re.compile(r"^([가나다라마바사아자차카타파하]\s*\.|[A-Z]\s*\.)")),
    (2, re.compile(r"^\(\s*\d+\s*\)")),
    (3, re.compile(r"^\(\s*[가나다라마바사아자차카타파하a-zA-Z]\s*\)")),
    (4, re.compile(r"^[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮]")),
    (5, re.compile(r"^[-·•▪]\s+")),
]


def clean_text(value: Any) -> str:
    """셀 값을 검색/비교 가능한 문자열로 정규화한다 (개행 보존)."""
    if value is None:
        return ""
    text = str(value)
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return text.strip()


def one_line(value: Any) -> str:
    return re.sub(r"\s+", " ", clean_text(value)).strip()


def compact(value: Any) -> str:
    """공백 제거 비교용."""
    return re.sub(r"\s+", "", one_line(value))


def build_content_frame(*segments: Any) -> str:
    """콘텐츠의 구조 경로를 정규화해 `` > ``로 조립한다.

    세그먼트는 호출자가 구조 순서대로 넘긴다. 빈 값은 버리고, NFKC/한 줄
    정규화 후 공백을 제거한 비교값이 같은 세그먼트는 첫 항목만 보존한다.
    """
    out: List[str] = []
    seen = set()
    for value in segments:
        segment = one_line(value)
        if not segment:
            continue
        key = compact(segment)
        if key in seen:
            continue
        seen.add(key)
        out.append(segment)
    return " > ".join(out)


def mark_strikethrough(value: Any, struck: bool) -> str:
    """취소선 스타일을 검색 가능한 명시적 텍스트 표기로 보존한다."""
    text = str(value or "")
    if not struck or not text or text.rstrip().endswith("(취소선)"):
        return text
    return f"{text} (취소선)"


def _seg_text(s: Any) -> str:
    """세그먼트 비교용 — (label, text) 튜플이면 원문 text 만 추출."""
    return s[1] if isinstance(s, tuple) else str(s)


def _seg_render(s: Any) -> str:
    """세그먼트 렌더용 — (label, text) 튜플이면 'label: text'(라벨 없으면 text)."""
    if isinstance(s, tuple):
        label, text = s
        return f"{label}: {text}" if label else str(text)
    return str(s)


def row_content(doc: str, sheet: str, path: List[Any], pairs: List[Tuple[str, Any]], title: str = "") -> str:
    """행 청크 content 공통 포맷: '{문서} > {시트} > {path} 항목: k: v, k: v'.

    조사 없는 콜론 템플릿(KB header:값 원칙). 원본 위치는 포함하지 않는다.
    - path 빈 행은 리전 제목(title)으로 fallback(정보 손실 방지).
    - 인접 중복 세그먼트 제거 + doc 와 path 선두가 같은 이중표기 제거.
    - path 세그먼트는 str 또는 (label, text) 튜플 혼합 가능(계층열 헤더라벨 렌더링).
      비교(필터·dedup·이중표기)는 _seg_text(원문 text), 최종 join 은 _seg_render(라벨 포함).
    """
    p = list(path) if path else ([title] if title else [])
    # ① 빈값 필터: 튜플이면 text 부분으로 truthy 판정
    segs = [s for s in ([doc, sheet] + p) if _seg_text(s)]
    # ② 인접 중복 제거: 원문 text 기준
    dedup = [s for i, s in enumerate(segs) if i == 0 or _seg_text(s) != _seg_text(segs[i - 1])]
    # doc 와 path 선두가 동일한 이중표기 제거 — 관계 기반(text 기준 비교)
    if doc and len(dedup) >= 3 and _seg_text(dedup[2]) == doc and p and _seg_text(p[0]) == doc:
        del dedup[2]
    kv = ", ".join(f"{k}: {v}" for k, v in pairs)
    # ③ 최종 join: 라벨 렌더링 적용(튜플이 직접 닿으면 TypeError 방지)
    body = " > ".join(_seg_render(s) for s in dedup)
    return f"{body} 항목: {kv}" if kv else f"{body} 항목"


def is_marker_value(value: Any) -> bool:
    text = compact(value)
    if not text:
        return False
    if text in MARKER_NORMALIZATION or text in AMBIGUOUS_MARKERS:
        return True
    return bool(re.fullmatch(r"[○●◎◯△▲×X✕✓✔]+", text))


def normalize_marker(value: Any) -> Optional[str]:
    """marker 값 → 정규화 토큰. marker가 아니면 None."""
    text = compact(value)
    if not text:
        return None
    if text in MARKER_NORMALIZATION:
        return MARKER_NORMALIZATION[text]
    if text in AMBIGUOUS_MARKERS:
        return "applicable"
    if re.fullmatch(r"[○◯]+", text):
        return "applicable"
    if re.fullmatch(r"[●]+", text):
        return "applicable_primary"
    return None


def is_ambiguous_marker(value: Any) -> bool:
    return compact(value) in AMBIGUOUS_MARKERS


def marker_label_ko(normalized: str) -> str:
    return MARKER_LABELS_KO.get(normalized, normalized)


def is_note_text(value: Any) -> bool:
    t = one_line(value)
    if not t:
        return False
    return any(t.startswith(p) for p in NOTE_PREFIXES)


def is_total_text(value: Any) -> bool:
    t = compact(value).lower()
    return t in TOTAL_TERMS


def infer_numbering_level(value: Any) -> Optional[int]:
    """항목 번호 패턴으로 계층 레벨 추정. 패턴이 없으면 None."""
    t = one_line(value)
    if not t:
        return None
    for level, pattern in _LEVEL_PATTERNS:
        if pattern.match(t):
            return level
    return None


# --- dotted-int 계층 spine (WBSID 1/1.1/1.1.1 류) ------------------------------
# 열-단위 게이팅으로 소수/IP/순번/버전 모호성 해소 (kordoc CGH detect_spine 동형 원리).
_SPINE_VALUE_RE = re.compile(r"^\d+(?:\.\d+)*$")   # bare dotted-int (1 / 1.1 / 1.1.10)
_SPINE_ROLLUP_RE = re.compile(r"^(ALL|전체|합계|소계|계)$", re.IGNORECASE)


def is_spine_column(
    values: Sequence[str], *, min_items: int = 5, min_ratio: float = 0.6, min_depth: int = 3
) -> bool:
    """열 값 리스트가 dotted-int 계층 spine 인가 — 열-단위 게이팅.

    rollup(ALL/전체/합계…) 제외 후 ≥60% 가 bare dotted-int, 깊이(세그 수) 2종 이상
    **그리고 최대 깊이 ≥3**(버전열 0.1/0.2/…/1.1 이 depth<=2 로 오탐되는 실측 FP 를 배제 —
    2-깊이 전용 spine 문서는 의도적 미탐 = kordoc 유지, 정밀 우선).
    (IP: 깊이 균일 → False. 순번 1,2,3: 깊이 1 → False. 소수/버전: 깊이<=2 → False.
     날짜 2026.05.04: 깊이 {3} 균일 1종 → len(depths)>=2 절이 배제.)
    """
    core = [v for v in values if v and not _SPINE_ROLLUP_RE.match(v.strip())]
    if len(core) < min_items:
        return False
    hits = [v.strip() for v in core if _SPINE_VALUE_RE.match(v.strip())]
    if len(hits) / len(core) < min_ratio:
        return False
    depths = {h.count(".") + 1 for h in hits}
    return max(depths) >= min_depth and len(depths) >= 2


def spine_depth(text: Any) -> Optional[int]:
    """bare dotted-int 이면 세그먼트 수(1.1.1→3), 아니면 None (트래커/보조용)."""
    t = one_line(text)
    if not t or not _SPINE_VALUE_RE.match(t):
        return None
    return t.count(".") + 1


def looks_like_code(value: Any) -> bool:
    """A01, HR, IT 같은 코드/약어 패턴 (SoT §8.3)."""
    t = one_line(value)
    if not t or len(t) > 12 or " " in t:
        return False
    return bool(re.fullmatch(r"[A-Z]{1,5}\d{0,4}|[가-힣]{1,4}|[A-Za-z가-힣]{1,6}", t))


GENERIC_STOPWORDS = {
    "있다", "한다", "이다", "및", "등", "관련", "기준", "사항", "경우", "이상", "이하",
    "the", "and", "of", "for",
}


def split_keywords(text: Any, limit: int = 40) -> List[str]:
    """BM25용 키워드 추출 (SoT §20). 형태소 분석기 없이 보수적으로 동작."""
    t = one_line(text)
    tokens = re.split(r"[\s,;/·>\(\)\[\]\{\}:\"'`~|]+", t)
    out: List[str] = []
    seen = set()
    for token in tokens:
        token = token.strip(" .-–—_①②③④⑤⑥⑦⑧⑨⑩※*")
        if len(token) < 2 or token in seen or token.lower() in GENERIC_STOPWORDS:
            continue
        if re.fullmatch(r"[\d.,%]+", token) and len(token) < 2:
            continue
        seen.add(token)
        out.append(token)
    return out[:limit]


def stable_id(*parts: Any) -> str:
    raw = "::".join(one_line(p) for p in parts)
    digest = hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]
    prefix = re.sub(r"[^0-9A-Za-z가-힣_.-]+", "_", one_line(parts[0]))[:48]
    return f"{prefix}::{digest}"


# --- 퍼센트 서식 표시값 복원 (kordoc + openpyxl 경로 공용) --------------------
def _pct_decimals(fmt: Any) -> int:
    """퍼센트 서식 문자열의 소수부 자릿수: '0.0%'→1, '0.00%'→2, '0%'→0."""
    m = re.search(r"0\.(0+)%", str(fmt or ""))
    return len(m.group(1)) if m else 0


def _is_pct_format(fmt: Any) -> bool:
    """number_format 에 퍼센트 연산자(%)가 있으면 True.

    인용 리터럴("..%..")과 이스케이프(\\%)의 % 는 값을 100배 하지 않는 표시문자이므로
    제거 후 남은 % 만 서식 연산자로 본다 (예: '0.0"%"', '0\\%' 는 퍼센트 서식 아님).
    """
    if not fmt:
        return False
    cleaned = re.sub(r'"[^"]*"', "", str(fmt))   # 인용 리터럴 제거
    cleaned = re.sub(r"\\.", "", cleaned)          # 이스케이프 문자 제거
    return "%" in cleaned


def cell_addr(row: int, col: int) -> str:
    return f"{get_column_letter(col)}{row}"


def range_a1(min_row: int, min_col: int, max_row: int, max_col: int) -> str:
    if (min_row, min_col) == (max_row, max_col):
        return cell_addr(min_row, min_col)
    return f"{cell_addr(min_row, min_col)}:{cell_addr(max_row, max_col)}"
