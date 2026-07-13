"""MinerU 레인 — 스캔/혼합/복잡 PDF 를 MinerU(hybrid: VLM 원격 + PaddleOCR 로컬)로 파싱.

설계: docs/superpowers/specs/2026-07-13-mineru-pdf-integration-design.md §4·§5

경계 격리: `_run_mineru_do_parse` 만 실제 MinerU 를 import/호출한다(테스트는 이 함수를
monkeypatch). 나머지(_content_list_to_elements/_elements_to_pages)는 순수 매핑 → 로컬 단위검증.
MinerU 는 로컬(Intel Mac) 미설치라 지연 import — 실경로는 배포서버 스택검증(plan Task 8)으로 분리.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import shutil
import tempfile

from kb_pipeline.blockify import elements_to_blocks

log = logging.getLogger("kb_pipeline.parse_service.parsers.pdf.mineru_lane")

_MINERU_BACKEND = "hybrid-http-client"

# MinerU content_list `type` → blockify `elements[]` `category`
_TYPE_TO_CATEGORY = {
    "text": "text", "title": "title", "list": "text",
    "table": "table", "image": "image", "equation": "equation",
}


def _content_list_to_elements(content_list: list[dict]) -> list[dict]:
    """MinerU content_list item → blockify elements[] 형태(표 HTML/이미지/수식/텍스트)."""
    elements: list[dict] = []
    for item in content_list:
        t = (item.get("type") or "text").lower()
        page_idx = item.get("page_idx", 0) or 0
        category = _TYPE_TO_CATEGORY.get(t, "text")
        if t == "table":
            content = {"html": item.get("table_body") or ""}
        elif t == "image":
            content = {"img_path": item.get("img_path") or ""}
        elif t == "equation":
            # MinerU equation 은 'latex' 또는 'text' 로 올 수 있음(blockify 헤더 문서화). 둘 다 수용.
            content = {"text": item.get("latex") or item.get("text") or ""}
        else:  # text/title/list
            content = {"markdown": item.get("text") or ""}
        elements.append({"category": category, "content": content, "page_idx": page_idx})
    return elements


def _elements_to_pages(elements: list[dict]) -> list[dict]:
    """elements → blocks(elements_to_blocks 재사용) → 0-based page_idx 그룹핑 → 1-based pages."""
    blocks = elements_to_blocks(elements)
    by_page: dict[int, list] = {}
    for b in blocks:
        by_page.setdefault(b.get("page_idx", 0) or 0, []).append(b)
    pages: list[dict] = []
    # NOTE: content_list 에 없는 페이지(빈 페이지)는 pages 에 누락 → page_number 비연속 가능
    # (ODL 레인은 렌더된 모든 페이지를 냄). 하류는 page_number 로 키하므로 갭 허용.
    for pidx in sorted(by_page):
        page_number = pidx + 1
        for b in by_page[pidx]:
            b["page_idx"] = page_number  # 1-based 정규화(기존 ODL/OCR 경로와 동일)
        pages.append({"page_number": page_number, "blocks": by_page[pidx]})
    return pages


def _run_mineru_do_parse(**kwargs) -> None:
    """실제 mineru do_parse 경계 — mineru import 를 이 helper 안으로 격리(테스트 monkeypatch 지점).
    do_parse 는 결과를 output_dir 로 디스크 출력하고 None 반환.
    정확한 인자/출력경로는 배포서버에서 소스 대조로 확정(plan Task 4 Step 1)."""
    from mineru.cli.common import do_parse  # noqa: PLC0415 (지연 import — 로컬 미설치 허용)
    do_parse(**kwargs)


def _invoke_mineru(pdf_bytes: bytes, filename: str, parse_method: str) -> list[dict]:
    server_url = os.environ.get("MINERU_VLM_SERVER_URL")
    if not server_url:
        raise RuntimeError("MINERU_VLM_SERVER_URL 미설정 — MinerU 레인 사용 불가")
    scratch = os.environ.get("SCRATCHPAD_DIR") or None
    output_dir = tempfile.mkdtemp(prefix="mineru_", dir=scratch)
    try:
        _run_mineru_do_parse(
            output_dir=output_dir,
            pdf_bytes_list=[pdf_bytes],
            pdf_file_names=[os.path.splitext(os.path.basename(filename))[0]],
            backend=_MINERU_BACKEND,
            server_url=server_url,
            parse_method=parse_method,
        )
        matches = glob.glob(os.path.join(output_dir, "**", "*content_list.json"), recursive=True)
        if not matches:
            raise RuntimeError(f"MinerU content_list.json 미생성: {output_dir}")
        with open(matches[0], encoding="utf-8") as f:
            return json.load(f)
    finally:
        shutil.rmtree(output_dir, ignore_errors=True)


def run_mineru(pdf_bytes: bytes, filename: str, parse_method: str) -> list[dict]:
    """MinerU 레인 진입 — content_list 획득 → pages 반환."""
    content_list = _invoke_mineru(pdf_bytes, filename, parse_method)
    return _elements_to_pages(_content_list_to_elements(content_list))
