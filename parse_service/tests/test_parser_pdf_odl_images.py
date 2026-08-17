"""§B.2 — ODL 이미지 참조 면적필터/개수불일치 가드/개수초과 전면VL escalate."""
import pymupdf
import pytest

from parse_service.parsers import pdf as pdf_parser
from parse_service.parsers.pdf import odl_image_summary


class _FakePage:
    def __init__(self, images, rect=(0, 0, 100, 100)):
        self._images = images
        self.rect = pymupdf.Rect(*rect)

    def get_image_info(self, hashes=False, xrefs=False):
        return self._images


class _FakeDoc:
    def __init__(self, pages):
        self._pages = pages
        self.page_count = len(pages)

    def __getitem__(self, i):
        return self._pages[i]

    def close(self):
        pass


def _patch_pymupdf_open(monkeypatch, doc):
    monkeypatch.setattr(pymupdf, "open", lambda stream, filetype: doc)


def test_count_mismatch_strips_all_refs_safely(monkeypatch):
    """ODL 참조 개수(1) != PyMuPDF 이미지 개수(2) — 위치매칭 포기, 전부 스트립(leak 없음)."""
    doc = _FakeDoc([_FakePage([{"bbox": (0, 0, 50, 50)}, {"bbox": (0, 0, 10, 10)}])])
    _patch_pymupdf_open(monkeypatch, doc)
    md = "본문 텍스트\n![](<a_images/imageFile1.png>)\n뒷부분"
    resolved, blocks_override = pdf_parser._resolve_odl_page_images(b"%PDF", md, 1, {})
    assert blocks_override is None
    assert "imageFile1.png" not in resolved
    assert "본문 텍스트" in resolved and "뒷부분" in resolved


def test_small_image_below_threshold_is_stripped(monkeypatch):
    """면적 미달(장식) — VL 호출 없이 참조만 제거."""
    doc = _FakeDoc([_FakePage([{"bbox": (0, 0, 1, 1)}])])   # 1/10000 = 0.01%
    _patch_pymupdf_open(monkeypatch, doc)
    monkeypatch.setenv("KBP_ODL_IMAGE_VL_MIN_AREA", "0.01")
    called = []
    monkeypatch.setattr(odl_image_summary, "summarize_odl_image",
                        lambda b: called.append(1) or "요약")
    md = "앞\n![](<a_images/imageFile1.png>)\n뒤"
    resolved, blocks_override = pdf_parser._resolve_odl_page_images(
        b"%PDF", md, 1, {"a_images/imageFile1.png": b"bytes"})
    assert blocks_override is None
    assert called == [], "임계 미달이면 VL을 부르지 않는다"
    assert "imageFile1.png" not in resolved and "요약" not in resolved


def test_large_image_above_threshold_gets_vl_summary(monkeypatch):
    """면적 충분 — 확보한 바이트로 VL 요약 호출 → 참조를 서술로 치환."""
    doc = _FakeDoc([_FakePage([{"bbox": (0, 0, 50, 50)}])])   # 2500/10000 = 25%
    _patch_pymupdf_open(monkeypatch, doc)
    monkeypatch.setenv("KBP_ODL_IMAGE_VL_MIN_AREA", "0.01")
    monkeypatch.setattr(odl_image_summary, "summarize_odl_image",
                        lambda b: "이건 신탁원부 발췌 화면이다")
    md = "앞\n![](<a_images/imageFile1.png>)\n뒤"
    resolved, blocks_override = pdf_parser._resolve_odl_page_images(
        b"%PDF", md, 1, {"a_images/imageFile1.png": b"realbytes"})
    assert blocks_override is None
    assert "imageFile1.png" not in resolved
    assert "신탁원부 발췌" in resolved


def test_vl_summary_failure_falls_back_to_strip_not_leak(monkeypatch):
    """VL 요약 실패(None 반환)해도 원본 경로는 절대 남지 않는다 — 참조 제거로 폴백."""
    doc = _FakeDoc([_FakePage([{"bbox": (0, 0, 50, 50)}])])
    _patch_pymupdf_open(monkeypatch, doc)
    monkeypatch.setenv("KBP_ODL_IMAGE_VL_MIN_AREA", "0.01")
    monkeypatch.setattr(odl_image_summary, "summarize_odl_image", lambda b: None)
    md = "![](<a_images/imageFile1.png>)"
    resolved, _ = pdf_parser._resolve_odl_page_images(
        b"%PDF", md, 1, {"a_images/imageFile1.png": b"bytes"})
    assert "imageFile1.png" not in resolved, "실패해도 원본 경로가 남으면 leak 재현"


def test_image_count_exceeds_escalate_reroutes_to_full_vl(monkeypatch):
    """이미지 참조 개수가 KBP_ODL_IMAGE_COUNT_VL_ESCALATE 초과 — 개별 서술 대신 그 페이지
    전체를 전면 VL 전사(vl 레인과 동일 경로)로 넘긴다."""
    images = [{"bbox": (0, 0, 1, 1)}] * 3
    doc = _FakeDoc([_FakePage(images)])
    _patch_pymupdf_open(monkeypatch, doc)
    monkeypatch.setenv("KBP_ODL_IMAGE_COUNT_VL_ESCALATE", "2")

    class _RP:
        page_number, jpeg = 1, b"jpegbytes"

    monkeypatch.setattr(pdf_parser, "_render_pages", lambda fb, page_numbers=None: [_RP()])
    monkeypatch.setattr(pdf_parser, "_ocr_elements_for_page",
                        lambda jpeg, name, ocr_url=None: [
                            {"category": "text", "content": {"markdown": "전면 VL 전사 결과"}}])
    md = "\n".join(f"![](<a_images/imageFile{i}.png>)" for i in range(1, 4))
    resolved, blocks_override = pdf_parser._resolve_odl_page_images(b"%PDF", md, 1, {})
    assert blocks_override is not None, "개수 초과면 blocks_override 로 완성된 블록을 준다"
    texts = " ".join(b.get("text") or "" for b in blocks_override)
    assert "전면 VL 전사 결과" in texts


def test_summarize_odl_image_returns_none_on_vl_failure(monkeypatch):
    """odl_image_summary — VL 호출 실패 시 예외를 삼키고 None(호출부가 참조 제거로 폴백)."""
    async def boom(*a, **k):
        raise RuntimeError("vl down")

    monkeypatch.setattr(
        "parse_service.parsers.ocr.vl_api.call_vl_api_with_base64", boom)
    assert odl_image_summary.summarize_odl_image(b"imgbytes") is None
