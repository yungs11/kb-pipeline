"""바이트 → 문자열 디코딩 사다리. text/html/csv 세 레인이 공유한다.

**순서가 중요하다** — utf-16 을 무조건 앞에 두면 cp949 한국어가 U+FFFD 없이 '성공'해
mojibake 가 임베딩까지 간다(실측: "규정가나".encode("cp949").decode("utf-16") == '풱꓁ꆰꪳ').
그래서 utf-16 은 **BOM 이 있을 때만** 후보에 넣는다.

**UTF-32 를 먼저 가른다.** UTF-32-LE BOM(``ff fe 00 00``)은 UTF-16-LE BOM(``ff fe``)으로
시작해서, 2바이트만 보면 utf-16 으로 '성공'하고 NUL 섞인 mojibake 가 예외 없이 통과한다
(실측: ``"규정".encode("utf-32").decode("utf-16") == '\\x00규\\x00정\\x00'``).
BOM 이 없는 UTF-32 는 판별 근거가 없으므로 조용히 넘기지 않고 :class:`ParserError` 로 끝낸다.

``errors="replace"`` 금지 — U+FFFD 범벅이 '성공한 쓰레기'로 적재된다.
"""
from __future__ import annotations

from parse_service.parsers import ParserError

_UTF32_BOMS = (b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")
_UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")


def _candidates(file_bytes: bytes) -> tuple[str, ...]:
    if file_bytes[:4] in _UTF32_BOMS:
        return ("utf-32", "utf-8-sig", "cp949")
    if file_bytes[:2] in _UTF16_BOMS:
        return ("utf-16", "utf-8-sig", "cp949")
    return ("utf-8-sig", "cp949")


def decode_text(file_bytes: bytes, filename: str) -> str:
    cands = _candidates(file_bytes)
    for enc in cands:
        try:
            return file_bytes.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ParserError(f"decode failed ({'/'.join(cands)}): {filename}")
