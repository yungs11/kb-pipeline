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

import logging

from kb_pipeline.blockify import recommended_parser

log = logging.getLogger("kb_pipeline.parse_service.parsing")

#: 한 페이지가 비었는지(스캔 페이지) 판정하는 공백 제거 후 최소 길이. OpenDataLoader 가
#: 디지털 페이지에 거의 빈 .md 만 남기는 경우를 스캔으로 오인하지 않도록 매우 보수적으로
#: 1 로 둔다(비공백 문자가 하나라도 있으면 디지털로 취급).
_DIGITAL_MIN_CHARS = 1

#: OCR 로 라우팅하는 이미지 확장자(``recommended_parser`` 는 .png/.jpg 를 markitdown 으로
#: 보내므로 페이지 보존 경로에서는 명시적으로 OCR 로 돌린다 — spec §4 단일 이미지).
_IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff", "webp"}


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


# ---------------------------------------------------------------------------
# 페이지 보존 파서 (spec §5.1.3) — 평탄화하지 않는 새 경로
# ---------------------------------------------------------------------------
#
# ``parse_to_markdown`` 은 페이지 .md 를 join 후 1회 blockify 한다(하위호환, 불변).
# ``parse_to_pages`` 는 페이지 경계를 평탄화 **전에** 보존한다:
#   * 디지털 PDF: OpenDataLoader convert 1회 → 페이지별 *.md(sorted glob, join 안 함)
#     → ``hybrid_to_blocks(md, page_idx=page_number)`` (page_idx 1-based 채움).
#   * 스캔 PDF / 단일 이미지: OCR(:18050) raw ``elements[]`` 보존
#     → ``elements_to_blocks(elements)`` (page_idx 보존) → page 별로 묶음.
#
# PageDoc 계약(spec §5.1.3):
#     PageDoc = {"page_number": int(1-based), "blocks": list[dict]}  # blocks 는 page_idx 채워짐


def _ocr_elements(payload: dict) -> list[dict]:
    """OCR(:18050) 응답에서 raw ``elements[]`` 를 보존해 반환한다.

    ``_ocr_markdown`` 은 markdown 문자열만 뽑고 elements 를 버린다 → 페이지 보존이 불가.
    ``parse_to_pages`` 는 page_idx 를 살리려고 elements 를 그대로 ``elements_to_blocks`` 에
    넘긴다. 응답에 elements 가 없으면 빈 리스트(호출자가 비치명 처리).
    """
    return list((payload or {}).get("elements", []) or [])


def _ocr_page(image_bytes: bytes, filename: str, *, ocr_url: str) -> list[dict]:
    """이미지(또는 렌더된 PDF 페이지) 1장을 OCR(:18050)에 POST → raw ``elements[]``.

    contract 은 ``_parse_structural`` 과 동일(POST /api/v1/ocr, multipart ``file`` +
    form ``strategy=hybrid``). 네트워크/HTTP 오류는 호출자에게 전파(상위에서 페이지 단위
    graceful 처리).
    """
    import httpx

    r = httpx.post(
        f"{ocr_url}/api/v1/ocr",
        files={"file": (_safe_basename(filename), image_bytes)},
        data={"strategy": "hybrid"},
        timeout=600,
    )
    r.raise_for_status()
    return _ocr_elements(r.json() or {})


def _elements_to_pages(elements: list[dict]) -> list[dict]:
    """OCR ``elements[]`` → page_idx 별로 묶은 ``list[PageDoc]``.

    ``elements_to_blocks`` 는 각 블록에 page_idx 를 채운다(blockify.py:311). 여기서는 그
    page_idx 로 블록을 그룹핑해 page_number(1-based) 별 PageDoc 을 만든다. elements 가
    page_idx 를 안 주면(전부 0) 단일 페이지(page_number=1)로 모은다.
    """
    from kb_pipeline.blockify import elements_to_blocks

    blocks = elements_to_blocks(elements)
    by_page: dict[int, list[dict]] = {}
    for b in blocks:
        pidx = int(b.get("page_idx", 0) or 0)
        # OCR page_idx 는 0-based 일 수 있다(elements_to_blocks 기본 0). 1-based 로 정규화:
        # 0 → 1, 그 외는 +1 하지 않고 그대로 두면 0-based/1-based 혼용으로 어긋난다.
        # OCR elements 는 보통 0-based page_idx → page_number = page_idx + 1.
        page_number = pidx + 1
        b["page_idx"] = page_number  # blocks 의 page_idx 도 1-based canonical 로 맞춘다
        by_page.setdefault(page_number, []).append(b)
    return [
        {"page_number": pn, "blocks": by_page[pn]} for pn in sorted(by_page)
    ]


#: OpenDataLoader 는 문서당 .md **1개**를 내고(per-page 파일 아님), ``markdown_page_separator``
#: 를 주면 각 페이지 **앞**에 이 구분자를 삽입한다 → SEP 로 split 해 페이지를 복원한다.
#: 콘텐츠에 나타날 일이 없는 sentinel.
_PAGE_SEP = "<<<ODL_PAGE_BREAK>>>"


def _parse_pdf_to_pages(
    file_bytes: bytes, filename: str, *, ocr_url: str
) -> list[dict]:
    """디지털 PDF → 페이지별 OpenDataLoader .md → ``hybrid_to_blocks(md, page_idx=page)``.

    OpenDataLoader convert 는 1회(JVM 호출 1회). 페이지별 *.md 를 sorted glob 으로 모으고
    **join 하지 않는다**. 비공백 텍스트가 있는 페이지는 디지털 → blockify. 거의 빈 페이지는
    스캔 후보 → 그 페이지를 PyMuPDF 로 렌더해 OCR(:18050)에 보내 elements_to_blocks(spec §4
    페이지 단위 판별). page_number canonical = .md 인덱스(1-based).
    """
    from kb_pipeline.blockify import hybrid_to_blocks
    import glob
    import os
    import tempfile

    import opendataloader_pdf

    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, _safe_basename(filename))
        if os.path.commonpath([os.path.realpath(tmp), os.path.realpath(src)]) != os.path.realpath(tmp):
            raise ParseError("unsafe filename")
        with open(src, "wb") as fh:
            fh.write(file_bytes)
        opendataloader_pdf.convert(
            input_path=src, output_dir=tmp, format="markdown",
            markdown_with_html=True, markdown_page_separator=_PAGE_SEP, quiet=True,
        )
        mds = sorted(glob.glob(os.path.join(tmp, "**", "*.md"), recursive=True))
        if not mds:
            raise ParseError(f"opendataloader produced no md for {filename}")
        # OpenDataLoader 는 문서당 .md 1개를 내고 각 페이지 앞에 ``_PAGE_SEP`` 를 넣는다.
        # (다중 .md 도 SEP 로 이어 붙여 동일하게 split.) split 결과는
        # ``["", page1, page2, ...]`` — 첫 SEP 앞의 빈 프리앰블 1개를 버리면 페이지 1..n.
        full = _PAGE_SEP.join(
            open(m, encoding="utf-8", errors="replace").read() for m in mds
        )
        md_texts = full.split(_PAGE_SEP)
        if len(md_texts) > 1 and not md_texts[0].strip():
            md_texts = md_texts[1:]

    # 스캔 페이지(거의 빈 .md)는 그 페이지를 렌더해 OCR 로 보충(있으면). 렌더는 lazy.
    rendered = None
    pages: list[dict] = []
    for i, md in enumerate(md_texts):
        page_number = i + 1  # canonical 1-based (spec §4)
        if len((md or "").strip()) >= _DIGITAL_MIN_CHARS:
            blocks = hybrid_to_blocks(md, page_idx=page_number)
            pages.append({"page_number": page_number, "blocks": blocks})
            continue
        # 스캔 후보 페이지 — 그 페이지만 OCR(:18050)로 보충(best-effort, 비치명).
        if rendered is None:
            from parse_service.pdf_pages import render_pdf_pages
            rendered = render_pdf_pages(file_bytes)
        page_jpeg = next(
            (rp.jpeg for rp in rendered if rp.page_number == page_number), None
        )
        if page_jpeg is None:
            log.warning("scanned page %d has no rendered image; emitting empty page", page_number)
            pages.append({"page_number": page_number, "blocks": []})
            continue
        try:
            elements = _ocr_page(page_jpeg, f"page-{page_number}.jpeg", ocr_url=ocr_url)
        except Exception:  # noqa: BLE001 - 페이지 단위 OCR 실패는 비치명(그 페이지 빈 블록).
            log.exception("OCR failed for scanned page %d", page_number)
            pages.append({"page_number": page_number, "blocks": []})
            continue
        from kb_pipeline.blockify import elements_to_blocks
        blocks = elements_to_blocks(elements)
        for b in blocks:
            b["page_idx"] = page_number  # 이 페이지 canonical 1-based 로 리맵(spec §4)
        pages.append({"page_number": page_number, "blocks": blocks})
    return pages


def parse_to_pages(
    file_bytes: bytes, filename: str, *, ocr_url: str, excel_url: str
) -> list[dict]:
    """페이지를 평탄화하지 않고 보존해 ``list[PageDoc]`` 을 돌려준다(spec §5.1.3).

    ``PageDoc = {"page_number": int(1-based), "blocks": list[dict]}`` — blocks 의 각 블록은
    page_idx 가 채워져 있다(``hybrid_to_blocks(md, page_idx=...)`` / ``elements_to_blocks``).

    라우팅(spec §4):
      * 디지털 PDF → OpenDataLoader 페이지별 .md(join 안 함) → ``hybrid_to_blocks``.
        거의 빈 페이지는 그 페이지만 OCR 로 보충(스캔 페이지, mixed PDF 대응).
      * 단일 이미지(.png/.jpg/...) → OCR(:18050) raw elements → ``elements_to_blocks``(page=1).
      * 그 외 structural(pptx/docx) → OCR raw elements → ``elements_to_blocks``.

    실패는 ``ParseError`` 로 정규화(``parse_to_markdown`` 과 동일 규약).
    """
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    try:
        if ext == "pdf":
            return _parse_pdf_to_pages(file_bytes, filename, ocr_url=ocr_url)
        if ext in _IMAGE_EXTS or _route(filename) == "structural":
            # 단일 이미지 / 스캔 단일 / pptx / docx → OCR raw elements 보존.
            elements = _ocr_page(file_bytes, filename, ocr_url=ocr_url)
            if not elements:
                raise ParseError(f"ocr/vlm empty for {filename}")
            return _elements_to_pages(elements)
        # markitdown 경로(xlsx 등)는 페이지 개념 없음 — spec §3 Excel 제외. 호출자는 이
        # 경로로 parse_to_pages 를 호출하지 않지만, 안전하게 단일 페이지로 강등한다.
        from kb_pipeline.blockify import hybrid_to_blocks
        md = _parse_markitdown(file_bytes, filename)
        return [{"page_number": 1, "blocks": hybrid_to_blocks(md, page_idx=1)}]
    except ParseError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ParseError(f"parse_to_pages failed for {filename}: {e}") from e
