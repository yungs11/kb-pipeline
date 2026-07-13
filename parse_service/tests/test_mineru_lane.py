"""MinerU 레인 — content_list→elements→blocks→pages 매핑 + in-process 호출 배선."""
import json
import os

import pytest

from parse_service.parsers.pdf import mineru_lane


def test_content_list_maps_to_pages_preserving_table_html():
    # 실제 MinerU content_list(v1) 스키마: heading=text+text_level, table_body=HTML, image=img_path.
    content_list = [
        {"type": "text", "text": "제목", "text_level": 1, "page_idx": 0},  # heading
        {"type": "text", "text": "본문 문단", "page_idx": 0},
        {"type": "table", "table_body": "<table><tr><td>셀</td></tr></table>", "page_idx": 0},
        {"type": "text", "text": "둘째 페이지 텍스트", "page_idx": 1},
        {"type": "image", "img_path": "imgs/p2.jpg", "page_idx": 1},
    ]
    pages = mineru_lane._elements_to_pages(
        mineru_lane._content_list_to_elements(content_list))
    assert [p["page_number"] for p in pages] == [1, 2]          # 0-based→1-based
    # heading 은 text_level 보존(구조 유지)
    head = pages[0]["blocks"][0]
    assert head["type"] == "text" and head.get("text_level") == 1
    # 표 HTML 원형 보존(불변식)
    tbl = next(b for b in pages[0]["blocks"] if b["type"] == "table")
    assert tbl["table_body"] == "<table><tr><td>셀</td></tr></table>"
    # 블록 page_idx 는 1-based page_number 로 정규화(기존 ODL 경로와 동일)
    assert all(b["page_idx"] == 1 for b in pages[0]["blocks"])
    assert all(b["page_idx"] == 2 for b in pages[1]["blocks"])
    img = next(b for b in pages[1]["blocks"] if b["type"] == "image")
    assert img["img_path"] == "imgs/p2.jpg"


def test_chart_list_code_content_not_lost():
    """chart(순서도)·list·code 는 별도 스키마 — 내용 유실 없이 매핑돼야."""
    content_list = [
        {"type": "chart", "img_path": "imgs/c.jpg", "content": "순서도: A→B→C", "page_idx": 0},
        {"type": "list", "list_items": ["첫째 항목", "둘째 항목"], "page_idx": 0},
        {"type": "code", "code_body": "print('hi')", "page_idx": 0},
        {"type": "equation", "text": "E=mc^2", "text_format": "latex", "page_idx": 0},
    ]
    pages = mineru_lane._elements_to_pages(
        mineru_lane._content_list_to_elements(content_list))
    texts = " ".join(b.get("text", "") for b in pages[0]["blocks"])
    assert "순서도: A→B→C" in texts, "chart content 유실 금지"
    assert "첫째 항목" in texts and "둘째 항목" in texts, "list_items 유실 금지"
    assert "print('hi')" in texts, "code_body 유실 금지"
    eq = next(b for b in pages[0]["blocks"] if b["type"] == "equation")
    assert eq["latex"] == "E=mc^2"


def test_chart_without_content_falls_back_to_image():
    content_list = [{"type": "chart", "img_path": "imgs/c.jpg", "content": "", "page_idx": 0}]
    pages = mineru_lane._elements_to_pages(
        mineru_lane._content_list_to_elements(content_list))
    img = pages[0]["blocks"][0]
    assert img["type"] == "image" and img["img_path"] == "imgs/c.jpg"


def test_real_live_vlm_content_list_maps_correctly():
    """Task 8 회귀 앵커 — 라이브 MinerU2.5 VLM 이 스캔 PDF 에서 실제 반환한 content_list(2026-07-13).
    'header' 타입(페이지 헤더)·table_body HTML·page_idx 0 을 실제 관측대로 고정."""
    real = [
        {"type": "text", "text": "This is a scanned-style page with a table below.",
         "bbox": [40, 115, 283, 140], "page_idx": 0},
        {"type": "table", "img_path": "images/75.jpg", "table_caption": [], "table_footnote": [],
         "table_body": "<table><tr><td>Name</td><td>Value</td></tr><tr><td>Alpha</td><td>123</td></tr></table>",
         "bbox": [43, 246, 621, 548], "page_idx": 0},
        {"type": "header", "text": "MinerU E2E Test Document", "bbox": [40, 48, 190, 70], "page_idx": 0},
    ]
    pages = mineru_lane._elements_to_pages(mineru_lane._content_list_to_elements(real))
    assert pages[0]["page_number"] == 1
    tbl = next(b for b in pages[0]["blocks"] if b["type"] == "table")
    assert tbl["table_body"] == "<table><tr><td>Name</td><td>Value</td></tr><tr><td>Alpha</td><td>123</td></tr></table>"
    texts = " ".join(b.get("text", "") for b in pages[0]["blocks"])
    assert "scanned-style" in texts and "E2E Test Document" in texts  # header→text 유지
    assert all(b["page_idx"] == 1 for b in pages[0]["blocks"])


def test_run_mineru_uses_invoke_boundary(monkeypatch):
    seen = {}

    def fake_invoke(pdf_bytes, filename, parse_method):
        seen["method"] = parse_method
        return [{"type": "text", "text": "ocr 결과", "page_idx": 0}]

    monkeypatch.setattr(mineru_lane, "_invoke_mineru", fake_invoke)
    pages = mineru_lane.run_mineru(b"%PDF", "a.pdf", "ocr")
    assert seen["method"] == "ocr"
    assert pages[0]["page_number"] == 1 and pages[0]["blocks"]


def test_invoke_requires_server_url(monkeypatch):
    monkeypatch.delenv("MINERU_VLM_SERVER_URL", raising=False)
    with pytest.raises(RuntimeError):
        mineru_lane._invoke_mineru(b"%PDF", "a.pdf", "ocr")


def test_invoke_passes_args_and_reads_disk_content_list(monkeypatch):
    monkeypatch.setenv("MINERU_VLM_SERVER_URL", "http://vlm:8000")
    captured = {}

    # _run_mineru_do_parse 가 실제 mineru do_parse(디스크 출력)를 감싸는 경계.
    # (1) 전달 인자 검증 (2) content_list.json 을 디스크에 써서 _invoke_mineru 디스크-read 실검증.
    def fake_run(**kw):
        captured.update(kw)
        out = kw["output_dir"]
        sub = os.path.join(out, "a", "auto")
        os.makedirs(sub, exist_ok=True)
        with open(os.path.join(sub, "a_content_list.json"), "w", encoding="utf-8") as f:
            json.dump([{"type": "text", "text": "ocr 결과", "page_idx": 0}], f)
        return None  # 실제 do_parse 처럼 None 반환

    monkeypatch.setattr(mineru_lane, "_run_mineru_do_parse", fake_run)
    content_list = mineru_lane._invoke_mineru(b"%PDF", "a.pdf", "ocr")
    assert captured["backend"] == "hybrid-http-client"
    assert captured["server_url"] == "http://vlm:8000"
    assert captured["parse_method"] == "ocr"
    # p_lang_list 는 do_parse 필수 위치인자 — 누락 시 실서버 TypeError (크래시 방지 회귀).
    assert captured["p_lang_list"] == ["korean"]
    assert content_list == [{"type": "text", "text": "ocr 결과", "page_idx": 0}]


def test_invoke_passes_max_concurrency_when_set(monkeypatch):
    monkeypatch.setenv("MINERU_VLM_SERVER_URL", "http://vlm:8000")
    monkeypatch.setenv("MINERU_MAX_CONCURRENCY", "48")
    monkeypatch.setenv("MINERU_LANG", "ch")
    captured = {}

    def fake_run(**kw):
        captured.update(kw)
        out = kw["output_dir"]
        sub = os.path.join(out, "a", "hybrid_ocr")
        os.makedirs(sub, exist_ok=True)
        with open(os.path.join(sub, "a_content_list.json"), "w", encoding="utf-8") as f:
            json.dump([], f)
        return None

    monkeypatch.setattr(mineru_lane, "_run_mineru_do_parse", fake_run)
    mineru_lane._invoke_mineru(b"%PDF", "a.pdf", "ocr")
    assert captured["max_concurrency"] == 48
    assert captured["p_lang_list"] == ["ch"]  # MINERU_LANG override
