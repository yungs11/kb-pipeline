"""triage 분류 규칙 — 합성 PageSignals(단위) + fitz 스모크(통합).

결정트리: native text → mixed?LLM:TEXT / no text → content?OCR:SKIP.
"""
from parse_service.parsers.pdf.triage import PageSignals, Bucket, classify


def _sig(**kw) -> PageSignals:
    s = PageSignals(page_number=1, width=595.0, height=842.0)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


# ── native text 있음 ──────────────────────────────────────────────────────────

def test_digital_text_only():
    s = classify(_sig(char_count=1362, word_count=93, has_native_text=True, image_coverage=0.0))
    assert s.bucket is Bucket.TEXT_ONLY


def test_text_with_small_logo_stays_text():
    # 텍스트 + 작은 로고(3%) → mixed 아님 → TEXT_ONLY
    s = classify(_sig(char_count=500, has_native_text=True, image_count=1, image_coverage=0.03))
    assert s.bucket is Bucket.TEXT_ONLY


def test_mixed_text_and_big_image_llm():
    # 텍스트 + 큰 이미지/차트(≥25%) → 혼합 → LLM(VL)
    s = classify(_sig(char_count=500, has_native_text=True, image_count=2, image_coverage=0.4))
    assert s.bucket is Bucket.LLM_NEEDED


# ── native text 없음 ──────────────────────────────────────────────────────────

def test_scanned_image_ocr():
    # 텍스트 0 + 큰 이미지(스캔) → OCR
    s = classify(_sig(char_count=0, has_native_text=False, image_count=1, image_coverage=1.0))
    assert s.bucket is Bucket.OCR_NEEDED


def test_vector_outlined_no_image_ocr():
    # 텍스트레이어 0 + 이미지 0 + content-stream 큼(글자 아웃라인/벡터표 = 신탁 유형) → OCR
    s = classify(_sig(char_count=0, has_native_text=False, image_count=0, content_len=50000))
    assert s.bucket is Bucket.OCR_NEEDED


def test_blank_page_skip():
    # 텍스트 0 + 이미지 0 + content 거의 없음 → SKIP
    s = classify(_sig(char_count=0, has_native_text=False, image_count=0, content_len=20))
    assert s.bucket is Bucket.SKIP


# ── fitz 스모크(통합) ─────────────────────────────────────────────────────────

def test_triage_document_digital_text_page():
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "This is a digital text page with enough words to classify.")
    data = doc.tobytes()
    doc.close()

    from parse_service.parsers.pdf.triage import triage_document
    sigs = triage_document(data)
    assert len(sigs) == 1
    assert sigs[0].page_number == 1          # 1-based(page.number+1)
    assert sigs[0].has_native_text is True
    assert sigs[0].bucket is Bucket.TEXT_ONLY


def test_triage_document_vector_page_no_text_is_ocr():
    # 텍스트 없이 벡터(선)만 그린 페이지 → content-stream 존재, native text 0 → OCR
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page()
    for i in range(30):
        page.draw_line((50, 50 + i * 10), (500, 50 + i * 10))
    data = doc.tobytes()
    doc.close()

    from parse_service.parsers.pdf.triage import triage_document
    sigs = triage_document(data)
    assert sigs[0].has_native_text is False
    assert sigs[0].content_len > 0
    assert sigs[0].bucket is Bucket.OCR_NEEDED


def test_triage_document_blank_page_skip():
    import pymupdf
    doc = pymupdf.open()
    doc.new_page()  # 완전 빈 페이지
    data = doc.tobytes()
    doc.close()

    from parse_service.parsers.pdf.triage import triage_document
    sigs = triage_document(data)
    assert sigs[0].bucket is Bucket.SKIP


def test_triage_document_bad_bytes_returns_empty():
    from parse_service.parsers.pdf.triage import triage_document
    assert triage_document(b"not a pdf at all") == []
