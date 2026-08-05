"""파서 '도구' — 외부 바이너리/라이브러리 래퍼. 파서(parsers/<도메인>)가 호출한다."""


class ToolError(Exception):
    """도구 실행 실패(변환 산출물 없음/CLI 오류). 파서가 ParserError 로 감싼다."""


def safe_basename(name: str) -> str:
    """업로드 파일명을 안전한 basename 으로 정규화(경로 탈출 차단).

    (구 parse_service/parsing.py:_safe_basename — Phase 2d 에서 parsing.py 삭제와 함께 이동.)
    POSIX/Windows 구분자 모두에서 마지막 컴포넌트만 취하고, 널 문자를 제거하며,
    제어문자만 ``_`` 로 치환한다. 한글·공백·괄호 등 정상 유니코드 파일명은 문서
    제목/메타데이터로 보존해야 하므로 제거하지 않는다. 실제 외부 도구용 임시 경로는
    각 tool adapter가 별도로 더 엄격하게 정규화한다.
    """
    import os
    import unicodedata

    base = os.path.basename((name or "").replace("\\", "/")).replace("\x00", "")
    base = "".join(
        "_" if unicodedata.category(char).startswith("C") else char
        for char in base
    ) or "upload"
    if base in {".", ".."}:
        base = "upload"
    if base.startswith("."):
        base = "_" + base
    return base
