"""공유 텍스트/좌표 유틸리티.

모든 모듈이 동일한 정규화·마커·계층·키워드 규칙을 쓰도록 이 모듈에 모은다.
(SoT §8 cell feature, §13.3 항목 번호 패턴, §14.3 marker normalization, §20 keywords)
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any, List, Optional

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


def cell_addr(row: int, col: int) -> str:
    return f"{get_column_letter(col)}{row}"


def range_a1(min_row: int, min_col: int, max_row: int, max_col: int) -> str:
    if (min_row, min_col) == (max_row, max_col):
        return cell_addr(min_row, min_col)
    return f"{cell_addr(min_row, min_col)}:{cell_addr(max_row, max_col)}"
