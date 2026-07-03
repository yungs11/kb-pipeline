"""OCR 도메인 파서 — pptx + 이미지/스캔. Phase 2c: in-process VL OCR (:18050 HTTP 제거).

내부: pptx→convert_to_pdf_bytes(gotenberg)→pdf_bytes_to_base64_list; 이미지→
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

log = logging.getLogger("kb_pipeline.parse_service.parsers.ocr")


def _sem() -> asyncio.Semaphore:
    global _VL_SEM
    if _VL_SEM is None:
        _VL_SEM = asyncio.Semaphore(int(os.environ.get("KBP_VL_MAX_CONCURRENT", "3")))
    return _VL_SEM


async def _file_to_base64_pages(file_bytes: bytes, filename: str) -> list[str]:
    from parse_service.parsers.ocr import image_utils, pdf_converter
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    suffix = "." + ext if ext else ""
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(file_bytes)
        path = tmp.name
    try:
        if ext in IMAGE_EXTS:
            return image_utils.image_file_to_base64_list(path)
        # pptx (및 pdf 변환 가능 office): gotenberg → PDF → 페이지 base64
        gotenberg = os.environ.get("GOTENBERG_URL", "http://localhost:3000")
        pdf_bytes, ok, _name = pdf_converter.convert_to_pdf_bytes(path, gotenberg)
        if not ok:
            raise ParserError(f"gotenberg conversion failed for {filename}")
        return pdf_converter.pdf_bytes_to_base64_list(pdf_bytes)
    finally:
        os.unlink(path)


async def ocr_file_to_elements(file_bytes: bytes, filename: str) -> dict:
    from parse_service.parsers.ocr import vl_api, elements_parser, prompts
    b64_pages = await _file_to_base64_pages(file_bytes, filename)
    system_p, user_p = prompts.build_system_prompt(), prompts.build_user_prompt()
    all_elements: list[dict] = []
    next_id = 0
    for page_num, b64 in enumerate(b64_pages, start=1):
        try:
            async with _sem():
                vl_resp, _t = await vl_api.call_vl_api_with_base64(b64, user_p, system_p)
            els, next_id = elements_parser.parse_vision_language_response_to_elements(
                vl_resp, page_num, next_id)
            all_elements.extend(els)
        except Exception:  # noqa: BLE001 — 페이지 실패 비치명
            log.exception("VL OCR failed page %d", page_num)
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
    return {"elements": all_elements, "metadata": {"page_cnt": len(b64_pages)}}


def ocr_elements_sync(file_bytes: bytes, filename: str) -> list[dict]:
    # parse-svc /parse 핸들러는 async def 라 이벤트루프가 도는 스레드에서 호출될 수 있다 —
    # 그 안에서 asyncio.run() 은 RuntimeError. 루프가 돌고 있으면 별도 스레드에서
    # asyncio.run 을 실행해 안전하게 블로킹한다.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(ocr_file_to_elements(file_bytes, filename))["elements"]
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(lambda: asyncio.run(ocr_file_to_elements(file_bytes, filename)))
        return fut.result()["elements"]


def _whole_file_elements(file_bytes: bytes, filename: str, ocr_url: str | None = None) -> list[dict]:
    return ocr_elements_sync(file_bytes, filename)


def _elements_to_pages(elements: list[dict]) -> list[dict]:
    from kb_pipeline.blockify import elements_to_blocks
    blocks = elements_to_blocks(elements)
    by_page: dict[int, list[dict]] = {}
    for b in blocks:
        page_number = int(b.get("page_idx", 0) or 0) + 1  # 0-based → 1-based canonical
        b["page_idx"] = page_number
        by_page.setdefault(page_number, []).append(b)
    return [{"page_number": pn, "blocks": by_page[pn]} for pn in sorted(by_page)]


def parse(file_bytes: bytes, filename: str, *, ocr_url: str | None = None) -> RouteResult:
    try:
        elements = _whole_file_elements(file_bytes, filename, ocr_url)
    except Exception as e:  # noqa: BLE001
        raise ParserError(f"ocr failed for {filename}: {e}") from e
    if not elements:
        raise ParserError(f"ocr/vlm empty for {filename}")
    return RouteResult(kind="pages", chunk_needed=True, pages=_elements_to_pages(elements))
