"""MinerU 레인 — content_list→elements→blocks→pages 매핑 + in-process 호출 배선."""
import json
import os

import pytest

from parse_service.parsers.pdf import mineru_lane


def test_content_list_maps_to_pages_preserving_table_html():
    content_list = [
        {"type": "title", "text": "제목", "page_idx": 0},
        {"type": "text", "text": "본문 문단", "page_idx": 0},
        {"type": "table", "table_body": "<table><tr><td>셀</td></tr></table>", "page_idx": 0},
        {"type": "text", "text": "둘째 페이지 텍스트", "page_idx": 1},
        {"type": "image", "img_path": "imgs/p2.jpg", "page_idx": 1},
    ]
    pages = mineru_lane._elements_to_pages(
        mineru_lane._content_list_to_elements(content_list))
    assert [p["page_number"] for p in pages] == [1, 2]          # 0-based→1-based
    # 표 HTML 원형 보존(불변식)
    tbl = next(b for b in pages[0]["blocks"] if b["type"] == "table")
    assert tbl["table_body"] == "<table><tr><td>셀</td></tr></table>"
    # 블록 page_idx 는 1-based page_number 로 정규화(기존 ODL 경로와 동일)
    assert all(b["page_idx"] == 1 for b in pages[0]["blocks"])
    assert all(b["page_idx"] == 2 for b in pages[1]["blocks"])
    img = next(b for b in pages[1]["blocks"] if b["type"] == "image")
    assert img["img_path"] == "imgs/p2.jpg"


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
    assert content_list == [{"type": "text", "text": "ocr 결과", "page_idx": 0}]
