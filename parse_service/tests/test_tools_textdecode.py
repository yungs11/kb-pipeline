import pytest

from parse_service.parsers import ParserError
from parse_service.tools.textdecode import decode_text


def test_utf8_plain():
    assert decode_text("규정 가나".encode("utf-8"), "a.txt") == "규정 가나"


def test_utf8_sig_strips_bom():
    assert decode_text("규정".encode("utf-8-sig"), "a.txt") == "규정"


def test_cp949_roundtrip():
    assert decode_text("규정가나".encode("cp949"), "a.csv") == "규정가나"


def test_utf16_only_with_bom():
    """BOM 이 있을 때만 utf-16 을 시도한다.

    무조건 utf-16 을 앞에 두면 cp949 한국어가 U+FFFD 없이 '성공'해 mojibake 가
    임베딩까지 간다: "규정가나".encode("cp949").decode("utf-16") == '풱꓁ꆰꪳ'
    """
    assert decode_text("규정".encode("utf-16"), "a.txt") == "규정"
    assert decode_text("규정가나".encode("cp949"), "a.txt") != "풱꓁ꆰꪳ"


def test_utf32_with_bom_not_silently_mojibake():
    """UTF-32-LE BOM(ff fe 00 00)은 utf-16 BOM(ff fe)으로 시작한다.

    2바이트만 보고 utf-16 을 태우면 예외 없이 NUL 섞인 mojibake 가 나온다
    (실측: "규정".encode("utf-32").decode("utf-16") == '\\x00규\\x00정\\x00').
    errors="replace" 를 금지한 것과 같은 실패 유형이라 같은 강도로 막는다.

    **탐지 대상은 BOM 이 있는 UTF-32 뿐이다** — "utf-32" 코덱은 BOM 을 붙이지만
    "utf-32-be"/"utf-32-le" 는 붙이지 않는다. BOM 없는 UTF-32 는 아래 테스트에서
    '조용한 mojibake 대신 명시적 실패' 로 끝나는 것을 계약으로 한다.
    """
    import codecs

    assert decode_text("규정".encode("utf-32"), "a.txt") == "규정"          # BOM 포함(LE)
    assert decode_text(codecs.BOM_UTF32_BE + "규정".encode("utf-32-be"),
                       "a.txt") == "규정"


def test_bomless_utf32_fails_loudly():
    """BOM 없는 UTF-32 는 판별 근거가 없다. 조용한 쓰레기 대신 ParserError 로 끝난다.

    실측: "규정".encode("utf-32-be") == b'\\x00\\x00\\xad\\xdc…' — 4바이트 BOM 판정에
    걸리지 않고 utf-8-sig·cp949 도 모두 UnicodeDecodeError 다.
    """
    with pytest.raises(ParserError):
        decode_text("규정".encode("utf-32-be"), "a.txt")


def test_undecodable_raises_parser_error():
    with pytest.raises(ParserError):
        decode_text(b"\xff\xfe\x00", "a.txt")
