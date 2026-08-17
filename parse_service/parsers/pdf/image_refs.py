"""이미지 참조(마크다운/HTML) 탐지·제거 — paddle_gw(§A.5)·ODL(§B.3) 공유 헬퍼.

단순 `\\([^)]*\\)` 는 경로에 `)` 가 들어가면(한글 파일명 등) 첫 `)` 에서 끊긴다 —
CommonMark 는 이 경우 destination 을 `<...>` 로 감싼다(실재 패턴:
`![image 1](<doc(우발)_images/imageFile1.png>)`, test_parser_pdf.py:36,47). 이 모듈은
꺾쇠괄호 래핑과 맨몸 두 형태를 모두 처리한다.
"""
from __future__ import annotations

import re

# destination = <...로 감싼 임의 문자열> 또는 <공백/괄호 없는 맨몸 경로>.
_DEST = r"(?:<[^>]*>|[^()\s]*)"
MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(\s*" + _DEST + r"\s*\)")
HTML_IMG_RE = re.compile(r"<img\b[^>]*/?>", re.I)
# http(s):// 로 시작하는 절대 URL 은 실제 이미지일 수 있어 제외 — 우리가 못 서비스하는
# 상대경로(gw/ODL 산출물)만 대상.
_ABS_URL_RE = re.compile(r"^\s*https?://", re.I)


def find_image_refs(md: str) -> list[re.Match]:
    """마크다운 안의 `![]()` 참조 전체(절대 URL 제외) — ODL↔PyMuPDF 개수 매칭용."""
    if not md:
        return []
    return [m for m in MD_IMAGE_RE.finditer(md) if not _ABS_URL_RE.match(image_dest(m.group(0)))]


def image_dest(md_image: str) -> str:
    """`![alt](<dest>)` 또는 `![alt](dest)` 에서 dest 만 뽑는다(꺾쇠괄호 벗김) — ODL 추출
    이미지 dict(§B.1) 조회 키로 쓴다."""
    inner = md_image[md_image.index("(") + 1: md_image.rindex(")")].strip()
    return inner[1:-1] if inner.startswith("<") and inner.endswith(">") else inner


def replace_image_refs(md: str, replacements: list[str]) -> str:
    """`find_image_refs(md)` 순서대로 각 참조를 `replacements[i]` 로 치환한다(빈 문자열=제거).

    개수가 안 맞으면 위치 매칭 불가로 판단해 `ValueError` — 호출부가 안전한 폴백
    (전부 스트립)으로 넘어가야 한다는 신호다.
    """
    refs = find_image_refs(md)
    if len(refs) != len(replacements):
        raise ValueError(f"ref count {len(refs)} != replacement count {len(replacements)}")
    out = []
    last = 0
    for ref, repl in zip(refs, replacements):
        out.append(md[last:ref.start()])
        out.append(repl)
        last = ref.end()
    out.append(md[last:])
    return "".join(out)


def strip_image_refs(md: str) -> str:
    """`![...](...)`/`<img ...>` 참조를 제거한다(절대 URL은 보존). 내용 손실 없이 지운다."""
    if not md:
        return md
    md = MD_IMAGE_RE.sub(lambda m: m.group(0) if _ABS_URL_RE.match(image_dest(m.group(0))) else "", md)
    md = HTML_IMG_RE.sub("", md)
    return md
