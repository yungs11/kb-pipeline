"""VL 퇴화(무한반복) 블록 필터 — 통계 신호로 검출·제거.

VLM(qwen/PaddleOCR-VL/게이트웨이)이 어려운 페이지(밀집 양식/저품질 스캔)에서 같은 구절을
무한 반복 생성하는 퇴화(degenerate loop)가 관측됨(2026-07-15, 소유권 문서 양식 페이지:
"기계음 손상완을 잡고"×수십 등). LLM 없이 두 가지 싼 신호로 검출:

  ① 지배 구절 반복 — 단어 5-gram 중 최빈 구절이 과다 반복 + 텍스트 대부분을 차지
  ② 압축률 — zlib 압축비. 정상 한국어 ~0.4-0.6, 반복 루프 <0.15

짧은 텍스트(<200자)는 판정 보류(정상 반복과 구분 불가 — 오검 방지 우선).
v1 은 text 계열 블록만 검사(표는 셀 값 반복이 정당할 수 있어 제외).
임계값은 실관측 퇴화 3종 + 정상 한국어 2종 픽스처로 고정(test_degen_filter).
"""
from __future__ import annotations

import logging
import zlib
from collections import Counter

log = logging.getLogger("kb_pipeline.parse_service.parsers.degen_filter")

_MIN_LEN = 200            # 이보다 짧으면 판정 보류
_COMPRESS_MAX = 0.16      # 압축비 이하 = 퇴화 (정상 한국어는 0.4+)
_NGRAM = 5                # 지배 구절 단위(단어)
_NGRAM_MIN_COUNT = 8      # 최소 반복 횟수
_NGRAM_MIN_COVER = 0.35   # 지배 구절이 전체 단어의 이 비율 이상 차지


def is_degenerate_text(text: str) -> bool:
    """VL 퇴화 반복 텍스트인지 판정. 확신 없으면 False(오검 방지 우선)."""
    if not text or len(text) < _MIN_LEN:
        return False

    # ① 압축률 — 반복 루프는 극단적으로 잘 압축됨.
    raw = text.encode("utf-8")
    ratio = len(zlib.compress(raw, 6)) / len(raw)
    if ratio < _COMPRESS_MAX:
        return True

    # ② 지배 구절(단어 5-gram) 과다 반복.
    words = text.split()
    if len(words) >= _NGRAM * 6:
        grams = Counter(tuple(words[i:i + _NGRAM]) for i in range(len(words) - _NGRAM + 1))
        top_count = grams.most_common(1)[0][1]
        if top_count >= _NGRAM_MIN_COUNT and (top_count * _NGRAM) / len(words) >= _NGRAM_MIN_COVER:
            return True

    return False


_CELL_RE = None  # lazy compile

_TABLE_MIN_CELLS = 20        # 이보다 작은 표는 판정 보류(정상 소형 표 오검 방지)
_TABLE_DOMINANT_RATIO = 0.6  # 동일 셀 값이 전체(유의미 셀)의 이 비율 이상 = 퇴화
_TABLE_CELL_MIN_LEN = 2      # 'O'/'X'/숫자 등 1글자 체크셀은 정당한 반복 → 분모·분자에서 제외


def is_degenerate_table(table_body: str) -> bool:
    """표 퇴화 판정 — 동일 셀 값이 표를 지배(예: '송개왕' ×45)하면 VL 퇴화로 본다.

    체크리스트(O/X)·숫자 반복은 정당하므로 2글자 미만 셀은 제외하고 계산. 소형 표 보류.
    """
    global _CELL_RE
    if not table_body:
        return False
    if _CELL_RE is None:
        import re
        _CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)
    import re as _re
    cells = [_re.sub(r"<[^>]+>", "", c).strip() for c in _CELL_RE.findall(table_body)]
    meaningful = [c for c in cells if len(c) >= _TABLE_CELL_MIN_LEN]
    if len(meaningful) < _TABLE_MIN_CELLS:
        return False
    top = Counter(meaningful).most_common(1)[0][1]
    return (top / len(meaningful)) >= _TABLE_DOMINANT_RATIO


def filter_degenerate_pages(pages: list) -> int:
    """pages[{page_number, blocks}] 에서 퇴화 블록 제거(제자리). 제거 수 반환.

    text 계열 = is_degenerate_text(반복/압축비), table = is_degenerate_table(지배 셀 값).
    """
    removed = 0
    for page in pages or []:
        blocks = page.get("blocks") or []
        kept = []
        for b in blocks:
            if b.get("type") == "table":
                if is_degenerate_table(b.get("table_body") or ""):
                    removed += 1
                    log.warning("degenerate VL table removed (page %s): %r...",
                                page.get("page_number"), (b.get("table_body") or "")[:60])
                    continue
                kept.append(b)
                continue
            if is_degenerate_text(b.get("text") or ""):
                removed += 1
                log.warning("degenerate VL block removed (page %s, %d chars): %r...",
                            page.get("page_number"), len(b.get("text") or ""),
                            (b.get("text") or "")[:60])
                continue
            kept.append(b)
        page["blocks"] = kept
    return removed
