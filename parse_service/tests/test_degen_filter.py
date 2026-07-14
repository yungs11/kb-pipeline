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
