"""OpenDataLoader PDF 도구 — PDF bytes → 페이지별 markdown 리스트.

opendataloader_pdf(JRE 21) 를 subprocess 로 부른다. 문서당 .md 1개가 나오고
``markdown_page_separator`` 로 페이지 앞에 sentinel 이 삽입된다 → split 해 복원.
"""
from __future__ import annotations

import glob
import os
import re
import tempfile

from parse_service.tools import ToolError

#: 콘텐츠에 나타날 일 없는 페이지 sentinel (기존 parsing.py:_PAGE_SEP 그대로).
PAGE_SEP = "<<<ODL_PAGE_BREAK>>>"


def _safe_basename(name: str) -> str:
    base = os.path.basename((name or "").replace("\\", "/")).replace("\x00", "")
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base) or "upload"
    return "_" + base if base.startswith(".") else base


def _odl_convert(*, input_path: str, output_dir: str) -> None:
    import opendataloader_pdf

    opendataloader_pdf.convert(
        input_path=input_path, output_dir=output_dir, format="markdown",
        markdown_with_html=True, markdown_page_separator=PAGE_SEP, quiet=True,
    )


def convert_pdf_to_page_markdowns(file_bytes: bytes, filename: str) -> list[str]:
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, _safe_basename(filename))
        if os.path.commonpath([os.path.realpath(tmp), os.path.realpath(src)]) != os.path.realpath(tmp):
            raise ToolError("unsafe filename")
        with open(src, "wb") as fh:
            fh.write(file_bytes)
        _odl_convert(input_path=src, output_dir=tmp)
        mds = sorted(glob.glob(os.path.join(tmp, "**", "*.md"), recursive=True))
        if not mds:
            raise ToolError(f"opendataloader produced no md for {filename}")
        full = PAGE_SEP.join(
            open(m, encoding="utf-8", errors="replace").read() for m in mds
        )
        md_texts = full.split(PAGE_SEP)
        if len(md_texts) > 1 and not md_texts[0].strip():
            md_texts = md_texts[1:]
        return md_texts
