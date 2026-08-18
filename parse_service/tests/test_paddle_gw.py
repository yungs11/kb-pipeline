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

    def fake_post(jpeg, name, opts=None):
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

    def fake_post(jpeg, name, opts=None):
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

    def boom(jpeg, name, opts=None):
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
                        lambda jpeg, name, opts=None: (f"{name} 본문", [], None))
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
                        lambda jpeg, name, opts=None: (f"{name} 본문", [], None))
    pages = paddle_gw.run_paddle_gateway(b"%PDF", "a.pdf")
    assert seen["page_numbers"] is None
    assert [p["page_number"] for p in pages] == [1, 2]


def test_probe_uses_first_rendered_page_of_subset(monkeypatch):
    """프로브는 렌더된 **첫 원소**를 쓴다 — 부분집합이면 그 집합의 첫 페이지다."""
    monkeypatch.setenv("KBP_PADDLE_OCR_GATEWAY_URL", "https://gw/ocr/paddleocr_vl")
    monkeypatch.setattr(paddle_gw, "_render_pages",
                        lambda fb, page_numbers=None: [_RP(7), _RP(9)])
    calls = []

    def boom(jpeg, name, opts=None):
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
                        lambda j, n, opts=None: called.append(n) or ("", [], None))
    assert paddle_gw.run_paddle_gateway(b"%PDF", "a.pdf", page_numbers=set()) == []
    assert called == []


# ══════════════════════════════════════════════════════════════════════════════
# 앵커 9-b — **6-key 계약의 *생산자* 검증**(2026-08-12 Phase 2a 재통합, 신설)
#
# 왜 별도 앵커인가: `test_parser_pdf_routing.py` 의 게이트 앵커들은
# `run_paddle_gateway` 를 통째로 monkeypatch 하므로 **픽스처가 status 를 넣어준다** —
# `paddle_gw.py` 를 안 고쳐도 초록이다. 반대편 실패(HEAD 판 채택 → layout/page_size 소실)는
# `hybrid_vl=0`·`vl_extra_tables=0` 이라는 **정상처럼 보이는 로그**로만 나타나고,
# 유일한 실측 그물 V7 은 "hybrid 가 사문일 수 있다" 고 유보해 소실과 사문을 구분하지 못한다.
# 그래서 실물 `run_paddle_gateway` 로 7키를 직접 단언한다.
# ══════════════════════════════════════════════════════════════════════════════
# v7(§A.4) — `gw2_meta` 추가로 6키→7키. `elapsed_ms` 추가로 7키→8키(페이지별 처리시간).
# 2026-08-18 재설계: gw1/gw2 이원화 폐지 — 항상 옵션 포함 1회 호출 + `_apply_gw2_block_content`
# 반영. `gw2_meta`는 이제 "트리거 여부"가 아니라 "이미지형 블록 content 반영 결과"
# 카운터({"outcome":"ok","blocks_total","blocks_applied","blocks_already_present",
# "blocks_empty"}, 비활성 시 {"outcome":"disabled_by_env"})를 담는다.
_EIGHT_KEYS = {"page_number", "blocks", "layout", "page_size", "status", "error",
               "gw2_meta", "elapsed_ms"}


def test_page_dict_carries_six_key_contract_on_success(monkeypatch):
    """정상 페이지 — 8키 전부 + `status == "ok"`.

    `layout`/`page_size` 는 `_hybrid_scan_pages` 의 `pg.get("layout")`·`_has_visual` 이 쓴다
    (빠지면 hybrid 가 **영구 거짓**이 되어 통째로 죽는다).
    `status` 는 `_parse_routed` 의 demote 판정이 쓴다(빠지면 게이트웨이 개별 페이지 실패가
    demote 도 VL 도 못 받고 게이트 EMPTY→quarantine 으로 **빈 페이지**가 된다).
    """
    monkeypatch.setenv("KBP_PADDLE_OCR_GATEWAY_URL", "https://gw/ocr/paddleocr_vl")
    monkeypatch.setattr(paddle_gw, "_render_pages",
                        lambda fb, page_numbers=None: [_RP(1)])
    monkeypatch.setattr(paddle_gw, "_post_page",
                        lambda jpeg, name, opts=None: ("본문 텍스트", [{"label": "text"}], (800, 600)))
    (pg,) = paddle_gw.run_paddle_gateway(b"%PDF", "a.pdf")
    assert set(pg) == _EIGHT_KEYS, f"8키 계약 위반: {sorted(pg)}"
    assert pg["status"] == "ok" and pg["error"] == ""
    assert pg["layout"] == [{"label": "text"}] and pg["page_size"] == (800, 600)
    assert pg["blocks"], "정상 페이지는 blocks 가 있다"
    assert pg["gw2_meta"] == {"outcome": "ok", "blocks_total": 0, "blocks_applied": 0,
                              "blocks_already_present": 0, "blocks_empty": 0}
    assert isinstance(pg["elapsed_ms"], float) and pg["elapsed_ms"] >= 0


def test_page_dict_carries_six_key_contract_on_page_error(monkeypatch):
    """개별 페이지 예외 — 8키 유지 + `status == "error"` + 사유 보존 + elapsed_ms 도 남는다.

    프로브(첫 페이지)가 아닌 페이지의 실패는 **레인 포기가 아니다** — 그 페이지만
    `status="error"` 로 표시하고 나머지는 계속 간다.
    """
    monkeypatch.setenv("KBP_PADDLE_OCR_GATEWAY_URL", "https://gw/ocr/paddleocr_vl")
    monkeypatch.setattr(paddle_gw, "_render_pages",
                        lambda fb, page_numbers=None: [_RP(1), _RP(2)])

    def flaky(jpeg, name, opts=None):
        if "page-2" in name:
            raise TimeoutError("poll timed out")
        return ("정상 p1", [], None)

    monkeypatch.setattr(paddle_gw, "_post_page", flaky)
    p1, p2 = paddle_gw.run_paddle_gateway(b"%PDF", "a.pdf")
    assert set(p2) == _EIGHT_KEYS, f"실패 페이지도 8키를 유지한다: {sorted(p2)}"
    assert p2["status"] == "error" and "TimeoutError" in p2["error"]
    assert isinstance(p2["elapsed_ms"], float) and p2["elapsed_ms"] >= 0
    assert p2["blocks"] == [] and p2["layout"] == [] and p2["page_size"] is None
    assert p1["status"] == "ok", "다른 페이지는 영향 없다(레인 포기 아님)"


# ── gw1/gw2 이원화 폐지(2026-08-18 재설계) — 항상 옵션 포함 1회 호출 ────────────

def test_always_calls_once_with_options(monkeypatch):
    """더 이상 트리거 판단이 없다 — 이미지형 블록 유무와 무관하게 항상 옵션 포함 1회 호출."""
    monkeypatch.setenv("KBP_PADDLE_OCR_GATEWAY_URL", "https://gw/ocr/paddleocr_vl")
    monkeypatch.setattr(paddle_gw, "_render_pages",
                        lambda fb, page_numbers=None: [_RP(1)])
    calls = []
    monkeypatch.setattr(paddle_gw, "_post_page",
                        lambda jpeg, name, opts=None: (calls.append(opts) or ("본문", [], (800, 600))))
    paddle_gw.run_paddle_gateway(b"%PDF", "a.pdf")
    assert len(calls) == 1, "gw 는 페이지당 정확히 1회만 호출된다"
    assert calls[0] == {"use_ocr_for_image_block": True, "use_chart_recognition": True}


def test_gw2_disabled_by_env_calls_without_options(monkeypatch):
    """KBP_GW_IMAGE_OCR_ENABLE=0 이면 옵션 없이 1회만 호출하고 block_content 반영도 안 한다
    (탈출구 — 기존 pre-gw2 동작으로 완전 복귀)."""
    monkeypatch.setenv("KBP_PADDLE_OCR_GATEWAY_URL", "https://gw/ocr/paddleocr_vl")
    monkeypatch.setenv("KBP_GW_IMAGE_OCR_ENABLE", "0")
    monkeypatch.setattr(paddle_gw, "_render_pages",
                        lambda fb, page_numbers=None: [_RP(1)])
    small_figure = [{"block_label": "figure", "block_bbox": [0, 0, 10, 10],
                     "block_content": "인식된 내용"}]
    calls = []

    def fake_post(jpeg, name, opts=None):
        calls.append(opts)
        return ("![](x_images/imageFile1.png)", small_figure, (800, 600))

    monkeypatch.setattr(paddle_gw, "_post_page", fake_post)
    (pg,) = paddle_gw.run_paddle_gateway(b"%PDF", "a.pdf")
    assert len(calls) == 1 and calls[0] is None, "env 로 꺼지면 옵션 없이 1회만 호출"
    assert pg["gw2_meta"] == {"outcome": "disabled_by_env"}
    text = "\n".join(b.get("text") or "" for b in pg["blocks"])
    assert "인식된 내용" not in text, "비활성 시 block_content 는 반영되지 않는다"
    assert "imageFile1.png" not in text, "안전망 strip 은 비활성 시에도 항상 실행된다"


# ── `_apply_gw2_block_content`(2026-08-18 재설계) — 존재 확인 기반 반영 ─────────

def test_apply_gw2_block_content_drops_empty_content():
    """block_content 가 계속 비어있는 블록(인식 실패)은 버려진다 — 별도 처리 없음."""
    md = "본문\n\n<img src=\"imgs/x.jpg\" />\n\n뒷문장"
    layout = [{"block_label": "image", "block_content": ""}]
    new_md, meta = paddle_gw._apply_gw2_block_content(md, layout)
    assert new_md == md, "빈 content 는 md 를 바꾸지 않는다(안전망 strip 이 나중에 참조만 제거)"
    assert meta == {"outcome": "ok", "blocks_total": 1, "blocks_applied": 0,
                    "blocks_already_present": 0, "blocks_empty": 1}


def test_apply_gw2_block_content_ignores_labels_outside_trigger_set():
    """`_IMAGE_TRIGGER_LABELS` 밖의 라벨(예: seal — 실측 I56)은 이 함수가 아예 건드리지
    않는다. 그래도 원래 이미지참조는 안전망(`_strip_gateway_image_refs`)이 별도로 지운다
    (이 함수의 책임 밖 — run_paddle_gateway 통합 테스트에서 확인)."""
    md = '본문\n\n<img src="imgs/seal.jpg" />\n\n뒷문장'
    layout = [{"block_label": "seal", "block_content": ""}]
    new_md, meta = paddle_gw._apply_gw2_block_content(md, layout)
    assert new_md == md
    assert meta == {"outcome": "ok", "blocks_total": 0, "blocks_applied": 0,
                    "blocks_already_present": 0, "blocks_empty": 0}


def test_apply_gw2_block_content_skips_when_already_present():
    """게이트웨이가 이미 본문에 자체 병합한 경우(I18/I56 실측) 중복 삽입하지 않는다."""
    md = '본문\n\n<img src="imgs/x.jpg" />\n\n갑 제3-4호증\n\n뒷문장'
    layout = [{"block_label": "image", "block_content": "갑 제3-4호증"}]
    new_md, meta = paddle_gw._apply_gw2_block_content(md, layout)
    assert new_md == md, "이미 반영된 내용은 다시 append 하지 않는다"
    assert meta == {"outcome": "ok", "blocks_total": 1, "blocks_applied": 0,
                    "blocks_already_present": 1, "blocks_empty": 0}


def test_apply_gw2_block_content_appends_when_absent():
    """header_image/footer_image 처럼 게이트웨이가 본문 스트림에 아예 안 섞어준 경우 append."""
    md = "본문\n\n뒷문장"
    layout = [{"block_label": "header_image", "block_content": "General Counseler 2030"}]
    new_md, meta = paddle_gw._apply_gw2_block_content(md, layout)
    assert new_md == "본문\n\n뒷문장\n\nGeneral Counseler 2030"
    assert meta == {"outcome": "ok", "blocks_total": 1, "blocks_applied": 1,
                    "blocks_already_present": 0, "blocks_empty": 0}


def test_apply_gw2_block_content_whitespace_normalized_match():
    """게이트웨이 자체 병합 시 개행/공백이 원문과 달라도(포맷 차이) 중복 삽입하지 않는다."""
    md = "본문\n\n갑   제3-4호증\n\n뒷문장"   # 공백이 더 들어간 형태로 이미 병합됨
    layout = [{"block_label": "image", "block_content": "갑 제3-4호증"}]
    new_md, meta = paddle_gw._apply_gw2_block_content(md, layout)
    assert new_md == md
    assert meta["blocks_already_present"] == 1 and meta["blocks_applied"] == 0


def test_apply_gw2_block_content_mixed_blocks_each_handled_independently():
    """실측(I56) 재현 — 한 페이지에 image(빈값)+image(이미 반영) 가 함께 있으면 각각
    독립적으로 처리된다: 하나는 버려지고 다른 하나는 중복 삽입되지 않는다."""
    md = '본문\n\n<img src="imgs/x.jpg" />\n\n갑 제3-4호증\n\n뒷문장'
    layout = [
        {"block_label": "image", "block_content": ""},
        {"block_label": "image", "block_content": "갑 제3-4호증"},
    ]
    new_md, meta = paddle_gw._apply_gw2_block_content(md, layout)
    assert new_md == md
    assert meta == {"outcome": "ok", "blocks_total": 2, "blocks_applied": 0,
                    "blocks_already_present": 1, "blocks_empty": 1}


def test_apply_gw2_block_content_no_image_blocks_is_noop():
    md = "본문 텍스트"
    new_md, meta = paddle_gw._apply_gw2_block_content(md, [{"block_label": "text"}])
    assert new_md == md
    assert meta == {"outcome": "ok", "blocks_total": 0, "blocks_applied": 0,
                    "blocks_already_present": 0, "blocks_empty": 0}


# ── run_paddle_gateway 통합 — block_content 반영 + 안전망 strip 이 함께 동작 ─────

def test_run_paddle_gateway_appends_recovered_content_and_strips_ref(monkeypatch):
    """header_image 류 — block_content 는 append 되고 원래 이미지참조는 strip 된다."""
    monkeypatch.setenv("KBP_PADDLE_OCR_GATEWAY_URL", "https://gw/ocr/paddleocr_vl")
    monkeypatch.setattr(paddle_gw, "_render_pages",
                        lambda fb, page_numbers=None: [_RP(1)])
    layout = [{"block_label": "header_image", "block_content": "General Counseler 2030"}]
    monkeypatch.setattr(
        paddle_gw, "_post_page",
        lambda jpeg, name, opts=None: ("본문 텍스트", layout, (800, 600)))
    (pg,) = paddle_gw.run_paddle_gateway(b"%PDF", "a.pdf")
    text = "\n".join(b.get("text") or "" for b in pg["blocks"])
    assert "General Counseler 2030" in text
    assert pg["gw2_meta"] == {"outcome": "ok", "blocks_total": 1, "blocks_applied": 1,
                              "blocks_already_present": 0, "blocks_empty": 0}


def test_run_paddle_gateway_drops_unrecognized_image_block_and_strips_ref(monkeypatch):
    """계속 인식 실패한 image 블록(빈 block_content)은 참조만 지워지고 아무 흔적도 안 남는다."""
    monkeypatch.setenv("KBP_PADDLE_OCR_GATEWAY_URL", "https://gw/ocr/paddleocr_vl")
    monkeypatch.setattr(paddle_gw, "_render_pages",
                        lambda fb, page_numbers=None: [_RP(1)])
    layout = [{"block_label": "image", "block_content": ""}]
    monkeypatch.setattr(
        paddle_gw, "_post_page",
        lambda jpeg, name, opts=None: (
            '본문\n\n<img src="imgs/x.jpg" alt="Image" />\n\n뒷문장', layout, (800, 600)))
    (pg,) = paddle_gw.run_paddle_gateway(b"%PDF", "a.pdf")
    text = "\n".join(b.get("text") or "" for b in pg["blocks"])
    assert "imgs/" not in text and "x.jpg" not in text
    assert pg["gw2_meta"] == {"outcome": "ok", "blocks_total": 1, "blocks_applied": 0,
                              "blocks_already_present": 0, "blocks_empty": 1}


def test_run_paddle_gateway_leaves_no_trace_for_label_outside_trigger_set(monkeypatch):
    """실측(I56) 재현 — `_IMAGE_TRIGGER_LABELS` 밖의 라벨(seal)도 안전망 strip 이
    참조를 지운다(이 라벨은 `_apply_gw2_block_content` 책임 밖이지만 leak 은 없어야 한다)."""
    monkeypatch.setenv("KBP_PADDLE_OCR_GATEWAY_URL", "https://gw/ocr/paddleocr_vl")
    monkeypatch.setattr(paddle_gw, "_render_pages",
                        lambda fb, page_numbers=None: [_RP(1)])
    layout = [{"block_label": "seal", "block_content": ""}]
    monkeypatch.setattr(
        paddle_gw, "_post_page",
        lambda jpeg, name, opts=None: (
            '본문\n\n<img src="imgs/seal.jpg" alt="Image" />\n\n뒷문장', layout, (800, 600)))
    (pg,) = paddle_gw.run_paddle_gateway(b"%PDF", "a.pdf")
    text = "\n".join(b.get("text") or "" for b in pg["blocks"])
    assert "imgs/" not in text and "seal.jpg" not in text
