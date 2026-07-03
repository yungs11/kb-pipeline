"""kordoc CLI 도구 — docx(네이티브)/폴백 포맷 → markdown(+<table> HTML).

호출 계약(참조 구현: excel-parser-markitdown/compare/adapters/kordoc_adapter.py):
    kordoc <src> --output <out.md> --format markdown
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile

from parse_service.tools import ToolError


def _safe_basename(name: str) -> str:
    base = os.path.basename((name or "").replace("\\", "/")).replace("\x00", "")
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base) or "upload"
    return "_" + base if base.startswith(".") else base


def convert_to_markdown(file_bytes: bytes, filename: str, *, timeout: float = 600.0) -> str:
    binp = os.environ.get("KORDOC_BIN", "kordoc")
    if not (shutil.which(binp) or os.path.exists(binp)):
        raise ToolError(f"kordoc binary not found: {binp}")
    with tempfile.TemporaryDirectory(prefix="kordoc_") as tmp:
        src = os.path.join(tmp, _safe_basename(filename))
        with open(src, "wb") as fh:
            fh.write(file_bytes)
        out = os.path.join(tmp, "out.md")
        p = subprocess.run([binp, src, "--output", out, "--format", "markdown"],
                           capture_output=True, text=True, timeout=timeout)
        if not os.path.exists(out):
            msg = (p.stderr or p.stdout or "kordoc produced no output").strip()
            raise ToolError(msg[:600])
        with open(out, encoding="utf-8", errors="replace") as fh:
            return fh.read()
