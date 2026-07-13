"""PyMuPDF 페이지 분류(Triage) — 비싼 처리(OCR/VL) 전 저비용 신호로 페이지별 경로 결정.

설계: docs/superpowers/specs/2026-07-08-pdf-triage-design.md

결정트리(**싼 신호만** — get_drawings/find_tables 는 안 쓴다: 텍스트-아웃라인/벡터표 문서에서
그리기 객체가 수만 개라 materialize 가 느림. 대신 content-stream 크기로 판별):

  native text 있음? (char>20)
    ├─ mixed(텍스트 + 래스터 이미지 ≥25%)  → LLM_NEEDED (이미지 시각정보 해석)
    └─ 그 외                                → TEXT_ONLY (ODL 텍스트 추출)
  native text 없음
    ├─ 내용 있음(이미지 or content-stream 큼) → OCR_NEEDED (스캔·아웃라인·벡터표 = 텍스트 읽기)
    └─ 없음                                    → SKIP (진짜 빈 페이지)

부수효과 없음(판정만). OCR_NEEDED 는 현재 VL fallback, 로컬 OCR 엔진 연결 시 그 경로로 분기.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

import pymupdf

log = logging.getLogger("kb_pipeline.parse_service.parsers.pdf.triage")


class Bucket(Enum):
    SKIP = auto()
    TEXT_ONLY = auto()
    OCR_NEEDED = auto()
    LLM_NEEDED = auto()


@dataclass
class PageSignals:
    page_number: int
    width: float
    height: float
    # text
    char_count: int = 0
    word_count: int = 0
    has_native_text: bool = False
    text_coverage: float = 0.0
    # images (raster)
    image_count: int = 0
    image_coverage: float = 0.0
    # content-stream 바이트 크기(빈 페이지 vs 벡터/아웃라인 판별용 — get_drawings 대체 싼 신호)
    content_len: int = 0
    # derived
    bucket: Optional[Bucket] = field(default=None, init=False)
    reason: str = field(default="", init=False)


def classify(
    sig: PageSignals,
    *,
    mixed_image_cov: float = 0.25,
    content_min: int = 300,
) -> PageSignals:
    """native text 유무가 1차 갈림길. mixed 는 native text 있는 쪽에서만 판정."""
    chars = sig.char_count
    imgcov = sig.image_coverage

    if sig.has_native_text:
        # 혼합: 텍스트 + 실제 래스터 이미지(≥mixed_image_cov) → VL(이미지 시각정보 해석 필요)
        if sig.image_count > 0 and imgcov >= mixed_image_cov:
            sig.bucket = Bucket.LLM_NEEDED
            sig.reason = f"혼합 콘텐츠(텍스트+이미지={imgcov:.2f})"
        else:
            sig.bucket = Bucket.TEXT_ONLY
            sig.reason = f"디지털 텍스트 (글자={chars}, 단어={sig.word_count})"
        return sig

    # 텍스트 레이어 없음: 내용 있으면 OCR(스캔·아웃라인·벡터표), 없으면 빈 페이지.
    if sig.image_count > 0 or sig.content_len > content_min:
        sig.bucket = Bucket.OCR_NEEDED
        sig.reason = f"텍스트없는 콘텐츠 (이미지={sig.image_count}, content={sig.content_len}B) → OCR/VL"
        return sig

    sig.bucket = Bucket.SKIP
    sig.reason = f"빈 페이지 (글자={chars}, 이미지={sig.image_count}, content={sig.content_len}B)"
    return sig


def extract_signals(page: "pymupdf.Page") -> PageSignals:
    """단일 fitz 페이지 → 저비용 신호. get_drawings/find_tables 는 쓰지 않는다(지연 방지)."""
    rect = page.rect
    page_area = (rect.width * rect.height) or 1.0
    sig = PageSignals(page_number=page.number + 1, width=rect.width, height=rect.height)

    words = page.get_text("words")  # (x0,y0,x1,y1,word,block,line,word_no)
    sig.word_count = len(words)
    sig.char_count = sum(len(w[4]) for w in words)
    sig.has_native_text = sig.char_count > 20

    blocks = page.get_text("blocks")  # (x0,y0,x1,y1,text,block_no,block_type)
    text_area = sum((b[2] - b[0]) * (b[3] - b[1]) for b in blocks if b[6] == 0)
    sig.text_coverage = min(text_area / page_area, 1.0)

    try:
        images = page.get_image_info(hashes=False, xrefs=False)
    except Exception:  # noqa: BLE001
        images = []
    sig.image_count = len(images)
    img_area = sum(
        (im["bbox"][2] - im["bbox"][0]) * (im["bbox"][3] - im["bbox"][1])
        for im in images if im.get("bbox")
    )
    sig.image_coverage = min(img_area / page_area, 1.0)

    # content-stream 크기는 **텍스트도 이미지도 없을 때만**(=빈 페이지 vs 벡터/아웃라인 판별에만)
    # 필요 → 그 경우에만 읽는다. read_contents 는 바이트만 반환(그리기 객체 materialize 없음 → 싸다).
    if not sig.has_native_text and sig.image_count == 0:
        try:
            sig.content_len = len(page.read_contents())
        except Exception:  # noqa: BLE001
            sig.content_len = 0

    return sig


def triage_document(pdf_bytes: bytes, **classify_kwargs) -> list[PageSignals]:
    """PDF bytes → 페이지 순서대로 분류된 PageSignals 리스트. 열기 실패 시 []."""
    try:
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    except Exception:  # noqa: BLE001 — 비-PDF/손상: 폴백을 위해 빈 리스트
        log.warning("triage: PDF 열기 실패 — 폴백(빈 리포트)")
        return []
    out: list[PageSignals] = []
    try:
        for page in doc:
            sig = extract_signals(page)
            classify(sig, **classify_kwargs)
            out.append(sig)
    finally:
        doc.close()
    return out
