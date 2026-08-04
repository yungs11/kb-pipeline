"""ocr_file_to_elements — 이미지/pptx 를 in-process 로 VL OCR."""
import json
import pytest
from parse_service.parsers import ocr as ocr_parser


@pytest.fixture
def fake_vl(monkeypatch):
    async def fake_call(base64_image, user_prompt, system_prompt, max_tokens=None):
        return json.dumps({"elements": [
            {"category": "figure",
             "content": {"html": "", "markdown": "hello", "text": ""},
             "id": 0, "page": 1}]}), 0.1
    monkeypatch.setattr("parse_service.parsers.ocr.vl_api.call_vl_api_with_base64",
                        fake_call)


def test_image_ocr_inprocess(monkeypatch, fake_vl):
    monkeypatch.setattr("parse_service.parsers.ocr.image_utils.image_file_to_base64_list",
                        lambda path, page_range=None: ["QUJD"])
    import asyncio
    res = asyncio.run(ocr_parser.ocr_file_to_elements(b"\x89PNG-fake", "img.png"))
    assert res["metadata"]["page_cnt"] == 1
    assert res["elements"][0]["content"]["markdown"] == "hello"
    assert res["elements"][0]["page_idx"] == 0  # elements_to_blocks 규약


def test_parse_uses_inprocess(monkeypatch, fake_vl):
    monkeypatch.setattr("parse_service.parsers.ocr.image_utils.image_file_to_base64_list",
                        lambda path, page_range=None: ["QUJD"])
    res = ocr_parser.parse(b"\x89PNG-fake", "img.png")   # ocr_url 파라미터 없이 동작
    assert res.kind == "pages" and res.pages[0]["page_number"] == 1


def test_text_only_figure_becomes_text_block(monkeypatch, fake_vl):
    """순수 텍스트 figure 는 text 블록으로 — markdown 이 enriched 로 흐른다(스택 검증 결함 회귀)."""
    monkeypatch.setattr("parse_service.parsers.ocr.image_utils.image_file_to_base64_list",
                        lambda path, page_range=None: ["QUJD"])
    res = ocr_parser.parse(b"\x89PNG-fake", "img.png")
    blocks = res.pages[0]["blocks"]
    assert blocks and blocks[0]["type"] == "text" and blocks[0]["text"] == "hello"


def test_http_client_rebinds_across_event_loops():
    """asyncio.run 마다 새 루프 — 클라이언트가 죽은 루프에 묶여 있으면 재생성해야 한다."""
    import asyncio
    from parse_service.parsers.ocr import vl_api

    async def _get():
        return vl_api.get_http_client()

    c1 = asyncio.run(_get())
    c2 = asyncio.run(_get())
    assert c2 is not c1 and not c2.is_closed
