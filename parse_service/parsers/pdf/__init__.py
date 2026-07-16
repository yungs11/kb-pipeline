"""PDF 도메인 파서 — OpenDataLoader(도구) 페이지별 md → blocks. 스캔 페이지는 OCR 보충."""
from __future__ import annotations

import logging
import re

from parse_service.parsers import RouteResult, ParserError
from parse_service.parsers.pdf.mineru_lane import run_mineru
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


def _render_pages(file_bytes: bytes):
    from parse_service.pdf_pages import render_pdf_pages
    return render_pdf_pages(file_bytes)


def _ocr_elements_for_page(jpeg: bytes, name: str, ocr_url: str | None = None,
                           *, diagram: bool = False) -> list[dict]:
    # Phase 2c: in-process VL OCR (HTTP 제거). diagram=True 면 순서도 서술 전용 프롬프트.
    from parse_service.parsers.ocr import ocr_elements_sync
    from parse_service.parsers.ocr import prompts
    override = ((prompts.DIAGRAM_SYSTEM_PROMPT, prompts.DIAGRAM_USER_PROMPT)
                if diagram else None)
    return ocr_elements_sync(jpeg, name, override)


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
    return res


def _parse_routed(file_bytes: bytes, filename: str, *, ocr_url: str) -> RouteResult:
    """문서수준 게이트 → ODL / vl(차트多) / paddle_gw(스캔) — 실패·빈결과 시 ODL/VL 폴백."""
    decision = _safe_decide_route(file_bytes)
    if decision is not None and decision.lane == "vl":
        # 차트/그림 페이지 비율 높음(스캔 여부 무관): 전 페이지 렌더→in-process VL(qwen).
        try:
            pages = _vl_lane(file_bytes, filename, ocr_url=ocr_url)
        except Exception:  # noqa: BLE001 — VL 레인 실패는 비치명
            log.exception("vl 레인 실패 — ODL 폴백 (%s)", filename)
        else:
            if pages and any(p.get("blocks") for p in pages):
                return RouteResult(kind="pages", chunk_needed=True, pages=pages)
            log.warning("vl 레인 빈 결과 — ODL 폴백 (%s)", filename)
    elif decision is not None and decision.lane == "paddle_gw":
        # 스캔 문서: PaddleOCR-VL 게이트웨이(GPU 전체 파이프라인). 실패/빈결과 → ODL 레인
        # (스캔 페이지는 그 안의 in-process VL 보충으로 처리 — MinerU 폴백 없음, 2026-07-15 결정).
        try:
            from parse_service.parsers.pdf.paddle_gw import run_paddle_gateway
            pages = run_paddle_gateway(file_bytes, filename)
        except Exception:  # noqa: BLE001 — 게이트웨이 실패는 비치명
            log.exception("paddle_gw 레인 실패 — ODL/VL 폴백 (%s)", filename)
        else:
            if pages and any(p.get("blocks") for p in pages):
                # 다이어그램 페이지는 VL 서술로 **교체** — 게이트웨이 OCR 조각/죽은 이미지참조 제거.
                _supplement_diagram_pages(pages, file_bytes,
                                          decision.diagram_pages, ocr_url, replace=True)
                return RouteResult(kind="pages", chunk_needed=True, pages=pages)
            log.warning("paddle_gw 빈 결과 — ODL/VL 폴백 (%s)", filename)
    elif decision is not None and decision.lane == "mineru":
        try:
            pages = run_mineru(file_bytes, filename, decision.parse_method,
                               decision.backend)
        except Exception:  # noqa: BLE001 — MinerU 실패는 비치명, ODL/VL 폴백
            log.exception("MinerU 레인(%s) 실패 — ODL/VL 폴백 (%s)",
                          decision.backend, filename)
        else:
            if pages and any(p.get("blocks") for p in pages):
                return RouteResult(kind="pages", chunk_needed=True, pages=pages)
            log.warning("MinerU 빈 결과 — ODL/VL 폴백 (%s)", filename)
    diagram_pages = tuple(getattr(decision, "diagram_pages", ()) or ()) if decision else ()
    return _odl_lane(file_bytes, filename, ocr_url=ocr_url, diagram_pages=diagram_pages)


def _vl_lane(file_bytes: bytes, filename: str, *, ocr_url: str) -> list[dict]:
    """차트/그림 중심 문서: 전 페이지 렌더 → in-process VL(qwen) elements → blocks.

    스캔 페이지 VL 보충과 동일 부품(_render_pages/_ocr_elements_for_page)을 문서 전체에 적용.
    페이지 단위 실패는 비치명(빈 blocks) — 전 페이지 실패면 parse() 의 빈결과 폴백이 ODL 로 잡음.
    """
    from kb_pipeline.blockify import elements_to_blocks
    pages: list[dict] = []
    for rp in _render_pages(file_bytes):
        try:
            elements = _ocr_elements_for_page(rp.jpeg, f"page-{rp.page_number}.jpeg", ocr_url)
        except Exception:  # noqa: BLE001
            log.exception("vl lane page %d failed (%s)", rp.page_number, filename)
            pages.append({"page_number": rp.page_number, "blocks": []})
            continue
        blocks = elements_to_blocks(elements)
        for b in blocks:
            b["page_idx"] = rp.page_number
        pages.append({"page_number": rp.page_number, "blocks": blocks})
    return pages


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
    for pno in diagram_pages:
        entry = next((p for p in pages if p["page_number"] == pno), None)
        if entry is None:
            continue
        if rendered is None:
            rendered = _render_pages(file_bytes)
        page_jpeg = next((rp.jpeg for rp in rendered if rp.page_number == pno), None)
        if page_jpeg is None:
            log.warning("diagram page %d has no rendered image", pno)
            continue
        try:
            elements = _ocr_elements_for_page(page_jpeg, f"page-{pno}-diagram.jpeg", ocr_url,
                                              diagram=True)
        except Exception:  # noqa: BLE001 — 다이어그램 VL 실패는 비치명(기존 블록 유지)
            log.exception("diagram VL supplement failed for page %d", pno)
            continue
        extra = elements_to_blocks(elements)
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
