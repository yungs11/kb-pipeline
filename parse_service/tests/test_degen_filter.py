"""VL 퇴화(무한반복/헛소리) 블록 필터 — 실제 관측 샘플(2026-07-15) 기반."""
from parse_service.parsers.degen_filter import is_degenerate_text, filter_degenerate_pages

# 실제 파싱에서 관측된 퇴화 출력(사용자 보고, 소유권 문서 밀집 양식 페이지)
DEGEN_LOOP = ("공공기관에서의 기계음 손상완돼 침대완성을 옮겨 공공기관에서 메모 가져줄게 옮겨 "
              "침대가 잘 잡고 새로운 기계음 손상완을 옮겨 침대가 잡고 새로운 기계음 손상완을 잡고 "
              + "기계음 손상완을 잡고 " * 60)
DEGEN_LOOP2 = "완성된 윤리적경영의 완성과 " + "완성의 협력을 위한 " * 40
DEGEN_PHRASE = "주차장의 순위\n" * 25

# 정상 한국어(신탁 문서 실제 문단 — 절대 걸리면 안 됨)
NORMAL = ("수탁자는 신탁건물의 준공 및 보존등기 완료시 분양수입금관리계좌로 분양대금을 완납한 "
          "수분양자에 대해서는 해당 신탁부동산의 소유권 이전절차를 진행한다. 다만, 수탁자의 "
          "귀책사유가 아닌 이유로 소유권이전이 불가능한 경우에 그 책임은 수탁자가 지지 아니한다. "
          "특약사항 제2조에 따라 수탁자는 건축허가조건 이행, 사용승인 및 보존등기 후 수분양자로의 "
          "소유권 이전 등 안정적인 신탁목적 달성을 위해 필요한 행정업무를 수행할 수 있다. "
          "또한 분양계약 해제 또는 해지 시 분양대금 반환 채무 이행과 관련한 제반 서류를 준비한다.")

# 정상이지만 가벼운 반복이 있는 텍스트(체크리스트류 — 오검 금지)
NORMAL_LIST = ("Check Point ① 소유권이전 요청(동의)서 징구\n"
               "Check Point ② 분양대금 입금 확인\n"
               "Check Point ③ 분양계약서 상 최종 수분양자 확인\n"
               "Check Point ④ 소유권이전 관리대장 확인\n"
               "각 항목은 담당자가 확인 후 기안문에 첨부한다.")


def test_degenerate_loop_detected():
    assert is_degenerate_text(DEGEN_LOOP)
    assert is_degenerate_text(DEGEN_LOOP2)
    assert is_degenerate_text(DEGEN_PHRASE)


def test_normal_korean_not_flagged():
    assert not is_degenerate_text(NORMAL)
    assert not is_degenerate_text(NORMAL_LIST)


def test_short_text_never_flagged():
    assert not is_degenerate_text("기계음 손상완을 잡고 기계음 손상완을 잡고")  # 짧으면 판정 보류


def test_filter_pages_removes_only_degenerate_blocks():
    pages = [
        {"page_number": 1, "blocks": [
            {"type": "text", "text": NORMAL, "page_idx": 1},
            {"type": "text", "text": DEGEN_LOOP, "page_idx": 1},
            {"type": "table", "table_body": "<table><tr><td>정상 표</td></tr></table>",
             "page_idx": 1},
        ]},
        {"page_number": 2, "blocks": [
            {"type": "text", "text": DEGEN_LOOP2, "page_idx": 2},
        ]},
    ]
    removed = filter_degenerate_pages(pages)
    assert removed == 2
    p1_types = [(b["type"], b.get("text", "")[:10]) for b in pages[0]["blocks"]]
    assert len(pages[0]["blocks"]) == 2                       # 정상 text + table 유지
    assert any(t == "table" for t, _ in p1_types), "표 블록은 v1 필터 대상 아님"
    assert pages[1]["blocks"] == []                           # 퇴화만 있던 페이지는 빔


# ---- v2: 표 블록도 검사(보수적 임계) ----

DEGEN_TABLE = ("<table>" + "".join(
    f"<tr><td>송개왕</td><td>02-</td><td>송개왕</td><td>송개왕</td></tr>" for _ in range(15))
    + "</table>")

NORMAL_TABLE = ("<table><tr><th>구분</th><th>전체 물건</th><th>기 이전</th><th>금회</th>"
                "<th>누계</th><th>잔여</th><th>이전율</th></tr>"
                "<tr><td>공동주택</td><td>90</td><td>25</td><td>5</td><td>30</td><td>60</td><td>33.3%</td></tr>"
                "<tr><td>근린생활시설</td><td>10</td><td>2</td><td>1</td><td>3</td><td>7</td><td>30.0%</td></tr>"
                "<tr><td>합계</td><td>100</td><td>27</td><td>6</td><td>33</td><td>67</td><td>33.0%</td></tr></table>")

# 반복 셀(O/X)이 정당한 현실적 체크리스트 — 항목 내용은 서로 다름. 오검 금지.
# (동일 장문 셀이 12번 반복되는 표는 VL 퇴화와 통계적으로 동형이라 구분 불가 — 알려진 트레이드오프)
_CHECK_ITEMS = ["소유권이전 요청서 징구 여부", "분양대금 완납 확인", "중도금 대출 완제 확인",
                "최종계약자와 등기명의자 일치", "매매계약서 등 증빙 첨부", "관리대장 중복 신청 점검",
                "등기위임장 날인 상태", "매도용 인감증명서 유효기간", "미분양물건 할인률 기재",
                "사업관계자 동의 여부", "부동산거래계약 신고 확인", "소송·제한권리 현황 점검"]
CHECK_TABLE = ("<table><tr><th>항목</th><th>확인</th></tr>" + "".join(
    f"<tr><td>{it}</td><td>O</td></tr>" for it in _CHECK_ITEMS) + "</table>")


def test_degenerate_table_detected():
    pages = [{"page_number": 1, "blocks": [
        {"type": "table", "table_body": DEGEN_TABLE, "page_idx": 1}]}]
    assert filter_degenerate_pages(pages) == 1
    assert pages[0]["blocks"] == []


def test_normal_tables_not_flagged():
    pages = [{"page_number": 1, "blocks": [
        {"type": "table", "table_body": NORMAL_TABLE, "page_idx": 1},
        {"type": "table", "table_body": CHECK_TABLE, "page_idx": 1}]}]
    assert filter_degenerate_pages(pages) == 0
    assert len(pages[0]["blocks"]) == 2


def test_giant_repeated_cell_table_detected():
    """실관측 표9 형(2026-07-15): 셀 2개뿐이지만 한 셀이 '손을'×수십(2490자) — R1 압축비."""
    giant = "손을 " * 500
    t = f"<table><tr><td>권리·의무승계내역</td><td>{giant}</td></tr></table>"
    pages = [{"page_number": 1, "blocks": [{"type": "table", "table_body": t, "page_idx": 1}]}]
    assert filter_degenerate_pages(pages) == 1


def test_scattered_repeat_table_detected():
    """실관측 표10 형: '송개왕' 이 60셀 중 43% 산재(dom 0.43, comp 0.34) — R3."""
    rows = []
    for i in range(15):
        rows.append(f"<tr><td>송개왕</td><td>0{i%3}-</td><td>송개왕</td><td>송개왕 순위</td></tr>")
    t = "<table>" + "".join(rows) + "</table>"
    pages = [{"page_number": 1, "blocks": [{"type": "table", "table_body": t, "page_idx": 1}]}]
    assert filter_degenerate_pages(pages) == 1


# ---- v4: 짧은 루프(60-200자) + 표 ttr(단어 다양성) — 실관측 잔존물(2026-07-15 2차) ----

def test_short_loop_text_detected():
    """실관측: '완성의 협력을 위한'×5 가 79자라 <200자 보류에 걸려 통과했음.
    60자+ 에선 3-gram 지배(≥0.5) 또는 ttr≤0.45 면 퇴화."""
    short_loop = "완성된 윤리적경영의 완성과 " + "완성의 협력을 위한 " * 5
    assert is_degenerate_text(short_loop)


def test_short_normal_annotation_not_flagged():
    """실관측 정상: 69자 주석 (주)¹... (ttr=1.0, top3=0.21) — 오검 금지."""
    normal = "(주)¹ 요청(동의)서 상 위탁자 및 시공사, 대리금융기관(우선수익자) 책임으로 분양대금 완납 확인(조건부 동의) 문구 기재"
    assert not is_degenerate_text(normal)
    check = ("Check Point ① 소유권이전 요청(동의)서 징구\nCheck Point ② 분양대금 입금 확인\n"
             "Check Point ③ 분양계약서 상 최종 수분양자 확인\nCheck Point ④ 소유권이전 관리대장 확인")
    assert not is_degenerate_text(check)


def test_low_ttr_table_detected():
    """실관측 표9(2차 런): rowspan 병합으로 dom=0.20 이지만 단어 다양성 ttr=0.35
    ('송개왕/송삼왕부송삼' 변주 반복) — 정상표 최저 ttr=0.69 와 분리."""
    # 같은 단어 변주가 셀들에 반복 → ttr 낮음
    cells = ["순번", "송개왕의", "성명", "낙인"] + ["송개왕", "송개왕 순위", "송삼왕부송삼", "송개왕"] * 12
    t = "<table>" + "".join(f"<tr><td>{c}</td></tr>" for c in cells) + "</table>"
    pages = [{"page_number": 1, "blocks": [{"type": "table", "table_body": t, "page_idx": 1}]}]
    assert filter_degenerate_pages(pages) == 1


# ── 2026-08-11: degen_filter 를 "삭제 함수" → "증거 생성기" 로 전환 ────────────
#
# 실측 반례: 정상 법인등기부 표가 R4(TTR)에 걸려 production 에서 삭제되고 있었다.
# 등기부 표는 같은 날짜·등기원인·문구 반복이 정상이라 lexical diversity 가 원래 낮다.
# 검색 recall 이 목표라면 false quarantine 보다 **silent deletion 이 더 나쁘다.**

import pathlib

import pytest

from parse_service.parsers.degen_filter import (
    Severity, apply_assessment, assess_page, assess_table_rules, visible_chars,
)

_FIXTURES = pathlib.Path(__file__).parent / "fixtures"
#: 시흥 장현지구 p52 의 table0 원본(법인 목적 조항 + 등기 날짜). **개인정보 없음** —
#: PII 는 같은 페이지의 table1 에만 있고 그 표는 퇴화 판정과 무관하다(joined 100자 <
#: _MIN_LEN 200 이라 comp 1.0 고정). 사람 대조 라벨 USABLE.
_REGISTRY_FP = (_FIXTURES / "degen_fp_registry_janghyeon_p52_table0.html").read_text("utf-8")


def _page(*blocks):
    return {"page_number": 1, "blocks": [dict(b) for b in blocks]}


def _table(body):
    return {"type": "table", "table_body": body, "page_idx": 1}


# ── ★1. 정상 반복 등기부 표 → R4 신호가 있어도 내용이 삭제되지 않는다 ──────────

def test_registry_table_is_soft_not_deleted():
    page = _page(_table(_REGISTRY_FP))
    before = len(page["blocks"])
    assert filter_degenerate_pages([page]) == 0, "정상 등기부 표를 삭제하면 안 된다"
    assert len(page["blocks"]) == before


def test_registry_table_is_still_reported_as_soft():
    """삭제하지 않되 **관측은 한다** — SOFT 근거와 통계가 남는다."""
    a = assess_page(_page(_table(_REGISTRY_FP)))
    b = a.blocks[0]
    assert b.severity is Severity.SOFT
    assert b.rules == ("R4",), f"R4 단독이어야 한다: {b.rules}"
    assert a.chars_soft > 0 and a.chars_hard == 0


def test_registry_fixture_statistics_are_in_the_expected_regime():
    """픽스처가 '정상인데 R4 에 걸리는' 구간에 실제로 있는지(회귀 시 조용히 무의미해짐)."""
    rules, stats = assess_table_rules(_REGISTRY_FP)
    assert rules == ("R4",)
    assert stats["comp"] > 0.16, "R1 임계 위 = 압축비로는 정상"
    assert stats["dom"] <= 0.26 and stats["ttr"] <= 0.30
    assert stats["meaningful"] >= 20, "소형 표 조기종료 구간이 아니어야 R4 가 평가된다"


def test_single_soft_block_does_not_promote_page():
    """단일 표가 R4 에 걸렸다고 페이지가 통째로 죽지 않는다(정상 본문 동반)."""
    page = _page(_table(_REGISTRY_FP), {"type": "text", "text": NORMAL, "page_idx": 1})
    assert filter_degenerate_pages([page]) == 0
    assert len(page["blocks"]) == 2


# ── 불변식 A/B ────────────────────────────────────────────────────────────────

def test_small_repeated_table_still_preserved():
    """유의미셀 <20 인 소형 반복 표는 R2/R3/R4 평가 자체를 하지 않는다(불변식 A).

    이 게이트를 재현하지 않으면 지금 살아남는 소형 표가 새로 삭제된다.
    """
    small = "<table>" + "".join("<tr><td>동일값</td><td>동일값</td></tr>"
                                for _ in range(5)) + "</table>"
    rules, stats = assess_table_rules(small)
    assert stats["meaningful"] < 20
    assert "R2" not in rules and "R3" not in rules and "R4" not in rules
    page = _page(_table(small))
    assert filter_degenerate_pages([page]) == 0


def test_short_table_comp_is_pinned_to_one():
    """joined < 200 이면 comp 를 계산하지 않고 1.0 으로 고정한다(불변식 B — R3 의 입력)."""
    _, stats = assess_table_rules("<table><tr><td>가나다</td><td>라마바</td></tr></table>")
    assert stats["comp"] == 1.0


# ── 순수성 / chars 정의 ───────────────────────────────────────────────────────

def test_assess_page_is_pure():
    page = _page(_table(_REGISTRY_FP), {"type": "text", "text": DEGEN_LOOP, "page_idx": 1})
    snapshot = [dict(b) for b in page["blocks"]]
    assess_page(page)
    assert page["blocks"] == snapshot, "assess_page 는 아무것도 바꾸지 않는다"


def test_chars_excludes_markup():
    """`chars` 는 태그·공백을 뺀 문자 수 — 마크업이 본문보다 긴 표에서 갈린다."""
    body = "<table><tr><td style='text-align: center; word-wrap: break-word;'>가나</td></tr></table>"
    assert visible_chars(body) == 2
    assert visible_chars(body) < len(body) / 10


# ── HARD 는 그대로 삭제된다 ───────────────────────────────────────────────────

def test_hard_rules_still_delete():
    page = _page(_table(DEGEN_TABLE), {"type": "text", "text": DEGEN_LOOP, "page_idx": 1})
    assert filter_degenerate_pages([page]) == 2
    assert page["blocks"] == []


def test_apply_assessment_removes_only_hard():
    page = _page(_table(_REGISTRY_FP), _table(DEGEN_TABLE))
    a = assess_page(page)
    assert apply_assessment(page, a) == 1
    assert len(page["blocks"]) == 1 and page["blocks"][0]["table_body"] == _REGISTRY_FP


# ── env: call-time 읽기 + 되돌림 손잡이 ──────────────────────────────────────

def test_compress_max_is_read_at_call_time(monkeypatch):
    """모듈 상수로 고정하면 monkeypatch 도 폐쇄망 재기동도 안 먹는다."""
    page = _page(_table(_REGISTRY_FP))
    assert filter_degenerate_pages([page]) == 0          # 기본 0.16 → 보존
    monkeypatch.setenv("KBP_DEGEN_COMPRESS_MAX", "0.25")  # .2119 < .25 → R1 승격
    page2 = _page(_table(_REGISTRY_FP))
    assert filter_degenerate_pages([page2]) == 1
    assert page2["blocks"] == []


def test_compress_max_does_not_move_text_threshold(monkeypatch):
    """표 임계를 올려도 텍스트 T1 은 움직이지 않는다(텍스트 규칙 무변경 원칙)."""
    monkeypatch.setenv("KBP_DEGEN_COMPRESS_MAX", "0.99")
    assert not is_degenerate_text(NORMAL), "정상 한국어가 T1 로 잡히면 안 된다"


def test_soft_rules_none_restores_old_behavior(monkeypatch):
    """`none` 센티널이면 전 규칙 HARD = 구동작 복원(되돌림 손잡이).

    빈 문자열을 센티널로 쓰지 않는 이유: compose 의 `${VAR:-기본값}` 이 빈 값에도
    기본값을 대입해 **폐쇄망에서만** 되돌림이 안 먹는다.
    """
    monkeypatch.setenv("KBP_DEGEN_SOFT_RULES", "none")
    page = _page(_table(_REGISTRY_FP))
    assert filter_degenerate_pages([page]) == 1, "구동작에서는 등기부 표가 삭제된다"


def test_soft_rules_default_is_r3_r4(monkeypatch):
    monkeypatch.delenv("KBP_DEGEN_SOFT_RULES", raising=False)
    a = assess_page(_page(_table(_REGISTRY_FP)))
    assert a.blocks[0].severity is Severity.SOFT


@pytest.mark.parametrize("value", ["", "   "])
def test_soft_rules_blank_falls_back_to_default(monkeypatch, value):
    """빈 값은 센티널이 아니라 **기본값**이다.

    ① compose 의 `${VAR:-R3,R4}` 가 빈 값에도 기본값을 넣으므로, in-process 만 빈 값을
       "전 규칙 HARD" 로 읽으면 호스트와 컨테이너 동작이 갈린다(실측 확인).
    ② 빈 값에 "삭제가 늘어나는 쪽" 을 배정하면 실수로 비웠을 때 안전화가 조용히 풀린다.
    """
    monkeypatch.setenv("KBP_DEGEN_SOFT_RULES", value)
    page = _page(_table(_REGISTRY_FP))
    assert filter_degenerate_pages([page]) == 0, "빈 값에서도 등기부 표는 보존된다"


# ── 관측 로그가 조용히 사라지지 않게 ─────────────────────────────────────────

def test_soft_observation_is_logged(caplog):
    """SOFT 관측 로그는 D1(임계 마진) 분포를 쌓는 **유일한** 데이터 수집 경로다."""
    with caplog.at_level("INFO", logger="kb_pipeline.parse_service.parsers.degen_filter"):
        filter_degenerate_pages([_page(_table(_REGISTRY_FP))])
    rec = [r for r in caplog.records if "SOFT 관측" in r.getMessage()]
    assert rec, "SOFT 보존 사실이 로그에 남아야 한다"
    msg = rec[0].getMessage()
    assert "R4" in msg and "comp" in msg and "ttr" in msg


# ── 2026-08-11 Phase 1 §6: 목차 leader dot 정규화 ────────────────────────────
#
# degen 필터가 정상 내용을 지우는 **세 번째** 사례였다(위 등기부 표 2건에 이어).
# 점선은 의미 없는 조판 장식인데 압축비(T1)·5-gram 지배(T2)에 반복으로 걸린다.

from parse_service.parsers.degen_filter import normalize_for_measure

_TOC_TITLES = ["총칙", "용어의 정의", "적용 범위", "우발비용의 개념", "사전점검 대상 사업",
               "점검 시기 및 주기", "점검 항목과 기준", "현장 실사 절차", "보고서 작성 요령",
               "이견 조정 및 재점검", "결과의 활용", "기록의 보존", "위임 및 준용", "부칙"]


def _toc(sep: str) -> str:
    return "\n".join(f"제{i}조 {t} {sep} {i * 5}"
                     for i, t in enumerate(_TOC_TITLES, 1))


@pytest.mark.parametrize("sep,name", [("." * 50, "연속점선"), (". " * 25, "공백점선"),
                                      ("·" * 40, "중간점"), ("…" * 12, "말줄임표")])
def test_toc_leader_dots_not_deleted(sep, name):
    """★ 핵심 앵커 — 목차 페이지가 통째로 삭제되면 안 된다."""
    page = _page({"type": "text", "text": _toc(sep), "page_idx": 1})
    assert filter_degenerate_pages([page]) == 0, f"{name} 목차가 삭제됐다"
    assert len(page["blocks"]) == 1


def test_normalization_keeps_true_degeneration():
    """정규화가 진짜 퇴화를 죽이지 않는다 — TP 보존 앵커."""
    assert is_degenerate_text(DEGEN_LOOP)
    assert is_degenerate_text(DEGEN_PHRASE)


def test_normalization_does_not_touch_block_content():
    """판정 입력만 정규화한다 — **블록 원문에는 점선이 그대로 남는다.**"""
    toc = _toc("." * 50)
    page = _page({"type": "text", "text": toc, "page_idx": 1})
    filter_degenerate_pages([page])
    assert page["blocks"][0]["text"] == toc
    assert "." * 50 in page["blocks"][0]["text"]


def test_normalize_keeps_normal_sentences():
    """점 사이에 공백 이외의 글자가 오면 매치가 끊긴다 — 항목 나열을 먹지 않는다."""
    for t in ("가. 나. 다. 라. 마.", "제1항. 제2항. 제3항.", "1. 총칙 2. 정의 3. 범위"):
        assert normalize_for_measure(t) == t


def test_normalization_not_applied_to_tables():
    """표 셀의 점선은 목차와 성격이 다르고 실측 근거가 없다 — 적용하지 않는다."""
    body = "<table>" + "".join(f"<tr><td>항목{i} {'.' * 30}</td></tr>" for i in range(30)) + "</table>"
    _, stats = assess_table_rules(body)
    assert "." * 30 in body and stats["joined"] > 0   # 원문 그대로 측정됨
