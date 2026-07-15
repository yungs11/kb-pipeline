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
