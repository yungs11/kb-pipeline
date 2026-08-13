"""PDF 도메인 파서 — ODL/VL/Paddle gateway 라우팅. 스캔 페이지는 OCR 보충."""
from __future__ import annotations

import logging
import collections
import os
import re

from parse_service.parsers import RouteResult, ParserError
from parse_service.tools import ToolError
from parse_service.tools.opendataloader import convert_pdf_to_page_markdowns

log = logging.getLogger("kb_pipeline.parse_service.parsers.pdf")
# digital(=텍스트 추출 성공) 판정 최소 **실제 텍스트** 글자수. 스캔 페이지도 ODL 이
# 이미지 참조/빈 표 구조를 non-empty markdown 으로 내므로, raw 길이가 아니라 태그·이미지
# 참조를 뺀 실 텍스트로 판정해야 스캔 페이지가 VL 로 넘어간다(2026-07-07 버그수정).
_DIGITAL_MIN_CHARS = 1
_HTML_TAG_RE = re.compile(r"<[^>]+>")   # <table>/<td> 등 (빈 표는 태그만 → 실텍스트 0)
_WS_RE = re.compile(r"\s+")


def _digital_text_len(md: str) -> int:
    """페이지 markdown 의 **실제 텍스트** 글자수 — 이미지 참조 줄/HTML 태그/공백 제외.

    OpenDataLoader 는 스캔 페이지에도 `![alt](path)` 이미지 참조나 빈 표
    (`<table><td> </td></table>`)를 non-empty 로 낸다. 이를 실텍스트로 세지 않아야
    그런 페이지가 digital 로 오판되지 않고 VL(OCR) 로 넘어간다. 이미지 참조는 경로에
    `)` 가 들어갈 수 있어(한글 파일명 등) 정규식 대신 **줄 단위**로 제거한다.
    """
    kept = [ln for ln in (md or "").splitlines() if not ln.lstrip().startswith("![")]
    stripped = _HTML_TAG_RE.sub("", "\n".join(kept))
    return len(_WS_RE.sub("", stripped))


def _page_markdowns(file_bytes: bytes, filename: str) -> list[str]:
    return convert_pdf_to_page_markdowns(file_bytes, filename)


# ─────────────────────────────────────────────────────────────────────────────
# Plan A §A4 — 스캔 페이지(paddle_gw)의 layout 기반 전면 VL 처리
# ─────────────────────────────────────────────────────────────────────────────
_VISUAL_LABELS = {"image", "figure", "chart"}
_PIPE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_FENCE_RE = re.compile(r"```json\s*|\s*```")


def _label(b: dict) -> str:
    """layout 블록 라벨 — 소문자 정규화(blockify 의 category 정규화 선례와 동일).

    정규화를 빼면 게이트웨이가 ``Table``/``Image`` 표기로 바뀔 때 판정이 조용히 꺼진다.
    """
    return (b.get("block_label") or "").strip().lower()


def _contributes(b: dict, page_size, counters: dict) -> bool:
    """이 layout 블록이 "전면 VL 이 필요한 그림"으로 쳐지는가.

    면적 하한(``KBP_VL_VISUAL_MIN_AREA``, 기본 0.05)을 image/figure/chart **전부에** 적용한다.
    실 스캔 실측(2026-08-02): 법원통지서 p1 의 QR 은 0.54% 로 걸러지고 그림 지배 페이지는
    34.6~43% 로 통과했다. 참양성 최소값은 5.06%(ABL p36) — 여유가 1.01배뿐이라 임계를 올리면
    참양성을 놓친다.

    **면적을 알 수 없으면 fail-closed(False)** 다. 발동은 paddle 본문이 VL 로 교체되는 회귀지만
    미발동은 현행 유지라 회귀가 아니다 — 불확실할 땐 현행을 택한다. 게이트웨이가 width/height 를
    주지 않을 때 fail-open 이면 전 페이지가 무조건 전면 VL 이 된다.
    """
    if _label(b) not in _VISUAL_LABELS:
        return False
    bb = b.get("block_bbox")
    if not page_size or not all(page_size) or not (
            isinstance(bb, (list, tuple)) and len(bb) >= 4):
        counters["area_guard_skipped"] += 1
        return False
    try:
        x0, y0, x1, y1 = (float(v) for v in bb[:4])
        pw, ph = float(page_size[0]), float(page_size[1])
    except (TypeError, ValueError):
        counters["area_guard_skipped"] += 1
        return False
    if pw <= 0 or ph <= 0:          # "0"/"0.0" 은 truthy 라 위 all() 을 통과한다
        counters["area_guard_skipped"] += 1
        return False
    area = abs(x1 - x0) * abs(y1 - y0) / (pw * ph)
    return area >= float(os.environ.get("KBP_VL_VISUAL_MIN_AREA", "0.05"))


def _has_visual(page: dict, counters: dict) -> bool:
    return any(_contributes(b, page.get("page_size"), counters)
               for b in (page.get("layout") or []))


_LEADER_RUN = re.compile(r"[.·…]{4,}")
_LEADER_SPACED = re.compile(r"(?:[.·]\s){3,}[.·]?")


def _strip_leader_dots(text: str) -> str:
    """목차의 leader dot(`. . . .` / `……`)을 공백으로 접는다.

    네이티브 텍스트 폴백 전용이다. 점선은 **의미 없는 조판 장식**인데 `degen_filter` 의 5-gram
    지배 규칙(degen_filter.py:60-63)에 반복 구절로 걸려 **목차 페이지가 통째로 삭제**된다
    (2026-08-04 실측: arXiv p5 4002자·p6 2527자가 `is_degenerate_text=True` → 빈 페이지).
    접으면 판정이 풀리고(True→False) 제목·페이지번호는 그대로 남는다(4002→1787자).
    `degen_filter` 쪽 임계는 건드리지 않는다 — 그 오탐은 별건이다.
    """
    return _LEADER_SPACED.sub(" ", _LEADER_RUN.sub(" ", text))


def _looks_like_failed_vl(elements: list[dict]) -> str | None:
    """VL 이 "성공처럼 보이지만 실패"한 형태인가. 실패 종류 문자열 또는 None.

    두 경로 모두 예외가 아니라 **정상 element 1개**로 도착한다:
      - ``vl_api`` 가 JSON 파싱 실패 시 합성하는 ``"[Error: …]"`` 플레이스홀더
      - ``elements_parser`` 가 파싱 실패 시 원문을 통째로 담은 fallback element(=max_tokens 절단)

    category 를 ``figure`` 로만 보면 안 된다 — ``ocr/__init__`` 이 반환 **전에** ``text`` 로
    재라벨하므로 항상 거짓이 된다. 절단 판정은 ```json 펜스를 벗긴 뒤 해야 한다 —
    fallback 은 정화본이 아니라 **원문(펜스 포함)** 을 담는다.
    """
    if len(elements) != 1:
        return None
    el = elements[0]
    if (el.get("category") or "").lower() not in ("figure", "text"):
        return None
    content = el.get("content") or {}
    raw = (content.get("markdown") or content.get("text") or "").lstrip()
    if raw.startswith("[Error:"):
        return "error_placeholder"
    unfenced = _FENCE_RE.sub("", raw).lstrip()
    if unfenced.startswith("{"):
        import json
        try:
            json.loads(unfenced)
        except Exception:  # noqa: BLE001 — 파싱 실패 = 절단
            return "truncated"
    return None


def vl_elements_to_blocks(elements: list[dict], *, page_idx: int,
                          adopt_vl_table: bool, counters: dict) -> list[dict]:
    """VL elements[] → blocks. **`elements_to_blocks` 를 직접 쓰면 안 되는 이유가 둘 있다.**

    ① **`figure` + `content.html` 전소** — VL 이 표를 `category="figure"` 로 내면서 html 을
       채우면, `ocr/__init__.py` 의 figure→text 재라벨은 **html 이 빌 때만** 발동해 구제되지
       않고, `elements_to_blocks` 는 그걸 **img_path 가 빈 image 블록**으로 만들어
       `<table>` HTML 을 통째로 버린다. 표가 많은 슬라이드가 정확히 이 형태다(사실 #25).
       → 표는 `table` 블록으로 살리고 **같은 element 의 산문도 함께** 살린다.
    ② **통짜 text 블록** — 그 외 element 에 `elements_to_blocks` 를 쓰면 markdown 전체가
       text 블록 1개가 되어, 하류에서 "표가 들어 있으면 drop" 할 때 본문 산문까지 사라진다.
       `hybrid_to_blocks` 는 산문/표를 정확히 분할하고 pipe 표도 `<table>` 로 변환한다.

    `adopt_vl_table` — VL 이 낸 표를 채택할지. **승계할 paddle 표가 있는 경우에만 False** 다
    (`_hybrid_scan_pages`). `vl` 레인은 승계 대상 자체가 없으므로 항상 True.
    버린 표는 `counters["vl_extra_tables"]` 로 **관측만** 한다(규칙을 만들지 않는다).
    """
    from kb_pipeline.blockify import elements_to_blocks, hybrid_to_blocks

    out: list[dict] = []
    for el in elements or []:
        cat = (el.get("category") or "").lower()
        content = el.get("content") or {}
        html = content.get("html") or ""
        md = content.get("markdown") or content.get("text") or ""
        if cat == "table":
            if adopt_vl_table:
                out.extend(elements_to_blocks([el]))
            else:
                counters["vl_extra_tables"] += 1
            continue
        if cat == "figure" and html.strip():
            if adopt_vl_table:
                out.append({"type": "table", "table_body": html, "page_idx": page_idx})
            else:
                counters["vl_extra_tables"] += 1
            if md.strip():
                out.extend(hybrid_to_blocks(md, page_idx=page_idx))
            continue
        if md.strip():
            out.extend(hybrid_to_blocks(md, page_idx=page_idx))

    cleaned: list[dict] = []
    for b in out:
        if b.get("type") == "table":
            if not adopt_vl_table:
                counters["vl_extra_tables"] += 1
                continue
        elif b.get("type") == "image" and not (b.get("img_path") or ""):
            continue
        b["page_idx"] = page_idx      # VL 경로는 0-based 로 넣는다 — 1-based 로 덮어쓴다
        cleaned.append(b)
    return cleaned


def _hybrid_scan_pages(pages: list[dict], file_bytes: bytes, target_pnos: set[int],
                       ocr_url: str | None, counters: dict) -> set[int]:
    """layout 이 그림·차트를 검출한 **스캔** 페이지를 전면 VL 출력으로 교체한다(in-place).

    대상(``target_pnos``)은 호출부가 준다 — 페이지수준 라우팅에서는 paddle 레인 페이지 집합이
    곧 스캔 페이지다. 면적 임계를 스캔 페이지에서만 실측했으므로 그 밖에는 적용하지 않는다.

    표는 paddle 이 정본이다(전면 VL 이 웹 스크린샷형 표를 세 번 다 놓친 실측). 그래서 그 페이지의
    기존 ``type=="table"`` 블록을 **원래 순서대로 승계**하고, VL 이 낸 표는 paddle 표가 하나도
    없을 때만 채택한다. heading 은 승계하지 않는다 — PAGE_HYBRID 는 전면 전사라 VL 출력에 제목이
    이미 들어 있어 중복된다.
    """
    from kb_pipeline.blockify import elements_to_blocks, hybrid_to_blocks
    from parse_service.parsers.ocr import prompts

    replaced: set[int] = set()
    if not target_pnos:
        return set()    # 대상 없음 → 근거 없는 적용보다 현행 유지

    hybrid_pnos: set[int] = set()
    for pg in pages:
        if pg.get("page_number") not in target_pnos:
            continue
        if pg.get("layout"):
            counters["layout_pages"] += 1
        if _has_visual(pg, counters):
            counters["visual_pages"] += 1
            hybrid_pnos.add(pg["page_number"])
    if not hybrid_pnos:
        return set()

    dpi = int(os.environ.get("KBP_VL_PAGE_DPI", "200"))
    rendered = _render_pages(file_bytes, hybrid_pnos, dpi=dpi)
    if not rendered:
        # render_pdf_pages 는 한 페이지 예외로도 문서 전체 [] 를 반환한다(비치명).
        log.warning("hybrid: render failed for %d page(s) — keeping paddle output",
                    len(hybrid_pnos))
        return set()
    by_pno = {rp.page_number: rp for rp in rendered}
    max_tokens = int(os.environ.get("KBP_VL_PAGE_MAX_TOKENS", "8000"))
    override = (prompts.PAGE_HYBRID_SYSTEM_PROMPT, prompts.PAGE_HYBRID_USER_PROMPT)

    # 대상 페이지를 **한 번에 배치 호출**한다(Plan B-3). jobs 와 pno 리스트를 같은 필터에서
    # 동시에 만들어야 렌더 부재 페이지에서 결과가 밀리지 않는다.
    pairs = [(pno, by_pno[pno]) for pno in sorted(hybrid_pnos) if pno in by_pno]
    if not pairs:
        return set()
    counters["vl_page_calls"] += len(pairs)
    try:
        batch = [els for els, _metas in _ocr_elements_for_pages(
            [(rp.jpeg, f"page-{pno}-hybrid.jpeg") for pno, rp in pairs], ocr_url,
            prompt_override=override, max_tokens=max_tokens)]
    except Exception:  # noqa: BLE001 — 배치 전체 실패도 비치명(전 페이지 paddle 원본 유지)
        log.exception("hybrid VL batch failed — keeping paddle output for %d page(s)",
                      len(pairs))
        return set()
    els_by_pno = {pno: els for (pno, _rp), els in zip(pairs, batch)}

    for pg in pages:
        pno = pg.get("page_number")
        if pno not in els_by_pno:
            continue
        elements = els_by_pno[pno]
        if not elements:
            log.warning("hybrid: empty VL result for page %d — keeping paddle output", pno)
            continue
        failed = _looks_like_failed_vl(elements)
        if failed:
            counters[failed] += 1
            log.warning("hybrid: %s for page %d — keeping paddle output", failed, pno)
            continue

        paddle_blocks = pg.get("blocks") or []
        keep = [b for b in paddle_blocks if b.get("type") == "table"]
        adopt_vl_table = not keep

        cleaned = vl_elements_to_blocks(elements, page_idx=pno,
                                        adopt_vl_table=adopt_vl_table,
                                        counters=counters)

        if not cleaned:
            log.warning("hybrid: all VL blocks filtered for page %d — keeping paddle output", pno)
            continue
        pg["blocks"] = keep + cleaned
        counters["tbl_backfill"] += len(keep)
        replaced.add(pno)

    # 교체된 페이지는 **게이트 대상에서 뺀다**(§4b) — 내용이 게이트웨이 산출물이 아니라
    # 전면 VL 산출물이라 v1 게이트의 판정 근거(게이트웨이 붕괴 패턴)가 적용되지 않는다.
    return replaced


def _render_pages(file_bytes: bytes, page_numbers: set[int] | None = None,
                  *, dpi: int | None = None):
    """페이지 렌더 래퍼. 기본값은 현행 그대로(전 페이지 / render_pdf_pages 기본 dpi=300).

    ``dpi=None`` 이면 인자를 넘기지 않는다 — ``get_pixmap(dpi=None)`` 은 렌더를 깨뜨린다.
    """
    from parse_service.pdf_pages import render_pdf_pages
    kwargs: dict = {}
    if page_numbers is not None:
        kwargs["page_numbers"] = page_numbers
    if dpi is not None:
        kwargs["dpi"] = dpi
    return render_pdf_pages(file_bytes, **kwargs)


def _ocr_elements_for_page(jpeg: bytes, name: str, ocr_url: str | None = None,
                           *, diagram: bool = False,
                           prompt_override: tuple[str, str] | None = None,
                           max_tokens: int | None = None) -> list[dict]:
    """in-process VL OCR 1페이지 (Phase 2c — HTTP 제거).

    프롬프트 우선순위: **diagram=True > prompt_override > PAGE_HYBRID(기본)**.

    - `diagram=True` → `DIAGRAM_*`. 이미 있는 블록에 추가/교체하는 **좁은 보충 경로**다.
      PAGE_HYBRID(전체 재분해)로 바꾸면 ODL additive 모드에서 표/본문이 중복된다 — 유지.
    - 그 외 = 페이지를 **처음부터 전사**하는 경로(vl 레인, odl 레인 스캔페이지) →
      `page_hybrid_prompts()`. 표/본문 원문전사 + 순서도 흐름서술 + 차트 3줄요약을 한
      프롬프트에서 처리한다. **호출 시점에** 부르는 이유는 env
      (`KBP_PAGE_HYBRID_DIAGRAM_RULE`)를 반영하기 위해서다 — 상수 직참조 금지.
    """
    from parse_service.parsers.ocr import ocr_elements_sync
    from parse_service.parsers.ocr import prompts
    if diagram:
        override = (prompts.DIAGRAM_SYSTEM_PROMPT, prompts.DIAGRAM_USER_PROMPT)
    else:
        override = prompt_override or prompts.page_hybrid_prompts()
    # max_tokens 는 **keyword** 로 넘긴다 — positional 로 밀면 상류 `except Exception` 에
    # 삼켜진 채 전 페이지가 빈 결과가 된다(§5-6).
    return ocr_elements_sync(jpeg, name, override, max_tokens=max_tokens)


def _ocr_elements_for_pages(jobs: list[tuple[bytes, str]], ocr_url: str | None = None,
                            *, diagram: bool = False,
                            prompt_override: tuple[str, str] | None = None,
                            max_tokens: int | None = None) -> list[tuple[list[dict], list]]:
    """여러 페이지를 **한 이벤트루프에서 동시** 처리 — jobs 순서대로 elements 리스트 반환.

    `_ocr_elements_for_page` 를 for 루프로 N 번 부르면 호출마다 `asyncio.run` 이 돌아 루프와
    HTTP 클라이언트가 매번 재생성되고 **동시성이 0** 이다(페이지 JPEG 1장 = 코루틴 1개).
    이 함수는 `ocr_elements_many_sync` 로 배치 전체에 루프를 1회만 쓴다(Plan B-3, §B0).
    동시성 상한은 `KBP_VL_MAX_CONCURRENT`.

    프롬프트 선택 규칙은 단수 함수와 **동일**하다(diagram=True > prompt_override > PAGE_HYBRID).
    개별 job 실패는 비치명 — 그 자리에 빈 리스트가 들어간다(인덱스 정렬 보존).
    """
    if not jobs:
        return []
    from parse_service.parsers.ocr import ocr_elements_many_sync
    from parse_service.parsers.ocr import prompts
    if diagram:
        override = (prompts.DIAGRAM_SYSTEM_PROMPT, prompts.DIAGRAM_USER_PROMPT)
    else:
        override = prompt_override or prompts.page_hybrid_prompts()
    # 반환: [(elements, [VLCallMeta, …]), …] — jobs 순서 보존.
    # **2026-08-13(2b-1)**: elements 만 돌려주던 것을 쌍으로 바꿨다. 소비처는
    # `els, metas = pair` 로 받는다(아래 3곳).
    return ocr_elements_many_sync(
        [(jpeg, name, override, max_tokens) for jpeg, name in jobs])


def _safe_decide_route(file_bytes: bytes):
    """게이트 호출 — pymupdf 부재/triage 예외를 삼켜 None(=ODL) 반환. 새 500 방지(가용성).

    gate 는 top-level import 하지 않는다(gate→triage→import pymupdf 라 pymupdf 부재 시
    모듈 로드가 통째로 깨져 ODL 레인까지 회귀). 여기서 지연 import + try/except 로 격리.
    """
    try:
        from parse_service.parsers.pdf.gate import decide_route
    except Exception:  # noqa: BLE001
        log.exception("게이트 import 실패(pymupdf 부재?) — ODL 레인")
        return None
    try:
        return decide_route(file_bytes)
    except Exception:  # noqa: BLE001
        log.exception("게이트 판정 실패 — ODL 레인")
        return None


def parse(file_bytes: bytes, filename: str, *, ocr_url: str) -> RouteResult:
    """문서수준 게이트 라우팅 + VL 퇴화(무한반복) 블록 필터."""
    res = _parse_routed(file_bytes, filename, ocr_url=ocr_url)
    from parse_service.parsers.degen_filter import filter_degenerate_pages
    removed = filter_degenerate_pages(res.pages or [])
    if removed:
        log.warning("VL 퇴화 블록 %d개 제거 (%s)", removed, filename)
        # 퇴화 필터는 `RouteResult` 생성 **뒤**에 blocks 를 지운다 — trace 의
        # `source`/`chars` 가 그대로면 "품질 상한 = empty 비율" 이 거짓이 된다(2b-1 §1).
        _refresh_trace_sources(res)
    return res


def _refresh_trace_sources(res: RouteResult) -> None:
    """`filter_degenerate_pages` 뒤 trace 를 최종 blocks 에 맞춘다.

    전량 삭제된 페이지는 `source` 를 **`empty`** 로 내리고 `attempts` 에 사유를 남긴다.
    관측이 파싱을 깨면 안 되므로 실패해도 조용히 넘어간다.
    """
    try:
        if not res.page_traces:
            return
        by_pno = {p["page_number"]: (p.get("blocks") or []) for p in (res.pages or [])}
        for t in res.page_traces:
            bl = by_pno.get(t["page_number"])
            if bl is None:
                continue
            chars = sum(len((b.get("table_body") or b.get("text") or "")) for b in bl)
            if not bl and t["source"] != "empty":
                t["attempts"] = list(t["attempts"]) + [("degen", "all_removed", {})]
                t["source"] = "empty"
            t["chars"] = chars
    except Exception:  # noqa: BLE001 — 관측이 파싱을 깨면 안 된다
        log.exception("trace 갱신 실패")


def _apply_gw_gate(pages: list, file_bytes: bytes, decision, filename: str,
                   *, target_pages: set[int]) -> list | None:
    """paddle_gw 레인 게이트 — 붕괴 페이지를 quarantine(blocks 비움)하고 판정을 돌려준다.

    `target_pages` (**필수**): 게이트를 태울 페이지 번호 집합. 페이지수준 라우팅에서는
    전량을 넘기면 **odl/vl/skip 페이지까지 판정 대상이 되어 phase 2 mutation 이 ODL
    네이티브 본문을 지운다**(§4b). 호출부가 `paddle_pnos - demoted - hybrid_replaced` 를 준다.

    게이트 실패가 파싱을 깨면 안 된다(가용성) — 예외면 None 을 돌려 기존 동작 그대로 둔다.
    ⚠️ 그래서 **배선 실수도 예외가 아니라 "게이트 없음" 으로 조용히 강등된다** — 구현/디버깅
    시 반드시 로그를 확인할 것.
    """
    try:
        from parse_service.parsers.pdf.page_verdict import apply_gw_page_gate
        targets = [p for p in pages if p.get("page_number") in target_pages]
        if not targets:
            return None
        verdicts = apply_gw_page_gate(
            targets, file_bytes,
            diagram_pages=tuple(getattr(decision, "diagram_pages", ()) or ()),
        )
        return [v.to_dict() for v in verdicts]
    except Exception:  # noqa: BLE001
        log.exception("paddle_gw 게이트 실패 — 판정 없이 진행 (%s)", filename)
        return None


def _parse_routed(file_bytes: bytes, filename: str, *, ocr_url: str) -> RouteResult:
    """**페이지수준 혼합 라우팅**(Plan B-5) — 페이지마다 자기 신호대로 레인을 고르고 병합한다.

    SKIP→skip / OCR_NEEDED→`KBP_GATE_OCR_LANE` / **LLM_NEEDED→vl** / TEXT_ONLY→odl.
    (2026-08-12 Phase 2a: 가로형·다이어그램·세로형 혼합콘텐츠가 전부 `vl` 로 간다.)
    문서수준 `vl` 레인은 삭제했다 — 그림 비율만 보고 문서 전체를 VL 로 넘겨 표를 깨뜨리던
    경로다(KIS 11p 실관측: 표 테두리 curve=350 이 순서도로 오탐 → 전 페이지 VL 재전사).

    **모든 지역변수를 분기 밖에서 seeding 한다** — 조건 블록 안에서만 정의하고 밖에서 읽으면
    그 조건이 거짓인 문서에서 NameError 로 문서 전체가 500 이 된다.
    """
    from kb_pipeline.blockify import hybrid_to_blocks, elements_to_blocks

    decision = _safe_decide_route(file_bytes)
    if decision is None:
        # pymupdf 부재·게이트 예외 — 현행과 동일하게 문서 전체 ODL.
        # **조기 return 도 triage 로그를 남긴다** — 왜 페이지 로그가 없는지 구분되지 않으면
        # 게이트 실패 문서만 튜닝 근거가 안 쌓이는 사각지대가 생긴다.
        result = _odl_lane(file_bytes, filename, ocr_url=ocr_url, diagram_pages=())
        _try_log_triage(None, result, {}, filename)
        return result

    lanes = dict(decision.page_lanes)
    total_pages = decision.total_pages
    narrate_pages = tuple(decision.narrate_pages or ())
    odl_pnos = {n for n, l in lanes.items() if l == "odl"}
    skip_pnos = {n for n, l in lanes.items() if l == "skip"}
    paddle_pnos = {n for n, l in lanes.items() if l == "paddle_gw"}
    vl_pnos = {n for n, l in lanes.items() if l == "vl"}

    # ── seeding (전부 분기 밖) ────────────────────────────────────────────────
    odl_md: list[str] = []
    gw_by_pno: dict[int, dict] = {}
    demoted_pnos: set[int] = set()
    # `_hybrid_scan_pages` 가 전면 VL 로 갈아끼운 페이지 — 게이트 대상에서 뺀다(§4b).
    # **분기 밖 seeding 필수**: 산출처가 `if paddle_pnos:` → `if gw_pages:` → `try:` 3중
    # 안에서만 돌아, 순수 디지털 PDF·GW 예외 경로에서 NameError → 문서 전체 500 이 된다.
    hybrid_replaced: set[int] = set()
    # 페이지별 시도 발자국 — (stage, outcome, meta). Phase 2b-1 관측.
    attempts: dict[int, list[tuple]] = {}
    odl_error: str | None = None

    def _att(pno: int, stage: str, outcome: str, meta: dict | None = None) -> None:
        attempts.setdefault(pno, []).append((stage, outcome, meta or {}))
    counters = {"layout_pages": 0, "visual_pages": 0, "area_guard_skipped": 0,
                "truncated": 0, "error_placeholder": 0, "vl_page_calls": 0,
                "tbl_backfill": 0, "vl_extra_tables": 0}

    # ── 1) ODL — odl 또는 skip 레인이 있으면(skip 도 md 로 블록을 만든다) ──────
    # `vl_pnos` 포함 필수 — 안 넣으면 전면 가로형 문서에서 `odl_md=[]` 이라 `_md(pno)` 가
    # 항상 "" 이고 병합의 md 폴백이 **구조적으로 도달 불가**가 된다.
    if odl_pnos or skip_pnos or vl_pnos or total_pages == 0:
        try:
            odl_md = _page_markdowns(file_bytes, filename)
        except Exception as exc:  # noqa: BLE001
            # **문서 실패가 아니라 VL 폴백**(사용자 확정 2026-08-04). odl_md 가 비면 odl 레인
            # 페이지는 전부 thin 판정 → 아래 VL 전사 배치가 내용을 살린다.
            #
            # `ToolError` 만 잡으면 안 된다 — `_odl_convert` 는 예외를 감싸지 않아
            # **JRE 부재 시 `subprocess.CalledProcessError` 가 그대로 올라온다**(2026-08-04 실측:
            # 자바 없는 PC 에서 10개 문서 전부 이 예외로 파싱 실패). ODL 은 외부 프로세스라
            # 어떤 예외든 낼 수 있으므로 전부 흡수하고 VL 로 넘긴다.
            log.exception("ODL 실패 — VL 폴백 (%s)", filename)
            odl_md = []
            # `failed` 와 `no_md` 를 **구분**한다(2b-1 §2) — 자바 부재가 "md 가 원래 없음"
            # 으로 위장되면 JRE 문제를 영영 못 찾는다. 문서 단위 사건이므로 pno=0 에 남긴다.
            odl_error = f"{type(exc).__name__}: {str(exc)[:120]}"

    # ── 2) 정합 가드 — 페이지수가 어긋나면 페이지수준 병합을 포기하고 문서 전체 ODL 위임 ──
    if odl_md and total_pages and len(odl_md) != total_pages:
        log.warning("ODL 페이지수 불일치(%d != %d) — 문서 전체 ODL 위임 (%s)",
                    len(odl_md), total_pages, filename)
        # diagram_pages=() : narrate_pages 는 pymupdf 기준이고 _odl_lane 의 page_number 는
        # ODL md 인덱스라, 어긋난 상태로 넘기면 서술이 엉뚱한 페이지에 붙는다.
        # ⚠️ 이 경로는 §3 의 목표(가로형→vl, 다이어그램 서술)를 **통째로 무효화한다**
        #    (§9 D13). ODL 이 페이지를 하나 더/덜 내는 것만으로 발동하므로 로그로 추적한다.
        result = _odl_lane(file_bytes, filename, ocr_url=ocr_url, diagram_pages=())
        _try_log_triage(decision, result, lanes, filename)
        return result
    if not total_pages:
        # 게이트가 열기 실패했거나 ODL 이 실패한 경우. odl_md 가 있으면 그 길이를 쓰고,
        # 둘 다 없으면 **렌더로 페이지 수를 얻는다** — 안 그러면 병합 루프가 0회 돌아
        # 페이지가 하나도 없는 문서가 된다(ODL 실패 시 VL 폴백이 무의미해진다).
        total_pages = len(odl_md)
        if not total_pages:
            probe = _render_pages(file_bytes)
            total_pages = len(probe)
            if total_pages:
                log.warning("게이트·ODL 모두 페이지수 미상 — 렌더로 %d 페이지 확인 (%s)",
                            total_pages, filename)

    # ── 3) 게이트웨이 — 스캔 페이지만 전송(B-2) ───────────────────────────────
    if paddle_pnos:
        gw_pages: list[dict] = []
        try:
            from parse_service.parsers.pdf.paddle_gw import run_paddle_gateway
            gw_pages = run_paddle_gateway(file_bytes, filename, page_numbers=set(paddle_pnos))
        except Exception:  # noqa: BLE001 — 레인 불능(프로브 실패/URL 미설정)
            log.exception("paddle_gw 레인 실패 — 페이지별 VL 폴백 (%s)", filename)
        if gw_pages:
            # Plan A §A4 — layout 이 그림·차트를 검출한 페이지를 전면 VL 로 교체.
            # 자체 try 필수: 이 지점 예외가 parse() 로 전파되면 문서 전체 500 이 된다.
            try:
                hybrid_replaced = _hybrid_scan_pages(
                    gw_pages, file_bytes, set(paddle_pnos), ocr_url, counters) or set()
            except Exception:  # noqa: BLE001
                log.exception("hybrid scan-page step failed (%s)", filename)
            gw_by_pno = {p["page_number"]: p for p in gw_pages}
        # 강등 대상은 **엔진 사고뿐**이다(§4a — v1 실측이 기각한 escalation 부활 방지).
        #   · 레인 불능(프로브 실패·URL 공란) → 응답 자체가 없다
        #   · 개별 페이지 `status == "error"` → 게이트웨이 타임아웃/5xx
        # ⚠️ `status == "ok"` + 빈 blocks 는 **강등하지 않는다.** 그 페이지 집단이 정확히
        #    v1 이 측정한 "게이트가 잡은 페이지" 이고, 거기서 VL 은 구조율 0 · 날조 2건이었다
        #    (Fisher p=0.021). 그대로 게이트에 넘겨 EMPTY/quarantine 판정을 받게 둔다.
        demoted_pnos = {n for n in paddle_pnos
                        if n not in gw_by_pno
                        or (gw_by_pno.get(n) or {}).get("status") == "error"}

    # ── 4) VL 전사 대상 = thin odl ∪ 강등 paddle. 300dpi 1회 렌더 + 배치 호출 ──
    def _md(pno: int) -> str:
        return odl_md[pno - 1] if 0 <= pno - 1 < len(odl_md) else ""

    # **`lanes` 에 없는 페이지도 포함**한다 — 게이트가 열기 실패하면 page_lanes 가 비고
    # total_pages 를 len(odl_md) 로 잡는데, 그 페이지들은 병합에서 기본 odl 로 처리되므로
    # thin 판정도 같은 집합에서 해야 한다(안 그러면 스캔 페이지가 VL 전사를 못 받는다).
    odl_like = {n for n in range(1, total_pages + 1)
                if lanes.get(n, "odl") == "odl"}
    thin_pnos = {n for n in odl_like if _digital_text_len(_md(n)) < _DIGITAL_MIN_CHARS}
    transcribe_pnos = thin_pnos | demoted_pnos | vl_pnos
    render_pnos = transcribe_pnos | set(narrate_pages)
    rendered = _render_pages(file_bytes, render_pnos) if render_pnos else None
    by_pno = {rp.page_number: rp for rp in (rendered or [])}

    vl_by_pno: dict[int, list[dict]] = {}
    if transcribe_pnos:
        # jobs 와 되매핑 키를 **같은 필터에서 동시에** 만든다 — 따로 만들면 렌더 부재 페이지에서
        # 한 칸씩 밀려 다른 페이지의 전사가 붙는다.
        pairs = [(n, by_pno[n]) for n in sorted(transcribe_pnos) if n in by_pno]
        if pairs:
            # **max_tokens 를 반드시 넘긴다** — 기본값 2000 으로는 조밀한 본문 페이지가 절단된다
            # (2026-08-04 실측: arXiv 논문 p6 2526자가 응답 절단으로 빈 페이지가 됐고, 상한을
            # 올리자 1438자로 복구). hybrid 경로와 같은 상한을 쓴다.
            # ※ 상한을 올려도 남는 실패가 있다 — 아래 재시도 블록의 모델측 퇴화 참조.
            page_max_tokens = int(os.environ.get("KBP_VL_PAGE_MAX_TOKENS", "8000"))
            try:
                _paired = _ocr_elements_for_pages(
                    [(rp.jpeg, f"page-{n}.jpeg") for n, rp in pairs], ocr_url,
                    max_tokens=page_max_tokens)
                batch = [els for els, _m in _paired]
                batch_metas = [m for _e, m in _paired]
            except Exception as exc:  # noqa: BLE001 — 배치 전체 실패도 비치명
                # **삼킴 3층**(2b-1): 배치 전체 실패는 meta 자체가 없다 —
                # 여기서 호출부가 사유를 만들어 준다.
                log.exception("VL 전사 배치 실패 (%s)", filename)
                batch = [[] for _ in pairs]
                batch_metas = [[{"error": f"{type(exc).__name__}: {str(exc)[:120]}"}]
                               for _ in pairs]
            for (n, rp), els, metas in zip(pairs, batch, batch_metas):
                for _m in (metas or [None]):
                    md_ = (_m.to_dict() if hasattr(_m, "to_dict")
                           else (_m if isinstance(_m, dict) else {}))
                    _att(n, "vl", "error" if md_.get("error") else "ok", md_)
                # 절단·에러 플레이스홀더는 "성공처럼 보이는 실패" 다 — 잘린 raw JSON 이 그대로
                # 본문 블록이 되는 것을 막는다(hybrid 경로와 동일 판정).
                failed = _looks_like_failed_vl(els)
                if not failed:
                    vl_by_pno[n] = els
                    continue
                # ── VL 실패 → **네이티브 텍스트 폴백** ────────────────────────────────
                # 실패 원인은 절단이 아니라 모델측 퇴화다(2026-08-04 실측: arXiv p5 목차가
                # leader dot `. . . .` 반복 루프에 빠졌다가 finish_reason="stop",
                # completion_tokens=226/상한 8000 으로 스스로 끊음).
                # **재시도는 무효였다** — 실측 회복률 0%(5/5 실패). `temperature=0.1`
                # (vl_api.py:196)이라 같은 이미지는 같은 실패를 반복한다.
                # 반면 이 경로에 오는 페이지는 **정의상 네이티브 텍스트를 가진 odl 레인**이라
                # (p5 4002자·p6 2527자) PyMuPDF 추출본이 빈 페이지보다 낫다. 렌더 시 이미
                # 뽑아둔 `RenderedPage.text`(pdf_pages.py:59)를 쓰므로 추가 비용이 없다.
                native = _strip_leader_dots(getattr(rp, "text", "") or "").strip()
                if native:
                    log.warning("VL 전사 %s — page %d, 네이티브 텍스트 %d자로 폴백 (%s)",
                                failed, n, len(native), filename)
                    vl_by_pno[n] = [{"category": "text",
                                     "content": {"markdown": native},
                                     "page": n - 1}]
                else:
                    log.warning("VL 전사 %s — page %d, 네이티브 텍스트도 없음 (%s)",
                                failed, n, filename)
                    vl_by_pno[n] = []

    # ── 5) 병합 ───────────────────────────────────────────────────────────────
    # `traces` 는 **별도 맵**이다 — `entry` dict(PageDoc 6-key 계약)에 키를 추가하면
    # 하류로 새어 계약이 바뀐다(Phase 2b-1 은 동작을 바꾸지 않는다).
    traces: dict[int, dict] = {}
    pages: list[dict] = []
    for pno in range(1, total_pages + 1):
        lane = lanes.get(pno, "odl")            # 미포함 기본 odl(명시)
        md = _md(pno)
        blocks: list[dict] = []                 # 분기 밖 초기화 — 안 하면 루프 캐리
        entry: dict = {"page_number": pno}

        src = None
        if lane == "paddle_gw" and pno not in demoted_pnos:
            gw = gw_by_pno.get(pno) or {}
            blocks = gw.get("blocks") or []
            # hybrid 가 갈아끼운 페이지는 내용이 게이트웨이가 아니라 **전면 VL** 산출물이다.
            src = "gw_hybrid" if pno in hybrid_replaced else "gw"
            # status/error 를 병합 dict 로 실어 보낸다(6-key 계약, §4e). 2a 에는 소비자가
            # 없지만(ENGINE_ERROR 는 §4a 로 도달 불가) 계약상 채운다 — 빼면 하류가 조용히 눈먼다.
            entry["status"] = gw.get("status")
            entry["error"] = gw.get("error")

        elif pno in transcribe_pnos:
            # ⚠️ **md 분기보다 앞**이어야 한다. `_DIGITAL_MIN_CHARS == 1` 이라, odl/skip 페이지가
            #    하나라도 있으면 step 1 이 돌아 odl_md 가 전 페이지 채워지고, 그러면 vl 페이지가
            #    실텍스트 1자만 있어도 md 분기에 걸려 **VL 전사 결과가 통째로 버려진다**
            #    (혼합 문서에서 VL 비용만 태우고 결과 폐기 — 목표 (1)이 무효가 된다).
            # `elements_to_blocks` 직접 호출 금지 — figure+html 표가 전소된다(위 헬퍼 docstring).
            # vl 레인은 승계할 paddle 표가 없으므로 adopt_vl_table=True 고정.
            blocks = vl_elements_to_blocks(vl_by_pno.get(pno) or [], page_idx=pno,
                                           adopt_vl_table=True, counters=counters)
            src = "vl"
            if not blocks and _digital_text_len(md) >= _DIGITAL_MIN_CHARS:
                blocks = hybrid_to_blocks(md, page_idx=pno)   # 2a 폴백 = 현행 유지
                src = "vl_md_fallback"     # 2b-2 가 제거를 검토하는 분기

        elif _digital_text_len(md) >= _DIGITAL_MIN_CHARS:
            blocks = hybrid_to_blocks(md, page_idx=pno)
            src = "odl_md"

        elif lane == "skip":
            # SKIP 은 애초에 내용이 거의 없는 페이지 — VL 을 부르지 않는다(현행과 동일).
            blocks = []
            src = "skip"

        else:
            # 현재 도달 불가(lane 값이 4종뿐이고 앞 분기가 전부 흡수). 방어코드로 유지 —
            # 값이 나오면 라우팅 버그 신호다.
            log.debug("미분류 페이지 p%d lane=%s md=%d — 빈 blocks (%s)",
                      pno, lane, _digital_text_len(md), filename)
            src = "unclassified"

        entry["blocks"] = blocks
        pages.append(entry)
        traces[pno] = {"lane": lane, "source": src, "attempts": list(attempts.get(pno, ()))}

    # ── 6) 서술 보충(odl 레인 전용 — narrate_pages 는 전부 네이티브 텍스트 페이지) ──
    if narrate_pages:
        _supplement_diagram_pages(pages, file_bytes, narrate_pages, ocr_url,
                                  rendered=rendered)

    # ── 7) v1 GW 게이트 — **supplement 뒤**(2026-08-11). `_supplement_diagram_pages` 의
    #      replace 분기는 기존 blocks 가 비어 있어도 VL 서술로 페이지를 채우는 **현존 유일한
    #      복구 경로**이고, EMPTY 조건("잉크는 많은데 텍스트가 없음")은 정확히 도면 페이지를
    #      겨냥한다 — 게이트를 앞에 두면 지금 정상 복구되던 도면 페이지가 영구 빈 페이지가 된다.
    #      quarantine 은 종결 판정이라 폴백으로 새지 않는다.
    #      대상은 **게이트웨이가 정상 응답한 paddle 페이지뿐** — 강등(엔진 사고)과 hybrid 교체분을
    #      빼지 않으면 phase 2 mutation 이 ODL 네이티브 본문까지 지운다(§4b).
    verdicts = None
    gate_pnos = paddle_pnos - demoted_pnos - hybrid_replaced
    if gate_pnos:
        verdicts = _apply_gw_gate(pages, file_bytes, decision, filename,
                                  target_pages=gate_pnos)

    # ── 8) trace 조립(Phase 2b-1 관측) ────────────────────────────────────────
    # **`source` 는 여기서 최종 확정한다** — 병합 루프 뒤에 blocks 를 바꾸는 곳이 있다:
    #   · `_supplement_diagram_pages`(append) — 비었던 페이지가 채워질 수 있다
    #   · 게이트 quarantine — blocks 를 비운다. 단 `source` 는 **안 바꾼다**(무엇이
    #     처리했나는 그대로다). `verdict`/`state` 로 표현한다.
    # ⚠️ `filter_degenerate_pages` 는 `parse()` 안, RouteResult 생성 **뒤**라 여기서
    #    못 잡는다 — `parse()` 가 삭제 후 갱신한다(아래 `_refresh_trace_sources`).
    if odl_error:
        _att(0, "odl", "failed", {"error": odl_error})   # 문서 단위 사건 → pno=0
    _blocks_by_pno = {p["page_number"]: (p.get("blocks") or []) for p in pages}
    _verdict_by_pno = {v["page_number"]: v for v in (verdicts or [])}
    _sig_by_pno = {sg.page_number: sg for sg in (decision.page_signals or ())}
    page_traces = []
    for pno in range(1, total_pages + 1):
        t = traces.get(pno) or {"lane": lanes.get(pno, "odl"), "source": None,
                                "attempts": []}
        bl = _blocks_by_pno.get(pno) or []
        chars = sum(len((b.get("table_body") or b.get("text") or "")) for b in bl)
        v = _verdict_by_pno.get(pno) or {}
        sig = _sig_by_pno.get(pno)
        page_traces.append({
            "page_number": pno,
            "bucket": (sig.bucket.name if sig and sig.bucket else None),
            "lane": t["lane"],
            # blocks 가 비면 `empty` 로 덮어쓴다 — VL 이 elements 를 냈는데 전량 필터된
            # 경우가 실재한다. 안 덮으면 "품질 상한 = empty 비율" 지표가 거짓이 된다.
            "source": (t["source"] if bl else "empty"),
            "attempts": list(t["attempts"]) + list(attempts.get(0, ())),
            "chars": chars,
            "verdict": v.get("verdict"),
            "state": v.get("state"),
            "verdict_reason": v.get("reason"),
        })

    log.info("parse-svc pdf(%s): pages=%d odl=%d skip=%d paddle=%d vl=%d demoted=%d "
             "transcribe=%d narrate=%d hybrid_vl=%d tbl_backfill=%d truncated=%d",
             filename, total_pages, len(odl_pnos), len(skip_pnos), len(paddle_pnos),
             len(vl_pnos), len(demoted_pnos), len(transcribe_pnos), len(narrate_pages),
             counters["vl_page_calls"], counters["tbl_backfill"], counters["truncated"])
    _src_dist = collections.Counter(t["source"] for t in page_traces)
    log.info("parse-svc pdf(%s) source: %s", filename,
             " · ".join(f"{k} {v}p" for k, v in sorted(_src_dist.items())))
    result = RouteResult(kind="pages", chunk_needed=True, pages=pages,
                         page_verdicts=verdicts, page_traces=page_traces)
    _try_log_triage(decision, result, lanes, filename)
    return result


def _try_log_triage(decision, result: RouteResult, lanes: dict, filename: str) -> None:
    """`_log_triage_table` 호출 래퍼 — 로그 버그가 파싱을 깨면 안 된다(가용성).

    `_parse_routed` 의 **모든 종료점**이 이걸 거친다(조기 return 2곳 포함).
    """
    try:
        _log_triage_table(decision, result, lanes=lanes, filename=filename)
    except Exception:  # noqa: BLE001
        log.exception("triage 로그 실패 (%s)", filename)


def _fmt_attempts(atts) -> str:
    """`attempts` 를 한 칸에 담는다 — `vl:ok(496,stop)` 식."""
    out = []
    for a in (atts or []):
        stage, outcome = a[0], a[1]
        meta = a[2] if len(a) > 2 else {}
        bits = [str(meta[k]) for k in ("tokens", "finish") if meta.get(k) is not None]
        if meta.get("error"):
            bits = [str(meta["error"])[:28]]
        out.append(f"{stage}:{outcome}" + (f"({','.join(bits)})" if bits else ""))
    return " → ".join(out) or "-"


def _log_triage_table(decision, result: RouteResult, *, lanes: dict, filename: str) -> None:
    """페이지별 triage 판정 로그 — 튜닝 근거 축적용(2026-08-06 도입, 2026-08-12 페이지수준화).

    `KBP_TRIAGE_LOG_TABLE=0` 이면 완전히 스킵(대량 처리 시 로그 폭주 억제 손잡이).
    `decision` 이 None(게이트 import 실패/decide_route 예외)이거나 `page_signals` 가
    없으면(triage_document 자체 실패) 그 사유만 짧게 남기고 종료한다 — 왜 로그가 없는지
    구분되어야 게이트 실패 문서만 튜닝 근거가 안 쌓이는 사각지대를 피할 수 있다.

    **컬럼이 문서수준(`lane_used`/`fallback_used`)에서 페이지수준(`lane`)으로 바뀌었다** —
    페이지마다 레인이 갈리므로 문서 단위 요약은 의미가 없다. `source`/`attempts` 컬럼은
    Phase 2b(PageTrace)가 더한다.
    """
    if os.environ.get("KBP_TRIAGE_LOG_TABLE", "1") == "0":
        return
    if decision is None or not decision.page_signals:
        log.info("triage %s: decision=None(게이트 실패/신호없음) — 페이지 로그 생략", filename)
        return

    log.info("triage %s: doc_lane=%s pages=%d", filename, decision.lane,
             decision.total_pages)
    # ⚠️ 기존 11컬럼은 **triage 튜닝 근거**다 — 버리지 말고 뒤에 붙인다(2b-1 §3).
    log.info("| p | triage | lane | dia | char | img | imgcov | curve | line | "
             "판정근거 | 성공여부 | source | attempts |")
    log.info("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    _tr = {t["page_number"]: t for t in (result.page_traces or [])}
    page_map = {p.get("page_number"): p for p in (result.pages or [])}
    for sig in decision.page_signals:
        entry = page_map.get(sig.page_number)
        has_blocks = bool(entry and entry.get("blocks"))
        t = _tr.get(sig.page_number) or {}
        log.info("| %d | %s | %s | %s | %d | %d | %.2f | %d | %d | %s | %s | %s | %s |",
                 sig.page_number, sig.bucket.name if sig.bucket else "-",
                 lanes.get(sig.page_number, "odl"), sig.is_diagram,
                 sig.char_count, sig.image_count, sig.image_coverage,
                 sig.curve_count, sig.line_count, sig.reason,
                 "성공" if has_blocks else "실패",
                 t.get("source") or "-", _fmt_attempts(t.get("attempts")))


def _odl_lane(file_bytes: bytes, filename: str, *, ocr_url: str,
              diagram_pages: tuple = ()) -> RouteResult:
    from kb_pipeline.blockify import hybrid_to_blocks, elements_to_blocks
    try:
        md_texts = _page_markdowns(file_bytes, filename)
    except ToolError as e:
        raise ParserError(str(e)) from e

    rendered = None
    pages: list[dict] = []
    for i, md in enumerate(md_texts):
        page_number = i + 1
        if _digital_text_len(md) >= _DIGITAL_MIN_CHARS:
            pages.append({"page_number": page_number,
                          "blocks": hybrid_to_blocks(md, page_idx=page_number)})
            continue
        if rendered is None:
            rendered = _render_pages(file_bytes)
        page_jpeg = next((rp.jpeg for rp in rendered if rp.page_number == page_number), None)
        if page_jpeg is None:
            log.warning("scanned page %d has no rendered image", page_number)
            pages.append({"page_number": page_number, "blocks": []})
            continue
        try:
            elements = _ocr_elements_for_page(page_jpeg, f"page-{page_number}.jpeg", ocr_url)
        except Exception:  # noqa: BLE001 — 페이지 단위 OCR 실패는 비치명
            log.exception("OCR failed for scanned page %d", page_number)
            pages.append({"page_number": page_number, "blocks": []})
            continue
        blocks = elements_to_blocks(elements)
        for b in blocks:
            b["page_idx"] = page_number
        pages.append({"page_number": page_number, "blocks": blocks})

    _supplement_diagram_pages(pages, file_bytes, diagram_pages, ocr_url, rendered=rendered)
    return RouteResult(kind="pages", chunk_needed=True, pages=pages)


def _diagram_blocks(elements: list[dict], pno: int, *, drop_tables: bool) -> list[dict]:
    """다이어그램 서술 elements → blocks. append 모드에서는 표만 걸러낸다(Plan B-4).

    **블록을 통째로 버리지 않는다.** DIAGRAM 출력은 `elements_to_blocks` 를 거치면 markdown
    전체가 **통짜 text 블록 1개**가 되므로(blockify), "표가 들어 있으면 drop" 하면 서술 전체가
    사라진다. 대신 `hybrid_to_blocks` 로 산문/표를 분할한 뒤 표 조각만 뺀다 —
    표의 정본은 ODL/paddle 의 `<table>` 이고 서술은 덧붙이기만 하기 때문이다.

    `drop_tables=False`(교체 모드)면 분할만 하고 전부 유지한다 — 그 페이지의 원본 블록이
    통째로 대체되므로 표를 뺄 이유가 없다.
    """
    from kb_pipeline.blockify import hybrid_to_blocks
    out: list[dict] = []
    for el in elements:
        content = el.get("content") or {}
        md = content.get("markdown") or content.get("text") or ""
        if not md.strip():
            continue
        for b in hybrid_to_blocks(md, page_idx=pno):
            if drop_tables and b.get("type") == "table":
                continue                    # 표 정본은 베이스 파서가 소유
            out.append(b)
    return out


def _supplement_diagram_pages(pages: list, file_bytes: bytes, diagram_pages: tuple,
                              ocr_url: str, rendered=None, replace: bool = False) -> None:
    """다이어그램(순서도/차트) 페이지 VL 서술 — ODL/paddle_gw 공용.

    - ODL 레인(replace=False, 추가): 기존 블록이 **네이티브 텍스트(정확)**라 유지하고 VL 서술을 덧붙임.
    - paddle_gw 레인(replace=True, 교체): 기존 블록도 같은 픽셀의 OCR(조각·오타)+죽은 이미지참조라
      VL 서술이 상위호환 → 통째 교체(2026-07-15 결정, 소유권 p4 중복 실측).
    VL 실패 시 어느 모드든 기존 블록 유지(비치명). pages 를 제자리 수정.
    """
    if not diagram_pages:
        return
    from kb_pipeline.blockify import elements_to_blocks
    # 렌더 정책은 **바꾸지 않는다** — rendered 가 None 이면 현행처럼 문서 전량 1회 렌더한다.
    # 이 함수는 `_odl_lane`(rendered=None)에서도 불리므로 선렌더 규칙을 바꾸면 그 경로가 흔들린다.
    if rendered is None:
        rendered = _render_pages(file_bytes)
    by_pno = {rp.page_number: rp.jpeg for rp in rendered}

    # 대상 페이지를 **한 번에 배치 호출**(Plan B-3). jobs 와 pno 리스트를 같은 필터에서 동시에
    # 만들어야 렌더 부재 페이지에서 결과가 밀리지 않는다.
    targets = [pno for pno in diagram_pages
               if any(p["page_number"] == pno for p in pages) and pno in by_pno]
    for pno in diagram_pages:
        if pno not in by_pno and any(p["page_number"] == pno for p in pages):
            log.warning("diagram page %d has no rendered image", pno)
    if not targets:
        return
    try:
        batch = [els for els, _metas in _ocr_elements_for_pages(
            [(by_pno[pno], f"page-{pno}-diagram.jpeg") for pno in targets],
            ocr_url, diagram=True)]
    except Exception:  # noqa: BLE001 — 배치 전체 실패도 비치명(기존 블록 유지)
        log.exception("diagram VL supplement batch failed for %d page(s)", len(targets))
        return
    els_by_pno = dict(zip(targets, batch))

    for pno in targets:
        entry = next((p for p in pages if p["page_number"] == pno), None)
        if entry is None:
            continue
        elements = els_by_pno.get(pno) or []
        if not elements:                    # 개별 job 실패 → 기존 블록 유지(비치명)
            continue
        extra = _diagram_blocks(elements, pno, drop_tables=not replace)
        for b in extra:
            b["page_idx"] = pno
        if replace and extra:
            # 교체 모드(paddle_gw): 게이트웨이 OCR 조각·죽은 이미지참조는 버리되, 게이트웨이가
            # 제대로 읽은 **제목(heading = text_level 보유)** 은 보존한다(2026-07-16, "Ⅱ.업무순서도"
            # 유실 실관측). heading + VL 서술 순으로 재구성.
            headings = [b for b in entry["blocks"] if b.get("text_level")]
            entry["blocks"] = headings + extra
        else:
            entry["blocks"].extend(extra)
