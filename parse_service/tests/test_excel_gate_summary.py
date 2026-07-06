"""parse-svc excel 경로가 gate_summary 를 in-process 로 계산해 RouteResult 에 싣는다."""


def _tiny_xlsx_bytes() -> bytes:
    import io
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["항목", "값"])
    ws.append(["a", "1"])
    ws.append(["b", "2"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_excel_parse_returns_gate_summary(monkeypatch):
    monkeypatch.setenv("EXCEL_PARSER_BACKEND", "openpyxl")  # no kordoc/java in CI
    from parse_service.parsers.excel import parse
    rr = parse(_tiny_xlsx_bytes(), "tiny.xlsx")
    assert rr.gate_summary is not None
    assert isinstance(rr.gate_summary, dict)
