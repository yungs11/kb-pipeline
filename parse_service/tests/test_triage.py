"""triage 분류 규칙 — 합성 PageSignals 로 버킷 검증."""
from parse_service.parsers.pdf.triage import PageSignals, Bucket, classify


def _sig(**kw) -> PageSignals:
    s = PageSignals(page_number=0, width=595.0, height=842.0)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def test_blank_page_skip():
    assert classify(_sig(char_count=2, image_coverage=0.0)).bucket is Bucket.SKIP


def test_digital_text_only():
    s = classify(_sig(char_count=1362, word_count=93, has_native_text=True,
                      text_coverage=0.34, image_coverage=0.0))
    assert s.bucket is Bucket.TEXT_ONLY


def test_scanned_simple_ocr():
    # 스캔(이미지 지배 + 글자 극소) + 단순(표 3개) → OCR
    s = classify(_sig(char_count=14, image_coverage=1.0, has_tables=True, table_count=3))
    assert s.bucket is Bucket.OCR_NEEDED


def test_many_tables_llm():
    # 표 >5 → LLM (디지털이어도)
    s = classify(_sig(char_count=1000, has_native_text=True, has_tables=True,
                      table_count=8, image_coverage=0.0))
    assert s.bucket is Bucket.LLM_NEEDED


def test_flowchart_vectors_llm():
    # 순수 벡터 순서도/차트(raster image 없음 → image_coverage=0, native 텍스트 없음) → LLM
    s = classify(_sig(char_count=5, image_coverage=0.0, has_native_text=False,
                      vector_drawing=True, drawing_count=120))
    assert s.bucket is Bucket.LLM_NEEDED


def test_vector_flowchart_not_skipped():
    # 텍스트 극소 + 이미지 0 이지만 벡터 다수 → SKIP 아님 → LLM (v2 SKIP 가드 회귀)
    s = classify(_sig(char_count=3, image_coverage=0.0, drawing_count=90, vector_drawing=True))
    assert s.bucket is Bucket.LLM_NEEDED


def test_digital_table_with_vector_borders_stays_text():
    # 디지털 표(경계선=벡터 다수)지만 native 텍스트 많음 → 벡터 트리거 미발동 → TEXT_ONLY
    s = classify(_sig(char_count=800, word_count=120, has_native_text=True, text_coverage=0.3,
                      drawing_count=200, vector_drawing=True, has_tables=True, table_count=2,
                      image_coverage=0.0))
    assert s.bucket is Bucket.TEXT_ONLY


def test_mixed_content_llm():
    # 이미지 비중 + native 텍스트 혼합 → LLM
    s = classify(_sig(char_count=500, has_native_text=True, image_count=2, image_coverage=0.4))
    assert s.bucket is Bucket.LLM_NEEDED


def test_form_widgets_llm():
    s = classify(_sig(char_count=80, has_native_text=True, has_forms=True, image_coverage=0.0))
    assert s.bucket is Bucket.LLM_NEEDED
