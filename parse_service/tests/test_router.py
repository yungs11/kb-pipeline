"""router — 확장자 → 도메인 파서 디스패치 (Phase 2a 매핑)."""
import pytest
from parse_service.parsers import RouteResult
from parse_service import router


@pytest.mark.parametrize("fname,expected_domain", [
    ("a.pdf", "pdf"), ("a.xlsx", "excel"), ("a.xlsm", "excel"), ("a.xls", "excel"),
    ("a.pptx", "ocr"), ("a.docx", "docx"),  # 2d: docx=kordoc 네이티브
    ("a.png", "ocr"), ("a.webp", "ocr"),
])
def test_dispatch(monkeypatch, fname, expected_domain):
    called = {}
    def fake(domain):
        def _p(fb, fn, **kw):
            called["domain"] = domain
            return RouteResult(kind="pages", chunk_needed=True, pages=[])
        return _p
    monkeypatch.setattr(router, "_PARSERS",
                        {d: fake(d) for d in ("pdf", "excel", "ocr", "docx", "fallback")})
    router.route(b"x", fname, ocr_url="u", excel_url="v")
    assert called["domain"] == expected_domain


def test_unknown_ext_falls_back(monkeypatch):
    called = {}
    def fb_parse(fb, fn, **kw):
        called["domain"] = "fallback"
        return RouteResult(kind="pages", chunk_needed=True, pages=[])
    monkeypatch.setattr(router, "_PARSERS", {"pdf": None, "excel": None,
                                             "ocr": None, "docx": None,
                                             "fallback": fb_parse})
    router.route(b"x", "a.hwpx", ocr_url="u", excel_url="v")
    assert called["domain"] == "fallback"
