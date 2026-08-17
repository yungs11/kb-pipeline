"""OCR 도메인 파서 — pptx + 이미지/스캔. Phase 2c: in-process VL OCR (:18050 HTTP 제거).

내부: 이미지→
image_file_to_base64_list; 페이지별 call_vl_api_with_base64→
parse_vision_language_response_to_elements→normalize_all_elements. 페이지 실패 비치명(skip).
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile

from parse_service.parsers import RouteResult, ParserError

IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff", "webp"}
_VL_SEM: asyncio.Semaphore | None = None
_VL_SEM_LOOP = None

log = logging.getLogger("kb_pipeline.parse_service.parsers.ocr")


def _sem() -> asyncio.Semaphore:
    """VL 동시성 제한 세마포어 — **현재 이벤트루프에 바인딩**해서 돌려준다.

    `asyncio.Semaphore` 는 `_LoopBoundMixin` 이라 경합 시 생성 루프를 붙잡는다. 전역 하나를
    재사용하면 다른 루프에서 `is bound to a different event loop` RuntimeError 가 난다 —
    parse-svc 는 호출마다 `asyncio.run`(ocr_elements_sync)으로 루프를 새로 만들 수 있어
    실제로 밟는 경로다. `vl_api.get_http_client()` 가 쓰는 재바인딩 패턴과 동일하게 처리한다.
    """
    global _VL_SEM, _VL_SEM_LOOP
    loop = asyncio.get_running_loop()
    if _VL_SEM is None or _VL_SEM_LOOP is not loop:
        _VL_SEM = asyncio.Semaphore(int(os.environ.get("KBP_VL_MAX_CONCURRENT", "8")))
        _VL_SEM_LOOP = loop
    return _VL_SEM


async def _file_to_base64_pages(file_bytes: bytes, filename: str) -> list[str]:
    from parse_service.parsers.ocr import image_utils
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    suffix = "." + ext if ext else ""
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(file_bytes)
        path = tmp.name
    try:
        if ext in IMAGE_EXTS:
            return image_utils.image_file_to_base64_list(path)
        raise ParserError(f"ocr 파서는 이미지만 받는다 — {filename}")
    finally:
        os.unlink(path)


async def ocr_file_to_elements(file_bytes: bytes, filename: str,
                               prompt_override: tuple[str, str] | None = None,
                               *, max_tokens: int | None = None) -> dict:
    """VL OCR — file_bytes 를 elements[] 로. prompt_override=(system, user) 주면 그 프롬프트로
    호출한다(다이어그램 전용 서술 등). None 이면 기본 전사 프롬프트.

    `max_tokens` 는 keyword-only 다(`*`). positional 로 받으면 4번째 인자가 조용히 밀려
    아래 `except Exception`(페이지 실패 비치명)에 삼켜진 채 **전 페이지가 빈 결과**가 된다.
    `None` 이면 `VL_MAX_TOKENS`(현행 동작). 스캔 페이지 전면 VL 만 상향한다.
    """
    from parse_service.parsers.ocr import vl_api, elements_parser, prompts
    b64_pages = await _file_to_base64_pages(file_bytes, filename)
    if prompt_override is not None:
        system_p, user_p = prompt_override
    else:
        system_p, user_p = prompts.build_system_prompt(), prompts.build_user_prompt()
    all_elements: list[dict] = []
    call_metas: list["vl_api.VLCallMeta"] = []   # 페이지 순서 정렬 보존
    next_id = 0

    # 페이지 VL 호출은 **동시**에 돌린다(`_sem()` 으로 KBP_VL_MAX_CONCURRENT 제한).
    # 이전에는 sequential await 라 세마포어가 아무 일도 하지 않았다 — 코루틴이 하나뿐이었다.
    async def _call(b64: str) -> tuple[str, "vl_api.VLCallMeta"]:
        async with _sem():
            return await vl_api.call_vl_api_with_base64(
                b64, user_p, system_p, max_tokens=max_tokens)

    responses = await asyncio.gather(*(_call(b) for b in b64_pages),
                                     return_exceptions=True)
    # **파싱은 순서대로** — `next_id` 가 순차 상태이고 elements 순서가 페이지 순서여야 한다.
    for page_num, resp in enumerate(responses, start=1):
        if isinstance(resp, BaseException):     # 페이지 실패 비치명(기존 계약 유지)
            # **삼킴 1층**(Phase 2b-1): 여기서 예외가 사라지면 상위는 "빈 결과" 와 구분할 수
            # 없다. `VLCallMeta.error` 로 사유를 실어 올린다 — 동작은 그대로(continue).
            log.error("VL OCR failed page %d", page_num, exc_info=resp)
            call_metas.append(vl_api.VLCallMeta(
                error=f"{type(resp).__name__}: {str(resp)[:160]}"))
            continue
        resp, meta = resp
        call_metas.append(meta)
        try:
            els, next_id = elements_parser.parse_vision_language_response_to_elements(
                resp, page_num, next_id)
            all_elements.extend(els)
        except Exception:  # noqa: BLE001 — 파싱 실패도 그 페이지만 비치명
            log.exception("VL element parse failed page %d", page_num)
    elements_parser.normalize_all_elements(all_elements)
    for el in all_elements:
        el["page_idx"] = int(el.get("page", 1)) - 1  # elements_to_blocks 규약(0-based)
        # 순수 텍스트 figure(markdown 만 있고 html/img/text 없음) → text 재분류.
        # blockify 의 figure→image 매핑은 img_path/text 만 읽어 markdown 을 버린다 —
        # VL OCR 스키마(table|figure)에서 본문 텍스트는 전부 figure.markdown 으로 오므로
        # 재분류하지 않으면 enriched_content 가 빈다(스택 검증에서 발견). blockify 계약 불변.
        content = el.get("content")
        if (
            (el.get("category") or "").lower() == "figure"
            and isinstance(content, dict)
            and not (content.get("html") or content.get("img_path") or content.get("text"))
            and (content.get("markdown") or "").strip()
        ):
            el["category"] = "text"
    # `call_metas` 는 **추가 키**다 — 기존 소비자(`["elements"]`)는 영향 없다.
    # 파일 1건에 페이지 수만큼 VL 을 호출하므로 **복수**다(pptx·다중 이미지).
    # PDF 전사 경로는 job 1건 = 1페이지라 `[0]` 만 쓴다.
    return {"elements": all_elements, "metadata": {"page_cnt": len(b64_pages)},
            "call_metas": call_metas}


def ocr_elements_sync(file_bytes: bytes, filename: str,
                      prompt_override: tuple[str, str] | None = None,
                      *, max_tokens: int | None = None) -> list[dict]:
    # parse-svc /parse 핸들러는 async def 라 이벤트루프가 도는 스레드에서 호출될 수 있다 —
    # 그 안에서 asyncio.run() 은 RuntimeError. 루프가 돌고 있으면 별도 스레드에서
    # asyncio.run 을 실행해 안전하게 블로킹한다.
    def _run():
        return asyncio.run(ocr_file_to_elements(
            file_bytes, filename, prompt_override, max_tokens=max_tokens))["elements"]
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _run()
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_run).result()


Job = tuple  # (file_bytes, filename, prompt_override | None, max_tokens | None)


async def ocr_elements_many(jobs: list[Job]) -> list[tuple[list[dict], list]]:
    """여러 건을 **한 이벤트루프에서** 동시 처리 — jobs 순서대로 elements 리스트를 돌려준다.

    호출 건마다 `asyncio.run` 을 돌리면(=`ocr_elements_sync` 를 N 번) 루프가 N 개 생겨
    `_VL_SEM` 과 `vl_api._http_client` 가 매번 재생성된다. 그러면 동시성도 없고 전역 상태만
    흔들린다. 이 진입점은 루프 하나를 공유해 그 둘을 일관되게 유지한다.

    건별 실패는 비치명 — 그 자리에 빈 리스트가 들어간다(인덱스 정렬 보존).
    """
    async def _one(job: Job) -> tuple[list[dict], list]:
        file_bytes, filename = job[0], job[1]
        prompt_override = job[2] if len(job) > 2 else None
        max_tokens = job[3] if len(job) > 3 else None
        res = await ocr_file_to_elements(
            file_bytes, filename, prompt_override, max_tokens=max_tokens)
        return res["elements"], res.get("call_metas") or []

    results = await asyncio.gather(*(_one(j) for j in jobs), return_exceptions=True)
    out: list[tuple[list[dict], list]] = []
    for job, r in zip(jobs, results):
        if isinstance(r, BaseException):
            # **삼킴 2층**(Phase 2b-1): 예외를 빈 리스트로만 바꾸면 상위가 "빈 응답" 과
            # 구분하지 못한다. meta 에 사유를 실어 올린다(동작은 그대로 — 건별 실패 비치명).
            log.error("VL OCR job failed (%s)", job[1], exc_info=r)
            from parse_service.parsers.ocr import vl_api
            out.append(([], [vl_api.VLCallMeta(
                error=f"{type(r).__name__}: {str(r)[:160]}")]))
        else:
            out.append(r)
    return out


def ocr_elements_many_sync(jobs: list[Job]) -> list[tuple[list[dict], list]]:
    """`ocr_elements_many` 의 동기 래퍼 — `asyncio.run` 을 **배치 전체에 1회**만 쓴다."""
    def _run():
        return asyncio.run(ocr_elements_many(jobs))
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _run()
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_run).result()


def _whole_file_elements(file_bytes: bytes, filename: str, ocr_url: str | None = None,
                         prompt_override: tuple[str, str] | None = None) -> list[dict]:
    return ocr_elements_sync(file_bytes, filename, prompt_override)


def _elements_to_pages(elements: list[dict]) -> list[dict]:
    from kb_pipeline.blockify import elements_to_blocks
    blocks = elements_to_blocks(elements)
    by_page: dict[int, list[dict]] = {}
    for b in blocks:
        page_number = int(b.get("page_idx", 0) or 0) + 1  # 0-based → 1-based canonical
        b["page_idx"] = page_number
        by_page.setdefault(page_number, []).append(b)
    return [{"page_number": pn, "blocks": by_page[pn]} for pn in sorted(by_page)]


def _page_traces_for_ocr(pages: list[dict]) -> list[dict]:
    """이미지/pptx 도메인 page_traces(2026-08-18 사용자 지시 — "어떤 lane을 탔는지 로그가
    없다"). PDF 처럼 triage/gate 레인 분기가 없어(§router.py — `IMAGE_EXTS`/pptx 전부
    `ocr` 도메인 하나로 매핑, paddle_gw 미배선) 기록할 "선택"이 없다 — 그래서 매 페이지가
    항상 같은 단일 사실("vl_ocr_direct")을 남긴다. PDF 쪽 page_trace 딕셔너리 계약
    (`parsers/pdf/__init__.py`)과 키를 맞춰 admin 화면(knowledge_base)이 도메인 구분 없이
    렌더할 수 있게 한다.
    """
    traces = []
    for p in pages:
        bl = p.get("blocks") or []
        chars = sum(len((b.get("table_body") or b.get("text") or "")) for b in bl)
        traces.append({
            "page_number": p.get("page_number"),
            "bucket": None,
            "lane": "vl_ocr_direct",
            "source": "vl" if bl else "empty",
            "attempts": [("route", "vl_ocr_direct",
                          {"reason": "image_or_pptx_domain_single_path"})],
            "chars": chars,
            "verdict": None,
            "state": None,
            "verdict_reason": None,
        })
    return traces


def parse(file_bytes: bytes, filename: str, *, ocr_url: str | None = None) -> RouteResult:
    """이미지 파일 → VL. **PAGE_HYBRID 프롬프트**(전사 + 시각 서술)를 쓴다.

    범용 전사 프롬프트(`build_user_prompt`)는 글자만 옮긴다 — 순서도·차트·아키텍처도
    이미지는 노드 라벨이 조각으로 흩어져 흐름·분기·계층이 사라진다(Plan A R2/R3 실측).
    이미지 파일은 그 자체가 시각 자료인 경우가 대부분이라 전사만으로는 부족하다.
    PAGE_HYBRID 는 기존 전사 프롬프트에 조항을 덧붙인 것이라 표 `<table>` 계약도 유지된다(R6).
    """
    from parse_service.parsers.ocr import prompts
    override = prompts.page_hybrid_prompts()  # call-time — env(KBP_PAGE_HYBRID_DIAGRAM_RULE) 반영
    try:
        elements = _whole_file_elements(file_bytes, filename, ocr_url,
                                        prompt_override=override)
    except Exception as e:  # noqa: BLE001
        raise ParserError(f"ocr failed for {filename}: {e}") from e
    if not elements:
        raise ParserError(f"ocr/vlm empty for {filename}")
    pages = _elements_to_pages(elements)
    # VL 퇴화(무한반복) 블록 제거 — pptx/이미지도 VL 경로라 pdf 레인과 동일 필터 적용.
    from parse_service.parsers.degen_filter import filter_degenerate_pages
    removed = filter_degenerate_pages(pages)
    if removed:
        log.warning("VL 퇴화 블록 %d개 제거 (%s)", removed, filename)
    return RouteResult(kind="pages", chunk_needed=True, pages=pages,
                       page_traces=_page_traces_for_ocr(pages))
