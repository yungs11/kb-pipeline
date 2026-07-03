"""파서 '도구' — 외부 바이너리/라이브러리 래퍼. 파서(parsers/<도메인>)가 호출한다."""


class ToolError(Exception):
    """도구 실행 실패(변환 산출물 없음/CLI 오류). 파서가 ParseError 로 감싼다."""
