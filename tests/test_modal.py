import re

import pytest

from kb_pipeline.modal import enrich, MODAL_OPEN_PREFIX, MODAL_CLOSE

# Byte-exact markers (U+3008 / U+3009).
OPEN_PREFIX = "〈MODAL"
CLOSE = "〈/MODAL〉"


def test_marker_constants_are_byte_exact():
    assert MODAL_OPEN_PREFIX == OPEN_PREFIX
    assert MODAL_CLOSE == CLOSE
    assert MODAL_OPEN_PREFIX.encode("utf-8") == b"\xe3\x80\x88MODAL"
    assert MODAL_CLOSE.encode("utf-8") == b"\xe3\x80\x88/MODAL\xe3\x80\x89"


def _text_llm(prompt, payload):
    return f"DESC[{payload[:10]}]"


def _vision_llm(img_path, prompt):
    return f"VISION[{img_path}]"


def test_table_block_produces_one_atomic_modal_span():
    blocks = [{"type": "table", "table_body": "<table><tr><td>x</td></tr></table>"}]
    content, ids = enrich(blocks, text_llm=_text_llm, vision_llm=_vision_llm)

    assert content.count(OPEN_PREFIX) == 1
    assert content.count(CLOSE) == 1
    assert content.startswith(OPEN_PREFIX)
    assert content.endswith(CLOSE)
    assert 'type="table"' in content
    assert ids == ["T1"]
    # payload preserved inside the span
    assert "<table><tr><td>x</td></tr></table>" in content


def test_image_block_uses_vision_llm():
    blocks = [{"type": "image", "img_path": "fig.png"}]
    content, ids = enrich(blocks, text_llm=_text_llm, vision_llm=_vision_llm)
    assert 'type="image"' in content
    assert "VISION[fig.png]" in content
    assert ids == ["I1"]
    assert content.count(OPEN_PREFIX) == 1 and content.count(CLOSE) == 1


def test_equation_block_uses_text_llm():
    blocks = [{"type": "equation", "latex": "E=mc^2"}]
    content, ids = enrich(blocks, text_llm=_text_llm, vision_llm=_vision_llm)
    assert 'type="equation"' in content
    assert ids == ["E1"]
    assert "E=mc^2" in content


def test_text_passes_through_and_order_preserved():
    blocks = [
        {"type": "text", "text": "intro"},
        {"type": "table", "table_body": "<table></table>"},
        {"type": "text", "text": "middle"},
        {"type": "image", "img_path": "a.png"},
        {"type": "text", "text": "outro"},
    ]
    content, ids = enrich(blocks, text_llm=_text_llm, vision_llm=_vision_llm)

    assert ids == ["T1", "I1"]
    assert len(ids) == len(set(ids))  # unique
    # exactly one open/close per modal
    assert content.count(OPEN_PREFIX) == 2
    assert content.count(CLOSE) == 2

    # plain text passes through untouched
    assert "intro" in content and "middle" in content and "outro" in content

    # order: intro < T1 < middle < I1 < outro
    pos_intro = content.index("intro")
    pos_t1 = content.index('id="T1"')
    pos_middle = content.index("middle")
    pos_i1 = content.index('id="I1"')
    pos_outro = content.index("outro")
    assert pos_intro < pos_t1 < pos_middle < pos_i1 < pos_outro


def test_each_modal_span_is_well_formed():
    blocks = [
        {"type": "table", "table_body": "<table>1</table>"},
        {"type": "equation", "latex": "x+y"},
        {"type": "image", "img_path": "i.png"},
    ]
    content, ids = enrich(blocks, text_llm=_text_llm, vision_llm=_vision_llm)
    assert ids == ["T1", "E1", "I1"]
    # one full span per modal: 〈MODAL ...〉body〈/MODAL〉
    spans = re.findall(
        re.escape(OPEN_PREFIX) + r'.*?' + re.escape(CLOSE), content, re.DOTALL
    )
    assert len(spans) == 3
    for span in spans:
        assert span.count(OPEN_PREFIX) == 1
        assert span.count(CLOSE) == 1


def test_modal_ids_unique_across_many():
    blocks = [{"type": "table", "table_body": f"<table>{i}</table>"} for i in range(5)]
    content, ids = enrich(blocks, text_llm=_text_llm, vision_llm=_vision_llm)
    assert ids == ["T1", "T2", "T3", "T4", "T5"]
    assert len(set(ids)) == 5
    assert content.count(OPEN_PREFIX) == 5


def test_missing_text_llm_raises_for_table():
    blocks = [{"type": "table", "table_body": "<table></table>"}]
    with pytest.raises(ValueError, match="text_llm is None"):
        enrich(blocks, text_llm=None, vision_llm=_vision_llm)


def test_missing_vision_llm_raises_for_image():
    blocks = [{"type": "image", "img_path": "a.png"}]
    with pytest.raises(ValueError, match="vision_llm is None"):
        enrich(blocks, text_llm=_text_llm, vision_llm=None)


def test_text_only_needs_no_llm():
    blocks = [{"type": "text", "text": "hello"}]
    content, ids = enrich(blocks, text_llm=None, vision_llm=None)
    assert content == "hello"
    assert ids == []
