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
    monkeypatch.setattr(paddle_gw, "_render_pages",
                        lambda fb, page_numbers=None: [_RP(1), _RP(2)])
    seen = []

    def fake_post(jpeg, name):
        seen.append(name)
        if "page-1" in name:
            return ("# 제목\n\n본문 텍스트\n\n<table><tr><td>셀A</td></tr></table>", [], None)
        return ("둘째 페이지 텍스트", [], None)

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
    monkeypatch.setattr(paddle_gw, "_render_pages",
                        lambda fb, page_numbers=None: [_RP(1), _RP(2), _RP(3)])

    def fake_post(jpeg, name):
        if "page-2" in name:
            raise RuntimeError("gateway 5xx")
        return (f"{name} 텍스트", [], None)

    monkeypatch.setattr(paddle_gw, "_post_page", fake_post)
    pages = paddle_gw.run_paddle_gateway(b"%PDF", "a.pdf")
    assert pages[0]["blocks"], "프로브(1p) 정상"
    assert pages[1]["blocks"] == []                         # 실패 페이지 = 빈
    assert pages[2]["blocks"], "성공 페이지 유지"


def test_probe_failure_raises_for_fast_fallback(monkeypatch):
    """첫 페이지(프로브) 실패 = 게이트웨이 불능 → 즉시 raise (페이지별 타임아웃 대기 없이
    parse() 가 바로 ODL/VL 폴백). 행 게이트웨이가 문서 전체를 붙잡던 문제의 회귀 고정."""
    monkeypatch.setenv("KBP_PADDLE_OCR_GATEWAY_URL", "https://gw/ocr/paddleocr_vl")
    monkeypatch.setattr(paddle_gw, "_render_pages",
                        lambda fb, page_numbers=None: [_RP(1), _RP(2)])
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


# ---- 비동기(tasks) 게이트웨이 (2026-07-15: dots CF 524 우회 — submit→poll→result) ----

def test_async_post_page_submits_polls_and_fetches_result(monkeypatch):
    """_post_page: POST /tasks → task_id, GET /tasks/{id} 폴링(queued→running→completed),
    GET /tasks/{id}/result → text. CF 100s 무관."""
    monkeypatch.setenv("KBP_PADDLE_OCR_GATEWAY_URL", "https://gw/ocr/dots_ocr")
    monkeypatch.setattr(paddle_gw, "_POLL_INTERVAL", 0)  # 테스트: 대기 없이
    calls = []

    class FakeResp:
        def __init__(self, body): self._b = body
        def raise_for_status(self): pass
        def json(self): return self._b

    state = {"n": 0}

    def fake_post(url, **kw):
        calls.append(("POST", url))
        assert url == "https://gw/ocr/dots_ocr/tasks"
        assert kw["data"]["lang"] == "korean"
        return FakeResp({"task_id": "t-1", "status": "queued"})

    def fake_get(url, **kw):
        calls.append(("GET", url))
        if url.endswith("/result"):
            return FakeResp({"status": "ok", "text": "# 결과\n<table><tr><td>셀</td></tr></table>"})
        state["n"] += 1
        return FakeResp({"status": "running" if state["n"] < 3 else "completed"})

    monkeypatch.setattr(paddle_gw.httpx, "post", fake_post)
    monkeypatch.setattr(paddle_gw.httpx, "get", fake_get)
    md, layout, page_size = paddle_gw._post_page(b"jpeg", "page-1.jpeg")
    assert "<table>" in md
    assert calls[0] == ("POST", "https://gw/ocr/dots_ocr/tasks")
    assert calls[-1] == ("GET", "https://gw/ocr/dots_ocr/tasks/t-1/result")


def test_async_task_failed_raises(monkeypatch):
    monkeypatch.setenv("KBP_PADDLE_OCR_GATEWAY_URL", "https://gw/ocr/dots_ocr")
    monkeypatch.setattr(paddle_gw, "_POLL_INTERVAL", 0)

    class FakeResp:
        def __init__(self, body): self._b = body
        def raise_for_status(self): pass
        def json(self): return self._b

    monkeypatch.setattr(paddle_gw.httpx, "post",
                        lambda url, **kw: FakeResp({"task_id": "t-2", "status": "queued"}))
    monkeypatch.setattr(paddle_gw.httpx, "get",
                        lambda url, **kw: FakeResp({"status": "failed", "error": "vlm oom"}))
    with pytest.raises(RuntimeError):
        paddle_gw._post_page(b"jpeg", "page-1.jpeg")


def test_async_poll_timeout_raises(monkeypatch):
    """폴링이 _DEFAULT_TIMEOUT 을 넘기면 RuntimeError(페이지 비치명 처리로 연결)."""
    monkeypatch.setenv("KBP_PADDLE_OCR_GATEWAY_URL", "https://gw/ocr/dots_ocr")
    monkeypatch.setattr(paddle_gw, "_POLL_INTERVAL", 0)
    monkeypatch.setattr(paddle_gw, "_DEFAULT_TIMEOUT", 0.01)  # 즉시 만료

    class FakeResp:
        def __init__(self, body): self._b = body
        def raise_for_status(self): pass
        def json(self): return self._b

    monkeypatch.setattr(paddle_gw.httpx, "post",
                        lambda url, **kw: FakeResp({"task_id": "t-3", "status": "queued"}))
    monkeypatch.setattr(paddle_gw.httpx, "get",
                        lambda url, **kw: FakeResp({"status": "running"}))
    with pytest.raises(RuntimeError):
        paddle_gw._post_page(b"jpeg", "page-1.jpeg")


def test_raw_json_layout_response_retried_once(monkeypatch):
    """실관측(2026-07-15 p10): dots 가 간헐적으로 markdown 대신 raw JSON layout
    ('[{"bbox":...,"category":...') 을 반환 — 단독 재호출은 정상. 1회 재시도로 복구."""
    monkeypatch.setenv("KBP_PADDLE_OCR_GATEWAY_URL", "https://gw/ocr/dots_ocr")
    monkeypatch.setattr(paddle_gw, "_POLL_INTERVAL", 0)

    class FakeResp:
        def __init__(self, body): self._b = body
        def raise_for_status(self): pass
        def json(self): return self._b

    submits = {"n": 0}

    def fake_post(url, **kw):
        submits["n"] += 1
        return FakeResp({"task_id": f"t-{submits['n']}", "status": "queued"})

    def fake_get(url, **kw):
        if url.endswith("/result"):
            if "t-1" in url:   # 1차: raw JSON layout (형식 오류)
                return FakeResp({"status": "ok",
                                 "text": '[{"bbox": [36, 55], "category": "Section-header", "text": "x"}]' * 50})
            return FakeResp({"status": "ok", "text": "# 정상\n<table><tr><td>셀</td></tr></table>"})
        return FakeResp({"status": "completed"})

    monkeypatch.setattr(paddle_gw.httpx, "post", fake_post)
    monkeypatch.setattr(paddle_gw.httpx, "get", fake_get)
    md, layout, page_size = paddle_gw._post_page(b"jpeg", "page-10.jpeg")
    assert submits["n"] == 2, "raw JSON 1회 재시도"
    assert "<table>" in md and not md.strip().startswith("[{")


# ---- 게이트웨이 죽은 이미지참조 제거 (2026-07-16: imgs/img_in_image_box_*.jpg → UI 404) ----

def test_strip_dead_image_refs():
    """게이트웨이가 넣는 imgs/ 상대경로 이미지 참조는 게이트웨이 서버에만 존재 → 제거.
    <img> 태그·마크다운 이미지·맨몸 경로 모두. 표/텍스트 내용은 보존."""
    md = ('<table><tr><td>등록번호 <img src="imgs/img_in_image_box_462_887.jpg" alt="Image" /></td>'
          '<td>195511-</td></tr></table>\n\n'
          '전주지방법원 전주등기소\n\n'
          'imgs/img_in_image_box_673_806.jpg\n\n'
          '![Image](imgs/img_in_image_box_1_2.jpg)\n\n'
          '※ 등기필정보 사용방법')
    out = paddle_gw._strip_gateway_image_refs(md)
    assert "img_in_image_box" not in out
    assert "imgs/" not in out
    assert "등록번호" in out and "195511-" in out   # 표 내용 보존
    assert "전주지방법원 전주등기소" in out
    assert "등기필정보 사용방법" in out
    assert "<table>" in out and "</table>" in out   # 표 구조 보존


def test_strip_keeps_real_content_untouched():
    md = "# 제목\n\n<table><tr><td>정상 표</td></tr></table>\n\n본문 텍스트"
    assert paddle_gw._strip_gateway_image_refs(md) == md


# ── Plan B-2 (2026-08-04): page_numbers — 스캔 페이지만 렌더·전송 ──────────────────
def test_page_numbers_filters_rendered_pages(monkeypatch):
    """부분집합을 주면 그 페이지만 게이트웨이로 간다(문서 절대 page_number 유지)."""
    monkeypatch.setenv("KBP_PADDLE_OCR_GATEWAY_URL", "https://gw/ocr/paddleocr_vl")
    seen = {}

    def fake_render(fb, page_numbers=None):
        seen["page_numbers"] = page_numbers
        pnos = sorted(page_numbers) if page_numbers is not None else [1, 2, 3]
        return [_RP(n) for n in pnos]

    monkeypatch.setattr(paddle_gw, "_render_pages", fake_render)
    monkeypatch.setattr(paddle_gw, "_post_page",
                        lambda jpeg, name: (f"{name} 본문", [], None))
    pages = paddle_gw.run_paddle_gateway(b"%PDF", "a.pdf", page_numbers={2, 5})
    assert seen["page_numbers"] == {2, 5}
    assert [p["page_number"] for p in pages] == [2, 5], "문서 절대 번호 유지"


def test_page_numbers_none_renders_all(monkeypatch):
    """None 이면 현행대로 전 페이지(하위호환)."""
    monkeypatch.setenv("KBP_PADDLE_OCR_GATEWAY_URL", "https://gw/ocr/paddleocr_vl")
    seen = {}

    def fake_render(fb, page_numbers=None):
        seen["page_numbers"] = page_numbers
        return [_RP(1), _RP(2)]

    monkeypatch.setattr(paddle_gw, "_render_pages", fake_render)
    monkeypatch.setattr(paddle_gw, "_post_page",
                        lambda jpeg, name: (f"{name} 본문", [], None))
    pages = paddle_gw.run_paddle_gateway(b"%PDF", "a.pdf")
    assert seen["page_numbers"] is None
    assert [p["page_number"] for p in pages] == [1, 2]


def test_probe_uses_first_rendered_page_of_subset(monkeypatch):
    """프로브는 렌더된 **첫 원소**를 쓴다 — 부분집합이면 그 집합의 첫 페이지다."""
    monkeypatch.setenv("KBP_PADDLE_OCR_GATEWAY_URL", "https://gw/ocr/paddleocr_vl")
    monkeypatch.setattr(paddle_gw, "_render_pages",
                        lambda fb, page_numbers=None: [_RP(7), _RP(9)])
    calls = []

    def boom(jpeg, name):
        calls.append(name)
        raise RuntimeError("gateway down")

    monkeypatch.setattr(paddle_gw, "_post_page", boom)
    with pytest.raises(RuntimeError):
        paddle_gw.run_paddle_gateway(b"%PDF", "a.pdf", page_numbers={7, 9})
    assert calls == ["page-7.jpeg"], "부분집합의 첫 페이지로 프로브하고 즉시 포기"


def test_empty_page_numbers_returns_empty(monkeypatch):
    """빈 set → 렌더 0장 → 빈 리스트. 게이트웨이를 부르지 않는다."""
    monkeypatch.setenv("KBP_PADDLE_OCR_GATEWAY_URL", "https://gw/ocr/paddleocr_vl")
    monkeypatch.setattr(paddle_gw, "_render_pages",
                        lambda fb, page_numbers=None: [])
    called = []
    monkeypatch.setattr(paddle_gw, "_post_page",
                        lambda j, n: called.append(n) or ("", [], None))
    assert paddle_gw.run_paddle_gateway(b"%PDF", "a.pdf", page_numbers=set()) == []
    assert called == []
