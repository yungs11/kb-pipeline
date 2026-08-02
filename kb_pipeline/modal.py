"""W2 Modal enrichment — inline modal-block LLM descriptions (SoT 3.3 / 3.4).

Walk blocks in document order, produce a single enriched content string:
  * text blocks pass through as plain markdown
  * table / equation blocks -> text_llm(prompt, payload) description
  * image blocks            -> vision_llm(img_path, prompt) description
Each modal is inlined as ONE ATOMIC marker.

Surrounding context (Philosophy A — parser owns atomicity)
----------------------------------------------------------------
두 경로가 있다(문서 단위 플래그 ``enrich_modals`` 로 배타 결정):

* **``enrich_modals=False`` (shipped 기본) — 문맥 *복사*.** 표/이미지/수식 앞 블록의
  **마지막 100자**와 뒤 블록들의 **앞 200자**를 〈MODAL…〈/MODAL〉 span **안으로 복사**한다
  (예산이 찰 때까지 여러 블록 누적).
  패턴(제목/각주) 판정은 **하지 않는다** — 순수 글자수 규칙. **복사이므로 원본 블록은
  자기 자리에 그대로 남는다**(이동/흡수 아님) → 페이지 오귀속·원본 유실 없음. 대가는
  최대 300자 중복(임베딩/그래프 추출에서 2회 계상)이며 수용된 트레이드오프다.
* **``enrich_modals=True`` — LLM 흡수(기존, 불변).** per-modal LLM 이 한국어 ``summary``
  + ``title_count``/``footnote_count`` 를 돌려주고, 그만큼의 앞뒤 블록을 원문 그대로
  span 안으로 **흡수(이동)** 한다(원본 세그먼트는 사라짐). LLM 이 유효 JSON 을 주지
  않으면 흡수 0(summary = raw) 으로 강등 — pre-absorption 동작과 byte-compatible.

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

#: 모달 앞/뒤에서 고려할 연속 text 블록 최대 수.
#: LLM 흡수 경로에선 '제목·각주 후보' 개수, 복사 경로에선 '문맥 스캔 범위'다
#: (복사는 이 윈도우 안의 비공백 블록을 **예산(앞100/뒤200자)이 찰 때까지** 누적한다.)
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


# --- LLM-free context copy (pure) ---------------------------------------------
# enrich_modals=False & wrap_modals=True 경로(shipped 기본)는 LLM 을 안 부르므로 주변
# 문맥을 **글자수 규칙으로 복사**한다. 제목/각주 패턴 판정은 하지 않는다(오탐 제거).
# 복사이므로 원본 블록은 그대로 남는다 — 페이지 오귀속·유실이 구조적으로 불가능.

#: 표 '앞' 문맥으로 복사할 최대 길이 — 표에 가까운 '끝'에서 100자.
_CTX_COPY_BEFORE_CHARS = 100
#: 표 '뒤' 문맥으로 복사할 최대 길이 — 표에 가까운 '앞'에서 200자.
#: 각주·단서가 표 뒤에 여러 줄로 붙는 경우가 많아 앞(100)보다 넉넉히 잡는다.
_CTX_COPY_AFTER_CHARS = 200


def _nonblank_texts(cands: list[tuple[int, str]]) -> list[str]:
    """nearest-first 후보에서 strip 후 비지 않은 텍스트만(순서 보존).

    ``_gather_*_window`` 는 빈/공백 text 블록도 window 에 넣으므로 그대로 쓰면 문맥이
    빈 조각으로 채워진다 → strip 후 빈 항목은 건너뛴다. 스캔 범위는
    BEFORE_WINDOW=3 / AFTER_WINDOW=6.
    """
    return [s for _, t in cands if (s := (t or "").strip())]


def _ctx_copy_before(before: list[tuple[int, str]]) -> str:
    """표 앞 문맥 — 표에서 거슬러 올라가며 **예산(_CTX_COPY_BEFORE_CHARS)을 채운다**.

    한 블록만 쓰면 예산이 남아도 멈춰 정작 중요한 제목을 놓친다(실측: 표 직전이
    ``(개정 2025.09.01.)`` 17자면 그것만 들어가고 그 앞의 제목 ``가정의례와 관련된 청원휴가
    허가기준`` 이 빠짐). → 여러 블록을 표에 가까운 순으로 누적하고, 예산을 넘기면 **가장 먼
    블록을 끝에서부터 잘라** 예산을 정확히 채운다. 블록 사이는 ``\\n`` 으로 잇는다.
    """
    budget = _CTX_COPY_BEFORE_CHARS
    parts: list[str] = []                       # 문서순(먼 것 → 가까운 것)으로 뒤집어 쌓는다
    for s in _nonblank_texts(before):           # before 는 nearest-first
        if budget <= 0:
            break
        if len(s) > budget:
            s = s[-budget:]                     # 표에 가까운 쪽(끝)만 남긴다
        parts.append(s)
        budget -= len(s) + 1                    # +1: 블록 사이 개행
    return "\n".join(reversed(parts))


def _ctx_copy_after(after: list[tuple[int, str]]) -> str:
    """표 뒤 문맥 — 표에서 내려가며 **예산(_CTX_COPY_AFTER_CHARS)을 채운다**.

    ``_ctx_copy_before`` 대칭. 각주가 여러 블록으로 쪼개져 있어도(``각 대상에…`` /
    ``** 사망 시의…`` / ``*** "2. 회갑"…``) 예산 안에서 이어 담고, 마지막 블록이 예산을
    넘으면 **앞에서부터 잘라** 채운다.
    """
    budget = _CTX_COPY_AFTER_CHARS
    parts: list[str] = []
    for s in _nonblank_texts(after):            # after 는 nearest-first(문서순과 동일)
        if budget <= 0:
            break
        if len(s) > budget:
            s = s[:budget]                      # 표에 가까운 쪽(앞)만 남긴다
        parts.append(s)
        budget -= len(s) + 1
    return "\n".join(parts)


#: oversize 안전상한(문자). bge-m3 윈도우 8192tok, ~2.3char/tok → 6000tok≈13800자.
#: 조립될 span 전체 추정치가 이보다 크면 2단계로 줄인다(A-guard): ①먼저 복사 문맥(ctx)만
#: 버려 원자화는 유지하고, ②본체(summary+payload+LLM 흡수분)만으로도 초과면 bare 로 강등해
#: atomic 원자화로 못 쪼개지는 ContextWindowExceeded 를 방지한다(초과 오판=bare 안전측).
_OVERSIZE_CHARS = 13800


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

    title/footnote 슬롯의 의미는 호출 경로에 따라 다르다:
      * LLM 흡수 경로 — 앞뒤 블록 **원문 전체**(그 블록들은 세그먼트에서 사라진다).
      * 복사 경로(shipped 기본) — 앞 끝 **≤100자** / 뒤 앞 **≤200자 사본**
        (원본 블록은 자기 자리에 그대로 남는다). 이 경로는 ``description=""`` 이라
        segments = [title, "", payload] → ``title\\n\\npayload`` 로 **빈 줄 1개**가 낀다.

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
            # per-decision bare(oversize 가드): wrap_modals=True 여도 이 table 은 마커 없이
            # payload 만 emit — 원자화하면 bge-m3 윈도우 초과로 못 쪼개져 적재실패하기 때문.
            if wrap_modals and not d.get("bare", False):
                if d.get("ctx_mode"):
                    # 복사 경로: 앞 ≤100자 / 뒤 ≤200자 사본만 쓴다(빈 문자열도 그대로 =
                    # 문맥 없음). 원본 블록은 consumed 가 아니라 세그먼트로 그대로 남는다.
                    title, footnote = d.get("ctx_before", ""), d.get("ctx_after", "")
                else:
                    # LLM 흡수 경로(불변): consumed 된 앞뒤 블록 원문을 join.
                    title = "\n".join(blocks[j].get("text", "") for j in d["title_idxs"])
                    footnote = "\n".join(blocks[j].get("text", "") for j in d["footnote_idxs"])
                seg = _wrap(
                    d["modal_id"], d["modal_type"], d["summary"], d["payload"],
                    title=title, footnote=footnote,
                )
            else:
                # 모달 비활성(wrap_modals=False) 또는 oversize bare: 〈MODAL〉 래핑 없이
                # OpenDataLoader 원본 payload 를 그대로 통과. 흡수 0(tc=fc=0)·복사 0(ctx="")
                # 이라 주변 블록은 인접 text 세그먼트로 남는다(무손실). decision 은 유지되므로
                # table drop 없음.
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
    wrap_modals: bool = True,
) -> tuple[dict[int, dict], set[int], list[str]]:
    """Phase A–C 공통 코어 — ``(decisions, consumed, modal_ids)`` 를 반환.

    enrich / enrich_with_spans 가 공유한다. 모달 식별·LLM 병렬 호출·충돌 해소까지 수행하고
    Phase D 조립은 호출자가 ``_assemble`` 로 한다. modal_ids 는 문서순(흡수되지 않은 모달).

    ``enrich_modals`` 와 ``wrap_modals`` 는 **분리**된 스위치다:
      * ``enrich_modals=False`` → **모달 LLM 을 호출하지 않는다**(Phase B 스킵). 요약은 빈
        문자열이고, 주변 문맥은 흡수 대신 **복사**한다(``_ctx_copy_before/after`` — 앞
        ≤100자·뒤 ≤200자 사본이 span 안으로 들어가고 **원본 블록은 그대로 남는다**).
        tc/fc 는 0 이라 ``consumed`` 는 항상 공집합.
      * ``wrap_modals`` → Phase C **consume**(LLM 경로에서 제목/각주를 모달 안으로 흡수)과
        _assemble 의 마커 래핑을 켠다/끈다. **False 면** consume 을 스킵해 제목/각주가 일반
        text 블록으로 남고(무손실) 복사도 하지 않으며(ctx="") _assemble 이 마커 없이
        payload 만 통과한다. **단 decisions 는 항상 채운다**(빈 idxs 로라도) — 안 채우면
        _assemble 이 table 을 drop(데이터 유실).
      * oversize(조립 span 추정 > ``_OVERSIZE_CHARS``)는 **2단계**로 줄인다(A-guard):
        ①복사 문맥(ctx)만 버려 래핑은 유지 → ②본체만으로도 초과면 tc/fc=0 + ``bare=True``
        로 강등해 wrap_modals=True 여도 마커 없이 payload 만 emit(적재실패 방지).
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
        # 모달 LLM 비활성(KBP_MODAL_ENRICH=0): LLM 0 회. 요약은 빈 문자열. 주변 문맥은
        # wrap_modals=True 면 글자수 규칙으로 **복사**(앞 끝200자·뒤 앞100자), False 면 없음.
        for m in modals:
            m["summary"] = ""
            if wrap_modals:
                m["ctx_before"] = _ctx_copy_before(m["before"])
                m["ctx_after"] = _ctx_copy_after(m["after"])
            else:
                m["ctx_before"] = m["ctx_after"] = ""
            # 복사는 흡수가 아니라 tc/fc=0 (consumed 공집합 → 원본 블록 생존).
            # ⚠️ 반드시 유지 — A-guard 와 Phase C 가 읽는다(KeyError 방지).
            m["tc"], m["fc"] = 0, 0
    elif modals:
        workers = min(max_workers, len(modals))  # max_workers>=1 검증됨; modals 비어있지 않음
        _b0 = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            for m, (summary, tc, fc) in zip(modals, ex.map(_call, modals)):
                m["summary"], m["tc"], m["fc"] = summary, tc, fc
                # LLM 경로는 흡수 그대로 — 복사 안 함. 키만 채워 하류 KeyError 방지.
                m["ctx_before"] = m["ctx_after"] = ""
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

    # A-guard(oversize) — **2단계**. 조립될 span 추정치가 _OVERSIZE_CHARS 초과면:
    #   ① 먼저 복사 문맥(ctx, 최대 300자)만 버린다 — 원자화(마커)는 유지. ctx 300자 때문에
    #      13500자 표를 bare 로 강등하면 래핑 가능한 표의 원자성을 공짜로 잃기 때문.
    #   ② 본체(summary+payload+LLM 흡수분)만으로도 초과면 tc/fc=0 + bare(마커 없이 payload).
    # 복사·LLM 경로 공통 — 임의 미래문서(다페이지 대형표)의 임베딩 초과를 막는다.
    for m in modals:
        title_est = sum(len(t) for _, t in m["before"][:m["tc"]])
        foot_est = sum(len(t) for _, t in m["after"][:m["fc"]])
        base_est = len(m["summary"]) + len(m["body"]) + title_est + foot_est
        if base_est + len(m["ctx_before"]) + len(m["ctx_after"]) > _OVERSIZE_CHARS:
            m["ctx_before"] = m["ctx_after"] = ""   # 1순위: 문맥 포기(원자성 유지)
        m["bare"] = base_est > _OVERSIZE_CHARS       # 2순위: 본체만으로도 초과면 bare
        if m["bare"]:
            m["tc"], m["fc"] = 0, 0
            m["ctx_before"] = m["ctx_after"] = ""

    # Phase C — 충돌 해소(문서순; 앞 모달 우선, 모달에서 연속, consumed 만나면 중단).
    # consume(제목/각주 흡수)만 wrap_modals 게이트 — decisions 는 **항상** 채워야 table 이
    # _assemble 에서 drop(데이터 유실)되지 않는다.
    # ⚠️ 복사 경로는 tc=fc=0 이라 여기서 아무것도 consume 하지 않는다(원본 전부 생존).
    # 따라서 '앞 모달 선점' 충돌해소도 복사엔 미적용 — [표1][X][표2] 의 X 는 표1.ctx_after·
    # 표2.ctx_before·원본으로 **3중 등장**한다(패턴 판정 없음의 귀결, 의도된 계약).
    consumed: set[int] = set()
    decisions: dict[int, dict] = {}
    modal_ids: list[str] = []
    for m in modals:
        title_idxs: list[int] = []
        footnote_idxs: list[int] = []
        if wrap_modals:
            for idx, _ in m["before"][:m["tc"]]:
                if idx in consumed:
                    break
                title_idxs.append(idx)
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
            "bare": m["bare"],
            # 복사 경로 명시 플래그(truthiness 폴백 금지 — _assemble 이 배타 분기).
            "ctx_mode": bool(m["ctx_before"] or m["ctx_after"]),
            "ctx_before": m["ctx_before"], "ctx_after": m["ctx_after"],
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
    wrap_modals: bool = True,
) -> tuple[str, list[str]]:
    """Enrich blocks into a single content string + ordered modal ids.

    ``enrich_modals=True`` — 모달(table/image/equation)마다 LLM 호출 1회로 한국어 요약 +
    주변 text 의 제목/각주 개수를 판정해, 제목·각주를 원문 그대로 〈MODAL…〈/MODAL〉 안으로
    **흡수(이동)** 한다. LLM 호출은 스레드풀로 **병렬** 실행하고(표 많은 문서의 parse 시간
    단축), 두 모달이 같은 사이 블록을 다투면 문서순으로 앞 모달이 선점한다(사후 충돌 해소).
    LLM 이 JSON 을 주지 않으면 흡수 0건 + 요약=원문(하위호환).

    ``enrich_modals=False`` (shipped 기본, LLM 0회) — 패턴 판정 없이 앞 블록 **끝 200자** +
    뒤 블록 **앞 100자**를 span 안으로 **복사**한다(요약은 빈 문자열). 복사이므로 **원본
    블록은 자기 페이지의 세그먼트로 그대로 남고**, 사본만 모달 span(=표 페이지)에 계상된다.
    ``wrap_modals=False`` 면 복사도 하지 않는다.

    :param text_llm: ``(prompt, payload) -> description`` for table/equation.
    :param vision_llm: ``(img_path, prompt) -> description`` for image.
    :param max_workers: 모달 LLM 동시 호출 상한(기본 8, 모달 수로 추가 제한).
    :returns: ``(enriched_content, modal_ids)``.
    :raises ValueError: if ``max_workers < 1``, or if a modal of a kind appears but
        its callable is None.
    """
    decisions, consumed, modal_ids = _enrich_core(
        blocks, text_llm=text_llm, vision_llm=vision_llm, max_workers=max_workers,
        enrich_modals=enrich_modals, wrap_modals=wrap_modals,
    )
    segments, _ = _assemble(blocks, decisions, consumed, wrap_modals=wrap_modals)
    return _SEGMENT_JOIN.join(segments), modal_ids


def enrich_with_spans(
    blocks: list[dict],
    *,
    text_llm: Callable[[str, str], str] | None,
    vision_llm: Callable[[str, str], str] | None,
    max_workers: int = 8,
    timing_sink: dict | None = None,
    enrich_modals: bool = True,
    wrap_modals: bool = True,
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

    ⚠️ 복사 경로(enrich_modals=False & wrap_modals=True)의 페이지 귀속 계약: **원본 블록의
    페이지 귀속은 불변**(이동이 아니므로 유실·재귀속 없음). 다만 **사본은 모달 세그먼트 안에
    있으므로 모달(표)의 페이지에 귀속**된다 — 앞 블록이 이전 페이지면 그 ≤200자 사본은 표
    페이지 span 에 계상된다. 하류(chunks_meta.page_number/페이지이미지)는 이를 전제한다.

    :returns: ``(enriched_content, modal_ids, page_spans)`` where
        ``page_spans = [{"page_number": int, "char_start": int, "char_end": int}, ...]``
        sorted by ``page_number``. char 오프셋은 enriched_content 기준(반열린 구간).
    """
    decisions, consumed, modal_ids = _enrich_core(
        blocks, text_llm=text_llm, vision_llm=vision_llm, max_workers=max_workers,
        timing_sink=timing_sink, enrich_modals=enrich_modals, wrap_modals=wrap_modals,
    )
    segments, seg_page_idx = _assemble(blocks, decisions, consumed, wrap_modals=wrap_modals)
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
