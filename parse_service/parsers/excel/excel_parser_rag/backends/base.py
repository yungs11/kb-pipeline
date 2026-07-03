"""Backend 프로토콜."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Protocol, Tuple

from ..config import ParserConfig


class BackendError(Exception):
    """백엔드 설정/입력 오류 (md 부재, 알 수 없는 backend, kordoc 실행 실패 등).

    SystemExit(BaseException) 대신 일반 Exception 이어야 서버 핸들러/잡 워커의
    `except Exception` 에 잡혀 500/failed 로 표면화된다.
    """


class Backend(Protocol):
    def parse(self, input_path: Path, config: ParserConfig) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        ...
