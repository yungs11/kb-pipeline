"""이식된 OCR 모듈 — VL 응답 파싱과 payload 빌드가 원본 계약대로 동작."""
import json


def test_parse_vl_response_to_elements():
    from parse_service.parsers.ocr.elements_parser import (
        parse_vision_language_response_to_elements)
    vl = json.dumps({"elements": [
        {"category": "table", "content": {"html": "<table><tr><td>x</td></tr></table>",
                                          "markdown": "", "text": ""},
         "id": 0, "page": 1}]})
    els, next_id = parse_vision_language_response_to_elements(vl, page_number=3, start_id=7)
    assert els[0]["category"] == "table"
    assert els[0]["page"] == 3 and els[0]["id"] == 7 and next_id == 8


def test_parse_vl_response_fallback_figure():
    from parse_service.parsers.ocr.elements_parser import (
        parse_vision_language_response_to_elements)
    els, _ = parse_vision_language_response_to_elements("not-json at all", 1, 0)
    assert els[0]["category"] == "figure"
    assert els[0]["content"]["markdown"] == "not-json at all"


def test_vl_payload_contains_image_and_schema(monkeypatch):
    monkeypatch.setenv("MODEL_API_URL", "http://vl.example/v1/chat/completions")
    monkeypatch.setenv("MODEL_NAME", "test-vl")
    from parse_service.parsers.ocr import vl_api
    payload = vl_api._build_payload("QUJD", "user-p", "sys-p")
    text = json.dumps(payload)
    assert "data:image/jpeg;base64,QUJD" in text and "sys-p" in text
