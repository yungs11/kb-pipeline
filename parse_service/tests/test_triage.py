"""triage 분류 규칙 — 합성 PageSignals(단위) + fitz 스모크(통합).

결정트리: native text → mixed?LLM:TEXT / no text → content?OCR:SKIP.
"""
from parse_service.parsers.pdf.triage import PageSignals, Bucket, classify, extract_signals


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
    # 새 임계(KBP_TRIAGE_NATIVE_TEXT_MIN_CHARS=100)를 넘도록 여러 줄로 넣는다 —
    # 한 줄이면 페이지 밖으로 잘려 글자수가 임계 아래로 떨어진다.
    page.insert_text((72, 72), ["This is a digital text page with enough words",
                                "to classify it as a native-text page, well above",
                                "the 100-character native text threshold."])
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

# ⚠️ 기본 텍스트는 **`KBP_TRIAGE_NATIVE_TEXT_MIN_CHARS`(기본 100)를 넉넉히 넘어야** 한다.
#    옛 기본값 20 시절 문구(48자)를 그대로 두면 임계를 100 으로 올린 순간 이 픽스처가
#    `has_native_text=False` 가 되어 **디지털 텍스트 페이지 테스트가 스캔 판정으로 뒤집힌다**
#    (2026-08-14 실측: 이 한 줄 때문에 4건이 깨졌다). 임계를 또 올리면 여기도 같이 늘린다.
#    ⚠️ **ASCII 여러 줄로 둔다.** 두 함정이 있다 —
#       (1) `insert_text` 는 줄바꿈을 안 해서 한 줄로 길게 주면 페이지 밖으로 잘린다.
#       (2) pymupdf 기본 폰트(Helvetica)에 **한글 글리프가 없어** 한글은 일부만 추출된다
#           (실측: 한글 3줄 = 78자, 임계 100 미달). 임계 테스트에 한글을 쓰지 말 것.
_DIGITAL_TEXT = [
    "This is a digital text page with plenty of native words",
    "so that the character count comfortably exceeds the",
    "native-text threshold used by triage classification.",
]


def _synth_pdf(draw=None, text=_DIGITAL_TEXT, images=0):
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


# ── 임계치 env 화(2026-08-06, 이미지 파서 고도화 준비) — 미설정 시 회귀 0 + override 확인 ──

def test_env_unset_matches_hardcoded_default():
    """env 미설정이면 classify() 결과가 기존 하드코딩 기본값과 완전히 동일(회귀 앵커)."""
    s = classify(_sig(char_count=500, has_native_text=True, image_count=2, image_coverage=0.24))
    assert s.bucket is Bucket.TEXT_ONLY  # mixed_image_cov=0.25 기본 — 0.24 는 미달


def test_mixed_image_cov_env_override(monkeypatch):
    monkeypatch.setenv("KBP_TRIAGE_MIXED_IMAGE_COV", "0.1")
    s = classify(_sig(char_count=500, has_native_text=True, image_count=2, image_coverage=0.24))
    assert s.bucket is Bucket.LLM_NEEDED  # 낮춘 임계(0.1) 는 0.24 를 충족


def test_diagram_curve_min_env_override(monkeypatch):
    """curve=10 인 페이지는 기본 임계(30) 로는 미검출, KBP_TRIAGE_DIAGRAM_CURVE_MIN=5 로
    낮추면 다이어그램으로 잡힌다."""
    s1 = classify(_sig(char_count=500, has_native_text=True, curve_count=10))
    assert not s1.is_diagram

    monkeypatch.setenv("KBP_TRIAGE_DIAGRAM_CURVE_MIN", "5")
    s2 = classify(_sig(char_count=500, has_native_text=True, curve_count=10))
    assert s2.is_diagram and s2.bucket is Bucket.LLM_NEEDED


def test_content_min_env_override(monkeypatch):
    monkeypatch.setenv("KBP_TRIAGE_CONTENT_MIN", "10000")
    # content_len=50000 은 기본 300 기준으론 OCR_NEEDED(test_vector_outlined_no_image_ocr),
    # 임계를 10000 으로 올려도 여전히 초과라 동일 판정(경계 반대편 검증).
    s = classify(_sig(char_count=0, has_native_text=False, image_count=0, content_len=5000))
    assert s.bucket is Bucket.SKIP  # 5000 < 10000(상향된 임계) → SKIP


def test_native_text_min_chars_env_override(monkeypatch):
    """extract_signals() 의 has_native_text 판정 — classify() 가 아니라 fitz 페이지 필요.

    글자수 15인 페이지는 기본 임계(20) 로 has_native_text=False, env 로 10 으로 낮추면 True.
    """
    import pymupdf
    from parse_service.parsers.pdf.triage import extract_signals

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "0123456789ABCDE")  # 정확히 15자
    try:
        sig_default = extract_signals(page)
        assert sig_default.char_count == 15
        assert sig_default.has_native_text is False  # 15 <= 20(기본)

        monkeypatch.setenv("KBP_TRIAGE_NATIVE_TEXT_MIN_CHARS", "10")
        sig_override = extract_signals(page)
        assert sig_override.has_native_text is True  # 15 > 10(낮춘 임계)
    finally:
        doc.close()


# ── 가로형 페이지 → LLM_NEEDED(2026-08-06, "가로형이면 묻고 따질 것 없이 VL") ──────────────

def test_extract_signals_derives_is_landscape():
    """실제 fitz 페이지의 width>height 비교로 is_landscape 가 파생되는지(_sig() 직접 구성이
    아니라 extract_signals() 자체를 거쳐야 검증되는 파생 로직)."""
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page(width=800, height=600)
    page.insert_text((50, 50), "가로형 슬라이드", fontsize=11)
    try:
        sig = extract_signals(page)
        assert sig.is_landscape is True
    finally:
        doc.close()

    # 세로형(기존 _synth_pdf 기본 width=600,height=800) 은 False 로 남아야 함(회귀 앵커).
    portrait = _synth_pdf(text="세로형 문서")
    assert _first_sig(portrait).is_landscape is False


def test_landscape_scanned_page_is_llm_needed():
    """가로형 + native text 없음(스캔 슬라이드) → LLM_NEEDED(OCR_NEEDED 아님)."""
    s = classify(_sig(char_count=0, has_native_text=False, image_count=1,
                      image_coverage=1.0, is_landscape=True))
    assert s.bucket is Bucket.LLM_NEEDED


def test_landscape_with_real_diagram_keeps_is_diagram_true():
    """가로형 + native text 있음 + 진짜 diagram 신호(curve 多) → LLM_NEEDED 이고
    is_diagram 은 True 로 보존돼야 한다(diagram 우선순위가 landscape 보다 높음 — v1 이
    낸 3-렌즈 수렴 결함 재발 방지 앵커: diagram_pages 집계에서 빠지면 안 됨)."""
    s = classify(_sig(char_count=500, has_native_text=True, curve_count=40,
                      is_landscape=True))
    assert s.bucket is Bucket.LLM_NEEDED
    assert s.is_diagram is True


def test_landscape_without_diagram_or_mixed_is_llm_needed_not_diagram():
    """가로형 + native text 있음 + diagram/mixed 신호 둘 다 없음 → LLM_NEEDED(가로형
    단독 사유), is_diagram 은 False."""
    s = classify(_sig(char_count=500, has_native_text=True, word_count=80,
                      is_landscape=True))
    assert s.bucket is Bucket.LLM_NEEDED
    assert s.is_diagram is False
    assert "가로형" in s.reason


def test_portrait_page_unaffected_by_landscape_rule():
    """세로형(is_landscape=False) 페이지는 기존 판정 불변(회귀 앵커)."""
    s = classify(_sig(char_count=500, has_native_text=True, word_count=80,
                      is_landscape=False))
    assert s.bucket is Bucket.TEXT_ONLY


def test_landscape_to_llm_toggle_off(monkeypatch):
    """KBP_TRIAGE_LANDSCAPE_TO_LLM=0 → 가로형이어도 기존 로직(디지털 텍스트면 TEXT_ONLY)
    으로 정상 판정(끄기 스위치 확인)."""
    monkeypatch.setenv("KBP_TRIAGE_LANDSCAPE_TO_LLM", "0")
    s = classify(_sig(char_count=500, has_native_text=True, word_count=80,
                      is_landscape=True))
    assert s.bucket is Bucket.TEXT_ONLY


# ── 네이티브 텍스트 임계 (KBP_TRIAGE_NATIVE_TEXT_MIN_CHARS) ──────────────────
def test_native_text_threshold_default_is_100(monkeypatch):
    """기본값 100 을 고정한다 — 20 에서 올린 값이다(2026-08-14).

    **왜 올렸나**: 스캔 페이지에 머리말·쪽번호·워터마크 같은 자투리 네이티브 텍스트가
    20자를 넘는 일이 흔해, 스캔인데 `has_native_text=True` 가 되어 TEXT_ONLY→odl 로
    잘못 라우팅됐다. 450페이지 격자탐색 실측: **ODL 오라우팅 20쪽 → 4쪽**.
    """
    monkeypatch.delenv("KBP_TRIAGE_NATIVE_TEXT_MIN_CHARS", raising=False)
    # 48자(옛 기본값 20 은 넘고 새 기본값 100 은 못 넘는다) → 스캔으로 본다
    s = _first_sig(_synth_pdf(text="테스트 본문 텍스트입니다. 다이어그램 신호 검증용 문장."))
    assert s.char_count < 100
    assert s.has_native_text is False, "48자는 자투리다 — 디지털 텍스트로 보면 안 된다"
    # 임계를 넘는 본문 → 디지털
    assert _first_sig(_synth_pdf()).has_native_text is True


def test_native_text_threshold_env_override(monkeypatch):
    """env 로 되돌릴 수 있다 — 임계가 현장 문서와 안 맞을 때의 탈출구."""
    monkeypatch.setenv("KBP_TRIAGE_NATIVE_TEXT_MIN_CHARS", "20")
    s = _first_sig(_synth_pdf(text="테스트 본문 텍스트입니다. 다이어그램 신호 검증용 문장."))
    assert s.has_native_text is True, "20 으로 낮추면 옛 동작"


def test_native_text_threshold_empty_env_falls_back_to_default(monkeypatch):
    """빈 값은 **기본값으로 되돌아간다** — `or` 를 쓰는 이유.

    `os.environ.get(k, "100")` 이었다면 빈 문자열이 그대로 와서 `int("")` → ValueError 로
    문서 전체가 죽는다. 폐쇄망 compose 에서 빈 값이 새는 유형이 실재한다.
    """
    monkeypatch.setenv("KBP_TRIAGE_NATIVE_TEXT_MIN_CHARS", "")
    assert _first_sig(_synth_pdf()).has_native_text is True   # 예외 없이 기본값 100
