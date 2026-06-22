<!-- plan-version: v3 -->
<!-- codex-validation: READY v3 at 2026-06-22T06:41:44Z -->

# MODAL 제목·각주 흡수 + 한글 요약 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 파서 모달 enrichment가 표/이미지/수식의 **제목·각주 text 블록을 `〈MODAL…〈/MODAL〉` 안으로 원문 그대로 흡수**하고, 요약을 **한국어**로 생성하도록 한다.

**Architecture:** Philosophy A(파서가 원자성 소유). `kb_pipeline/modal.py::enrich`를 2-pass로 재구성: Pass 1에서 모달마다 앞/뒤 후보 text 윈도우를 모아 기존 LLM 호출 1회를 확장(요약+제목수+각주수 JSON)하고, Pass 2에서 흡수 블록을 제외하며 확장 wrap을 출력. LLM이 JSON을 안 주면 fallback(흡수 0 + 요약=원문)이라 하위호환 유지. 청커/`modal_spans` 계약 무변경.

**Tech Stack:** Python 3, pytest, httpx(기존), FastAPI(parse-svc). LLM 시그니처 `text_llm(prompt,payload)->str` / `vision_llm(img_path,prompt)->str` 무변경.

## Global Constraints

- 제목·각주 **텍스트는 원문 그대로 보존**(LLM 재작성 금지). LLM은 요약문만 생성, **한국어**.
- `enrich(blocks, *, text_llm, vision_llm) -> (content, modal_ids)` **시그니처 무변경**.
- 모달 마커는 byte-exact 유지: open prefix `〈MODAL`(U+3008), close `〈/MODAL〉`. wrap의 무-흡수 케이스는 **현재와 byte 동일**(`{open}{summary}\n{payload}{close}`).
- 흡수된 text 블록은 `enriched_content`에 **정확히 1회**(span 안)만 등장. 외부 중복 금지.
- 윈도우 상수: `BEFORE_WINDOW = 3`, `AFTER_WINDOW = 6`.
- 모달 id 카운터는 문서 순서로 `T{n}`/`E{n}`/`I{n}`.
- adaptive_chunk·`parse_service/app.py::_MODAL_RE`·`n_blocks` 로직 변경 없음.
- 작업 디렉토리: `/Users/xxx/workspace/8.kb-pipeline`. 테스트: `python -m pytest`.

---

### Task 1: 후보 윈도우 헬퍼 + 상수

**Files:**
- Modify: `kb_pipeline/modal.py` (상수 + `_is_text`/`_gather_before_window`/`_gather_after_window` 추가)
- Test: `tests/test_modal_boundary.py` (신규)

**Interfaces:**
- Produces: `BEFORE_WINDOW:int=3`, `AFTER_WINDOW:int=6`; `_is_text(block:dict)->bool`; `_gather_before_window(blocks:list[dict], i:int, consumed:set[int]) -> list[tuple[int,str]]` (nearest-first: i-1 먼저); `_gather_after_window(blocks, i, consumed) -> list[tuple[int,str]]` (nearest-first: i+1 먼저). 둘 다 연속 `text` 블록만, 비-text/consumed 만나면 중단, 최대 윈도우 크기.

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_modal_boundary.py`

```python
from kb_pipeline.modal import (
    BEFORE_WINDOW, AFTER_WINDOW, _is_text,
    _gather_before_window, _gather_after_window,
)


def test_window_constants():
    assert BEFORE_WINDOW == 3 and AFTER_WINDOW == 6


def test_is_text():
    assert _is_text({"type": "text", "text": "x"})
    assert not _is_text({"type": "table"})


def test_before_window_nearest_first_stops_at_nontext():
    blocks = [
        {"type": "text", "text": "a"},     # 0
        {"type": "table"},                 # 1 (non-text barrier)
        {"type": "text", "text": "b"},     # 2
        {"type": "text", "text": "c"},     # 3
        {"type": "table"},                 # 4  <- i
    ]
    out = _gather_before_window(blocks, 4, set())
    assert out == [(3, "c"), (2, "b")]  # nearest-first, stops before table@1


def test_before_window_stops_at_consumed():
    blocks = [
        {"type": "text", "text": "a"},  # 0
        {"type": "text", "text": "b"},  # 1
        {"type": "table"},              # 2  <- i
    ]
    assert _gather_before_window(blocks, 2, {1}) == []  # 1 consumed -> stop


def test_before_window_caps_at_BEFORE_WINDOW():
    blocks = [{"type": "text", "text": str(k)} for k in range(5)] + [{"type": "table"}]
    out = _gather_before_window(blocks, 5, set())
    assert [t for _, t in out] == ["4", "3", "2"]  # only 3 nearest


def test_after_window_nearest_first_stops_at_nontext():
    blocks = [
        {"type": "table"},                 # 0 <- i
        {"type": "text", "text": "a"},     # 1
        {"type": "text", "text": "b"},     # 2
        {"type": "image"},                 # 3 barrier
        {"type": "text", "text": "c"},     # 4
    ]
    out = _gather_after_window(blocks, 0, set())
    assert out == [(1, "a"), (2, "b")]


def test_after_window_caps_at_AFTER_WINDOW():
    blocks = [{"type": "table"}] + [{"type": "text", "text": str(k)} for k in range(8)]
    out = _gather_after_window(blocks, 0, set())
    assert len(out) == AFTER_WINDOW and out[0] == (1, "0")
```

- [ ] **Step 2: 실패 확인**

Run: `python -m pytest tests/test_modal_boundary.py -q`
Expected: FAIL (ImportError: cannot import name 'BEFORE_WINDOW').

- [ ] **Step 3: 구현** — `kb_pipeline/modal.py` 상단(마커 상수 아래, `_open_marker` 위)에 추가

```python
#: 모달 앞/뒤에서 제목·각주 후보로 고려할 연속 text 블록 최대 수.
BEFORE_WINDOW = 3
AFTER_WINDOW = 6


def _is_text(block: dict) -> bool:
    return block.get("type") == "text"


def _gather_before_window(blocks: list[dict], i: int, consumed: set[int]) -> list[tuple[int, str]]:
    """모달 직전의 연속 text 블록을 nearest-first(i-1 먼저)로 수집.

    비-text 블록 또는 이미 ``consumed`` 된 인덱스를 만나면 중단(원자 경계 침범 방지).
    최대 ``BEFORE_WINDOW`` 개.
    """
    out: list[tuple[int, str]] = []
    j = i - 1
    while j >= 0 and len(out) < BEFORE_WINDOW:
        if j in consumed or not _is_text(blocks[j]):
            break
        out.append((j, blocks[j].get("text", "")))
        j -= 1
    return out


def _gather_after_window(blocks: list[dict], i: int, consumed: set[int]) -> list[tuple[int, str]]:
    """모달 직후의 연속 text 블록을 nearest-first(i+1 먼저)로 수집. 최대 ``AFTER_WINDOW`` 개."""
    out: list[tuple[int, str]] = []
    j, n = i + 1, len(blocks)
    while j < n and len(out) < AFTER_WINDOW:
        if j in consumed or not _is_text(blocks[j]):
            break
        out.append((j, blocks[j].get("text", "")))
        j += 1
    return out
```

- [ ] **Step 4: 통과 확인**

Run: `python -m pytest tests/test_modal_boundary.py -q`
Expected: PASS (6 passed).

- [ ] **Step 5: 커밋**

```bash
git add kb_pipeline/modal.py tests/test_modal_boundary.py
git commit -m "feat(modal): add title/footnote candidate window helpers"
```

---

### Task 2: LLM 응답 파서 (`_parse_boundary_response`)

**Files:**
- Modify: `kb_pipeline/modal.py`
- Test: `tests/test_modal_boundary.py`

**Interfaces:**
- Produces: `_parse_boundary_response(raw:str, n_before:int, n_after:int) -> tuple[str,int,int]` 반환 `(summary, title_count, footnote_count)`. JSON `{"summary","title_count","footnote_count"}` 파싱 → counts를 `[0,n_before]`/`[0,n_after]`로 clamp. 파싱 실패/요약 비어있음 → fallback `(raw.strip(), 0, 0)`.

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_modal_boundary.py` 에 추가

```python
import json
from kb_pipeline.modal import _parse_boundary_response


def test_parse_valid_json_clamps_counts():
    raw = json.dumps({"summary": "한글요약", "title_count": 2, "footnote_count": 5})
    assert _parse_boundary_response(raw, n_before=3, n_after=2) == ("한글요약", 2, 2)


def test_parse_strips_code_fence():
    raw = '```json\n{"summary":"s","title_count":1,"footnote_count":0}\n```'
    assert _parse_boundary_response(raw, 3, 3) == ("s", 1, 0)


def test_parse_negative_counts_floored_to_zero():
    raw = json.dumps({"summary": "s", "title_count": -1, "footnote_count": 1})
    assert _parse_boundary_response(raw, 3, 3) == ("s", 0, 1)


def test_parse_non_json_falls_back():
    assert _parse_boundary_response("그냥 설명 (JSON 아님)", 3, 3) == ("그냥 설명 (JSON 아님)", 0, 0)


def test_parse_missing_summary_falls_back():
    raw = json.dumps({"title_count": 2, "footnote_count": 1})
    assert _parse_boundary_response(raw, 3, 3) == (raw, 0, 0)


def test_parse_non_int_counts_fall_back():
    raw = json.dumps({"summary": "s", "title_count": "two", "footnote_count": 1})
    assert _parse_boundary_response(raw, 3, 3) == (raw, 0, 0)


def test_parse_ignores_trailing_text_and_braces():
    # 바깥 중괄호/후행 잡음이 있어도 첫 유효 JSON 객체만 파싱(greedy 회귀 방지).
    raw = '설명: {"summary":"s","title_count":1,"footnote_count":1} 추가 {잡음}'
    assert _parse_boundary_response(raw, 3, 3) == ("s", 1, 1)
```

- [ ] **Step 2: 실패 확인**

Run: `python -m pytest tests/test_modal_boundary.py -q -k parse`
Expected: FAIL (ImportError: cannot import name '_parse_boundary_response').

- [ ] **Step 3: 구현** — `kb_pipeline/modal.py` 상단 import에 `import json` 추가하고 헬퍼 추가

```python
import json


def _parse_boundary_response(raw: str, n_before: int, n_after: int) -> tuple[str, int, int]:
    """LLM 응답 → ``(summary, title_count, footnote_count)``.

    첫 유효 JSON 객체를 ``json.JSONDecoder().raw_decode`` 로 파싱한다(코드펜스/선후행
    잡음/바깥 중괄호 무시, 문자열 내부 중괄호 안전 — greedy 정규식 회귀 방지). 성공 시
    counts 를 ``[0, n_before]`` / ``[0, n_after]`` 로 clamp. 파싱 실패·요약 누락·정수
    아님이면 fallback ``(text, 0, 0)`` (``text == raw.strip()``) — 흡수 0건(하위호환).
    """
    text = (raw or "").strip()
    decoder = json.JSONDecoder()
    idx = text.find("{")
    while idx != -1:
        try:
            obj, _ = decoder.raw_decode(text[idx:])
        except json.JSONDecodeError:
            idx = text.find("{", idx + 1)
            continue
        if isinstance(obj, dict) and isinstance(obj.get("summary"), str) and obj["summary"].strip():
            try:
                tc, fc = int(obj["title_count"]), int(obj["footnote_count"])
            except (ValueError, TypeError, KeyError):
                break
            return obj["summary"].strip(), max(0, min(tc, n_before)), max(0, min(fc, n_after))
        idx = text.find("{", idx + 1)
    return text, 0, 0
```

> `json.JSONDecodeError` 는 `ValueError` 의 서브클래스다. `raw_decode` 는 첫 JSON 값만
> 파싱하고 후행 데이터를 무시하므로 ```json…``` 펜스나 "설명: {…} 잡음" 형태에 안전하다.

- [ ] **Step 4: 통과 확인**

Run: `python -m pytest tests/test_modal_boundary.py -q -k parse`
Expected: PASS (7 passed).

- [ ] **Step 5: 커밋**

```bash
git add kb_pipeline/modal.py tests/test_modal_boundary.py
git commit -m "feat(modal): add boundary LLM response parser with safe fallback"
```

---

### Task 3: 한글 프롬프트 + 경계 payload 빌더

**Files:**
- Modify: `kb_pipeline/modal.py` (영어 `_TABLE_PROMPT`/`_EQUATION_PROMPT`/`_IMAGE_PROMPT` 제거, `_boundary_prompt`/`_boundary_payload` 추가)
- Test: `tests/test_modal_boundary.py`

**Interfaces:**
- Produces: `_boundary_prompt(modal_type:str) -> str` (한국어 요약 + 제목수/각주수 JSON 지시); `_boundary_payload(before:list[tuple[int,str]], after:list[tuple[int,str]], body:str) -> str` (앞 후보 `B1..` nearest-first, 본문, 뒤 후보 `A1..` nearest-first).

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_modal_boundary.py` 에 추가

```python
from kb_pipeline.modal import _boundary_prompt, _boundary_payload


def test_prompt_is_korean_and_requests_json():
    p = _boundary_prompt("table")
    assert ("한국어" in p) or ("한글" in p)
    assert "title_count" in p and "footnote_count" in p and "summary" in p


def test_prompt_varies_by_type():
    assert "표" in _boundary_prompt("table")
    assert "수식" in _boundary_prompt("equation")
    assert "이미지" in _boundary_prompt("image")


def test_payload_lists_candidates_nearest_first_and_body():
    before = [(5, "가까운제목"), (4, "먼제목")]   # nearest-first
    after = [(7, "가까운각주")]
    body = "<table>X</table>"
    out = _boundary_payload(before, after, body)
    assert "B1: 가까운제목" in out and "B2: 먼제목" in out
    assert "A1: 가까운각주" in out
    assert "<table>X</table>" in out
    # 본문은 앞 후보 뒤, 뒤 후보 앞에 위치
    assert out.index("B1:") < out.index("<table>X</table>") < out.index("A1:")


def test_payload_handles_empty_windows():
    out = _boundary_payload([], [], "BODY")
    assert "BODY" in out  # no crash, body present
```

- [ ] **Step 2: 실패 확인**

Run: `python -m pytest tests/test_modal_boundary.py -q -k "prompt or payload"`
Expected: FAIL (ImportError).

- [ ] **Step 3: 구현** — `kb_pipeline/modal.py` 에서 기존 `_TABLE_PROMPT`/`_EQUATION_PROMPT`/`_IMAGE_PROMPT` 3개 상수를 **삭제**하고 아래로 교체

```python
_SUMMARY_LANG = "반드시 한국어(한글)로"

_TYPE_INTRO = {
    "table": "다음은 문서에서 추출한 표와 그 앞뒤 후보 줄이다.",
    "equation": "다음은 문서에서 추출한 수식과 그 앞뒤 후보 줄이다.",
    "image": "다음은 문서에서 추출한 이미지/도표와 그 앞뒤 후보 줄이다.",
}


def _boundary_prompt(modal_type: str) -> str:
    """한국어 요약 + 제목/각주 개수 판정을 JSON 으로 요구하는 프롬프트."""
    intro = _TYPE_INTRO.get(modal_type, "다음은 문서에서 추출한 본문과 그 앞뒤 후보 줄이다.")
    return (
        f"{intro}\n"
        f"1) 본문을 검색용으로 {_SUMMARY_LANG} 요약하라.\n"
        "2) '앞 후보' 중 이 본문의 제목/캡션인 줄 수(title_count)를 세어라"
        "(본문에 가까운 쪽부터 연속, 무관한 줄 제외).\n"
        "3) '뒤 후보' 중 이 본문의 각주/설명인 줄 수(footnote_count)를 세어라"
        "(본문에 가까운 쪽부터 연속, 무관한 줄 제외).\n"
        '오직 JSON만 출력하라: {"summary": "...", "title_count": N, "footnote_count": M}'
    )


def _boundary_payload(before: list[tuple[int, str]], after: list[tuple[int, str]], body: str) -> str:
    """앞 후보(B1..)/본문/뒤 후보(A1..)를 한 문자열로. before/after 는 nearest-first."""
    lines = ["[앞 후보 — 본문에서 가까운 순]"]
    lines += [f"B{k}: {t}" for k, (_, t) in enumerate(before, 1)] or ["(없음)"]
    lines += ["", "[본문]", body, "", "[뒤 후보 — 본문에서 가까운 순]"]
    lines += [f"A{k}: {t}" for k, (_, t) in enumerate(after, 1)] or ["(없음)"]
    return "\n".join(lines)
```

주의: `lines += [..] or ["(없음)"]` 는 리스트가 비면 `["(없음)"]` 를 붙인다(빈 리스트는 falsy).

- [ ] **Step 4: 통과 확인**

Run: `python -m pytest tests/test_modal_boundary.py -q -k "prompt or payload"`
Expected: PASS (4 passed).

- [ ] **Step 5: 커밋**

```bash
git add kb_pipeline/modal.py tests/test_modal_boundary.py
git commit -m "feat(modal): Korean summary prompt + boundary payload builder"
```

---

### Task 4: 확장 `_wrap` (제목·각주, 하위호환)

**Files:**
- Modify: `kb_pipeline/modal.py` (`_wrap` 시그니처 확장)
- Test: `tests/test_modal_boundary.py`

**Interfaces:**
- Produces: `_wrap(modal_id:str, modal_type:str, description:str, payload:str, *, title:str="", footnote:str="") -> str`. segment `[title?, description, payload, footnote?]` 를 `"\n"` join 후 open marker+close 로 감쌈. **title/footnote 없으면 현재와 byte 동일**.

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_modal_boundary.py` 에 추가

```python
from kb_pipeline.modal import _wrap, MODAL_OPEN_PREFIX, MODAL_CLOSE


def test_wrap_backward_compatible_without_title_footnote():
    out = _wrap("T1", "table", "DESC", "<table/>")
    assert out == '〈MODAL id="T1" type="table"〉DESC\n<table/>〈/MODAL〉'


def test_wrap_inserts_title_before_and_footnote_after():
    out = _wrap("T1", "table", "요약", "<table/>", title="제목줄", footnote="각주줄")
    assert out.startswith(MODAL_OPEN_PREFIX) and out.endswith(MODAL_CLOSE)
    # 순서: 제목 < 요약 < payload < 각주
    assert out.index("제목줄") < out.index("요약") < out.index("<table/>") < out.index("각주줄")
    assert out.count(MODAL_OPEN_PREFIX) == 1 and out.count(MODAL_CLOSE) == 1
```

- [ ] **Step 2: 실패 확인**

Run: `python -m pytest tests/test_modal_boundary.py -q -k wrap`
Expected: FAIL (TypeError: unexpected keyword 'title', 또는 AssertionError).

- [ ] **Step 3: 구현** — `kb_pipeline/modal.py` 의 기존 `_wrap` 를 교체

```python
def _wrap(modal_id: str, modal_type: str, description: str, payload: str,
          *, title: str = "", footnote: str = "") -> str:
    """원자 〈MODAL …〉[title]\n{desc}\n{payload}\n[footnote]〈/MODAL〉 span 생성.

    title/footnote 가 비면 ``{open}{desc}\n{payload}{close}`` 로 현재와 byte 동일.
    """
    segments: list[str] = []
    if title:
        segments.append(title)
    segments.append(description)
    segments.append(payload)
    if footnote:
        segments.append(footnote)
    return f"{_open_marker(modal_id, modal_type)}" + "\n".join(segments) + MODAL_CLOSE
```

- [ ] **Step 4: 통과 확인**

Run: `python -m pytest tests/test_modal_boundary.py -q -k wrap`
Expected: PASS (2 passed).

- [ ] **Step 5: 커밋**

```bash
git add kb_pipeline/modal.py tests/test_modal_boundary.py
git commit -m "feat(modal): extend _wrap with optional title/footnote (backward compatible)"
```

---

### Task 5: `enrich` 2-pass 재구성 (흡수 통합)

**Files:**
- Modify: `kb_pipeline/modal.py` (`enrich` 본문 교체)
- Test: `tests/test_modal.py` (기존 9개 유지 — green), `tests/test_modal_absorb.py` (신규 통합)

**Interfaces:**
- Consumes: Task 1–4 헬퍼. `text_llm(prompt,payload)->str`, `vision_llm(img_path,prompt)->str`.
- Produces: `enrich(blocks, *, text_llm, vision_llm) -> tuple[str, list[str]]` — 시그니처 무변경, 흡수 동작 추가.

- [ ] **Step 1: 통합 실패 테스트 작성** — `tests/test_modal_absorb.py` (신규)

```python
import json

from kb_pipeline.modal import enrich

OPEN = "〈MODAL"
CLOSE = "〈/MODAL〉"


def _json_llm(summary="한글요약", tc=0, fc=0):
    def call(prompt, payload):
        return json.dumps({"summary": summary, "title_count": tc, "footnote_count": fc})
    return call


def test_absorbs_title_and_footnotes_verbatim_single_span():
    blocks = [
        {"type": "text", "text": "TITLE"},
        {"type": "text", "text": "SUBTITLE"},
        {"type": "table", "table_body": "<TBL/>"},
        {"type": "text", "text": "FOOT_A"},
        {"type": "text", "text": "FOOT_B"},
        {"type": "text", "text": "UNRELATED_BODY"},
    ]
    content, ids = enrich(blocks, text_llm=_json_llm("한글요약", tc=2, fc=2), vision_llm=None)
    assert ids == ["T1"]
    assert content.count(OPEN) == 1 and content.count(CLOSE) == 1
    span = content[content.index(OPEN):content.index(CLOSE) + len(CLOSE)]
    # 흡수 텍스트가 span 안에, 문서 순서대로
    assert span.index("TITLE") < span.index("SUBTITLE") < span.index("한글요약")
    assert span.index("한글요약") < span.index("<TBL/>") < span.index("FOOT_A") < span.index("FOOT_B")
    # 외부 중복 0
    assert content.count("TITLE") == 1 and content.count("FOOT_A") == 1
    # 무관 문단은 span 밖
    assert content.index("UNRELATED_BODY") > content.index(CLOSE)


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
```

- [ ] **Step 2: 실패 확인**

Run: `python -m pytest tests/test_modal_absorb.py -q`
Expected: FAIL (기존 `enrich` 가 흡수를 안 해 span 밖에 TITLE 잔존 → AssertionError).

- [ ] **Step 3: 구현** — `kb_pipeline/modal.py` 의 `enrich` 함수 본문을 아래로 교체(시그니처/docstring 유지)

```python
def enrich(
    blocks: list[dict],
    *,
    text_llm: Callable[[str, str], str] | None,
    vision_llm: Callable[[str, str], str] | None,
) -> tuple[str, list[str]]:
    """Enrich blocks into a single content string + ordered modal ids.

    모달(table/image/equation)마다 LLM 호출 1회로 한국어 요약 + 주변 text 의
    제목/각주 개수를 판정해, 제목·각주를 원문 그대로 〈MODAL…〈/MODAL〉 안으로 흡수한다.
    LLM 이 JSON 을 주지 않으면 흡수 0건 + 요약=원문(하위호환).

    :raises ValueError: if a modal of a kind appears but its callable is None.
    """
    n = len(blocks)
    consumed: set[int] = set()
    decisions: dict[int, dict] = {}
    counters = {"table": 0, "image": 0, "equation": 0}

    # Pass 1 — 모달별 id/요약/흡수범위 결정(문서 순서; consumed 로 이중흡수 방지).
    for i in range(n):
        btype = blocks[i].get("type")
        if btype not in ("table", "image", "equation"):
            continue

        before = _gather_before_window(blocks, i, consumed)
        after = _gather_after_window(blocks, i, consumed)

        if btype == "table":
            if text_llm is None:
                raise ValueError(
                    "table block encountered but text_llm is None; "
                    "a text LLM callable is required to describe tables."
                )
            counters["table"] += 1
            modal_id, body = f"T{counters['table']}", blocks[i].get("table_body", "")
            raw = text_llm(_boundary_prompt("table"), _boundary_payload(before, after, body))
        elif btype == "equation":
            if text_llm is None:
                raise ValueError(
                    "equation block encountered but text_llm is None; "
                    "a text LLM callable is required to describe equations."
                )
            counters["equation"] += 1
            modal_id, body = f"E{counters['equation']}", blocks[i].get("latex", "")
            raw = text_llm(_boundary_prompt("equation"), _boundary_payload(before, after, body))
        else:  # image
            if vision_llm is None:
                raise ValueError(
                    "image block encountered but vision_llm is None; "
                    "a vision LLM callable is required to describe images."
                )
            counters["image"] += 1
            modal_id, body = f"I{counters['image']}", blocks[i].get("img_path", "")
            prompt = _boundary_prompt("image") + "\n\n" + _boundary_payload(before, after, body)
            raw = vision_llm(body, prompt)

        summary, tc, fc = _parse_boundary_response(raw, len(before), len(after))
        title_idxs = sorted(idx for idx, _ in before[:tc])     # nearest tc → 문서 순서
        footnote_idxs = sorted(idx for idx, _ in after[:fc])
        consumed.update(title_idxs)
        consumed.update(footnote_idxs)
        decisions[i] = {
            "modal_id": modal_id, "modal_type": btype, "payload": body,
            "summary": summary, "title_idxs": title_idxs, "footnote_idxs": footnote_idxs,
        }

    # Pass 2 — 출력(흡수된 text 는 건너뜀).
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

Run: `python -m pytest tests/test_modal_absorb.py tests/test_modal.py tests/test_modal_boundary.py -q`
Expected: PASS (기존 **10** + 신규 전부). `tests/test_modal.py` 10개 무변경 통과 — **4그룹**:
- ① **상수(1)** `test_marker_constants_are_byte_exact` — enrich 미사용, 마커 상수 불변.
- ② **텍스트 전용(1)** `test_text_only_needs_no_llm` — 모달 블록 없음 → Pass1에서 LLM 미호출·raise 없음.
- ③ **ValueError 가드(2)** `test_missing_text_llm_raises_for_table` / `test_missing_vision_llm_raises_for_image` — None 콜러블 → `ValueError`. **fallback 아님**. 메시지 `"text_llm is None"` / `"vision_llm is None"` 보존(Task5 코드의 raise 문자열 그대로). 윈도우 수집(`_gather_*`)이 None-체크보다 먼저 돌지만 부수효과 없어 무해.
- ④ **fallback(6)** `test_table_block_produces_one_atomic_modal_span`, `test_image_block_uses_vision_llm`, `test_equation_block_uses_text_llm`, `test_text_passes_through_and_order_preserved`, `test_each_modal_span_is_well_formed`, `test_modal_ids_unique_across_many` — 비-JSON mock → `_parse_boundary_response` fallback(흡수 0, 요약=원문), payload는 verbatim 삽입이라 단언 유지.

- [ ] **Step 5: 커밋**

```bash
git add kb_pipeline/modal.py tests/test_modal_absorb.py
git commit -m "feat(modal): absorb title/footnote into MODAL span via LLM boundary (Philosophy A)"
```

---

### Task 6: parse-svc modal_spans — 확장 span 캡처 검증

**Files:**
- Test: `parse_service/tests/test_parse.py` (테스트 추가; 코드 변경 없음 — 회귀 검증)

**Interfaces:**
- Consumes: `parse_service.app.run_parse`, `_MODAL_RE` (무변경). 확장 span 도 `[start,end)` 로 정확히 캡처되는지 확인.

- [ ] **Step 1: 실패할 수도 있는 회귀 테스트 작성** — `parse_service/tests/test_parse.py` 에 추가

```python
def test_modal_span_covers_absorbed_title_and_footnote(monkeypatch):
    """제목·각주 흡수 후에도 modal_spans char_range 가 확장 span 전체를 가리킨다."""
    import json
    import parse_service.app as svc

    # text 단락 + 파이프표 + text 각주.
    md = "캡션줄\n\n| a | b |\n| - | - |\n| 1 | 2 |\n\n각주줄\n"
    monkeypatch.setattr(svc, "parse_to_markdown", lambda b, f, **k: md)

    out = svc.run_parse(
        b"x", "d.pdf",
        text_llm=lambda prompt, payload: json.dumps(
            {"summary": "요약", "title_count": 1, "footnote_count": 1}
        ),
        vision_llm=None, ocr_url="http://x", excel_url="http://y",
    )
    enriched = out["enriched_content"]
    spans = out["modal_spans"]
    assert len(spans) == 1
    start, end = spans[0]["char_range"]
    sub = enriched[start:end]
    assert sub.startswith(MODAL_OPEN_PREFIX) and sub.endswith(MODAL_CLOSE)
    assert "요약" in sub          # 요약이 span 안
    assert "각주줄" in sub         # 흡수된 각주가 span 안
    # 흡수된 각주는 enriched 전체에서 1회만(외부 중복 0)
    assert enriched.count("각주줄") == 1
```

- [ ] **Step 2: 실행**

Run: `python -m pytest parse_service/tests/test_parse.py -q`
Expected: PASS (회귀 통과 시 코드 변경 불필요). FAIL 이면 `_MODAL_RE`/흡수 상호작용 디버그.

- [ ] **Step 3: (FAIL 시에만) 수정**

`_MODAL_RE` 가 확장 span 을 못 잡는 경우는 없어야 한다(`.*?` DOTALL). FAIL 이면 enrich 의 wrap 내부에 의도치 않은 `〈/MODAL〉` 가 끼었는지 확인. PASS 면 이 Step 생략.

- [ ] **Step 4: 커밋**

```bash
git add parse_service/tests/test_parse.py
git commit -m "test(parse-svc): modal_spans covers absorbed title/footnote span"
```

---

### Task 7: 전체 회귀 + 라이브 스모크(수동)

**Files:**
- (없음 — 검증만)

**Interfaces:**
- Consumes: 전체 테스트 스위트 + 실행 중인 facade(:19000)/adaptive_chunk(:18060)/parse-svc(:19001).

- [ ] **Step 1: 전체 단위 테스트**

Run: `python -m pytest tests/ parse_service/tests/ service/tests/ -q`
Expected: PASS (전부 green).

- [ ] **Step 2: parse-svc 재기동(새 modal 로직 로드)**

parse-svc(:19001) 프로세스를 재시작한다(기존 기동 방식대로). `GET :19001/healthz` 200 확인.

- [ ] **Step 3: 라이브 /parse 스모크 — 한글 요약 + 흡수 육안 확인**

실제 표가 있는 문서(예: 청원휴가 표)를 `POST :19001/parse` 로 보내고 응답 `enriched_content` 의 첫 〈MODAL…〈/MODAL〉 span 안에 (a) 제목, (b) **한국어** 요약, (c) `<table>`, (d) 각주가 모두 들어있는지 확인. 마커 없는 각주("각 대상에 대해…")가 잡혔는지 확인.
Expected: 사용자 확정 "결과" 형태와 일치.

- [ ] **Step 4: 커밋(문서/런북 갱신 시에만)**

```bash
git add -A && git commit -m "docs: modal title/footnote absorption smoke verified"
```

---

## Self-Review

- **Spec coverage:** 흡수(§3.1 Pass1/2)=Task5; LLM 계약+한글(§3.2)=Task3; wrap(§3.3)=Task4; fallback(§3.4)=Task2; 윈도우(§3.1)=Task1; modal_spans(§4)=Task6; 엣지(§5) 1–8 = Task5 신규 테스트 + Task6. 누락 없음.
- **Placeholder scan:** 모든 코드 step 에 실제 코드/명령/기대출력 포함. TBD 없음.
- **Type consistency:** `_gather_*`→`list[tuple[int,str]]`, `_parse_boundary_response`→`(str,int,int)`, `_wrap` kw `title`/`footnote`, `enrich` 시그니처 무변경 — Task 간 일치.
- **하위호환:** 무-흡수 wrap byte 동일(Task4 test), 비-JSON fallback(Task2/5 test). `tests/test_modal.py` 10개 green = **상수1 + 텍스트전용1 + ValueError가드2 + fallback6** (Task5 Step4). ValueError 가드 2개는 fallback이 아니라 None-콜러블 raise 경로(메시지 보존).
