"""파서 '도구' — 외부 바이너리/라이브러리 래퍼. 파서(parsers/<도메인>)가 호출한다."""


class ToolError(Exception):
    """도구 실행 실패(변환 산출물 없음/CLI 오류). 파서가 ParserError 로 감싼다."""


def safe_basename(name: str) -> str:
    """업로드 파일명을 안전한 basename 으로 정규화(경로 탈출 차단).

    (구 parse_service/parsing.py:_safe_basename — Phase 2d 에서 parsing.py 삭제와 함께 이동.)
    POSIX/Windows 구분자 모두에서 마지막 컴포넌트만 취하고, 널 문자를 제거하며,
    ``[A-Za-z0-9._-]`` 밖의 문자는 ``_`` 로 치환한다.
    """
    import os
    import re

    base = os.path.basename((name or "").replace("\\", "/")).replace("\x00", "")
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base) or "upload"
    if base.startswith("."):
        base = "_" + base
    return base
