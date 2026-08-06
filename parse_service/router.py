"""확장자 → 도메인 파서 디스패치. 파싱 로직 없음(얇은 계층).

매핑(2026-08-06): 엑셀→excel(자체청킹, chunk_needed=False), 이미지→ocr(in-process VL),
평문→text(그대로 블록화), **그 외 전부→pdf**.

비-PDF 는 `run_parse` 가 **변환 API 로 PDF 를 만든 뒤** 여기로 보낸다(app.py). 변환은
router 가 하지 않는다 — `route()` 의 `filename` 은 값 복사라 페이지 이미지 소비처
(`_render_and_upload`)에 전파되지 않기 때문이다.

kordoc(docx) 레인은 제거됐다 — hwp 정관 실측(2026-08-06)에서 55개 헤딩이 전부 같은 레벨로
나와 장·조 계층이 사라졌다. 변환 API → ODL 은 같은 문서에서 계층을 만든다.
"""
from __future__ import annotations

from parse_service.parsers import RouteResult, ParserError
from parse_service.parsers import pdf as _pdf
from parse_service.parsers import ocr as _ocr
from parse_service.parsers import excel as _excel
from parse_service.tools import fileconvert


def _pdf_parse(fb, fn, *, ocr_url, excel_url):
    return _pdf.parse(fb, fn, ocr_url=ocr_url)


def _ocr_parse(fb, fn, *, ocr_url, excel_url):
    return _ocr.parse(fb, fn, ocr_url=ocr_url)


def _excel_parse(fb, fn, *, ocr_url, excel_url):
    return _excel.parse(fb, fn, excel_url=excel_url)


def _text_parse(fb, fn, **_):
    """평문 → 단일 페이지 blocks. 변환도 파서도 거치지 않는다."""
    from kb_pipeline.blockify import hybrid_to_blocks
    # BOM 이 있을 때만 utf-16 을 시도한다. **순서가 중요하다** — utf-16 을 무조건 앞에 두면
    # cp949 한국어가 U+FFFD 없이 '성공'해 mojibake 가 임베딩까지 간다.
    #   실측: "규정가나".encode("cp949").decode("utf-16") == '풱꓁ꆰꪳ'
    cands = (("utf-16",) if fb[:2] in (b"\xff\xfe", b"\xfe\xff") else ()) + ("utf-8-sig", "cp949")
    for enc in cands:
        try:
            md = fb.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        # errors="replace" 금지 — U+FFFD 범벅이 '성공한 쓰레기'로 임베딩까지 간다.
        raise ParserError(f"decode failed ({'/'.join(cands)}): {fn}")
    if not md.strip():
        raise ParserError(f"empty text file: {fn}")
    return RouteResult(kind="pages", chunk_needed=True,
                       pages=[{"page_number": 1,
                               "blocks": hybrid_to_blocks(md, page_idx=1)}])


_PARSERS = {"pdf": _pdf_parse, "excel": _excel_parse,
            "ocr": _ocr_parse, "text": _text_parse}


def domain_of(filename: str) -> str:
    """확장자 → 도메인. `run_parse` 도 `%PDF` 가드 대상 판정에 쓴다(공개)."""
    ext = fileconvert.ext_of(filename)          # 정의는 fileconvert 하나뿐
    if ext in _excel.EXCEL_EXTS:                # xlsx xlsm xls — 자체 청킹, 변환 금지
        return "excel"
    if ext in _ocr.IMAGE_EXTS:                  # png jpg … — 이미지 직행
        return "ocr"
    if ext in fileconvert.TEXT_EXTS:            # txt md csv json — 변환 불가·불필요
        return "text"
    return "pdf"                                # 변환을 거쳤으므로 여기 오는 건 전부 PDF


_domain = domain_of          # 하위호환(기존 테스트가 이 이름을 쓴다)


def route(file_bytes: bytes, filename: str, *, ocr_url: str, excel_url: str) -> RouteResult:
    return _PARSERS[domain_of(filename)](file_bytes, filename,
                                         ocr_url=ocr_url, excel_url=excel_url)
