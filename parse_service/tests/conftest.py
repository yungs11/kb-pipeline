"""parse_service 테스트 공통 설정.

`MODEL_NAME` 을 **모듈 최상단에서** 채운다(fixture 아님). `vl_api` 가 미설정 시
RuntimeError 를 던지도록 바뀌었는데, `test_app_chunk_needed.py` 처럼 모듈 최상단에서
`import parse_service.app` 하는 테스트가 있어 **collection 단계**에 걸린다 —
autouse fixture 는 그때 이미 늦다. conftest 는 테스트 모듈 import 보다 먼저 로드된다.

`setdefault` 라서 실행 환경에 값이 있으면 건드리지 않는다. "미설정이면 실패한다"는
계약 자체는 `test_vl_api.py` 가 `monkeypatch.delenv` 로 국소 재현해 검증한다.
"""
import os

os.environ.setdefault("MODEL_NAME", "test-vl")
