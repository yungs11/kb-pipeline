"""파서 백엔드 선택 (kordoc 통합설계 §4).

get_backend(name).parse(input_path, config) -> (chunks: list[dict], stats: dict)
"""
from __future__ import annotations

from .base import Backend, BackendError


def get_backend(name: str) -> Backend:
    name = (name or "kordoc").lower()
    if name == "auto":
        from .auto_backend import AutoBackend
        return AutoBackend()
    if name == "openpyxl":
        from .openpyxl_backend import OpenpyxlBackend
        return OpenpyxlBackend()
    if name == "kordoc":
        from .kordoc_backend import KordocBackend
        return KordocBackend()
    raise BackendError(f"알 수 없는 backend: {name!r} (kordoc | openpyxl)")
