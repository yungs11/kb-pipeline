"""enrich() 모달 LLM 호출 병렬화 — 동시성 증명 + 순서/검증."""
import json
import threading

import pytest

from kb_pipeline.modal import enrich

OPEN = "〈MODAL"


def test_modal_llm_calls_run_concurrently():
    # n개 호출이 동시에 배리어에 도달해야만 통과. 순차면 첫 호출이 배리어에서
    # 대기→BrokenBarrierError(timeout) → enrich 예외 → 테스트 실패.
    n = 3
    barrier = threading.Barrier(n, timeout=8)

    def llm(prompt, payload):
        barrier.wait()
        return json.dumps({"summary": "s", "title_count": 0, "footnote_count": 0})

    blocks = [{"type": "table", "table_body": f"<t{i}/>"} for i in range(n)]
    content, ids = enrich(blocks, text_llm=llm, vision_llm=None)
    assert ids == ["T1", "T2", "T3"]
    assert content.count(OPEN) == 3


def test_modal_ids_document_order_preserved_when_parallel():
    barrier = threading.Barrier(3, timeout=8)

    def llm(prompt, payload):
        barrier.wait()
        return json.dumps({"summary": "s", "title_count": 0, "footnote_count": 0})

    blocks = [
        {"type": "table", "table_body": "<a/>"},
        {"type": "equation", "latex": "x"},
        {"type": "image", "img_path": "p.png"},
    ]
    # image 는 vision_llm 경로 → 같은 배리어 공유.
    content, ids = enrich(blocks, text_llm=llm, vision_llm=lambda img, prompt: llm(prompt, img))
    assert ids == ["T1", "E1", "I1"]
    pos = [content.index(f'id="{m}"') for m in ("T1", "E1", "I1")]
    assert pos == sorted(pos)


def test_max_workers_one_still_correct_sequentially():
    # max_workers=1 이면 순차지만 결과는 동일해야 한다(배리어 없이).
    def llm(prompt, payload):
        return json.dumps({"summary": "s", "title_count": 1, "footnote_count": 0})

    blocks = [
        {"type": "text", "text": "TTL"},
        {"type": "table", "table_body": "<x/>"},
    ]
    content, ids = enrich(blocks, text_llm=llm, vision_llm=None, max_workers=1)
    assert ids == ["T1"] and content.count("TTL") == 1 and content.startswith(OPEN)


def test_invalid_max_workers_rejected():
    # 0/음수는 은폐(1로 강등)하지 않고 명시적으로 거부한다.
    for bad in (0, -1):
        with pytest.raises(ValueError, match="max_workers"):
            enrich(
                [{"type": "table", "table_body": "<x/>"}],
                text_llm=lambda p, pl: "{}", vision_llm=None, max_workers=bad,
            )
