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


def test_parse_endpoint_surfaces_gate_summary(monkeypatch):
    """POST /parse on an xlsx (chunks route) carries top-level gate_summary."""
    from fastapi.testclient import TestClient
    import parse_service.app as appmod
    from parse_service.parsers import RouteResult

    gate = {"sheets": [], "ok": True}
    monkeypatch.setattr(
        appmod, "_route",
        lambda fb, fn, **kw: RouteResult(
            kind="chunks", chunk_needed=False,
            chunks=[{"chunk_index": 0, "text": "표1", "titles_context": [], "pages": []}],
            gate_summary=gate,
        ),
    )
    r = TestClient(appmod.app).post(
        "/parse", files={"file": ("a.xlsx", b"PK")}, data={"filename": "a.xlsx"})
    assert r.status_code == 200
    body = r.json()
    assert "gate_summary" in body
    assert body["gate_summary"] == gate
