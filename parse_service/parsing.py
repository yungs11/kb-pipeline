"""parse-svc parser adapter (W6 routing): file bytes -> markdown + inline HTML tables.

This module is the parse-svc-owned copy of the parsing logic (moved out of the
facade so the heavy parsing dependencies — java/OpenDataLoader/markitdown/OCR —
are isolated here). Routes by ``kb_pipeline.blockify.recommended_parser(filename)``:
  * "structural"  -> PDF: OpenDataLoader (markdown_with_html); pptx/docx/image/
                     scanned -> OCR/VLM structural service (:18050).
  * "markitdown"  -> markitdown library (xlsx and other simple formats).

The OCR/VLM contract is the one used by excel-parser-markitdown's
``OcrHttpAdapter`` (POST /api/v1/ocr, multipart ``file`` + form ``strategy=hybrid``;
response ``content.{markdown|text}`` with an ``elements[*].content`` fallback).
"""
from __future__ import annotations

from kb_pipeline.blockify import recommended_parser


class ParseError(Exception):
    ...


def _safe_basename(name: str) -> str:
    """Sanitize an upload filename to a safe basename (no path traversal).

    Takes the last path component for both POSIX and Windows separators,
    strips nulls, and replaces anything outside ``[A-Za-z0-9._-]``.
    """
    import os
    import re

    base = os.path.basename((name or "").replace("\\", "/")).replace("\x00", "")
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base) or "upload"
    if base.startswith("."):
        base = "_" + base
    return base


def _route(filename: str) -> str:
    return recommended_parser(filename)  # "structural" | "markitdown"


def _parse_markitdown(file_bytes: bytes, filename: str, **_) -> str:
    from markitdown import MarkItDown  # markitdown lib
    import io
    res = MarkItDown().convert_stream(
        io.BytesIO(file_bytes), file_extension="." + filename.rsplit(".", 1)[-1]
    )
    return res.text_content


def _ocr_markdown(payload: dict, filename: str) -> str:
    """Extract markdown from the OCR/VLM (:18050) response.

    Matches the OcrHttpAdapter contract: prefer whole-doc ``content.markdown``
    (or ``content.text``); when those are empty (common for scanned PDFs that
    only populate per-element content) reconstruct from ``elements[*].content``.
    """
    content = (payload or {}).get("content", {}) or {}
    text = content.get("markdown") or content.get("text")
    if not (text or "").strip():
        elements = (payload or {}).get("elements", []) or []
        parts = []
        for el in elements:
            ec = (el or {}).get("content", {}) or {}
            parts.append(ec.get("markdown") or ec.get("text") or "")
        text = "\n\n".join(p for p in parts if p.strip())
    return text or ""


def _parse_structural(file_bytes: bytes, filename: str, *, ocr_url: str, excel_url: str) -> str:
    # PDF -> OpenDataLoader (markdown_with_html=True); image/scanned/pptx/docx ->
    # OCR/VLM service (:18050). Returns markdown + inline HTML tables.
    import httpx
    ext = filename.rsplit(".", 1)[-1].lower()
    if ext == "pdf":
        import opendataloader_pdf, glob, os, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, _safe_basename(filename))
            # Belt-and-braces: confirm the resolved path stays inside tmp.
            if os.path.commonpath([os.path.realpath(tmp), os.path.realpath(src)]) != os.path.realpath(tmp):
                raise ParseError("unsafe filename")
            with open(src, "wb") as fh:
                fh.write(file_bytes)
            opendataloader_pdf.convert(
                input_path=src, output_dir=tmp, format="markdown",
                markdown_with_html=True, quiet=True,
            )
            mds = sorted(glob.glob(os.path.join(tmp, "**", "*.md"), recursive=True))
            if not mds:
                raise ParseError(f"opendataloader produced no md for {filename}")
            return "\n\n".join(
                open(m, encoding="utf-8", errors="replace").read() for m in mds
            )
    # pptx/docx/image -> OCR/VLM structural service (:18050).
    r = httpx.post(
        f"{ocr_url}/api/v1/ocr",
        files={"file": (_safe_basename(filename), file_bytes)},
        data={"strategy": "hybrid"},
        timeout=600,
    )
    r.raise_for_status()
    md = _ocr_markdown(r.json() or {}, filename)
    if not md.strip():
        raise ParseError(f"ocr/vlm empty for {filename}")
    return md


def parse_to_markdown(file_bytes: bytes, filename: str, *, ocr_url: str, excel_url: str) -> str:
    fn = _parse_structural if _route(filename) == "structural" else _parse_markitdown
    try:
        return fn(file_bytes, filename, ocr_url=ocr_url, excel_url=excel_url)
    except ParseError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ParseError(f"parse failed for {filename}: {e}") from e
