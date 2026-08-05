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


def test_inprocess_uses_original_filename_as_document_title(monkeypatch):
    """:8600과 동일하게 임시파일 stem 대신 원본 basename을 문서 제목으로 전달한다."""
    from parse_service.parsers.excel.excel_parser_rag import backends, gate

    captured = {}

    class CapturingBackend:
        def parse(self, input_path, config):
            captured["temporary_stem"] = input_path.stem
            captured["document_title"] = config.document_title
            return (
                [{
                    "content_text": f"{config.document_title}의 관련 주석: 원문 주석",
                    "title": "관련 주석",
                    "path": ["관련 주석"],
                }],
                {},
            )

    monkeypatch.setattr(backends, "get_backend", lambda _name: CapturingBackend())
    monkeypatch.setattr(gate, "compute_gate_summary", lambda _path, _chunks: {"sheets": []})

    res = excel_parser.parse(
        b"PK",
        "../../2-1. 위임전결기준표(2026.04.17. 개정).xlsx",
    )

    assert captured["temporary_stem"].startswith("excel_parser_")
    assert captured["document_title"] == "2-1. 위임전결기준표(2026.04.17. 개정)"
    assert res.chunks[0]["text"].startswith(
        "2-1. 위임전결기준표(2026.04.17. 개정)의"
    )
    assert "excel_parser_" not in res.chunks[0]["text"]


def test_empty_chunks_returns_gate_summary(monkeypatch):
    """빈 청크는 더 이상 raise 하지 않는다 — gate_summary 를 실은 RouteResult 를 반환
    (깨진 엑셀은 크래시가 아니라 다운스트림 게이트에서 reject)."""
    monkeypatch.setattr(excel_parser, "_fetch_rag_chunks", lambda fb, fn, excel_url: ([], {"sheets": []}))
    res = excel_parser.parse(b"PK", "a.xlsx", excel_url="http://x")
    assert res.kind == "chunks" and res.chunks == [] and res.gate_summary is not None
