"""PDF 문서수준 게이트 — triage 버킷 집계로 ODL/MinerU 라우팅 + parse_method 결정."""
import pytest
from parse_service.parsers.pdf import gate
from parse_service.parsers.pdf.triage import PageSignals, Bucket


def _sig(bucket):
    s = PageSignals(page_number=1, width=600, height=800)
    s.bucket = bucket
    return s


@pytest.mark.parametrize("buckets,lane,method", [
    ([Bucket.TEXT_ONLY, Bucket.TEXT_ONLY], "odl", None),       # 전부 텍스트
    ([Bucket.TEXT_ONLY, Bucket.SKIP], "odl", None),            # 텍스트+빈페이지
    ([Bucket.SKIP], "odl", None),                              # 전부 빈페이지
    ([], "odl", None),                                         # 열기 실패(빈 리스트)
    ([Bucket.OCR_NEEDED, Bucket.OCR_NEEDED], "mineru", "ocr"), # 순수 스캔
    ([Bucket.OCR_NEEDED, Bucket.SKIP], "mineru", "ocr"),       # 스캔+빈페이지
    ([Bucket.TEXT_ONLY, Bucket.OCR_NEEDED], "mineru", "ocr"),  # 혼합(텍스트+스캔)=ocr 강제(유실 방지)
    ([Bucket.OCR_NEEDED, Bucket.LLM_NEEDED], "mineru", "ocr"), # 스캔 포함=ocr 강제
    ([Bucket.LLM_NEEDED], "mineru", "auto"),                   # 스캔 없는 텍스트+이미지=auto 안전
    ([Bucket.TEXT_ONLY, Bucket.LLM_NEEDED], "mineru", "auto"), # 스캔 없는 혼합=auto
])
def test_decide_route(monkeypatch, buckets, lane, method):
    monkeypatch.setattr(gate, "triage_document", lambda b: [_sig(x) for x in buckets])
    d = gate.decide_route(b"%PDF")
    assert (d.lane, d.parse_method) == (lane, method)


def test_triage_exception_falls_back_to_odl(monkeypatch):
    def boom(b):
        raise RuntimeError("corrupt page iteration")
    monkeypatch.setattr(gate, "triage_document", boom)
    d = gate.decide_route(b"%PDF")
    assert (d.lane, d.parse_method) == ("odl", None)
