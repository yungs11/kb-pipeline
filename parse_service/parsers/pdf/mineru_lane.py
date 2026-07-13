"""MinerU 레인 — 스캔/혼합/복잡 PDF 를 MinerU(hybrid: VLM 원격 + PaddleOCR 로컬)로 파싱.

설계: docs/superpowers/specs/2026-07-13-mineru-pdf-integration-design.md §4·§5

경계 격리: `_run_mineru_do_parse` 만 실제 MinerU 를 import/호출한다(테스트는 이 함수를
monkeypatch). 나머지(_content_list_to_elements/_elements_to_pages)는 순수 매핑 → 로컬 단위검증.
MinerU 는 로컬(Intel Mac) 미설치라 지연 import — 실경로는 배포서버 스택검증(plan Task 8)으로 분리.
"""
from __future__ import annotations

import concurrent.futures
import glob
import json
import logging
import os
import shutil
import tempfile

from kb_pipeline.blockify import elements_to_blocks

log = logging.getLogger("kb_pipeline.parse_service.parsers.pdf.mineru_lane")

_MINERU_BACKEND = "hybrid-http-client"
# PaddleOCR OCR-det 언어(do_parse p_lang_list). "korean"=Korean,English / "ch"=중·영·일·번체·라틴.
_DEFAULT_LANG = "korean"

# MinerU content_list(v1, vlm 스키마) 아이템 형태 — 배포서버 소스 대조로 확정(plan Task 4 Step 1):
#   text  : {type:'text', text, [text_level:N]}      # heading 은 별도 'title' 타입이 아니라 text_level
#   equation: {type:'equation', text, text_format:'latex'}
#   image : {type:'image', img_path, image_caption:[], content}
#   table : {type:'table', table_body:<HTML>, table_caption:[], img_path}
#   chart : {type:'chart', img_path, content:<md 문자열>}   # 순서도/차트 = 별도 타입
#   list  : {type:'list', sub_type, list_items:[<문자열>,...]}
#   code  : {type:'code', sub_type, code_body}


def _list_items_to_markdown(list_items: list) -> str:
    """MinerU list_items(문자열 배열) → 마크다운 불릿 리스트."""
    lines = []
    for it in list_items or []:
        s = it if isinstance(it, str) else (it.get("text") if isinstance(it, dict) else str(it))
        if s and s.strip():
            lines.append(f"- {s.strip()}")
    return "\n".join(lines)


def _content_list_to_elements(content_list: list[dict]) -> list[dict]:
    """MinerU content_list item → blockify elements[] 형태(표 HTML/이미지/수식/텍스트/차트/리스트).

    blockify.elements_to_blocks 계약: table=content.html / image=content.img_path /
    equation=content.text / title=category 'title'+최상위 text_level / 그 외=content.markdown.
    """
    elements: list[dict] = []
    for item in content_list:
        t = (item.get("type") or "text").lower()
        page_idx = item.get("page_idx", 0) or 0

        if t == "table":
            elements.append({"category": "table",
                             "content": {"html": item.get("table_body") or ""},
                             "page_idx": page_idx})
        elif t == "image":
            elements.append({"category": "image",
                             "content": {"img_path": item.get("img_path") or ""},
                             "page_idx": page_idx})
        elif t == "chart":
            # chart(순서도/차트) = VLM 추출 content(마크다운) 우선 → 검색가능 텍스트.
            # content 비면 이미지 참조로 보존(내용 유실 방지).
            md = item.get("content") or ""
            if md:
                elements.append({"category": "text", "content": {"markdown": md},
                                 "page_idx": page_idx})
            else:
                elements.append({"category": "image",
                                 "content": {"img_path": item.get("img_path") or ""},
                                 "page_idx": page_idx})
        elif t == "equation":
            elements.append({"category": "equation",
                             "content": {"text": item.get("text") or item.get("latex") or ""},
                             "page_idx": page_idx})
        elif t == "list":
            elements.append({"category": "text",
                             "content": {"markdown": item.get("text")
                                         or _list_items_to_markdown(item.get("list_items") or [])},
                             "page_idx": page_idx})
        elif t == "code":
            elements.append({"category": "text",
                             "content": {"markdown": item.get("code_body") or item.get("text") or ""},
                             "page_idx": page_idx})
        else:  # 'text' — heading 은 text_level 보유
            text_level = item.get("text_level")
            el = {"category": "text",
                  "content": {"markdown": item.get("text") or ""},
                  "page_idx": page_idx}
            if text_level:
                el["category"] = "title"     # blockify: title → text 블록 + text_level 유지
                el["text_level"] = text_level
            elements.append(el)
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
    lang = os.environ.get("MINERU_LANG") or _DEFAULT_LANG
    scratch = os.environ.get("SCRATCHPAD_DIR") or None
    output_dir = tempfile.mkdtemp(prefix="mineru_", dir=scratch)
    try:
        # do_parse 필수 위치인자: output_dir, pdf_file_names, pdf_bytes_list, p_lang_list, backend...
        # (p_lang_list 누락 시 TypeError). model 파라미터는 없음 — http-client 는 vLLM 서빙 모델 사용.
        kwargs = dict(
            output_dir=output_dir,
            pdf_bytes_list=[pdf_bytes],
            pdf_file_names=[os.path.splitext(os.path.basename(filename))[0]],
            p_lang_list=[lang],
            backend=_MINERU_BACKEND,
            server_url=server_url,
            parse_method=parse_method,
        )
        # mineru_vl_utils 동시 요청 수(기본 100). 배포서버 GPU vLLM --max-num-seqs 와 맞춰 조정.
        max_conc = os.environ.get("MINERU_MAX_CONCURRENCY")
        if max_conc:
            kwargs["max_concurrency"] = int(max_conc)
        # MinerU VLM http 클라이언트는 실행 중 루프가 있으면 loop.run_until_complete 를 써서
        # "event loop is already running"(FastAPI async 핸들러) 로 실패한다. 실행 중 루프가 없는
        # 워커 스레드에서 돌리면 mineru_vl_utils 가 asyncio.run() 깨끗한 경로를 탄다.
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            ex.submit(_run_mineru_do_parse, **kwargs).result()
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
