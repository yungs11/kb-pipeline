"""Plan A §A4 — 스캔 페이지(paddle_gw) layout 기반 전면 VL 처리.

**seam 주의**: VL 절단·환각 판정은 `_ocr_elements_for_page` 를 fake 로 잡으면 안 된다 —
`ocr/__init__` 의 figure→text 재라벨을 건너뛰어 **로직이 없어도 통과**한다(거짓 초록불).
그런 테스트는 `vl_api.call_vl_api_with_base64` 레벨로 내려 실제 배선을 태운다.
"""
import json

import pytest

import parse_service.parsers.pdf as pdf_parser
import parse_service.parsers.pdf.paddle_gw as pg
from parse_service.parsers.pdf.gate import RouteDecision


# ── 헬퍼 ────────────────────────────────────────────────────────────────────
def _blk(bb, label="image"):
    return {"block_label": label, "block_bbox": bb}


def _page(pno, blocks, layout=(), page_size=(1000, 1000)):
    return {"page_number": pno, "blocks": list(blocks),
            "layout": list(layout), "page_size": page_size}


def _big(label="image"):
    """페이지의 36% — 면적 하한(5%) 통과."""
    return _blk([0, 0, 600, 600], label)


def _tiny(label="image"):
    """페이지의 0.25% — 면적 하한 미달(법원통지서 QR 0.54% 상당)."""
    return _blk([0, 0, 50, 50], label)


def _vl_els(md="본문 서술", cat="text", html="", extra=None):
    els = [{"category": cat, "content": {"html": html, "markdown": md, "text": ""},
            "page": 1}]
    return els + list(extra or [])


@pytest.fixture
def wire(monkeypatch):
    """게이트웨이/렌더/VL 을 모두 fake 로 잡고, 호출 기록을 돌려준다."""
    rec = {"vl": [], "render": []}

    class _RP:
        def __init__(self, n):
            self.page_number, self.jpeg, self.text = n, b"jpeg", ""

    def fake_render(fb, page_numbers=None, *, dpi=None):
        rec["render"].append((tuple(sorted(page_numbers or ())), dpi))
        return [_RP(n) for n in sorted(page_numbers or ())]

    monkeypatch.setattr(pdf_parser, "_render_pages", fake_render)

    def set_vl(elements_or_fn):
        # B-3: 배치 seam(복수). jobs 순서대로 결과 리스트를 돌려준다.
        def fake_batch(jobs, ocr_url=None, *, diagram=False,
                       prompt_override=None, max_tokens=None):
            out = []
            for _jpeg, name in jobs:
                rec["vl"].append({"name": name, "override": prompt_override,
                                  "max_tokens": max_tokens, "diagram": diagram})
                # 2b-1: (elements, metas) 쌍 계약
                out.append((elements_or_fn(name) if callable(elements_or_fn)
                            else list(elements_or_fn), []))
            return out
        monkeypatch.setattr(pdf_parser, "_ocr_elements_for_pages", fake_batch)
    rec["set_vl"] = set_vl
    return rec


def _run(pages, decision, rec):
    counters = {"layout_pages": 0, "visual_pages": 0, "area_guard_skipped": 0,
                "truncated": 0, "error_placeholder": 0, "vl_page_calls": 0,
                "tbl_backfill": 0, "vl_extra_tables": 0}
    pdf_parser._hybrid_scan_pages(pages, b"%PDF",
                                  set(getattr(decision, 'ocr_pages', ()) or ()),
                                  None, counters)
    return counters


# ── 판정: 면적 하한 ──────────────────────────────────────────────────────────
def test_tiny_image_does_not_trigger(wire):
    """법원통지서 p1 형: 글자 위주 스캔 + 작은 QR → paddle 유지, VL 0회."""
    wire["set_vl"](_vl_els())
    pages = [_page(1, [{"type": "text", "text": "원문"}], [_tiny()])]
    c = _run(pages, RouteDecision(lane="paddle_gw", ocr_pages=(1,)), wire)
    assert wire["vl"] == []
    assert pages[0]["blocks"] == [{"type": "text", "text": "원문"}]
    assert c["visual_pages"] == 0 and c["layout_pages"] == 1


def test_big_image_triggers(wire):
    wire["set_vl"](_vl_els("순서도: A → B → C"))
    pages = [_page(1, [{"type": "text", "text": "조각"}], [_big()])]
    c = _run(pages, RouteDecision(lane="paddle_gw", ocr_pages=(1,)), wire)
    assert c["vl_page_calls"] == 1 and c["visual_pages"] == 1
    assert any("A → B → C" in (b.get("text") or "") for b in pages[0]["blocks"])


def test_chart_label_also_uses_area_floor(wire):
    """chart 도 면적 하한을 적용한다(라벨별 예외 없음)."""
    wire["set_vl"](_vl_els())
    pages = [_page(1, [{"type": "text", "text": "원문"}], [_tiny("chart")])]
    _run(pages, RouteDecision(lane="paddle_gw", ocr_pages=(1,)), wire)
    assert wire["vl"] == []


def test_page_size_missing_is_fail_closed(wire):
    """면적 미상 → fail-closed(현행 유지) + 계측."""
    wire["set_vl"](_vl_els())
    pages = [_page(1, [{"type": "text", "text": "원문"}], [_big()], page_size=None)]
    c = _run(pages, RouteDecision(lane="paddle_gw", ocr_pages=(1,)), wire)
    assert wire["vl"] == [] and c["area_guard_skipped"] == 1


def test_zero_page_size_is_fail_closed(wire):
    """width/height 가 "0" 문자열이어도 ZeroDivisionError 없이 fail-closed."""
    wire["set_vl"](_vl_els())
    pages = [_page(1, [{"type": "text", "text": "원문"}], [_big()], page_size=("0", "0"))]
    c = _run(pages, RouteDecision(lane="paddle_gw", ocr_pages=(1,)), wire)
    assert wire["vl"] == [] and c["area_guard_skipped"] == 1


def test_unknown_and_missing_labels_do_not_raise(wire):
    wire["set_vl"](_vl_els())
    layout = [{"score": 0.9}, {"block_label": None}, _blk([0, 0, 600, 600], "SEAL"), _big()]
    pages = [_page(1, [{"type": "text", "text": "x"}], layout)]
    c = _run(pages, RouteDecision(lane="paddle_gw", ocr_pages=(1,)), wire)
    assert c["vl_page_calls"] == 1          # 미지 라벨은 무시, big image 로 발동


def test_label_case_is_normalized(wire):
    wire["set_vl"](_vl_els())
    pages = [_page(1, [{"type": "text", "text": "x"}], [_blk([0, 0, 600, 600], "Image")])]
    c = _run(pages, RouteDecision(lane="paddle_gw", ocr_pages=(1,)), wire)
    assert c["vl_page_calls"] == 1


# ── 대상 한정(§A0) ──────────────────────────────────────────────────────────
def test_native_page_excluded_even_with_big_image(wire):
    """gate 는 스캔 1장으로 문서 전체를 paddle 로 보낸다 — 네이티브 페이지는 대상 아님."""
    wire["set_vl"](_vl_els())
    pages = [_page(1, [{"type": "text", "text": "네이티브"}], [_big()]),
             _page(2, [{"type": "text", "text": "스캔"}], [_big()])]
    _run(pages, RouteDecision(lane="paddle_gw", ocr_pages=(2,)), wire)
    assert [v["name"] for v in wire["vl"]] == ["page-2-hybrid.jpeg"]


def test_empty_ocr_pages_skips_everything(wire):
    """구버전 gate(ocr_pages 없음) → 전체 no-op."""
    wire["set_vl"](_vl_els())
    pages = [_page(1, [{"type": "text", "text": "x"}], [_big()])]
    _run(pages, RouteDecision(lane="paddle_gw"), wire)
    assert wire["vl"] == [] and wire["render"] == []


def test_no_layout_is_noop(wire):
    """구버전 게이트웨이(layout 없음) → 현행 동작 그대로."""
    wire["set_vl"](_vl_els())
    pages = [_page(1, [{"type": "text", "text": "원문"}], [])]
    _run(pages, RouteDecision(lane="paddle_gw", ocr_pages=(1,)), wire)
    assert wire["vl"] == [] and pages[0]["blocks"] == [{"type": "text", "text": "원문"}]


# ── 렌더 ────────────────────────────────────────────────────────────────────
def test_render_called_once_with_subset_and_dpi(wire, monkeypatch):
    monkeypatch.setenv("KBP_VL_PAGE_DPI", "200")
    wire["set_vl"](_vl_els())
    pages = [_page(n, [{"type": "text", "text": "x"}], [_big()]) for n in (1, 2, 3)]
    _run(pages, RouteDecision(lane="paddle_gw", ocr_pages=(1, 2, 3)), wire)
    assert wire["render"] == [((1, 2, 3), 200)], "페이지마다 부르면 O(n^2) 회귀"


def test_render_failure_keeps_paddle(wire, monkeypatch):
    monkeypatch.setattr(pdf_parser, "_render_pages",
                        lambda fb, page_numbers=None, *, dpi=None: [])
    wire["set_vl"](_vl_els())
    pages = [_page(1, [{"type": "text", "text": "원문"}], [_big()])]
    _run(pages, RouteDecision(lane="paddle_gw", ocr_pages=(1,)), wire)
    assert wire["vl"] == [] and pages[0]["blocks"] == [{"type": "text", "text": "원문"}]


# ── 프롬프트·max_tokens 배선 ─────────────────────────────────────────────────
def test_uses_page_hybrid_prompt_and_max_tokens(wire, monkeypatch):
    monkeypatch.setenv("KBP_VL_PAGE_MAX_TOKENS", "8000")
    from parse_service.parsers.ocr import prompts
    wire["set_vl"](_vl_els())
    pages = [_page(1, [{"type": "text", "text": "x"}], [_big()])]
    _run(pages, RouteDecision(lane="paddle_gw", ocr_pages=(1,)), wire)
    call = wire["vl"][0]
    assert call["override"] == (prompts.PAGE_HYBRID_SYSTEM_PROMPT,
                                prompts.PAGE_HYBRID_USER_PROMPT)
    assert call["max_tokens"] == 8000
    assert call["diagram"] is False


def test_max_tokens_reaches_payload(monkeypatch):
    """배선 전 구간 앵커 — seam 을 vl_api._request_vl_api 로 내려 payload 를 관측한다.

    `call_vl_api_with_base64` 를 patch 하면 `_build_payload` 가 그 안에서 호출되므로
    payload 자체가 생성되지 않는다.
    """
    from parse_service.parsers.ocr import vl_api, image_utils
    seen = {}

    async def fake_request(payload):
        seen["payload"] = payload
        return {"choices": [{"message": {"content": json.dumps({"elements": []})}}]}, 0.1

    monkeypatch.setattr(vl_api, "_request_vl_api", fake_request)
    monkeypatch.setattr(image_utils, "image_file_to_base64_list", lambda p: ["QUJD"])
    pdf_parser._ocr_elements_for_page(b"jpeg", "p.jpeg", None,
                                      prompt_override=("s", "u"), max_tokens=8000)
    assert seen["payload"]["max_tokens"] == 8000


# ── 표 처리 ─────────────────────────────────────────────────────────────────
def test_paddle_tables_survive_and_vl_tables_dropped(wire):
    """표 정본은 paddle. VL 표는 버리고 paddle 표는 원래 순서대로 남긴다."""
    wire["set_vl"](_vl_els("서술", extra=[
        {"category": "table", "content": {"html": "<table><tr><td>VL</td></tr></table>",
                                          "markdown": "", "text": ""}, "page": 1}]))
    paddle = [{"type": "table", "table_body": "<table><tr><td>PADDLE</td></tr></table>"},
              {"type": "text", "text": "조각"}]
    pages = [_page(1, paddle, [_big()])]
    c = _run(pages, RouteDecision(lane="paddle_gw", ocr_pages=(1,)), wire)
    tables = [b for b in pages[0]["blocks"] if b["type"] == "table"]
    assert len(tables) == 1 and "PADDLE" in tables[0]["table_body"]
    assert c["vl_extra_tables"] == 1 and c["tbl_backfill"] == 1


def test_vl_table_adopted_when_paddle_has_none(wire):
    """paddle 표가 0개면 VL 표를 채택한다(표 소실 방지 안전망)."""
    wire["set_vl"](_vl_els("서술", extra=[
        {"category": "table", "content": {"html": "<table><tr><td>VL</td></tr></table>",
                                          "markdown": "", "text": ""}, "page": 1}]))
    pages = [_page(1, [{"type": "text", "text": "조각"}], [_big()])]
    _run(pages, RouteDecision(lane="paddle_gw", ocr_pages=(1,)), wire)
    tables = [b for b in pages[0]["blocks"] if b["type"] == "table"]
    assert len(tables) == 1 and "VL" in tables[0]["table_body"]


def test_prose_survives_when_markdown_mixes_table(wire):
    """v1 의 산문 소실 버그 재발 방지 — hybrid_to_blocks 분할 앵커.

    elements_to_blocks 로 되돌리면 산문+표가 통짜 text 블록 1개가 되고, 표 drop 규칙이
    페이지 본문을 통째로 지운다.
    """
    md = "첫 문단 산문입니다.\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n둘째 문단 산문입니다."
    wire["set_vl"](_vl_els(md))
    paddle = [{"type": "table", "table_body": "<table><tr><td>P</td></tr></table>"}]
    pages = [_page(1, paddle, [_big()])]
    _run(pages, RouteDecision(lane="paddle_gw", ocr_pages=(1,)), wire)
    texts = " ".join(b.get("text") or "" for b in pages[0]["blocks"])
    assert "첫 문단 산문입니다." in texts and "둘째 문단 산문입니다." in texts


def test_figure_with_html_keeps_its_prose(wire):
    """figure+html 은 표만 규칙대로 처리하고 산문은 반드시 살린다(빈 image 블록 미생성)."""
    wire["set_vl"]([{"category": "figure",
                     "content": {"html": "<table><tr><td>H</td></tr></table>",
                                 "markdown": "그림 옆 산문", "text": ""}, "page": 1}])
    paddle = [{"type": "table", "table_body": "<table><tr><td>P</td></tr></table>"}]
    pages = [_page(1, paddle, [_big()])]
    _run(pages, RouteDecision(lane="paddle_gw", ocr_pages=(1,)), wire)
    blocks = pages[0]["blocks"]
    assert any("그림 옆 산문" in (b.get("text") or "") for b in blocks)
    assert not any(b["type"] == "image" and not b.get("img_path") for b in blocks)


def test_heading_not_duplicated(wire):
    """PAGE_HYBRID 는 전면 전사라 VL 이 제목을 낸다 — paddle heading 을 승계하면 2개가 된다."""
    wire["set_vl"](_vl_els("# 6. 요구사항\n\n본문"))
    paddle = [{"type": "text", "text": "6. 요구사항", "text_level": 1},
              {"type": "text", "text": "조각"}]
    pages = [_page(1, paddle, [_big()])]
    _run(pages, RouteDecision(lane="paddle_gw", ocr_pages=(1,)), wire)
    headings = [b for b in pages[0]["blocks"] if b.get("text_level")]
    assert len(headings) == 1


# ── 실패 판정 ────────────────────────────────────────────────────────────────
def test_vl_exception_keeps_paddle(wire, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("VL down")
    monkeypatch.setattr(pdf_parser, "_ocr_elements_for_pages", boom)
    pages = [_page(1, [{"type": "text", "text": "원문"}], [_big()])]
    _run(pages, RouteDecision(lane="paddle_gw", ocr_pages=(1,)), wire)
    assert pages[0]["blocks"] == [{"type": "text", "text": "원문"}]


def test_empty_vl_result_keeps_paddle(wire):
    wire["set_vl"]([])
    pages = [_page(1, [{"type": "text", "text": "원문"}], [_big()])]
    _run(pages, RouteDecision(lane="paddle_gw", ocr_pages=(1,)), wire)
    assert pages[0]["blocks"] == [{"type": "text", "text": "원문"}]


@pytest.mark.parametrize("raw,kind", [
    ("[Error: Failed to parse API response - x]", "error_placeholder"),
    ('{"elements": [{"category": "text", "content": {"mark', "truncated"),
    ('```json\n{"elements": [{"category": "text", "content": {"mark', "truncated"),
])
def test_fake_success_forms_keep_paddle(wire, raw, kind):
    """예외도 빈 결과도 아닌 '성공처럼 보이는 실패' 세 형태.

    ```json 펜스 케이스가 핵심 — elements_parser 는 파싱 전 펜스를 벗기지만 실패 시
    fallback 에는 **원문(펜스 포함)** 을 넣는다. 펜스를 안 벗기고 판정하면 놓친다.
    """
    wire["set_vl"](_vl_els(raw))
    pages = [_page(1, [{"type": "text", "text": "원문"}], [_big()])]
    c = _run(pages, RouteDecision(lane="paddle_gw", ocr_pages=(1,)), wire)
    assert pages[0]["blocks"] == [{"type": "text", "text": "원문"}]
    assert c[kind] == 1


def test_valid_json_codeblock_is_not_truncation(wire):
    """유효 JSON 코드블록 페이지를 절단으로 **오탐하지 않는다**.

    `hybrid_to_blocks` 는 펜스 코드블록을 버리므로(실측) 이 페이지는 결과적으로 blocks 가 비고
    "전부 걸러짐 → paddle 유지" 가드가 잡는다. 중요한 건 `truncated` 로 오분류되지 않는 것이다 —
    §V5 가 그 카운터를 max_tokens 상향 트리거로 쓰기 때문이다.
    """
    wire["set_vl"](_vl_els('```json\n{"a": 1}\n```'))
    pages = [_page(1, [{"type": "text", "text": "원문"}], [_big()])]
    c = _run(pages, RouteDecision(lane="paddle_gw", ocr_pages=(1,)), wire)
    assert c["truncated"] == 0 and c["error_placeholder"] == 0
    assert pages[0]["blocks"] == [{"type": "text", "text": "원문"}]


def test_prose_survives_alongside_code_block(wire):
    """코드블록 자체는 `hybrid_to_blocks` 가 버리지만 앞뒤 산문은 살아남는다(알려진 한계)."""
    wire["set_vl"](_vl_els("설명 문단\n\n```python\nx=1\n```\n\n뒷 문단"))
    pages = [_page(1, [{"type": "text", "text": "원문"}], [_big()])]
    _run(pages, RouteDecision(lane="paddle_gw", ocr_pages=(1,)), wire)
    texts = " ".join(b.get("text") or "" for b in pages[0]["blocks"])
    assert "설명 문단" in texts and "뒷 문단" in texts


def test_much_shorter_vl_output_is_accepted(wire):
    """R4 회귀 방지 — 시각 페이지에서 len(VL) << len(paddle) 은 정상이다.

    분량 비교 가드를 다시 넣으면 이 테스트가 실패한다(LICO p3: paddle 46,610자 → VL 156자).
    """
    wire["set_vl"](_vl_els("간트 요약 3줄."))
    pages = [_page(1, [{"type": "text", "text": "가" * 46610}], [_big("chart")])]
    _run(pages, RouteDecision(lane="paddle_gw", ocr_pages=(1,)), wire)
    joined = " ".join(b.get("text") or "" for b in pages[0]["blocks"])
    assert "간트 요약 3줄." in joined and len(joined) < 1000


# ── page_idx 계약 ────────────────────────────────────────────────────────────
def test_page_idx_is_one_based(wire):
    wire["set_vl"](_vl_els("서술"))
    pages = [_page(7, [{"type": "text", "text": "x"}], [_big()])]
    _run(pages, RouteDecision(lane="paddle_gw", ocr_pages=(7,)), wire)
    assert all(b.get("page_idx") == 7 for b in pages[0]["blocks"])


# ── 실 게이트웨이 응답 픽스처 ────────────────────────────────────────────────
def test_real_gateway_layout_shapes():
    """`_parse_layout` 이 실 응답 형태를 계약대로 읽는가(2026-08-02 덤프 기준)."""
    body = {"layout": [{"page_index": None, "width": 1626, "height": 1125,
                        "detection": [{"label": "chart", "score": 0.9,
                                       "coordinate": [367, 92, 1122, 1046]}],
                        "blocks": [{"block_label": "chart",
                                    "block_bbox": [367, 92, 1122, 1046],
                                    "block_content": ""}]}]}
    blocks, size = pg._parse_layout(body)
    assert size == (1626, 1125) and blocks[0]["block_label"] == "chart"

    # blocks 가 비면 detection 을 정규화해 대체 — **bbox 도 함께** 정규화해야 면적 하한이 산다
    body["layout"][0]["blocks"] = []
    blocks, size = pg._parse_layout(body)
    assert blocks == [{"block_label": "chart", "block_bbox": [367, 92, 1122, 1046]}]

    # 구버전/형식이상 → ([], None)
    assert pg._parse_layout({}) == ([], None)
    assert pg._parse_layout({"layout": "nope"}) == ([], None)


# ── 관측: 삼킨 VL 실패가 attempts 에 남는가 (2026-08-14) ─────────────────────
def test_hybrid_records_swallowed_vl_failure_in_attempts(wire):
    """hybrid 가 VL 실패를 삼킬 때 **반드시 발자국을 남긴다**.

    이 함수는 실패 시 paddle 산출물을 조용히 유지한다. 그런데 V6-③ 실측상
    `source=empty` 가 나오는 경로가 바로 여기다(hybrid 로 못 살린 스캔 페이지가
    게이트 quarantine 으로 간다). 기록이 없으면 2b-2 의 실패 규칙
    ("error 이면서 empty 면 문서 실패")이 **정작 실패하는 자리에서만 발동하지
    않는다** — 실측에서 그 페이지들이 `attempts=[]` 였다.
    """
    seen: list[tuple] = []
    wire["set_vl"](lambda _n: [])          # VL 이 elements 를 못 냄 → empty_result
    pages = [_page(1, [{"type": "text", "text": "원문"}], [_big()])]
    counters = {"layout_pages": 0, "visual_pages": 0, "area_guard_skipped": 0,
                "truncated": 0, "error_placeholder": 0, "vl_page_calls": 0,
                "tbl_backfill": 0, "vl_extra_tables": 0}
    pdf_parser._hybrid_scan_pages(
        pages, b"%PDF", {1}, None, counters,
        att=lambda pno, stage, outcome, meta=None: seen.append((pno, stage, outcome)))

    assert seen, "VL 실패를 삼켰는데 attempts 가 비어 있다 — 2b-2 규칙이 무력화된다"
    assert seen[0][0] == 1 and seen[0][1] == "hybrid_vl"
    assert seen[0][2] != "ok", f"실패인데 ok 로 기록됐다: {seen}"
    assert pages[0]["blocks"] == [{"type": "text", "text": "원문"}], "동작은 불변"
