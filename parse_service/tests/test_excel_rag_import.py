"""이식된 excel_parser_rag 가 import 되고 backend 팩토리가 동작한다."""


def test_get_backend_importable():
    from parse_service.parsers.excel.excel_parser_rag.backends import get_backend
    b = get_backend("openpyxl")   # node 불필요 백엔드로 임포트/생성만 검증
    assert b is not None
