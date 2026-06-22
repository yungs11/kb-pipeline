"""Integration tests for enrich() title/footnote absorption (Philosophy A)."""
import json

from kb_pipeline.modal import enrich

OPEN = "〈MODAL"
CLOSE = "〈/MODAL〉"


def _json_llm(summary="한글요약", tc=0, fc=0):
    def call(prompt, payload):
        return json.dumps({"summary": summary, "title_count": tc, "footnote_count": fc})
    return call


def test_absorbs_title_and_footnotes_verbatim_single_span():
    # 토큰은 서로 부분문자열이 아니게 선택(count 정확성).
    blocks = [
        {"type": "text", "text": "CAPLINE"},
        {"type": "text", "text": "REVNOTE"},
        {"type": "table", "table_body": "<TBL/>"},
        {"type": "text", "text": "NOTEALPHA"},
        {"type": "text", "text": "NOTEBETA"},
        {"type": "text", "text": "BODYPARA"},
    ]
    content, ids = enrich(blocks, text_llm=_json_llm("한글요약", tc=2, fc=2), vision_llm=None)
    assert ids == ["T1"]
    assert content.count(OPEN) == 1 and content.count(CLOSE) == 1
    span = content[content.index(OPEN):content.index(CLOSE) + len(CLOSE)]
    # 흡수 텍스트가 span 안에, 문서 순서대로
    assert span.index("CAPLINE") < span.index("REVNOTE") < span.index("한글요약")
    assert span.index("한글요약") < span.index("<TBL/>") < span.index("NOTEALPHA") < span.index("NOTEBETA")
    # 외부 중복 0
    assert content.count("CAPLINE") == 1 and content.count("NOTEALPHA") == 1
    # 무관 문단은 span 밖
    assert content.index("BODYPARA") > content.index(CLOSE)


def test_counts_clamped_to_available_window():
    blocks = [
        {"type": "text", "text": "ONLYTITLE"},
        {"type": "table", "table_body": "<X/>"},
    ]
    content, _ = enrich(blocks, text_llm=_json_llm("s", tc=99, fc=99), vision_llm=None)
    # 앞 후보 1개만 흡수, crash 없음. ONLYTITLE 는 span 안(외부 0회)
    assert content.startswith(OPEN)
    assert content.count("ONLYTITLE") == 1


def test_non_json_response_no_absorption_backward_compatible():
    blocks = [
        {"type": "text", "text": "TITLE"},
        {"type": "table", "table_body": "<X/>"},
        {"type": "text", "text": "FOOT"},
    ]
    content, _ = enrich(blocks, text_llm=lambda p, pl: "설명 텍스트 (JSON 아님)", vision_llm=None)
    # 흡수 없음: TITLE/FOOT 는 span 밖
    assert content.index("TITLE") < content.index(OPEN)
    assert content.index("FOOT") > content.index(CLOSE)
    span = content[content.index(OPEN):content.index(CLOSE) + len(CLOSE)]
    assert "설명 텍스트" in span  # 요약=원문 응답


def test_between_block_not_double_claimed():
    blocks = [
        {"type": "table", "table_body": "<T1/>"},
        {"type": "text", "text": "MID"},
        {"type": "table", "table_body": "<T2/>"},
    ]
    content, ids = enrich(blocks, text_llm=_json_llm("s", tc=1, fc=1), vision_llm=None)
    assert ids == ["T1", "T2"]
    assert content.count("MID") == 1  # 앞 표가 각주로 선점, 뒤 표는 미흡수


def test_footnote_only_absorption():
    blocks = [
        {"type": "table", "table_body": "<X/>"},
        {"type": "text", "text": "FOOT_ONLY"},
    ]
    content, _ = enrich(blocks, text_llm=_json_llm("s", tc=0, fc=1), vision_llm=None)
    span = content[content.index(OPEN):content.index(CLOSE) + len(CLOSE)]
    assert "FOOT_ONLY" in span and content.count("FOOT_ONLY") == 1
