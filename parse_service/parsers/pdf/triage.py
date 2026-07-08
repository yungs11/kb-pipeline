"""PyMuPDF 페이지 분류(Triage) — OCR/VL 비용 전 저비용 시그널로 페이지별 처리경로 결정.

설계: docs/superpowers/specs/2026-07-08-pdf-triage-design.md
버킷: SKIP(빈 페이지) / TEXT_ONLY(디지털→ODL) / OCR_NEEDED(스캔·단순→VL fallback) /
      LLM_NEEDED(순서도·차트·혼합·표>5·양식→VL). 부수효과 없음(판정만).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

import pymupdf

log = logging.getLogger("kb_pipeline.parse_service.parsers.pdf.triage")

# 대용량 PDF 방어: 페이지 수가 이보다 크면 느린 신호(find_tables/get_drawings)를 생략(0으로)해
# triage 지연을 제한한다. 그런 문서는 텍스트/이미지 신호만으로 분류(표/벡터 트리거 미발동).
_HEAVY_SCAN_MAX_PAGES = 300


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
    # images
    image_count: int = 0
    image_coverage: float = 0.0
    # structure
    has_tables: bool = False
    table_count: int = 0
    has_forms: bool = False
    block_count: int = 0
    vector_drawing: bool = False
    drawing_count: int = 0
    # derived
    bucket: Optional[Bucket] = field(default=None, init=False)
    reason: str = field(default="", init=False)


def classify(
    sig: PageSignals,
    *,
    blank_char: int = 10,
    ocr_image_cov: float = 0.25,
    ocr_text_max: int = 30,
    vector_min: int = 40,
    mixed_image_cov: float = 0.25,
) -> PageSignals:
    """우선순위 SKIP → LLM_NEEDED → OCR_NEEDED → TEXT_ONLY. 첫 매치에서 확정.

    LLM 은 **순서도/차트(벡터) + 혼합(이미지+텍스트)** 만 트리거한다(2026-07-08 결정:
    표 개수·양식은 LLM 트리거에서 제외 — 표는 텍스트가 있어 ODL/OCR 로 충분, 양식은
    스캔 코퍼스에서 거의 안 켜짐). `has_forms` 는 SKIP 방지용으로만 남긴다.
    """
    chars = sig.char_count
    imgcov = sig.image_coverage

    # 1) SKIP — 진짜 빈 페이지(텍스트/이미지/벡터/양식 모두 없음).
    #    ⚠️ 배제조건 필수: 순수 벡터 순서도(image 0·drawing↑)·이미지/스캔 페이지(image>0)·
    #    양식 페이지(has_forms)가 SKIP 으로 오검돼 드롭되는 것을 막는다.
    if (chars < blank_char and sig.image_count == 0
            and sig.drawing_count < vector_min and not sig.has_forms):
        sig.bucket = Bucket.SKIP
        sig.reason = f"빈 페이지 (글자={chars}, 이미지={sig.image_count}, 벡터={sig.drawing_count})"
        return sig

    # 2) LLM_NEEDED — 순서도/차트(벡터) + 혼합만.
    llm: list[str] = []
    # 벡터 다수 + native 텍스트 적음 = 순서도/차트(디지털 표의 경계선 오탐 방지: 표는 native
    # 텍스트가 많아 has_native_text=True → 여기 안 걸리고 ODL/OCR 로 처리).
    if sig.drawing_count >= vector_min and not sig.has_native_text:
        llm.append(f"벡터드로잉 {sig.drawing_count}(순서도/차트 근사)")
    if sig.image_count > 0 and sig.has_native_text and imgcov >= mixed_image_cov:
        llm.append(f"혼합 콘텐츠(이미지={imgcov:.2f}+텍스트)")
    if llm:
        sig.bucket = Bucket.LLM_NEEDED
        sig.reason = "복잡 레이아웃: " + ", ".join(llm)
        return sig

    # 3) OCR_NEEDED — 스캔/이미지 중심 + 단순. `or image_count>0` 로 near-textless 페이지가
    #    작은 figure(이미지 비율 0.02~0.25)를 달고 TEXT_ONLY(빈 ODL)로 새는 gap-band 방지.
    if (imgcov >= ocr_image_cov or sig.image_count > 0) and chars < ocr_text_max:
        sig.bucket = Bucket.OCR_NEEDED
        sig.reason = f"스캔/이미지 중심 단순 (이미지={imgcov:.2f}, 글자={chars})"
        return sig

    # 4) TEXT_ONLY — 디지털 본문
    sig.bucket = Bucket.TEXT_ONLY
    sig.reason = (
        f"디지털 텍스트 (글자={chars}, 단어={sig.word_count}, 텍스트비율={sig.text_coverage:.2f})"
    )
    return sig


def extract_signals(page: "pymupdf.Page", *, heavy_scan: bool = True) -> PageSignals:
    """단일 fitz 페이지 → 저비용 신호. 개별 신호 추출 실패는 보수적 기본값으로 무시.

    page_number 는 **1-based**(page.number 는 0-based 라 +1) — parse() 출력 및 다운스트림
    page_idx 와 일치시킨다. heavy_scan=False 면 find_tables/get_drawings 를 생략(대용량 방어).
    """
    rect = page.rect
    page_area = (rect.width * rect.height) or 1.0
    sig = PageSignals(page_number=page.number + 1, width=rect.width, height=rect.height)

    words = page.get_text("words")  # (x0,y0,x1,y1,word,block,line,word_no)
    sig.word_count = len(words)
    sig.char_count = sum(len(w[4]) for w in words)
    sig.has_native_text = sig.char_count > 20

    blocks = page.get_text("blocks")  # (x0,y0,x1,y1,text,block_no,block_type)
    sig.block_count = len(blocks)
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

    # 양식 위젯: PyMuPDF 1.27 에서 form widget 은 page.annots() 가 아니라 page.widgets() 로만
    # 잡힌다. LLM 트리거는 아니지만 SKIP 방지용으로 계산(cheap).
    try:
        for _w in (page.widgets() or []):
            sig.has_forms = True
            break
    except Exception:  # noqa: BLE001
        pass

    # 벡터 드로잉(순서도/차트 근사). ⚡지연 최적화(2026-07-08): get_drawings 는 벡터 path 많은
    # 페이지에서 무겁다. 벡터 LLM 트리거는 `not has_native_text` 에서만, SKIP 도 char<10(⊂ 텍스트
    # 없음)에서만 벡터를 보므로 **native 텍스트가 있는 페이지에선 아예 계산하지 않는다**(디지털
    # 본문=대다수 → 큰 지연 절감). find_tables 는 표를 라우팅에 안 쓰므로 호출 자체를 제거.
    if heavy_scan and not sig.has_native_text:
        try:
            sig.drawing_count = len(page.get_drawings())
        except Exception:  # noqa: BLE001
            sig.drawing_count = 0
    sig.vector_drawing = sig.drawing_count > 0
    # table_count/has_tables 는 라우팅 미사용 → 미계산(기본 0/False).

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
        heavy = len(doc) <= _HEAVY_SCAN_MAX_PAGES
        if not heavy:
            log.warning("triage: %d 페이지(>%d) — heavy 신호(find_tables/get_drawings) 생략",
                        len(doc), _HEAVY_SCAN_MAX_PAGES)
        for page in doc:
            sig = extract_signals(page, heavy_scan=heavy)
            classify(sig, **classify_kwargs)
            out.append(sig)
    finally:
        doc.close()
    return out
