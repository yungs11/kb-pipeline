"""PyMuPDF 페이지 분류(Triage) — 비싼 처리(OCR/VL) 전 저비용 신호로 페이지별 경로 결정.

설계: docs/superpowers/specs/2026-07-08-pdf-triage-design.md

결정트리(**싼 신호만** — get_drawings/find_tables 는 안 쓴다: 텍스트-아웃라인/벡터표 문서에서
그리기 객체가 수만 개라 materialize 가 느림. 대신 content-stream 크기로 판별):

  native text 있음? (char>20)
    ├─ 다이어그램(curve≥30 or line≥100 or img≥5&cov≥0.1) → LLM_NEEDED + is_diagram
    ├─ mixed(텍스트 + 래스터 이미지 ≥25%)  → LLM_NEEDED (이미지 시각정보 해석)
    └─ 그 외                                → TEXT_ONLY (ODL 텍스트 추출)
  native text 없음
    ├─ 내용 있음(이미지 or content-stream 큼) → OCR_NEEDED (스캔·아웃라인·벡터표 = 텍스트 읽기)
    └─ 없음                                    → SKIP (진짜 빈 페이지)

다이어그램 신호(2026-07-14 실측 근거 — 정의서 p5 curve=144 / 소유권pptx p4 line=148·img11 /
소유권 p3 텍스트 line=53 미검출): **native text 있는 페이지에만** get_cdrawings 로 curve/line 을
센다. 병적 케이스(텍스트 없는 아웃라인 문서, 3.2만 curve)는 char=0 이라 이 경로를 안 타서
기존 "get_drawings 금지" 성능 결정과 충돌하지 않는다(디지털 페이지 실측 ~13ms/p).
표는 직선/rect 위주(curve 적음), 순서도는 곡선 커넥터·다수 직선 화살표·도형이미지로 구분.

부수효과 없음(판정만). OCR_NEEDED 는 현재 VL fallback, 로컬 OCR 엔진 연결 시 그 경로로 분기.
"""
from __future__ import annotations

import logging
import os
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
    # vector drawings (native-text 페이지 한정 — 다이어그램/순서도 신호)
    curve_count: int = 0
    line_count: int = 0
    # derived
    bucket: Optional[Bucket] = field(default=None, init=False)
    reason: str = field(default="", init=False)
    is_diagram: bool = field(default=False, init=False)
    # 가로형 페이지(width>height) — 2026-08-06, pptx 유래 등 방향 자체가 VL 필요 신호.
    is_landscape: bool = field(default=False, init=False)


def classify(
    sig: PageSignals,
    *,
    mixed_image_cov: float | None = None,
    content_min: int | None = None,
    diagram_curve_min: int | None = None,
    diagram_line_min: int | None = None,
    diagram_img_count: int | None = None,
    diagram_combo_curve_min: int | None = None,
) -> PageSignals:
    """native text 유무가 1차 갈림길. mixed/diagram 은 native text 있는 쪽에서만 판정.

    임계치 6개는 env 로 소스 수정 없이 조정 가능하다(2026-08-06, 이미지 파서 고도화 준비).
    인자를 명시하면 그 값이 우선하고, `None`(기본)이면 **호출 시점에** env 를 읽는다 —
    모듈 로드 시 읽으면 `monkeypatch.setenv` 가 안 먹는다(`tools/fileconvert.py` 관례).
    호출부(`gate.py`의 `triage_document(pdf_bytes)`)는 인자를 안 넘기므로 env 미설정 시
    아래 하드코딩 기본값과 100% 동일하게 동작한다(회귀 0).
    """
    if mixed_image_cov is None:
        mixed_image_cov = float(os.environ.get("KBP_TRIAGE_MIXED_IMAGE_COV") or 0.25)
    if content_min is None:
        content_min = int(os.environ.get("KBP_TRIAGE_CONTENT_MIN") or 300)
    if diagram_curve_min is None:
        diagram_curve_min = int(os.environ.get("KBP_TRIAGE_DIAGRAM_CURVE_MIN") or 30)
    if diagram_line_min is None:
        diagram_line_min = int(os.environ.get("KBP_TRIAGE_DIAGRAM_LINE_MIN") or 100)
    if diagram_img_count is None:
        diagram_img_count = int(os.environ.get("KBP_TRIAGE_DIAGRAM_IMG_COUNT") or 5)
    if diagram_combo_curve_min is None:
        diagram_combo_curve_min = int(os.environ.get("KBP_TRIAGE_DIAGRAM_COMBO_CURVE_MIN") or 10)

    chars = sig.char_count
    imgcov = sig.image_coverage
    # 가로형 페이지(width>height) → 묻고 따질 것 없이 LLM_NEEDED(VL). 2026-08-06, 사용자
    # 지시 — pptx 유래 등 방향 자체가 VL 필요 신호. 단, 진짜 다이어그램/혼합 판정보다
    # **우선순위를 낮춘다**(다이어그램 신호가 있으면 diagram_pages 집계용 is_diagram=True
    # 를 계속 보존해야 하므로 — ultracode 검증에서 잡힌 결함, 즉시 return 하지 않는다).
    landscape_to_llm = os.environ.get("KBP_TRIAGE_LANDSCAPE_TO_LLM", "1") != "0"

    if sig.has_native_text:
        # 다이어그램(순서도/차트) = ① 곡선 커넥터형(curve 多) or ② 직선화살표+도형이미지 복합형.
        # 단독 신호는 오검 — 실측(약관 292p): line 단독은 테두리 표(p275 line=1249, curve=8),
        # img 단독은 아이콘/QR 페이지(p12 img=11) 를 오검. 진짜 순서도는 소유권pptx p4 처럼
        # line(화살표)+img(도형)+curve(화살촉/라운드) 가 **동시에** 나타난다(148/11/12).
        # 정의서 p5(곡선 커넥터형)는 curve=144 로 ① 에 걸림.
        if (sig.curve_count >= diagram_curve_min
                or (sig.line_count >= diagram_line_min
                    and sig.image_count >= diagram_img_count
                    and sig.curve_count >= diagram_combo_curve_min)):
            sig.is_diagram = True
            sig.bucket = Bucket.LLM_NEEDED
            sig.reason = (f"다이어그램(curve={sig.curve_count}, line={sig.line_count}, "
                          f"img={sig.image_count}/{imgcov:.2f})")
        # 혼합: 텍스트 + 실제 래스터 이미지(≥mixed_image_cov) → VL(이미지 시각정보 해석 필요)
        elif sig.image_count > 0 and imgcov >= mixed_image_cov:
            sig.bucket = Bucket.LLM_NEEDED
            sig.reason = f"혼합 콘텐츠(텍스트+이미지={imgcov:.2f})"
        elif landscape_to_llm and sig.is_landscape:
            sig.bucket = Bucket.LLM_NEEDED
            sig.reason = f"가로형 문서 페이지 (width={sig.width:.0f} > height={sig.height:.0f})"
        else:
            sig.bucket = Bucket.TEXT_ONLY
            sig.reason = f"디지털 텍스트 (글자={chars}, 단어={sig.word_count})"
        return sig

    # 텍스트 레이어 없음(스캔): landscape 는 OCR_NEEDED/SKIP 보다 우선(사용자 의도 — 스캔
    # 여부와 무관하게 가로형이면 무조건 VL).
    if landscape_to_llm and sig.is_landscape:
        sig.bucket = Bucket.LLM_NEEDED
        sig.reason = f"가로형 문서 페이지(스캔) (width={sig.width:.0f} > height={sig.height:.0f})"
        return sig

    # 내용 있으면 OCR(스캔·아웃라인·벡터표), 없으면 빈 페이지.
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
    sig.is_landscape = sig.width > sig.height

    words = page.get_text("words")  # (x0,y0,x1,y1,word,block,line,word_no)
    sig.word_count = len(words)
    sig.char_count = sum(len(w[4]) for w in words)
    # env 로 조정 가능(2026-08-06) — 호출 시점에 읽는다(classify() 와 동일 관례).
    # **20 → 100 (2026-08-14)**: 스캔 페이지에 머리말·쪽번호·워터마크 같은 자투리
    # 네이티브 텍스트가 20자를 넘는 일이 흔해, 스캔인데 `has_native_text=True` 가 되어
    # TEXT_ONLY→odl 로 잘못 라우팅됐다. 450페이지 격자탐색 실측: ODL 오라우팅이
    # **20쪽 → 4쪽**으로 줄었다. `or` 를 쓰는 이유는 빈 값을 기본값으로 되돌리기 위함이다
    # (`get(k, "100")` 이면 빈 문자열이 그대로 와서 int("") → ValueError).
    native_text_min_chars = int(os.environ.get("KBP_TRIAGE_NATIVE_TEXT_MIN_CHARS") or 100)
    sig.has_native_text = sig.char_count > native_text_min_chars

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

    # 벡터 드로잉(curve/line)은 **native text 있는 페이지에만** 센다(다이어그램 신호).
    # 병적 케이스(텍스트 없는 아웃라인 문서, 수만 curve)는 char=0 → 이 경로 안 탐(성능 가드).
    if sig.has_native_text:
        try:
            for d in page.get_cdrawings():
                for it in d.get("items", []):
                    k = it[0]
                    if k == "c":
                        sig.curve_count += 1
                    elif k == "l":
                        sig.line_count += 1
        except Exception:  # noqa: BLE001 — 드로잉 파싱 실패는 신호 0 유지(비치명)
            pass

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
