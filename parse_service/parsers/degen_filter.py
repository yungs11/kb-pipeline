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


_SHORT_MIN = 60           # 짧은 루프 판정 하한(이 미만은 완전 보류)
_SHORT_G3_COVER = 0.5     # 60-200자: 3-gram 지배 점유율(실관측 퇴화 0.71 vs 정상 최대 0.21)
_SHORT_TTR_MAX = 0.45     # 60-200자: 단어 다양성 하한(실관측 퇴화 0.33 vs 정상 최저 0.73)


def is_degenerate_text(text: str) -> bool:
    """VL 퇴화 반복 텍스트인지 판정. 확신 없으면 False(오검 방지 우선)."""
    if not text or len(text) < _SHORT_MIN:
        return False
    words = text.split()

    # 짧은 텍스트(60-200자): 강한 신호만 — 3-gram 지배 or 극저 다양성.
    # 실관측(2026-07-15): '완성의 협력을 위한'×5(79자, top3=0.71, ttr=0.33)가 <200 보류로 통과했었음.
    if len(text) < _MIN_LEN:
        n = len(words)
        if n >= 9:
            g3 = Counter(tuple(words[i:i + 3]) for i in range(n - 2))
            top3 = g3.most_common(1)[0][1] * 3 / n
            ttr = len(set(words)) / n
            if top3 >= _SHORT_G3_COVER or ttr <= _SHORT_TTR_MAX:
                return True
        return False

    # ① 압축률 — 반복 루프는 극단적으로 잘 압축됨.
    raw = text.encode("utf-8")
    ratio = len(zlib.compress(raw, 6)) / len(raw)
    if ratio < _COMPRESS_MAX:
        return True

    # ② 지배 구절(단어 5-gram) 과다 반복.
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
    """표 퇴화 판정 — 실관측(2026-07-15 소유권 양식페이지) 임계 보정 v3.

    실측 분포: 정상표 dom≤0.17·comp≥0.39 / 퇴화표 ① 거대반복셀형 comp=0.03(셀2개,
    '손을'×수십 2490자) ② 산재반복형 dom=0.43·comp=0.34('송개왕' 60셀). 세 규칙:
      R1 셀 연결 텍스트 압축비 < 0.16 (텍스트와 동일 — 거대반복셀형; 셀 수 무관)
      R2 지배 셀값 ≥ 0.6 (동일 값이 표를 지배; 20셀+)
      R3 dom ≥ 0.35 AND comp < 0.36 (산재반복형; 정상 최악 0.17/0.39 와 마진)
    체크리스트(O/X)·숫자(2글자 미만 셀)는 dom 계산에서 제외(정당한 반복 오검 방지).
    """
    global _CELL_RE
    if not table_body:
        return False
    if _CELL_RE is None:
        import re
        _CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)
    import re as _re
    cells = [_re.sub(r"<[^>]+>", "", c).strip() for c in _CELL_RE.findall(table_body)]
    joined = " ".join(c for c in cells if c)

    # R1: 셀 연결 텍스트 압축비 — 거대 반복셀(예: '손을'×수십)은 셀 수와 무관하게 잡힘.
    if len(joined) >= _MIN_LEN:
        raw = joined.encode("utf-8")
        comp = len(zlib.compress(raw, 6)) / len(raw)
        if comp < _COMPRESS_MAX:
            return True
    else:
        comp = 1.0

    meaningful = [c for c in cells if len(c) >= _TABLE_CELL_MIN_LEN]
    if len(meaningful) < _TABLE_MIN_CELLS:
        return False
    dom = Counter(meaningful).most_common(1)[0][1] / len(meaningful)
    # R2: 지배 셀값
    if dom >= _TABLE_DOMINANT_RATIO:
        return True
    # R3: 산재 반복형 — 중간 dom 이지만 압축비도 낮음(정상표는 dom≤0.17 or comp≥0.39)
    if dom >= 0.35 and comp < 0.36:
        return True
    # R4: 단어 다양성 — rowspan 병합 등으로 dom 이 낮아도 같은 단어 변주가 표를 채움.
    #     실관측(2026-07-15 2차): 퇴화표 ttr=0.35 vs 정상표 최저 0.69.
    words = joined.split()
    if len(words) >= 30:
        ttr = len(set(words)) / len(words)
        if ttr <= 0.45:
            return True
    return False


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
