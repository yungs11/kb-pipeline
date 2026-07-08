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
    simple_table_max: int = 5,
    vector_min: int = 40,
    mixed_image_cov: float = 0.25,
) -> PageSignals:
    """우선순위 SKIP → LLM_NEEDED → OCR_NEEDED → TEXT_ONLY. 첫 매치에서 확정."""
    chars = sig.char_count
    imgcov = sig.image_coverage

    # 1) SKIP — 진짜 빈 페이지(텍스트/이미지/벡터/양식 모두 없음).
    #    ⚠️ 아래 3개 배제조건 필수(v2/v3): 이 조건들이 없으면 SKIP 이 너무 공격적이라
    #    - 순수 벡터 순서도(image_count 0 이지만 drawing_count↑) → 벡터 LLM 트리거를 못 탐
    #    - 이미지(스캔) 페이지(char 0 이지만 image_count>0) → OCR 못 타고 드롭(콘텐츠 유실)
    #    - 양식 페이지(char 0 이지만 has_forms) → 양식 LLM 트리거를 못 탐(드롭)
    #    ⇒ 이런 페이지는 SKIP 하지 않고 아래 규칙으로 넘긴다.
    if (chars < blank_char and sig.image_count == 0
            and sig.drawing_count < vector_min and not sig.has_forms):
        sig.bucket = Bucket.SKIP
        sig.reason = f"빈 페이지 (글자={chars}, 이미지={sig.image_count}, 벡터={sig.drawing_count})"
        return sig

    # 2) LLM_NEEDED — 하드 트리거(하나라도 참이면 즉시 VL)
    llm: list[str] = []
    if sig.table_count > simple_table_max:
        llm.append(f"표 {sig.table_count}개(>{simple_table_max})")
    # 벡터 다수 + native 텍스트 적음 = 순서도/차트(디지털 표의 경계선 오탐 방지: 표는 native
    # 텍스트가 많아 has_native_text=True → 여기 안 걸리고 ODL/표>5 규칙으로 처리).
    if sig.drawing_count >= vector_min and not sig.has_native_text:
        llm.append(f"벡터드로잉 {sig.drawing_count}(순서도/차트 근사)")
    if sig.has_forms:
        llm.append("양식(Form) 위젯")
    if sig.image_count > 0 and sig.has_native_text and imgcov >= mixed_image_cov:
        llm.append(f"혼합 콘텐츠(이미지={imgcov:.2f}+텍스트)")
    if llm:
        sig.bucket = Bucket.LLM_NEEDED
        sig.reason = "복잡 레이아웃: " + ", ".join(llm)
        return sig

    # 3) OCR_NEEDED — 스캔/이미지 중심 + 단순. `or image_count>0` 로 near-textless 페이지가
    #    작은 figure(이미지 비율 0.02~0.25)를 달고 TEXT_ONLY(빈 ODL)로 새는 gap-band 방지(v3).
    if (imgcov >= ocr_image_cov or sig.image_count > 0) and chars < ocr_text_max:
        sig.bucket = Bucket.OCR_NEEDED
        sig.reason = f"스캔/이미지 중심 단순 (이미지={imgcov:.2f}, 글자={chars}, 표={sig.table_count})"
        return sig

    # 4) TEXT_ONLY — 디지털 본문
    sig.bucket = Bucket.TEXT_ONLY
    sig.reason = (
        f"디지털 텍스트 (글자={chars}, 단어={sig.word_count}, 텍스트비율={sig.text_coverage:.2f})"
    )
    return sig
