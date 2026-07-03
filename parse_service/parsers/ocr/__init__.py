"""OCR 도메인 파서 — pptx + 이미지/스캔. Phase 2a: HTTP(:18050) 위임, 2c 에서 in-process."""
from __future__ import annotations

from parse_service.parsers import RouteResult, ParserError

IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff", "webp"}


def _whole_file_elements(file_bytes: bytes, filename: str, ocr_url: str) -> list[dict]:
    from parse_service.parsing import _ocr_page  # 기존 HTTP contract 재사용
    return _ocr_page(file_bytes, filename, ocr_url=ocr_url)


def _elements_to_pages(elements: list[dict]) -> list[dict]:
    from kb_pipeline.blockify import elements_to_blocks
    blocks = elements_to_blocks(elements)
    by_page: dict[int, list[dict]] = {}
    for b in blocks:
        page_number = int(b.get("page_idx", 0) or 0) + 1  # 0-based → 1-based canonical
        b["page_idx"] = page_number
        by_page.setdefault(page_number, []).append(b)
    return [{"page_number": pn, "blocks": by_page[pn]} for pn in sorted(by_page)]


def parse(file_bytes: bytes, filename: str, *, ocr_url: str) -> RouteResult:
    try:
        elements = _whole_file_elements(file_bytes, filename, ocr_url)
    except Exception as e:  # noqa: BLE001 — HTTP/네트워크 오류 정규화
        raise ParserError(f"ocr failed for {filename}: {e}") from e
    if not elements:
        raise ParserError(f"ocr/vlm empty for {filename}")
    return RouteResult(kind="pages", chunk_needed=True, pages=_elements_to_pages(elements))
