"""셀 타입을 아는 호출부용 marker 판정 보조 (SoT §8.1).

SoT §8.1 의 marker 후보에 숫자 0 은 없다 — 'O' 문자만 영문자/숫자 혼동으로
confidence 감점 대상이다. textutil 의 is_marker_value/normalize_marker 는
텍스트만 보므로 숫자 0 (int/float 셀 또는 "0" 같은 수치 문자열)을
applicable marker 로 오인할 수 있다. 셀 객체/원문을 아는 호출부는
이 모듈의 *_cell guard 를 거쳐 수치 값을 marker 에서 제외한다.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from .textutil import compact, is_ambiguous_marker, is_marker_value, normalize_marker

_NUMERIC_TEXT_RE = re.compile(r"^-?[\d,]+(?:\.\d+)?%?$")
_NUMERIC_TYPES = ("int", "float")


def is_numeric_value(cell: Any, text: Any = None) -> bool:
    """셀 data_type 이 수치이거나 텍스트가 수치 패턴이면 True."""
    if cell is not None and getattr(cell, "data_type", None) in _NUMERIC_TYPES:
        return True
    t = compact(text)
    return bool(t) and bool(_NUMERIC_TEXT_RE.fullmatch(t))


def is_marker_cell(cell: Any, text: Any) -> bool:
    """수치 셀(숫자 0 포함)은 marker 가 아니다 (SoT §8.1)."""
    if is_numeric_value(cell, text):
        return False
    return is_marker_value(text)


def normalize_marker_cell(cell: Any, text: Any) -> Optional[str]:
    if is_numeric_value(cell, text):
        return None
    return normalize_marker(text)


def is_ambiguous_marker_cell(cell: Any, text: Any) -> bool:
    if is_numeric_value(cell, text):
        return False
    return is_ambiguous_marker(text)
