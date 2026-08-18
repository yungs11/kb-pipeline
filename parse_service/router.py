"""확장자 → 도메인 파서 디스패치. 파싱 로직 없음(얇은 계층).

매핑(2026-08-18): 엑셀→excel(자체청킹, chunk_needed=False), HWP/HWPX/DOCX→kordoc,
이미지→ocr(in-process VL **직접 호출; PDF 변환/triage/GW 미경유**),
html/htm→html(markdownify + `<table>` 보존, 평문→text,
**그 외 전부→pdf**. DOC/PPT/PPTX는 `run_parse`가 먼저 PDF로 변환한다.

DOC/PPT/PPTX는 `run_parse`가 **변환 API로 PDF를 만든 뒤** 여기로 보낸다(app.py). 변환은
router가 하지 않는다 — `route()`의 `filename`은 값 복사라 페이지 이미지 소비처
(`_render_and_upload`)에 전파되지 않기 때문이다. HWP/HWPX/DOCX는 원본 그대로 이
router의 kordoc 도메인으로 들어온다.

kordoc 레인은 4.9.0으로 재도입했다. 출력 Markdown을 그대로 blockify하며 단순 pipe 표는
HTML로 렌더하고, 병합표 inline HTML의 rowspan/colspan은 원문 그대로 보존한다.
"""
from __future__ import annotations

from parse_service.parsers import RouteResult, ParserError
from parse_service.parsers import pdf as _pdf
from parse_service.parsers import ocr as _ocr
from parse_service.parsers import excel as _excel
from parse_service.parsers import html as _html
from parse_service.parsers import docx as _kordoc
from parse_service.tools import fileconvert


def _pdf_parse(fb, fn, *, ocr_url, excel_url):
    return _pdf.parse(fb, fn, ocr_url=ocr_url)


def _ocr_parse(fb, fn, *, ocr_url, excel_url):
    return _ocr.parse(fb, fn, ocr_url=ocr_url)


def _excel_parse(fb, fn, *, ocr_url, excel_url):
    return _excel.parse(fb, fn, excel_url=excel_url)


def _html_parse(fb, fn, **_):
    return _html.parse(fb, fn)


def _kordoc_parse(fb, fn, **_):
    return _kordoc.parse(fb, fn)


def _text_parse(fb, fn, **_):
    """평문 → 단일 페이지 blocks. 변환도 파서도 거치지 않는다."""
    from kb_pipeline.blockify import hybrid_to_blocks
    from parse_service.tools.textdecode import decode_text
    md = decode_text(fb, fn)
    if not md.strip():
        raise ParserError(f"empty text file: {fn}")
    blocks = hybrid_to_blocks(md, page_idx=1)
    # 빈 블록 가드 — 본문이 있는데 블록이 0개면 조용한 빈 적재가 된다. XML 편입으로 이
    # 경로가 실제로 열린다: `<?xml …?><root><item id="1"/></root>` 같은 속성 전용 export 는
    # 텍스트 노드가 없어 blocks=0 인데, run_parse 는 예외 없이 enriched_content="" 로 200 을
    # 돌려주고 facade `/chunk` 에도 빈 본문 가드가 없다. 편입 전에는 `%PDF` 가드가
    # parse_failed 로 크게 죽었으므로, 가드가 없으면 "큰 실패 → 조용한 성공" 으로 실패
    # 유형이 나빠진다. `parsers/html` 도 같은 가드를 갖는다(레인 간 대칭).
    #   부수효과(의도됨): 코드펜스만 있는 .md, 주석만 있는 파일, '---' 만 있는 파일처럼
    #   지금까지 조용히 빈 결과를 내던 입력도 이제 parse_failed 로 크게 실패한다.
    #   (코드펜스가 0 블록인 근본 원인은 blockify 에 fence 분기가 없는 것 — deferred D47.)
    if not blocks:
        raise ParserError(f"no blocks from text file: {fn}")
    return RouteResult(kind="pages", chunk_needed=True,
                       pages=[{"page_number": 1, "blocks": blocks}])


_PARSERS = {"pdf": _pdf_parse, "excel": _excel_parse, "kordoc": _kordoc_parse,
            "ocr": _ocr_parse, "html": _html_parse, "text": _text_parse}


def domain_of(filename: str) -> str:
    """확장자 → 도메인. `run_parse` 도 `%PDF` 가드 대상 판정에 쓴다(공개)."""
    ext = fileconvert.ext_of(filename)          # 정의는 fileconvert 하나뿐
    if ext in _excel.EXCEL_EXTS:                # xlsx xlsm xls — 자체 청킹, 변환 금지
        return "excel"
    if ext in _kordoc.KORDOC_EXTS:              # hwp hwpx docx — kordoc Markdown 직행
        return "kordoc"
    if ext in _ocr.IMAGE_EXTS:                  # png jpg … — PDF/triage/GW 없이 VL 직행
        return "ocr"
    if ext in _html.HTML_EXTS:                  # html htm — 형변환 없이 자체 레인
        return "html"
    if ext in fileconvert.TEXT_EXTS:            # txt md csv json log xml — 변환 불가·불필요
        return "text"
    return "pdf"                                # 변환을 거쳤으므로 여기 오는 건 전부 PDF


_domain = domain_of          # 하위호환(기존 테스트가 이 이름을 쓴다)


def route(file_bytes: bytes, filename: str, *, ocr_url: str, excel_url: str) -> RouteResult:
    return _PARSERS[domain_of(filename)](file_bytes, filename,
                                         ocr_url=ocr_url, excel_url=excel_url)
