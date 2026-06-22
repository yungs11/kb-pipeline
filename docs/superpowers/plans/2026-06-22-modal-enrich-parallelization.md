<!-- plan-version: v3 -->
<!-- codex-validation: READY v3 at 2026-06-22T08:10:56Z -->

# 모달 enrich 병렬화 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `kb_pipeline/modal.py::enrich`의 모달별 LLM 호출을 **스레드풀로 병렬화**해 표 많은 PDF의 parse 시간을 ~1/N로 줄인다(흡수 결과·계약 불변).

**Architecture:** 3-phase 분리 — (A) 최대 윈도우 수집 + id 부여 + None 검증(순차) → (B) `ThreadPoolExecutor`로 모달 LLM 동시 호출 → (C) 충돌 해소(순차, "앞 모달 우선·모달에서 연속") → (D) 출력. **동치성(spec §4):** (가-1) count-불변 LLM이면 흡수 *집합* 동일(귀납); (가-2) 출력까지 byte-identical 하려면 LLM *응답*도 동일해야 하는데 **테스트 mock이 이를 충족**(고정-summary mock, 또는 fallback으로 consumption이 없어 윈도우 불변) → 결정적 테스트 36개 byte-identical 통과. 실제(윈도우 민감·비결정) LLM은 경계 판정이 미세히 달라질 수 있으나 Phase C가 **이중흡수 금지·연속성·앞모달 우선**을 항상 보장(유효한 변형). 엄밀 동일은 주장하지 않는다.

**Tech Stack:** Python `concurrent.futures.ThreadPoolExecutor`, pytest, `threading.Barrier`(동시성 증명).

## Global Constraints

- `enrich(blocks, *, text_llm, vision_llm, max_workers: int = 8) -> (content, modal_ids)` — `max_workers`는 **추가 키워드(기본 8)**, 기존 호출부(parse_service) 무수정. `max_workers < 1`은 **즉시 `ValueError`**(0/음수 은폐 금지).
- 흡수 결과·modal_ids 문서순·`_wrap`·`_boundary_*`·`_parse_boundary_response`·윈도우 헬퍼·`_MODAL_RE`·n_blocks·청커 **전부 무변경**.
- None 콜러블 시 **기존 메시지 보존**: `"table block encountered but text_llm is None; …"`, `"equation block encountered but text_llm is None; …"`, `"image block encountered but vision_llm is None; …"`. LLM 작업 전 raise.
- 두 모달 사이 블록은 **앞(문서순 먼저) 모달이 선점**. title/footnote는 모달에서 **연속**, consumed 만나면 중단.
- 작업 디렉토리 `/Users/xxx/workspace/8.kb-pipeline`, 테스트 `.venv-kb/bin/python -m pytest`.

---

### Task 1: enrich 3-phase 병렬화

**Files:**
- Modify: `kb_pipeline/modal.py` (`import concurrent.futures` 추가, `enrich` 본문 교체)
- Test: `tests/test_modal_parallel.py` (신규)

**Interfaces:**
- Consumes: `_gather_before_window`/`_gather_after_window`(consumed=set()로 최대 윈도우), `_boundary_prompt`, `_boundary_payload`, `_parse_boundary_response`, `_wrap`.
- Produces: `enrich(blocks, *, text_llm, vision_llm, max_workers=8) -> (str, list[str])` — 시그니처에 `max_workers` 추가, 동작 동일(병렬).

- [ ] **Step 1: 동시성/순서 증명 테스트 작성** — `tests/test_modal_parallel.py` (신규)

```python
"""enrich() 모달 LLM 호출 병렬화 — 동시성 증명 + 순서/동치 회귀."""
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
```

- [ ] **Step 2: 실패 확인** (현재 순차 enrich → 배리어 데드락)

Run: `.venv-kb/bin/python -m pytest tests/test_modal_parallel.py -q`
Expected: FAIL — `test_modal_llm_calls_run_concurrently`/`..._order...`가 `BrokenBarrierError`(또는 그로 인한 예외)로 실패. `max_workers` 키워드 미지원이면 `TypeError`로 실패.

- [ ] **Step 3: 구현** — `kb_pipeline/modal.py`

(a) 상단 import에 추가:

```python
import concurrent.futures
```

(b) `enrich` 함수 전체를 아래로 교체(시그니처에 `max_workers` 추가):

```python
def enrich(
    blocks: list[dict],
    *,
    text_llm: Callable[[str, str], str] | None,
    vision_llm: Callable[[str, str], str] | None,
    max_workers: int = 8,
) -> tuple[str, list[str]]:
    """Enrich blocks into a single content string + ordered modal ids.

    모달(table/image/equation)마다 LLM 호출 1회로 한국어 요약 + 주변 text 의
    제목/각주 개수를 판정해, 제목·각주를 원문 그대로 〈MODAL…〈/MODAL〉 안으로 흡수한다.
    LLM 호출은 스레드풀로 **병렬** 실행하고(표 많은 문서의 parse 시간 단축), 두 모달이
    같은 사이 블록을 다투면 문서순으로 앞 모달이 선점한다(사후 충돌 해소). LLM 이 JSON 을
    주지 않으면 흡수 0건 + 요약=원문(하위호환).

    :param max_workers: 모달 LLM 동시 호출 상한(기본 8, 모달 수로 추가 제한).
    :raises ValueError: if ``max_workers < 1``, or if a modal of a kind appears but
        its callable is None.
    """
    if max_workers < 1:
        raise ValueError(f"max_workers must be >= 1, got {max_workers}")
    n = len(blocks)
    _KEY = {"table": "table_body", "equation": "latex", "image": "img_path"}
    _PREFIX = {"table": "T", "equation": "E", "image": "I"}

    # Phase A — 모달 식별/ id 부여/ 최대 윈도우 수집/ None 검증 (문서순, LLM 없음).
    modals: list[dict] = []
    counters = {"table": 0, "image": 0, "equation": 0}
    for i in range(n):
        btype = blocks[i].get("type")
        if btype not in ("table", "image", "equation"):
            continue
        if btype in ("table", "equation") and text_llm is None:
            raise ValueError(
                f"{btype} block encountered but text_llm is None; "
                f"a text LLM callable is required to describe {btype}s."
            )
        if btype == "image" and vision_llm is None:
            raise ValueError(
                "image block encountered but vision_llm is None; "
                "a vision LLM callable is required to describe images."
            )
        counters[btype] += 1
        modals.append({
            "i": i,
            "type": btype,
            "modal_id": f"{_PREFIX[btype]}{counters[btype]}",
            "body": blocks[i].get(_KEY[btype], ""),
            "before": _gather_before_window(blocks, i, set()),  # 최대 윈도우(consumed 무시)
            "after": _gather_after_window(blocks, i, set()),
        })

    # Phase B — 모달 LLM 병렬 호출(ex.map 은 입력 순서 보존).
    def _call(m: dict) -> tuple[str, int, int]:
        prompt = _boundary_prompt(m["type"])
        payload = _boundary_payload(m["before"], m["after"], m["body"])
        if m["type"] == "image":
            raw = vision_llm(m["body"], prompt + "\n\n" + payload)
        else:
            raw = text_llm(prompt, payload)
        return _parse_boundary_response(raw, len(m["before"]), len(m["after"]))

    if modals:
        workers = min(max_workers, len(modals))  # max_workers>=1 검증됨; modals 비어있지 않음
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            for m, (summary, tc, fc) in zip(modals, ex.map(_call, modals)):
                m["summary"], m["tc"], m["fc"] = summary, tc, fc

    # Phase C — 충돌 해소(문서순; 앞 모달 우선, 모달에서 연속, consumed 만나면 중단).
    consumed: set[int] = set()
    decisions: dict[int, dict] = {}
    for m in modals:
        title_idxs: list[int] = []
        for idx, _ in m["before"][:m["tc"]]:
            if idx in consumed:
                break
            title_idxs.append(idx)
        footnote_idxs: list[int] = []
        for idx, _ in m["after"][:m["fc"]]:
            if idx in consumed:
                break
            footnote_idxs.append(idx)
        consumed.update(title_idxs)
        consumed.update(footnote_idxs)
        decisions[m["i"]] = {
            "modal_id": m["modal_id"], "modal_type": m["type"], "payload": m["body"],
            "summary": m["summary"],
            "title_idxs": sorted(title_idxs), "footnote_idxs": sorted(footnote_idxs),
        }

    # Phase D — 출력(흡수된 text 는 건너뜀).
    segments: list[str] = []
    modal_ids: list[str] = []
    for i in range(n):
        if i in consumed:
            continue
        if i in decisions:
            d = decisions[i]
            title = "\n".join(blocks[j].get("text", "") for j in d["title_idxs"])
            footnote = "\n".join(blocks[j].get("text", "") for j in d["footnote_idxs"])
            segments.append(_wrap(
                d["modal_id"], d["modal_type"], d["summary"], d["payload"],
                title=title, footnote=footnote,
            ))
            modal_ids.append(d["modal_id"])
        elif blocks[i].get("type") == "text":
            text = blocks[i].get("text", "")
            if text:
                segments.append(text)
        # 알 수 없는 타입: 무시(기존과 동일).

    return "\n\n".join(segments), modal_ids
```

- [ ] **Step 4: 통과 확인 (신규 + 기존 전부)**

Run: `.venv-kb/bin/python -m pytest tests/test_modal_parallel.py tests/test_modal.py tests/test_modal_boundary.py tests/test_modal_absorb.py -q`
Expected: PASS. 기존 modal 테스트(test_modal.py 10 + boundary + absorb = 36개)가 **무변경** 통과(결정적 mock → 동치성), 신규 4개(동시성/순서/max_workers=1/invalid) 통과.

- [ ] **Step 5: parse-svc 계약 회귀**

Run: `.venv-kb/bin/python -m pytest parse_service/tests/test_parse.py -q`
Expected: PASS (modal_spans/contract 불변).

- [ ] **Step 6: 커밋**

```bash
git add kb_pipeline/modal.py tests/test_modal_parallel.py \
  docs/superpowers/specs/2026-06-22-modal-enrich-parallelization-design.md \
  docs/superpowers/plans/2026-06-22-modal-enrich-parallelization.md
git commit -m "perf(modal): parallelize per-modal LLM calls (3-phase, conflict-resolve post-pass)"
```

---

## Self-Review

- **Spec coverage:** Phase A/B/C/D(§3)=Step3; 동치성(§4)=기존 between_block 테스트+Step1; 동시성 증명(§6)=Step1 배리어; 시그니처 max_workers(§5)=Step3.
- **Placeholder scan:** 모든 step에 실제 코드/명령/기대출력. TBD 없음.
- **Type consistency:** `_gather_*`→`list[tuple[int,str]]`, `_parse_boundary_response`→`(str,int,int)`, `enrich`+`max_workers=8`. Phase C는 nearest-first walk 후 `sorted` → 문서순(기존 Pass2와 동일 키).
- **동치성/하위호환:** spec §4 — (가-1) count-불변 LLM이면 흡수 집합 동일(귀납), (가-2) byte-identical은 LLM 응답 동일까지 필요하나 테스트 mock(고정-summary 또는 fallback無consumption→윈도우 불변)이 충족 → 결정적 테스트 36개 byte-identical. 실제 LLM은 불변식 보존하의 유효한 변형(엄밀 동일 미주장). None 검증 메시지 보존. `max_workers<1`→ValueError(Step1 `test_invalid_max_workers_rejected`). max_workers 기본 8 → 호출부 무수정.
