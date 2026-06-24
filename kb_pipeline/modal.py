"""W2 Modal enrichment — inline modal-block LLM descriptions (SoT 3.3 / 3.4).

Walk blocks in document order, produce a single enriched content string:
  * text blocks pass through as plain markdown
  * table / equation blocks -> text_llm(prompt, payload) description
  * image blocks            -> vision_llm(img_path, prompt) description
Each modal is inlined as ONE ATOMIC marker.

Title/footnote absorption (Philosophy A — parser owns atomicity)
----------------------------------------------------------------
A table/image/equation's **title/caption** (the text block(s) immediately before)
and **footnote/notes** (the text block(s) immediately after) are absorbed VERBATIM
into the same atomic 〈MODAL…〈/MODAL〉 span so they never get split away by the
downstream chunker. Which surrounding lines belong to the modal is decided by the
SAME per-modal LLM call that already produces the description: it returns a Korean
``summary`` plus ``title_count`` / ``footnote_count``. If the LLM does not return
valid JSON, we fall back to no absorption (summary = raw response) — byte-compatible
with the pre-absorption behavior.

EXACT modal marker (single source of truth — producer here and the W1 Rust
consumer MUST use byte-identical markers). The angle-bracket chars are
U+3008 〈 (open) and U+3009 〉 (close):

    〈MODAL id="X" type="table|image|equation"〉{body}〈/MODAL〉

The marker is closed with:  〈/MODAL〉
"""

from __future__ import annotations

import concurrent.futures
import json
import time
from typing import Callable

# U+3008 / U+3009 — byte-identical with the Rust consumer.
_LANGLE = "〈"  # 〈
_RANGLE = "〉"  # 〉

#: Literal prefix that opens a modal marker (before id/type attributes).
MODAL_OPEN_PREFIX = f"{_LANGLE}MODAL"
#: Literal closing marker.
MODAL_CLOSE = f"{_LANGLE}/MODAL{_RANGLE}"

#: 모달 앞/뒤에서 제목·각주 후보로 고려할 연속 text 블록 최대 수.
BEFORE_WINDOW = 3
AFTER_WINDOW = 6


# --- candidate windows (pure) -------------------------------------------------

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


# --- boundary LLM response parser (pure) --------------------------------------

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


# --- Korean prompt + boundary payload -----------------------------------------

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
        "2) '앞 후보' 중 이 본문의 제목/머리글/캡션인 줄 수(title_count)를 세어라.\n"
        "   - 표·그림의 이름이나 번호를 가리키는 제목 줄(예: 「○○ 기준」, 「[표 1] …」,\n"
        "     본문 바로 위에서 이 본문을 지칭하는 머리글)은 **반드시 포함**하라.\n"
        "   - 그 제목 바로 아래의 개정일자·근거·시행일 같은 부가 표기도 제목의 일부로 포함하라.\n"
        "   - 본문에 가까운 쪽부터 연속으로 세고, 본문과 무관한 앞 단락(다른 주제의 문장)은 제외하라.\n"
        "3) '뒤 후보' 중 이 본문의 각주/설명/단서인 줄 수(footnote_count)를 세어라.\n"
        "   - 표 아래의 주석·예외·산정기준·비고(마커 `*`/`**`/`※`/`주)` 유무 무관)는 포함하라.\n"
        "   - 본문에 가까운 쪽부터 연속으로 세고, 다음 절의 제목이나 무관한 본문은 제외하라.\n"
        '오직 JSON만 출력하라: {"summary": "...", "title_count": N, "footnote_count": M}'
    )


def _boundary_payload(before: list[tuple[int, str]], after: list[tuple[int, str]], body: str) -> str:
    """앞 후보(B1..)/본문/뒤 후보(A1..)를 한 문자열로. before/after 는 nearest-first."""
    lines = ["[앞 후보 — 본문에서 가까운 순]"]
    lines += [f"B{k}: {t}" for k, (_, t) in enumerate(before, 1)] or ["(없음)"]
    lines += ["", "[본문]", body, "", "[뒤 후보 — 본문에서 가까운 순]"]
    lines += [f"A{k}: {t}" for k, (_, t) in enumerate(after, 1)] or ["(없음)"]
    return "\n".join(lines)


# --- marker assembly ----------------------------------------------------------

def _open_marker(modal_id: str, modal_type: str) -> str:
    return f'{MODAL_OPEN_PREFIX} id="{modal_id}" type="{modal_type}"{_RANGLE}'


def _wrap(modal_id: str, modal_type: str, description: str, payload: str,
          *, title: str = "", footnote: str = "") -> str:
    """원자 〈MODAL …〉[title]\\n{desc}\\n{payload}\\n[footnote]〈/MODAL〉 span 생성.

    title/footnote 가 비면 ``{open}{desc}\\n{payload}{close}`` 로 현재와 byte 동일.
    """
    segments: list[str] = []
    if title:
        segments.append(title)
    segments.append(description)
    segments.append(payload)
    if footnote:
        segments.append(footnote)
    return f"{_open_marker(modal_id, modal_type)}" + "\n".join(segments) + MODAL_CLOSE


#: Segments are joined into the enriched content with a blank line between them.
#: This is a TWO-character join ("\n" + "\n") and page_span offset arithmetic in
#: ``enrich_with_spans`` MUST account for exactly these 2 chars between segments.
_SEGMENT_JOIN = "\n\n"


def _assemble(
    blocks: list[dict],
    decisions: dict[int, dict],
    consumed: set[int],
    *,
    wrap_modals: bool = True,
) -> tuple[list[str], list[int]]:
    """Phase D 조립 — 출력 세그먼트 리스트와 각 세그먼트의 page_idx 를 만든다.

    enrich / enrich_with_spans 가 공유하는 조립 로직. ``decisions`` 와 ``consumed`` 는
    Phase A–C(모달 식별·LLM·충돌 해소)의 결과로, 이 함수는 그것을 문서순으로 평탄화한다.

    :returns: ``(segments, seg_page_idx)`` — 두 리스트는 길이가 같고 index 정렬된다.
        ``segments[k]`` 는 enriched 본문 한 조각, ``seg_page_idx[k]`` 는 그 조각을 만든
        블록의 ``page_idx``(모달 세그먼트는 모달 블록의 page_idx). page_idx 키가 없으면 0.
    """
    segments: list[str] = []
    seg_page_idx: list[int] = []
    n = len(blocks)
    for i in range(n):
        if i in consumed:
            continue
        if i in decisions:
            d = decisions[i]
            if wrap_modals:
                title = "\n".join(blocks[j].get("text", "") for j in d["title_idxs"])
                footnote = "\n".join(blocks[j].get("text", "") for j in d["footnote_idxs"])
                seg = _wrap(
                    d["modal_id"], d["modal_type"], d["summary"], d["payload"],
                    title=title, footnote=footnote,
                )
            else:
                # 모달 비활성: 〈MODAL〉 래핑 없이 OpenDataLoader 원본 payload 를 그대로 통과.
                # 제목/각주는 흡수 0(tc=fc=0)이라 인접 text 블록으로 남고, atomic 마커가 없으니
                # recursive 청커가 제목·표·각주를 자연스럽게 한 청크로 묶는다(표가 제목/각주에서
                # 떨어져 나가 청크가 깨지던 문제 해소).
                seg = d["payload"]
            segments.append(seg)
            seg_page_idx.append(int(blocks[i].get("page_idx", 0) or 0))
        elif blocks[i].get("type") == "text":
            text = blocks[i].get("text", "")
            if text:
                segments.append(text)
                seg_page_idx.append(int(blocks[i].get("page_idx", 0) or 0))
        # 알 수 없는 타입: 무시(기존과 동일).
    return segments, seg_page_idx


def _enrich_core(
    blocks: list[dict],
    *,
    text_llm: Callable[[str, str], str] | None,
    vision_llm: Callable[[str, str], str] | None,
    max_workers: int,
    timing_sink: dict | None = None,
    enrich_modals: bool = True,
) -> tuple[dict[int, dict], set[int], list[str]]:
    """Phase A–C 공통 코어 — ``(decisions, consumed, modal_ids)`` 를 반환.

    enrich / enrich_with_spans 가 공유한다. 모달 식별·LLM 병렬 호출·충돌 해소까지 수행하고
    Phase D 조립은 호출자가 ``_assemble`` 로 한다. modal_ids 는 문서순(흡수되지 않은 모달).

    ``enrich_modals=False`` 면 **모달 LLM 을 호출하지 않고**(Phase B 스킵) 각 모달을
    요약 없음·흡수 0(``summary=""``, ``tc=fc=0``)으로 강등한다 — 즉 OpenDataLoader 원본
    payload 를 그대로 ``〈MODAL〉…〈/MODAL〉`` 로 감싸 통과시킨다(원자성·page_spans 유지,
    LLM 0 회). LLM 실패 폴백과 byte-동일한 경로다.
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
        # enrich_modals=False 면 LLM 을 안 부르므로 callable 이 None 이어도 무방(원본 payload
        # 통과). enrich_modals=True 일 때만 해당 종류의 LLM 이 필요하다.
        if enrich_modals and btype in ("table", "equation") and text_llm is None:
            raise ValueError(
                f"{btype} block encountered but text_llm is None; "
                f"a text LLM callable is required to describe {btype}s."
            )
        if enrich_modals and btype == "image" and vision_llm is None:
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
    # 런타임 LLM 실패(524/timeout/5xx 등)는 **그 모달만** 흡수 0·요약 생략으로 강등하고
    # 문서 전체는 살린다(표 payload 는 wrap 에 그대로 보존). 재시도는 안 한다 — 524 는 보통
    # 일관적이고, 재시도가 동시호출 수를 키워 프록시를 더 과부하시켜 524 를 늘리기 때문.
    # 모달별 LLM 호출 시간(type, ms). list.append 는 GIL 원자 → 스레드풀서 안전.
    _call_ms: list[tuple[str, float]] = []

    def _call(m: dict) -> tuple[str, int, int]:
        prompt = _boundary_prompt(m["type"])
        payload = _boundary_payload(m["before"], m["after"], m["body"])
        _t0 = time.perf_counter()
        try:
            if m["type"] == "image":
                raw = vision_llm(m["body"], prompt + "\n\n" + payload)
            else:
                raw = text_llm(prompt, payload)
            res = _parse_boundary_response(raw, len(m["before"]), len(m["after"]))
        except Exception:  # noqa: BLE001 — 어떤 LLM 실패든 모달 단위로 강등(문서는 생존)
            res = ("", 0, 0)
        _call_ms.append((m["type"], (time.perf_counter() - _t0) * 1000.0))
        return res

    modal_wall_ms = 0.0
    if not enrich_modals:
        # 모달 LLM 비활성(KBP_MODAL_ENRICH=0): LLM 0 회 — 각 모달을 요약 없음·흡수 0 으로
        # 강등해 OpenDataLoader 원본 payload 를 그대로 〈MODAL〉 로 감싼다(원자성 유지).
        for m in modals:
            m["summary"], m["tc"], m["fc"] = "", 0, 0
    elif modals:
        workers = min(max_workers, len(modals))  # max_workers>=1 검증됨; modals 비어있지 않음
        _b0 = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            for m, (summary, tc, fc) in zip(modals, ex.map(_call, modals)):
                m["summary"], m["tc"], m["fc"] = summary, tc, fc
        modal_wall_ms = (time.perf_counter() - _b0) * 1000.0

    # 모니터링(P2): 모달 LLM(표/이미지 분석) 단계 분해 — wall(병렬) + 호출 수 + 타입별 합 +
    # per-call 상위. 표 N개×LLM 가 파서 ~5분의 유력 진원지인지 데이터로 드러낸다.
    if timing_sink is not None:
        by_type: dict[str, dict] = {}
        for t, ms in _call_ms:
            d = by_type.setdefault(t, {"n": 0, "ms": 0.0})
            d["n"] += 1
            d["ms"] = round(d["ms"] + ms, 1)
        timing_sink.update({
            "modal_llm_wall_ms": round(modal_wall_ms, 1),
            "modal_llm_calls": len(_call_ms),
            "by_type": by_type,
            "counters": dict(counters),
            "max_workers": max_workers,
            "per_call_ms": sorted((round(ms, 1) for _, ms in _call_ms), reverse=True)[:20],
        })

    # Phase C — 충돌 해소(문서순; 앞 모달 우선, 모달에서 연속, consumed 만나면 중단).
    consumed: set[int] = set()
    decisions: dict[int, dict] = {}
    modal_ids: list[str] = []
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

    # modal_ids: 문서순으로 (흡수되지 않은) 모달만 — _assemble 출력 순서와 일치.
    for i in range(n):
        if i in consumed:
            continue
        if i in decisions:
            modal_ids.append(decisions[i]["modal_id"])

    return decisions, consumed, modal_ids


def enrich(
    blocks: list[dict],
    *,
    text_llm: Callable[[str, str], str] | None,
    vision_llm: Callable[[str, str], str] | None,
    max_workers: int = 8,
    enrich_modals: bool = True,
) -> tuple[str, list[str]]:
    """Enrich blocks into a single content string + ordered modal ids.

    모달(table/image/equation)마다 LLM 호출 1회로 한국어 요약 + 주변 text 의
    제목/각주 개수를 판정해, 제목·각주를 원문 그대로 〈MODAL…〈/MODAL〉 안으로 흡수한다.
    LLM 호출은 스레드풀로 **병렬** 실행하고(표 많은 문서의 parse 시간 단축), 두 모달이
    같은 사이 블록을 다투면 문서순으로 앞 모달이 선점한다(사후 충돌 해소). LLM 이 JSON 을
    주지 않으면 흡수 0건 + 요약=원문(하위호환).

    :param text_llm: ``(prompt, payload) -> description`` for table/equation.
    :param vision_llm: ``(img_path, prompt) -> description`` for image.
    :param max_workers: 모달 LLM 동시 호출 상한(기본 8, 모달 수로 추가 제한).
    :returns: ``(enriched_content, modal_ids)``.
    :raises ValueError: if ``max_workers < 1``, or if a modal of a kind appears but
        its callable is None.
    """
    decisions, consumed, modal_ids = _enrich_core(
        blocks, text_llm=text_llm, vision_llm=vision_llm, max_workers=max_workers,
        enrich_modals=enrich_modals,
    )
    segments, _ = _assemble(blocks, decisions, consumed, wrap_modals=enrich_modals)
    return _SEGMENT_JOIN.join(segments), modal_ids


def enrich_with_spans(
    blocks: list[dict],
    *,
    text_llm: Callable[[str, str], str] | None,
    vision_llm: Callable[[str, str], str] | None,
    max_workers: int = 8,
    timing_sink: dict | None = None,
    enrich_modals: bool = True,
) -> tuple[str, list[str], list[dict]]:
    """``enrich`` 와 동일하게 조립하되, page 별 char-span 도 함께 산출한다(spec 5.1.4).

    ``enriched`` / ``modal_ids`` 는 :func:`enrich` 와 **byte-identical** 이다(같은 코어/
    조립 경로). 추가로 ``page_spans`` 를 반환한다 — enriched_content 의 문자 오프셋 기준으로
    각 페이지가 차지하는 ``[char_start, char_end)`` 반열린 구간.

    page_spans 산출(명시적 오프셋 추적):
      * ``_assemble`` 가 만든 ``segments`` 를 ``"\\n\\n"`` 로 이어 붙이며 running offset 을
        누적한다. 세그먼트 사이의 blank-line join(2자)도 offset 에 포함한다.
      * 세그먼트 k 의 페이지 = ``seg_page_idx[k]`` (= 그 세그먼트를 만든 블록의 ``page_idx``;
        모달 세그먼트는 모달 블록의 page_idx). 페이지별로 min(char_start)/max(char_end) 를
        모아 span 1개씩 만든다(``page_number = page_idx``).
      * 블록에 page_idx 가 전부 비면(전부 0) → 전체를 page 1 로 덮는 단일 span 으로 강등.

    :returns: ``(enriched_content, modal_ids, page_spans)`` where
        ``page_spans = [{"page_number": int, "char_start": int, "char_end": int}, ...]``
        sorted by ``page_number``. char 오프셋은 enriched_content 기준(반열린 구간).
    """
    decisions, consumed, modal_ids = _enrich_core(
        blocks, text_llm=text_llm, vision_llm=vision_llm, max_workers=max_workers,
        timing_sink=timing_sink, enrich_modals=enrich_modals,
    )
    segments, seg_page_idx = _assemble(blocks, decisions, consumed, wrap_modals=enrich_modals)
    enriched = _SEGMENT_JOIN.join(segments)

    # 페이지별 [min char_start, max char_end) 를 running offset 으로 누적.
    join_len = len(_SEGMENT_JOIN)
    page_bounds: dict[int, list[int]] = {}  # page_idx -> [char_start, char_end]
    offset = 0
    any_page_marked = False
    for k, seg in enumerate(segments):
        if k > 0:
            offset += join_len  # 세그먼트 사이 blank-line join(2자)
        start = offset
        end = offset + len(seg)
        offset = end
        pidx = seg_page_idx[k]
        if pidx:
            any_page_marked = True
        if pidx in page_bounds:
            b = page_bounds[pidx]
            if start < b[0]:
                b[0] = start
            if end > b[1]:
                b[1] = end
        else:
            page_bounds[pidx] = [start, end]

    # 모든 page_idx 가 0(미표기) → 전체를 page 1 로 덮는 단일 span 으로 강등(안전).
    if not any_page_marked:
        if not enriched:
            return enriched, modal_ids, []
        return enriched, modal_ids, [
            {"page_number": 1, "char_start": 0, "char_end": len(enriched)}
        ]

    page_spans = [
        {"page_number": pidx, "char_start": b[0], "char_end": b[1]}
        for pidx, b in sorted(page_bounds.items())
    ]
    return enriched, modal_ids, page_spans
