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


def test_modal_llm_failure_does_not_fail_whole_document():
    # 런타임 LLM 실패(524/timeout 등)는 그 모달만 흡수 0·요약 생략으로 강등하고
    # 문서 전체 enrich 는 성공해야 한다(표 payload 는 보존).
    calls = {"n": 0}

    def llm(prompt, payload):
        calls["n"] += 1
        raise RuntimeError("524 proxy timeout")

    blocks = [
        {"type": "text", "text": "INTRO"},
        {"type": "table", "table_body": "<TBL/>"},
        {"type": "text", "text": "OUTRO"},
    ]
    content, ids = enrich(blocks, text_llm=llm, vision_llm=None)
    assert ids == ["T1"]                     # 모달은 여전히 방출
    assert "<TBL/>" in content               # 표 본문 보존
    assert "INTRO" in content and "OUTRO" in content
    # 흡수 0 → 주변 텍스트는 모달 밖
    assert content.index("INTRO") < content.index("〈MODAL")
    assert content.index("OUTRO") > content.index("〈/MODAL〉")
    assert calls["n"] == 1                    # 재시도 없음 — 즉시 폴백


def test_partial_modal_failure_other_modals_unaffected():
    # 표1만 실패, 표2는 정상 → 표2 요약은 살아있고 문서 성공.
    def llm(prompt, payload):
        if "<BAD/>" in payload:
            raise RuntimeError("524")
        return json.dumps({"summary": "정상요약", "title_count": 0, "footnote_count": 0})

    blocks = [
        {"type": "table", "table_body": "<BAD/>"},
        {"type": "text", "text": "사이"},
        {"type": "table", "table_body": "<GOOD/>"},
    ]
    content, ids = enrich(blocks, text_llm=llm, vision_llm=None)
    assert ids == ["T1", "T2"]
    assert "<BAD/>" in content and "<GOOD/>" in content
    assert "정상요약" in content              # 정상 표 요약 보존
