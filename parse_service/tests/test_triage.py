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


# ---- 다이어그램 신호 (2026-07-14: curve/line/img 합성 — native-text 페이지 한정) ----

def _synth_pdf(draw=None, text="테스트 본문 텍스트입니다. 다이어그램 신호 검증용 문장.", images=0):
    """합성 1페이지 PDF bytes — text(네이티브) + draw(page) 콜백 + 작은 이미지 n개."""
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    if text:
        page.insert_text((50, 50), text, fontsize=11)
    if draw:
        draw(page)
    if images:
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 40))
        pix.clear_with(90)
        for i in range(images):
            x = 60 + (i % 5) * 100
            y = 200 + (i // 5) * 100
            page.insert_image(pymupdf.Rect(x, y, x + 90, y + 90), pixmap=pix)
    data = doc.tobytes()
    doc.close()
    return data


def _first_sig(pdf_bytes):
    from parse_service.parsers.pdf.triage import triage_document
    sigs = triage_document(pdf_bytes)
    assert len(sigs) == 1
    return sigs[0]


def test_diagram_by_curves():
    """곡선 커넥터형 순서도(정의서 p5 패턴: curve=144) — curve≥30 검출."""
    def draw(page):
        import pymupdf
        for i in range(40):
            y = 100 + i * 5
            page.draw_bezier((50, y), (150, y + 40), (250, y - 40), (350, y))
    s = _first_sig(_synth_pdf(draw=draw))
    assert s.curve_count >= 30 and s.is_diagram
    assert s.bucket == Bucket.LLM_NEEDED


def test_diagram_by_lines_shapes_and_curves():
    """직선화살표+도형이미지 복합형 PPT 순서도(소유권 p4: line=148·img=11·curve=12) —
    line≥100 AND img≥5 AND curve≥10 동시 충족 시 검출."""
    def draw(page):
        for i in range(120):
            y = 100 + i * 4
            page.draw_line((50, y), (350, y))
        for i in range(12):
            y = 120 + i * 40
            page.draw_bezier((400, y), (430, y + 15), (460, y - 15), (490, y))
    s = _first_sig(_synth_pdf(draw=draw, images=8))
    assert s.is_diagram and s.bucket == Bucket.LLM_NEEDED


def test_line_heavy_table_not_diagram():
    """대형 테두리 표(약관 p275 실측: line=1249·img=8·curve=8) — curve 부족으로 미검출.
    line 단독 규칙이 표를 순서도로 오검하던 회귀 고정."""
    def draw(page):
        for i in range(150):
            y = 60 + i * 4
            page.draw_line((50, y), (350, y))
        for i in range(8):
            y = 100 + i * 60
            page.draw_bezier((400, y), (420, y + 10), (440, y - 10), (460, y))
    s = _first_sig(_synth_pdf(draw=draw, images=6))
    assert not s.is_diagram, "표(line 多·curve 少)를 다이어그램으로 오검하면 안 됨"


def test_icon_page_not_diagram():
    """아이콘/QR 다수 텍스트 페이지(약관 p12 실측: img=11·line=9·curve=4) —
    img 단독으론 미검출(마스코트/QR 페이지 오검 회귀 고정)."""
    s = _first_sig(_synth_pdf(images=8))
    assert not s.is_diagram, "아이콘/QR 페이지를 다이어그램으로 오검하면 안 됨"


def test_text_page_with_boxes_not_diagram():
    """박스 테두리 있는 텍스트 페이지(소유권 p3 패턴: line=53) — 오검 금지."""
    def draw(page):
        for i in range(12):
            y = 100 + i * 20
            page.draw_line((50, y), (350, y))
    s = _first_sig(_synth_pdf(draw=draw))
    assert not s.is_diagram
    assert s.bucket == Bucket.TEXT_ONLY


def test_textless_vector_page_skips_drawing_scan():
    """텍스트 없는 벡터 페이지(아웃라인 신탁, 3.2만 curve 병적 케이스) —
    native text 가드로 get_cdrawings 자체를 안 돌린다(curve_count=0 유지, OCR_NEEDED)."""
    def draw(page):
        for i in range(40):
            y = 100 + i * 5
            page.draw_bezier((50, y), (150, y + 40), (250, y - 40), (350, y))
    s = _first_sig(_synth_pdf(draw=draw, text=""))
    assert s.curve_count == 0 and s.line_count == 0   # 스캔 안 함
    assert not s.is_diagram
    assert s.bucket == Bucket.OCR_NEEDED
