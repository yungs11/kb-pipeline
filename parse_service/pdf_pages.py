"""parse-svc 페이지 렌더 — 원본 PDF 를 페이지별 JPEG + 텍스트로 래스터화.

knowledge_base ``core/pdf_pages.py:render_pdf_pages`` 미러(spec §5.1.2). PyMuPDF(``fitz``)로
페이지마다 (1-based page_number, JPEG 바이트, 추출 텍스트)를 만들어 ``RenderedPage`` 로
돌려준다. 이미지 래스터는 텍스트 레이어가 없어도 동작하므로 디지털/스캔 PDF 공통으로 쓴다
(spec §4: 이미지 렌더는 디지털/스캔 공통).

기본 ``dpi=300, jpg_quality=90`` — 뷰어 화질 우선(spec §5.1.2 D3). PyMuPDF(``fitz``)는 함수
내부에서 lazy import 한다(순수 매핑 테스트는 fitz 불요). PDF 가 아니거나 열기/렌더 실패면
**빈 리스트**를 반환한다(비치명 — 호출자는 페이지 이미지 없이 진행, 적재 실패로 번지지 않음).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger("kb_pipeline.parse_service.pdf_pages")


@dataclass(frozen=True)
class RenderedPage:
    """렌더된 PDF 한 페이지 — 1-based ``page_number``, JPEG 바이트, 추출 텍스트."""

    page_number: int
    jpeg: bytes
    text: str


def render_pdf_pages(
    pdf_bytes: bytes, *, dpi: int = 300, jpg_quality: int = 90
) -> list[RenderedPage]:
    """원본 PDF 를 페이지별 JPEG(알파 없음) + 텍스트로 렌더한다.

    PyMuPDF(``fitz``)는 함수 내부에서 lazy import 한다. PDF 가 아니거나 열기 실패면 빈
    리스트를 반환한다(호출자가 페이지 매핑 skip — 적재 실패로 번지지 않음). 손상 PDF/렌더
    오류도 동일하게 빈 리스트(비치명).
    """
    try:
        import fitz  # PyMuPDF
    except Exception:  # noqa: BLE001 - fitz 부재는 페이지 이미지 미생성(비치명).
        log.warning("PyMuPDF(fitz) unavailable — skipping page render")
        return []
    pages: list[RenderedPage] = []
    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            for i, page in enumerate(doc):
                pix = page.get_pixmap(dpi=dpi, alpha=False)
                jpeg = pix.tobytes(output="jpeg", jpg_quality=jpg_quality)
                text = page.get_text("text") or ""
                pages.append(RenderedPage(page_number=i + 1, jpeg=jpeg, text=text))
    except Exception:  # noqa: BLE001 - 손상 PDF/렌더 오류는 비치명(페이지 이미지 없이 진행).
        log.exception("render_pdf_pages failed")
        return []
    return pages
