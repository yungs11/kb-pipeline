"""paddle_gw 레인 — 스캔 PDF 를 PaddleOCR-VL 게이트웨이(GPU)로 페이지별 병렬 파싱."""
import pytest

from parse_service.parsers.pdf import paddle_gw


class _RP:
    def __init__(self, n, jpeg=b"jpegbytes"):
        self.page_number, self.jpeg = n, jpeg


def test_requires_gateway_url(monkeypatch):
    monkeypatch.delenv("KBP_PADDLE_OCR_GATEWAY_URL", raising=False)
    with pytest.raises(RuntimeError):
        paddle_gw.run_paddle_gateway(b"%PDF", "a.pdf")


def test_pages_parsed_in_parallel_with_table_html(monkeypatch):
    """페이지별 POST → markdown+HTML표 → hybrid_to_blocks(page_idx). 표 HTML 보존."""
    monkeypatch.setenv("KBP_PADDLE_OCR_GATEWAY_URL", "https://gw/ocr/paddleocr_vl")
    monkeypatch.setattr(paddle_gw, "_render_pages", lambda fb: [_RP(1), _RP(2)])
    seen = []

    def fake_post(jpeg, name):
        seen.append(name)
        if "page-1" in name:
            return "# 제목\n\n본문 텍스트\n\n<table><tr><td>셀A</td></tr></table>"
        return "둘째 페이지 텍스트"

    monkeypatch.setattr(paddle_gw, "_post_page", fake_post)
    pages = paddle_gw.run_paddle_gateway(b"%PDF", "a.pdf")
    assert [p["page_number"] for p in pages] == [1, 2]
    p1 = pages[0]
    tbl = next(b for b in p1["blocks"] if b["type"] == "table")
    assert "셀A" in tbl["table_body"]                      # 표 HTML 보존
    assert all(b["page_idx"] == 1 for b in p1["blocks"])   # 1-based page_idx
    assert any("둘째 페이지" in (b.get("text") or "") for b in pages[1]["blocks"])
    assert len(seen) == 2


def test_nonprobe_page_failure_nonfatal_empty_blocks(monkeypatch):
    """프로브(1p) 성공 후 개별 페이지 실패는 비치명 — 그 페이지만 빈 blocks."""
    monkeypatch.setenv("KBP_PADDLE_OCR_GATEWAY_URL", "https://gw/ocr/paddleocr_vl")
    monkeypatch.setattr(paddle_gw, "_render_pages", lambda fb: [_RP(1), _RP(2), _RP(3)])

    def fake_post(jpeg, name):
        if "page-2" in name:
            raise RuntimeError("gateway 5xx")
        return f"{name} 텍스트"

    monkeypatch.setattr(paddle_gw, "_post_page", fake_post)
    pages = paddle_gw.run_paddle_gateway(b"%PDF", "a.pdf")
    assert pages[0]["blocks"], "프로브(1p) 정상"
    assert pages[1]["blocks"] == []                         # 실패 페이지 = 빈
    assert pages[2]["blocks"], "성공 페이지 유지"


def test_probe_failure_raises_for_fast_fallback(monkeypatch):
    """첫 페이지(프로브) 실패 = 게이트웨이 불능 → 즉시 raise (페이지별 타임아웃 대기 없이
    parse() 가 바로 ODL/VL 폴백). 행 게이트웨이가 문서 전체를 붙잡던 문제의 회귀 고정."""
    monkeypatch.setenv("KBP_PADDLE_OCR_GATEWAY_URL", "https://gw/ocr/paddleocr_vl")
    monkeypatch.setattr(paddle_gw, "_render_pages", lambda fb: [_RP(1), _RP(2)])
    calls = []

    def boom(jpeg, name):
        calls.append(name)
        raise RuntimeError("gateway hang/down")

    monkeypatch.setattr(paddle_gw, "_post_page", boom)
    with pytest.raises(RuntimeError):
        paddle_gw.run_paddle_gateway(b"%PDF", "a.pdf")
    assert calls == ["page-1.jpeg"], "프로브 1회만 호출하고 즉시 포기(나머지 페이지 시도 안 함)"


def test_render_uses_low_dpi_env(monkeypatch):
    """게이트웨이용 렌더는 KBP_PADDLE_GW_DPI(기본 150) — MinIO 페이지이미지(300)와 분리."""
    seen = {}

    def fake_render(pdf_bytes, *, dpi=300, jpg_quality=90):
        seen["dpi"] = dpi
        return []

    import parse_service.pdf_pages as pp
    monkeypatch.setattr(pp, "render_pdf_pages", fake_render)
    monkeypatch.delenv("KBP_PADDLE_GW_DPI", raising=False)
    paddle_gw._render_pages(b"%PDF")
    assert seen["dpi"] == 150                     # 기본 150
    monkeypatch.setenv("KBP_PADDLE_GW_DPI", "200")
    paddle_gw._render_pages(b"%PDF")
    assert seen["dpi"] == 200                     # env override
