"""parsers/excel — 자체청킹 결과를 facade 청크 스키마로, chunk_needed=False."""
from parse_service.parsers import excel as excel_parser


def test_chunks_normalized_and_flag_false(monkeypatch):
    rag = [{"content_text": "표1 내용", "title": "시트1", "path": ["시트1"]},
           {"content_text": "표2 내용", "title": "시트2", "path": ["시트2"]}]
    monkeypatch.setattr(excel_parser, "_fetch_rag_chunks", lambda fb, fn, excel_url: (rag, {"sheets": []}))
    res = excel_parser.parse(b"PK", "a.xlsx", excel_url="http://x")
    assert res.kind == "chunks" and res.chunk_needed is False
    assert res.chunks[0]["chunk_index"] == 0
    assert res.chunks[0]["text"] == "표1 내용"
    assert "titles_context" in res.chunks[0] and "pages" in res.chunks[0]


def test_inprocess_openpyxl_smoke(monkeypatch):
    """실제 openpyxl 백엔드로 초소형 xlsx 를 파싱(파일시스템/kordoc 무관 백엔드)."""
    import io
    import openpyxl
    monkeypatch.setenv("EXCEL_PARSER_BACKEND", "openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"], ws["B1"], ws["A2"], ws["B2"] = "이름", "값", "가", 1
    buf = io.BytesIO()
    wb.save(buf)
    res = excel_parser.parse(buf.getvalue(), "t.xlsx")
    assert res.chunk_needed is False and res.chunks


def test_empty_chunks_returns_gate_summary(monkeypatch):
    """빈 청크는 더 이상 raise 하지 않는다 — gate_summary 를 실은 RouteResult 를 반환
    (깨진 엑셀은 크래시가 아니라 다운스트림 게이트에서 reject)."""
    monkeypatch.setattr(excel_parser, "_fetch_rag_chunks", lambda fb, fn, excel_url: ([], {"sheets": []}))
    res = excel_parser.parse(b"PK", "a.xlsx", excel_url="http://x")
    assert res.kind == "chunks" and res.chunks == [] and res.gate_summary is not None
