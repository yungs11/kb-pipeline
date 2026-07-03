"""기존 openpyxl region 파이프라인을 Backend 인터페이스로 감싼 어댑터."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Tuple

from ..config import ParserConfig


class OpenpyxlBackend:
    def parse(self, input_path: Path, config: ParserConfig) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        from ..pipeline import parse_excel_for_rag
        return parse_excel_for_rag(input_path, config)
