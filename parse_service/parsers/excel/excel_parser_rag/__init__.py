"""Excel Parser-RAG — 범용 엑셀 → RAG JSONL 청크 변환기 (SoT 기준 구현)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import ParserConfig
from .textutil import PARSER_VERSION

__version__ = "1.0.0"
__all__ = ["ExcelRagParser", "ParserConfig", "PARSER_VERSION", "__version__"]


class ExcelRagParser:
    """SoT §27.1 Python API.

    >>> parser = ExcelRagParser()
    >>> chunks = parser.parse("./data/sample.xlsx")
    """

    def __init__(self, config: Optional[ParserConfig] = None):
        self.config = config or ParserConfig()
        self.last_stats: Dict[str, Any] = {}

    def parse(self, input_file: str | Path) -> List[Dict[str, Any]]:
        from .pipeline import parse_excel_for_rag

        chunks, stats = parse_excel_for_rag(input_file, self.config)
        self.last_stats = stats
        return chunks

    def parse_with_stats(self, input_file: str | Path) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        from .pipeline import parse_excel_for_rag

        chunks, stats = parse_excel_for_rag(input_file, self.config)
        self.last_stats = stats
        return chunks, stats
