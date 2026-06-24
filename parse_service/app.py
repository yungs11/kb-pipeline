"""parse-svc FastAPI service (:19001).

Owns the heavy parsing path (parse→blockify→modal) lifted out of the kb-pipeline
facade so java/OpenDataLoader/markitdown/OCR dependencies are isolated here. The
facade calls this service over HTTP (``service/parse_client.py``).

Endpoints:
  * ``POST /parse``    multipart ``file`` + form ``filename, content_type?``
                       -> ``{enriched_content, n_blocks, modal_spans}`` where each
                       modal span is ``{id, type, char_range:[start,end]}`` locating
                       the 〈MODAL…〈/MODAL〉 atomic region inside ``enriched_content``.
  * ``GET  /healthz``  -> ``{status, deps}``.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from typing import Any, Callable

from fastapi import FastAPI, UploadFile, File, Form

from parse_service.parsing import (
    parse_to_markdown,
    parse_to_pages,
    ParseError,
    _safe_basename,
)
from parse_service.pdf_pages import render_pdf_pages
from kb_pipeline.modal import enrich_with_spans, MODAL_OPEN_PREFIX, MODAL_CLOSE

log = logging.getLogger("kb_pipeline.parse_service")

# U+E000–U+F8FF: Unicode Private Use Area. OpenDataLoader 는 PDF 의 매핑 불가 글자
# (커스텀 폰트 기호·장식선 등)를 이 영역으로 쏟아낸다 → 깨진 글자처럼 보이고, 텍스트에
# 끼어 "제목↔표" 인접을 끊어 모달 제목/각주 흡수까지 방해한다. 파싱 직후 제거한다.
_PUA_RE = re.compile("[-]")


def _strip_pua(text: str) -> str:
    """Private Use Area(깨진/미매핑 글자) 제거."""
    return _PUA_RE.sub("", text)


def _default_docs_id(file_bytes: bytes) -> str:
    """orchestrator 미전달 시 docs_id 폴백 — ``content_hash(file_bytes)[:16]``.

    orchestrator 와 **동일 식**(sha256 hex prefix 16자)이어야 MinIO 키가 양쪽에서 일치한다
    (spec §3 D-docs_id). 정상 경로에서는 orchestrator 가 보낸 docs_id 를 쓰고, 누락 시에만
    이 폴백을 쓴다.
    """
    return hashlib.sha256(file_bytes).hexdigest()[:16]


def _image_to_jpeg(file_bytes: bytes) -> bytes:
    """단일 이미지를 JPEG 로 정규화(alpha 제거, RGB). Pillow 는 lazy import.

    이미 JPEG 인 입력도 재인코딩으로 동일 콘텐츠타입 보장(``/obj`` 프록시·챗 인용이
    image/jpeg 가정). Pillow 부재/디코드 실패는 ``None`` 반환(비치명 — 썸네일만 누락).
    """
    try:
        import io

        from PIL import Image

        img = Image.open(io.BytesIO(file_bytes))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        return buf.getvalue()
    except Exception:  # noqa: BLE001 - 이미지 정규화 실패는 비치명(썸네일만 누락).
        log.exception("image->jpeg normalization failed")
        return b""


app = FastAPI(title="kb-pipeline parse-svc")


class FrontError(Exception):
    """parse→blockify→modal failed. ``detail`` is the stable reason string."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


# Locate each 〈MODAL id="X" type="Y"〉…〈/MODAL〉 atomic span in the enriched text.
# The open marker carries id/type attributes (modal.py _open_marker); the close is
# the literal MODAL_CLOSE. We use a non-greedy body so nested-free atomic spans map
# 1:1 to char ranges. re.escape guards the U+3008/U+3009 angle-bracket markers.
_MODAL_RE = re.compile(
    re.escape(MODAL_OPEN_PREFIX)
    + r'\s+id="(?P<id>[^"]*)"\s+type="(?P<type>[^"]*)"〉'
    + r".*?"
    + re.escape(MODAL_CLOSE),
    re.DOTALL,
)


def _modal_spans(enriched: str) -> list[dict]:
    """Locate every 〈MODAL…〈/MODAL〉 span by exact char offset in ``enriched``.

    Returns ``[{id, type, char_range:[start,end]}]`` in document order. The
    ``char_range`` is a half-open ``[start, end)`` slice such that
    ``enriched[start:end]`` is exactly the 〈MODAL…〈/MODAL〉 substring.
    """
    spans: list[dict] = []
    for m in _MODAL_RE.finditer(enriched):
        spans.append(
            {
                "id": m.group("id"),
                "type": m.group("type"),
                "char_range": [m.start(), m.end()],
            }
        )
    return spans


def _strip_pua_blocks(blocks: list[dict]) -> None:
    """블록 텍스트 단계에서 PUA(깨진/미매핑 글자)를 in-place 제거한다(spec §5.1.5).

    ``parse_to_markdown`` 경로는 markdown 문자열 전체에 ``_strip_pua`` 를 걸었지만, 페이지
    보존 경로는 평탄화 전 블록 텍스트에 직접 건다(text/table/equation/image 본문 키 각각).
    """
    for b in blocks:
        if "text" in b and isinstance(b["text"], str):
            b["text"] = _strip_pua(b["text"])
        if "table_body" in b and isinstance(b["table_body"], str):
            b["table_body"] = _strip_pua(b["table_body"])
        if "latex" in b and isinstance(b["latex"], str):
            b["latex"] = _strip_pua(b["latex"])


def _render_and_upload(
    file_bytes: bytes, filename: str, docs_id: str, *,
    minio: Any | None,
    render: Callable[[bytes], list] | None = None,
) -> tuple[int, list[dict]]:
    """PDF/이미지 페이지를 렌더해 MinIO 에 업로드하고 ``(page_count, pages)`` 를 만든다.

    pages = ``[{page_number, page_uuid, minio_object}]`` (spec §5.1.5 응답). 키 규칙(잠금):
    ``page_uuid="{docs_id}_{p}"``, ``minio_object="{docs_id}/{docs_id}_{p}.jpeg"``.

    minio 가 없으면(미설정) 업로드는 건너뛰되 page 메타(키 조립)는 그대로 만든다 — 키는
    orchestrator/UI 가 조립하는 규칙과 동일하므로 메타만 있어도 일관적. 개별 페이지 업로드
    실패는 ``put_page_image`` 가 비치명 처리(None 반환)한다.
    """
    render = render or render_pdf_pages
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    pages: list[dict] = []
    if ext == "pdf":
        rendered = render(file_bytes)
        page_count = len(rendered)
        for rp in rendered:
            page_uuid = f"{docs_id}_{rp.page_number}"
            key = minio.page_image_object_key(docs_id, page_uuid) if minio else (
                f"{docs_id}/{page_uuid}.jpeg"
            )
            if minio is not None:
                minio.put_page_image(docs_id, page_uuid, rp.jpeg)
            pages.append({
                "page_number": rp.page_number,
                "page_uuid": page_uuid,
                "minio_object": key,
            })
        return page_count, pages
    # 단일 이미지 — 원본 1장을 JPEG 정규화해 page 1 로 업로드(spec §5.1.5).
    jpeg = _image_to_jpeg(file_bytes)
    page_uuid = f"{docs_id}_1"
    key = minio.page_image_object_key(docs_id, page_uuid) if minio else (
        f"{docs_id}/{page_uuid}.jpeg"
    )
    if minio is not None and jpeg:
        minio.put_page_image(docs_id, page_uuid, jpeg)
    pages.append({"page_number": 1, "page_uuid": page_uuid, "minio_object": key})
    return 1, pages


def run_parse(file_bytes: bytes, filename: str, *,
              text_llm: Callable[[str, str], str],
              vision_llm: Callable[[str, str], str] | None,
              ocr_url: str, excel_url: str,
              docs_id: str | None = None,
              minio: Any | None = None,
              parse_pages: Callable[..., list[dict]] | None = None,
              render: Callable[[bytes], list] | None = None) -> dict:
    """Run page-preserving parse→blockify→modal and return the parse-svc contract.

    Returns the **additive** contract (spec §5.1.5)::

        {enriched_content, n_blocks, modal_spans,
         docs_id, page_count, pages, page_spans}

    where ``pages = [{page_number, page_uuid, minio_object}]`` and
    ``page_spans = [{page_number, char_start, char_end}]`` (char offsets into
    ``enriched_content``). ``parse_pages``/``render``/``minio`` let callers (and
    tests) inject the page parser / renderer / minio store. ``docs_id`` defaults to
    ``content_hash(file_bytes)[:16]`` when orchestrator does not supply it.

    Raises ``FrontError(detail)`` on failure (``parse_failed`` for a ParseError,
    ``internal_error`` otherwise). 이미지/PDF render+upload 는 **best-effort** — 실패해도
    enriched_content/page_spans 는 정상 반환(썸네일만 누락).
    """
    parse_pages = parse_pages or parse_to_pages
    docs_id = docs_id or _default_docs_id(file_bytes)
    # 모달 LLM 동시호출 상한. 프록시(LiteLLM/Cloudflare) 과부하로 인한 524 를 줄이려고
    # 기본 3 으로 낮춘다(KBP_MODAL_MAX_WORKERS 로 조정; 524 잦으면 2/1 로).
    max_workers = max(1, int(os.environ.get("KBP_MODAL_MAX_WORKERS", "3")))
    # 모달 LLM(표/이미지 검색요약 + 제목/각주 흡수) 토글. 기본 off — OpenDataLoader 원본
    # payload 를 그대로 〈MODAL〉 로 통과시켜 LLM 0 회(속도↑). 〈MODAL〉 원자성·page_spans 는
    # 유지되므로 청킹/페이지 지표엔 무영향(검색은 표 의미요약만 손실). KBP_MODAL_ENRICH=1 로 재활성.
    enrich_modals = os.environ.get("KBP_MODAL_ENRICH", "0") != "0"
    modal_sink: dict = {}
    try:
        _t = time.perf_counter()
        pages = parse_pages(file_bytes, filename, ocr_url=ocr_url, excel_url=excel_url)
        parse_ms = (time.perf_counter() - _t) * 1000.0  # opendataloader/OCR 단계
        # 페이지 blocks 를 문서순으로 concat(평탄화). PUA 는 블록 텍스트 단계에서 제거.
        blocks: list[dict] = []
        for pd in pages:
            page_blocks = pd.get("blocks", []) or []
            _strip_pua_blocks(page_blocks)
            blocks.extend(page_blocks)
        _t = time.perf_counter()
        enriched, _modal_ids, page_spans = enrich_with_spans(
            blocks, text_llm=text_llm, vision_llm=vision_llm, max_workers=max_workers,
            timing_sink=modal_sink,  # 모달 LLM(표/이미지 분석) 단계 분해
            enrich_modals=enrich_modals,  # 기본 off → 원본 payload 통과(LLM 0회)
        )
        modal_ms = (time.perf_counter() - _t) * 1000.0
    except ParseError:
        log.exception("parse failed for %s", filename)
        raise FrontError("parse_failed")
    except Exception:  # noqa: BLE001
        log.exception("parse-svc front-end failed for %s", filename)
        raise FrontError("internal_error")

    # 렌더+업로드는 best-effort(비치명) — enriched/page_spans 는 위에서 이미 확정.
    _t = time.perf_counter()
    try:
        page_count, image_pages = _render_and_upload(
            file_bytes, filename, docs_id, minio=minio, render=render,
        )
    except Exception:  # noqa: BLE001 - 렌더/업로드 실패는 비치명(이미지 없이 진행).
        log.exception("render/upload failed for %s", filename)
        page_count, image_pages = 0, []
    render_ms = (time.perf_counter() - _t) * 1000.0

    return {
        "enriched_content": enriched,
        "n_blocks": len(blocks),
        "modal_spans": _modal_spans(enriched),
        "docs_id": docs_id,
        "page_count": page_count,
        "pages": image_pages,
        "page_spans": page_spans,
        # 모니터링(P2, additive): 파서 단계 분해 — parse(opendataloader/OCR) vs
        # modal_enrich(표/이미지 LLM) vs render_upload. modal_llm 에 표 N개×LLM 분해.
        "timing_metrics": {
            "parse_ms": round(parse_ms, 1),
            "modal_enrich_ms": round(modal_ms, 1),
            "render_upload_ms": round(render_ms, 1),
            "counters": {
                "page_count": page_count,
                "n_blocks": len(blocks),
                **modal_sink.get("counters", {}),
            },
            "modal_llm": {
                "wall_ms": modal_sink.get("modal_llm_wall_ms"),
                "calls": modal_sink.get("modal_llm_calls"),
                "by_type": modal_sink.get("by_type"),
                "per_call_ms": modal_sink.get("per_call_ms"),
                "max_workers": modal_sink.get("max_workers"),
            },
        },
    }


def _lazy_text_llm() -> Callable[[str, str], str]:
    """A text-LLM callable that builds the real client on first invocation.

    Deferring construction means the endpoint never touches the OpenRouter key (or
    even imports the llm client) until a modal block actually needs description —
    so tests that monkeypatch ``run_parse`` never trip the env-var requirement.
    """
    def call(prompt: str, payload: str) -> str:
        from service.llm import get_text_llm
        return get_text_llm()(prompt, payload)

    return call


def _lazy_minio() -> Any | None:
    """``MINIO_*`` 환경변수로 MinioStore 를 만든다(미설정/실패 시 None → 업로드 skip).

    minio 패키지 부재·연결 오류는 비치명 — page 메타(키 조립)는 그대로 만들고 업로드만
    건너뛴다(``run_parse``/``_render_and_upload`` 가 minio=None 을 허용).
    """
    try:
        from parse_service.minio_client import MinioStore

        return MinioStore.from_env()
    except Exception:  # noqa: BLE001 - minio 미설정/부재는 비치명(업로드 skip).
        log.warning("minio unavailable — page images will not be uploaded")
        return None


@app.get("/healthz")
def healthz():
    return {"status": "ok", "deps": {"ocr": os.environ.get("KBP_OCR_URL")}}


@app.post("/parse")
async def parse(file: UploadFile = File(...), filename: str = Form(...),
                content_type: str | None = Form(None),
                docs_id: str | None = Form(None)):
    """Parse one upload into enriched content + modal spans + page images.

    ``_safe_basename`` sanitizes the filename (no path traversal) before it ever
    reaches the parser/temp-file path. ``docs_id`` is optional — orchestrator sends
    ``content_hash(file_bytes)[:16]`` so MinIO keys match across both sides; absent
    it falls back to the same formula (spec §5.1.5).
    """
    data = await file.read()
    safe_name = _safe_basename(filename or file.filename or "upload")
    try:
        out = run_parse(
            data, safe_name,
            text_llm=_lazy_text_llm(), vision_llm=None,
            ocr_url=os.environ.get("KBP_OCR_URL", "http://localhost:18050"),
            excel_url=os.environ.get("KBP_EXCEL_URL", "http://localhost:18055"),
            docs_id=docs_id or None,
            minio=_lazy_minio(),
        )
    except FrontError as exc:
        return {"status": "failed", "detail": exc.detail}
    return out
