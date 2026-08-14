"""html/htm 도메인 파서 — 형변환 API 없이 "markdown + inline <table> HTML" 을 만든다.

**표를 먼저 빼낸다.** markdownify(그리고 그것을 감싼 markitdown)는 표를 GFM pipe 표로
평탄화하는데, 병합셀이 있으면 열 정렬 자체가 깨진다(실측: rowspan/colspan 표가
헤더 3열 · 데이터행 2셀로 나온다). 리포 불변식은 "표는 <table> HTML 보존"이므로
파싱 **전에** 원문 <table> 을 문자열로 보관하고 자리에 sentinel 을 심은 뒤,
markdown 변환이 끝난 다음 되돌린다.

**변환 대상 노드를 고르지 않는다 — soup 전체를 쓴다.** `<body>` 로 스코핑하려던 초안은
세 번 연속 결함을 냈다: ① body 밖 <table> 소실(bs4 는 브라우저와 달리 body 밖 노드를
재부모화하지 않는다), ② `</head>` 생략 시 head 제거가 본문 전멸, ③ 빈 <head> 때문에
"body 밖에 내용 있음" 판정이 상시 참이 되어 XHTML prolog 누출. 전부 **분기 자체**에서
나왔다. 분기를 없애고 "본문 아닌 노드를 떼어낸다"로 바꾸면 셋 다 사라진다.

markitdown 은 도입하지 않는다 — Phase 2d(`a8f9818`)에서 같은 병합 손실 사유로 제거됐고
재유입 가드 `tests/test_no_markitdown.py` 가 있다. 설계문서
`docs/superpowers/specs/2026-08-11-markup-lane-design.md` §2 참조.
"""
from __future__ import annotations

import re
import uuid

from bs4 import BeautifulSoup, NavigableString
from bs4.element import CData, Comment, Declaration, Doctype, ProcessingInstruction
from markdownify import MarkdownConverter

from parse_service.parsers import RouteResult, ParserError
from parse_service.tools.textdecode import decode_text

#: 이 레인이 받는 확장자. **여기가 유일한 정의다** — router 가 import 해 쓴다.
HTML_EXTS = {"html", "htm"}

#: 표 **내부**의 빈 줄을 접는다. markdown-it 의 html_block 은 빈 줄에서 끝나므로
#: (CommonMark type 6), 표 안에 빈 줄이 남으면 table_body 가 거기서 잘리고 나머지
#: 마크업이 raw 텍스트 블록으로 샌다(실측). bs4 는 텍스트 노드 안의 \n\n 을 접지 않는다.
#:
#: **허용된 손실**: 셀 안 <pre>/<code> 의 의미 있는 빈 줄도 함께 접힌다
#: (실측: <pre>a\n\nb</pre> → <pre>a\nb</pre>). 접지 않으면 표 블록 자체가 깨지므로
#: (표 전체 손실 vs 빈 줄 하나 손실) 접는 쪽을 택했다.
_BLANK_LINES = re.compile(r"\n[ \t]*\n+")

#: 본문이 아닌 태그. **`<head>` 를 통째로 지우면 안 된다** — `</head>` 가 생략된 html
#: (HTML5 가 허용하고 실무에서 흔하다)에서 bs4 는 브라우저와 달리 head 를 자동으로 닫지
#: 않고 `<body>` 를 head 자식으로 중첩시킨다. 그 상태에서 head 를 extract 하면 본문·표가
#: 통째로 사라진다(실측: `<html></html>` 만 남는다). 그래서 head **안의 태그**만 지운다.
#: noscript/template/iframe 은 브라우저가 렌더하지 않는 텍스트라 본문 노이즈가 된다.
_DROP_TAGS = ("script", "style", "title", "meta", "link", "base",
              "noscript", "template", "iframe")

#: 본문이 아닌 문자열 노드. XHTML prolog(`<?xml version="1.0"?>`)는
#: ProcessingInstruction 인데 markdownify 가 **텍스트로 렌더한다** — 지우지 않으면
#: `xml version="1.0" encoding="UTF-8"?` 가 본문 블록으로 적재된다(실측).
#: Comment/Doctype 은 markdownify 가 건너뛰지만, 판정을 한곳에 모아 둔다.
_DROP_STRINGS = (CData, Comment, Declaration, Doctype, ProcessingInstruction)


def _strip_non_content(soup) -> None:
    for tag in soup(_DROP_TAGS):
        tag.extract()
    for node in soup.find_all(string=lambda s: isinstance(s, _DROP_STRINGS)):
        node.extract()


def _strip_data_uri_images(target) -> None:
    """data-URI <img> 를 alt 텍스트로 바꾼다.

    blockify 는 이미지를 ``{"type": "image", "img_path": <src 원문>}`` 으로 만들고
    modal 은 그 값을 payload 본문으로 쓴다(`kb_pipeline/modal.py:586`). data-URI 면
    수 MB base64 가 enriched_content → 청킹 → 임베딩까지 흐르는데, html 은
    `IMAGE_EXTS` 가 아니라 MinIO 업로드도 없어 **참조 불가능한 거대 문자열**만 남는다.
    표 추출보다 **먼저** 돌려야 표 안의 data-URI 도 걸린다.
    """
    for img in target.find_all("img"):
        if (img.get("src") or "").strip().lower().startswith("data:"):
            img.replace_with(NavigableString(img.get("alt") or ""))


def _extract_tables(target, sentinel_fmt: str) -> list[str]:
    """최상위 <table> 을 원문 문자열로 걷어내고 자리에 sentinel 을 심는다.

    중첩 표는 건너뛴다 — 바깥 표 문자열에 통째로 들어가므로, 따로 뽑으면 같은 표가
    블록 둘로 중복된다. ``find_all`` 이 돌려주는 리스트는 정적이라 순회 중 교체해도
    안전하고, 바깥 표를 교체해도 안쪽 표의 부모 사슬은 그대로라 ``find_parent`` 판정이
    유지된다.
    """
    tables: list[str] = []
    for t in target.find_all("table"):
        if t.find_parent("table") is not None:
            continue
        tables.append(_BLANK_LINES.sub("\n", str(t)))
        t.replace_with(NavigableString(sentinel_fmt.format(len(tables) - 1)))
    return tables


def parse(file_bytes: bytes, filename: str, *, ocr_url: str | None = None) -> RouteResult:
    """html/htm → 단일 페이지 blocks. ocr_url 은 router 계약상 받되 쓰지 않는다."""
    text = decode_text(file_bytes, filename)
    soup = BeautifulSoup(text, "html.parser")
    _strip_non_content(soup)
    _strip_data_uri_images(soup)

    # sentinel 은 호출마다 유일해야 한다. 고정 문자열이면 본문에 같은 글자가 있을 때
    # 그 텍스트가 표로 치환돼 표가 중복되고 문단이 조각난다(실측). 대문자 영숫자만
    # 쓰는 이유는 markdownify 이스케이프(`_`, `*`, `[` 앞 백슬래시)를 피하기 위해서다.
    nonce = uuid.uuid4().hex[:12].upper()
    sentinel_fmt = f"KBPTBL{nonce}" + "{}ENDX"
    sentinel_re = re.compile(f"KBPTBL{nonce}" + r"(\d+)ENDX")

    tables = _extract_tables(soup, sentinel_fmt)
    md = MarkdownConverter(heading_style="ATX").convert_soup(soup)

    def _restore(m: "re.Match[str]") -> str:
        idx = int(m.group(1))
        if idx >= len(tables):
            return m.group(0)
        # 앞뒤 빈 줄이 있어야 markdown-it 이 html_block 으로 연다(문단 안 inline HTML 이
        # 아니라). 들여쓰기 없이 열 0 에서 시작해야 한다 — 4칸 들여쓰면 코드블록이 된다.
        return "\n\n" + tables[idx] + "\n\n"

    md = sentinel_re.sub(_restore, md)
    if not md.strip():
        raise ParserError(f"empty html: {filename}")
    from kb_pipeline.blockify import hybrid_to_blocks
    blocks = hybrid_to_blocks(md, page_idx=1)
    if not blocks:
        raise ParserError(f"no blocks from html: {filename}")
    return RouteResult(kind="pages", chunk_needed=True,
                       pages=[{"page_number": 1, "blocks": blocks}])
