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

import logging
import os
import re
from typing import Callable

from fastapi import FastAPI, UploadFile, File, Form

from parse_service.parsing import parse_to_markdown, ParseError, _safe_basename
from kb_pipeline.blockify import hybrid_to_blocks
from kb_pipeline.modal import enrich, MODAL_OPEN_PREFIX, MODAL_CLOSE

log = logging.getLogger("kb_pipeline.parse_service")

# U+E000–U+F8FF: Unicode Private Use Area. OpenDataLoader 는 PDF 의 매핑 불가 글자
# (커스텀 폰트 기호·장식선 등)를 이 영역으로 쏟아낸다 → 깨진 글자처럼 보이고, 텍스트에
# 끼어 "제목↔표" 인접을 끊어 모달 제목/각주 흡수까지 방해한다. 파싱 직후 제거한다.
_PUA_RE = re.compile("[-]")


def _strip_pua(text: str) -> str:
    """Private Use Area(깨진/미매핑 글자) 제거."""
    return _PUA_RE.sub("", text)

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


def run_parse(file_bytes: bytes, filename: str, *,
              text_llm: Callable[[str, str], str],
              vision_llm: Callable[[str, str], str] | None,
              ocr_url: str, excel_url: str,
              parse: Callable[..., str] | None = None) -> dict:
    """Run parse→blockify→modal and return the parse-svc contract.

    Returns ``{enriched_content, n_blocks, modal_spans}``. ``parse`` lets callers
    (and tests) inject the parser; it defaults to this module's ``parse_to_markdown``.
    Raises ``FrontError(detail)`` on failure (``parse_failed`` for a ParseError,
    ``internal_error`` otherwise).
    """
    parse = parse or parse_to_markdown
    # 모달 LLM 동시호출 상한. 프록시(LiteLLM/Cloudflare) 과부하로 인한 524 를 줄이려고
    # 기본 3 으로 낮춘다(KBP_MODAL_MAX_WORKERS 로 조정; 524 잦으면 2/1 로).
    max_workers = max(1, int(os.environ.get("KBP_MODAL_MAX_WORKERS", "3")))
    try:
        md = _strip_pua(parse(file_bytes, filename, ocr_url=ocr_url, excel_url=excel_url))
        blocks = hybrid_to_blocks(md)
        enriched, _modal_ids = enrich(
            blocks, text_llm=text_llm, vision_llm=vision_llm, max_workers=max_workers,
        )
    except ParseError:
        log.exception("parse failed for %s", filename)
        raise FrontError("parse_failed")
    except Exception:  # noqa: BLE001
        log.exception("parse-svc front-end failed for %s", filename)
        raise FrontError("internal_error")
    return {
        "enriched_content": enriched,
        "n_blocks": len(blocks),
        "modal_spans": _modal_spans(enriched),
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


@app.get("/healthz")
def healthz():
    return {"status": "ok", "deps": {"ocr": os.environ.get("KBP_OCR_URL")}}


@app.post("/parse")
async def parse(file: UploadFile = File(...), filename: str = Form(...),
                content_type: str | None = Form(None)):
    """Parse one upload into enriched content + modal spans.

    ``_safe_basename`` sanitizes the filename (no path traversal) before it ever
    reaches the parser/temp-file path.
    """
    data = await file.read()
    safe_name = _safe_basename(filename or file.filename or "upload")
    try:
        out = run_parse(
            data, safe_name,
            text_llm=_lazy_text_llm(), vision_llm=None,
            ocr_url=os.environ.get("KBP_OCR_URL", "http://localhost:18050"),
            excel_url=os.environ.get("KBP_EXCEL_URL", "http://localhost:18055"),
        )
    except FrontError as exc:
        return {"status": "failed", "detail": exc.detail}
    return out
