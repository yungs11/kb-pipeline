"""parse-svc FastAPI service (:19001).

Owns the heavy parsing path (parse→blockify→modal) lifted out of the kb-pipeline
facade so java/OpenDataLoader/kordoc/VL-OCR dependencies are isolated here. The
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

from fastapi import FastAPI, UploadFile, File, Form, Response

from parse_service.tools import safe_basename as _safe_basename, ToolError
from parse_service.tools import fileconvert
from parse_service.tools import drm
from parse_service import router
from parse_service.router import route as _route_impl
from parse_service.parsers.ocr import IMAGE_EXTS
from parse_service.parsers import RouteResult, ParserError
from parse_service.pdf_pages import render_pdf_pages
from kb_pipeline.modal import enrich_with_spans, MODAL_OPEN_PREFIX, MODAL_CLOSE

log = logging.getLogger("kb_pipeline.parse_service")

# U+E000–U+F8FF: Unicode Private Use Area. OpenDataLoader 는 PDF 의 매핑 불가 글자
# (커스텀 폰트 기호·장식선 등)를 이 영역으로 쏟아낸다 → 깨진 글자처럼 보이고, 텍스트에
# 끼어 "제목↔표" 인접을 끊어 모달 문맥 복사/흡수까지 방해한다. 파싱 직후 제거한다.
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


def _route(fb, fn, **kw):
    """모듈 레벨 라우팅 훅 — 테스트가 monkeypatch 하는 대상(router.route 위임)."""
    return _route_impl(fb, fn, **kw)


class FrontError(Exception):
    """parse→blockify→modal failed. ``detail`` is the stable reason string.

    ``traces`` 는 Phase 2b-2 관측 — **실패한 문서에서만 관측이 0 이 되는 것**을 막는다.
    `detail` 은 문자열 그대로 둔다(하류 `runner.py` 와 `test_parse.py` 가 의존).
    """

    def __init__(self, detail: str, *, traces: list | None = None):
        super().__init__(detail)
        self.detail = detail
        self.traces = traces


_NATIVE_TRACE_LANES = {
    # HTML은 추상 도메인명이 아니라 실제 변환 엔진을 로그에 드러낸다. MarkItDown은
    # 제거됐고 현재 구현은 BeautifulSoup 전처리 + markdownify(표 원문 HTML 복원)다.
    "html": ("markdownify", "markdownify_markdown"),
    "kordoc": ("kordoc_native", "kordoc_markdown"),
    "text": ("text_native", "plain_text"),
    # 정상 PDF/OCR은 각 파서가 더 상세한 trace를 만든다. 아래 둘은 축퇴/레거시
    # 경로에서 trace가 비었을 때 로그 화면에서 문서가 사라지지 않게 하는 안전망이다.
    "pdf": ("odl", "odl_md"),
    "ocr": ("vl_ocr_direct", "vl"),
}


def _trace_chars(blocks: list[dict]) -> int:
    return sum(len((b.get("table_body") or b.get("text") or "")) for b in blocks or [])


def _default_page_traces(
    pages: list[dict], *, domain: str, extension: str, parse_ms: float,
) -> list[dict]:
    """상세 trace를 만들지 않는 도메인에 공통 관측 행을 보완한다.

    native HTML/kordoc/text는 현재 논리 1페이지이므로 그 행에 전체 parser wall time을
    기록한다. 혹시 여러 페이지인 축퇴 PDF 경로에는 문서 전체 시간을 페이지 시간으로
    오인하지 않도록 ``processing_ms=None``으로 두고, 별도 ``timing_metrics.total_ms``를
    문서 처리시간으로 쓴다.
    """
    lane, source = _NATIVE_TRACE_LANES.get(domain, (f"{domain}_native", domain))
    one_page = len(pages or []) == 1
    traces = []
    for index, page in enumerate(pages or [], start=1):
        blocks = page.get("blocks") or []
        traces.append({
            "page_number": page.get("page_number") or index,
            "bucket": None,
            "lane": lane,
            "source": source if blocks else "empty",
            "attempts": [["route", lane, {
                "reason": f".{extension or 'unknown'} -> {lane}",
                "extension": extension or None,
            }]],
            "chars": _trace_chars(blocks),
            "verdict": None,
            "state": None,
            "verdict_reason": None,
            "processing_ms": round(parse_ms, 1) if one_page else None,
        })
    return traces


def _excel_trace(rr: RouteResult, *, extension: str, parse_ms: float) -> list[dict]:
    backend = str((rr.gate_summary or {}).get("parser_backend") or "unknown").lower()
    lane = f"excel_{backend}"
    chunks = rr.chunks or []
    return [{
        # Excel은 페이지 모델이 아니라 문서 자체청킹 모델이다. document_pages 저장계약이
        # 정수 식별자를 요구하므로 1번을 문서수준 logical trace id로 쓴다.
        "page_number": 1,
        "bucket": None,
        "lane": lane,
        "source": "excel_rag_parser",
        "attempts": [["route", lane, {
            "reason": f".{extension or 'unknown'} -> {lane}",
            "extension": extension or None,
            "backend": backend,
        }]],
        "chars": sum(len(c.get("text") or "") for c in chunks),
        "verdict": None,
        "state": None,
        "verdict_reason": None,
        "processing_ms": round(parse_ms, 1),
    }]


def _parser_total_ms(*values: float) -> float:
    return round(sum(float(v or 0.0) for v in values), 1)


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

    (구 parse_to_markdown 경로는 markdown 문자열 전체에 ``_strip_pua`` 를 걸었다 — 2d 삭제.)
    페이지 보존 경로는 평탄화 전 블록 텍스트에 직접 건다(text/table/equation/image 본문 키 각각).
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

    **``minio_object`` 는 실제로 올라간 객체만 가리킨다.** 업로드를 안 했거나(미설정)
    실패했으면 ``None`` 이다. 예전에는 결과와 무관하게 키를 채웠는데, 그러면 소비자가
    존재하지 않는 객체를 ``chunks_meta`` 에 저장하고 인용 이미지가 조용히 404 가 된다
    (실측 2026-08-05: MinIO 없이 단독 기동 시 7페이지 전부 가짜 키를 응답에 실었다).
    이미지가 없다는 건 ``None`` 으로 말해야 소비자가 링크를 안 만든다.

    페이지 메타(``page_count``·``page_number``)는 업로드와 무관하게 항상 만든다 —
    청크→페이지 매핑(adaptive_chunk ``pages``)과 문서 메타가 여기 의존한다.
    """
    render = render or render_pdf_pages
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    pages: list[dict] = []
    # 업로드가 한 번 실패하면 남은 페이지는 시도하지 않는다. 접속 불가일 때 페이지마다
    # 재시도하면 minio 라이브러리 백오프가 누적된다(실측: 7페이지에 50초 — 86페이지면 10분).
    # KBP_DISABLE_PAGE_IMAGE_UPLOAD=1 이면 MinIO 연결과 무관하게 업로드를 건너뛴다
    # (2026-08-19, parser_test_ui/parse-only 배포용 — 잡 큐 blob 스토리지(입력파일/결과
    # JSON)는 이 토글과 무관하게 그대로 동작한다, 페이지 이미지 업로드만 끈다).
    can_upload = (minio is not None
                  and os.environ.get("KBP_DISABLE_PAGE_IMAGE_UPLOAD", "0") != "1")
    if ext == "pdf":
        rendered = render(file_bytes)
        page_count = len(rendered)
        for rp in rendered:
            page_uuid = f"{docs_id}_{rp.page_number}"
            key = None
            if can_upload:
                key = minio.put_page_image(docs_id, page_uuid, rp.jpeg)
                if key is None:
                    log.warning(
                        "page image upload failed for %s — 남은 페이지 업로드를 중단한다"
                        " (minio_object=None 으로 응답)", page_uuid)
                    can_upload = False
            pages.append({
                "page_number": rp.page_number,
                "page_uuid": page_uuid,
                "minio_object": key,
            })
        return page_count, pages
    # 단일 이미지 — 원본 1장을 JPEG 정규화해 page 1 로 업로드(spec §5.1.5).
    # **이미지일 때만** 정규화를 시도한다 — 텍스트(.txt/.csv 등)까지 넣으면 업로드마다
    # `_image_to_jpeg` 가 log.exception 을 남겨 ERROR 스택트레이스가 상시화된다.
    # 페이지 메타 자체는 여기서도 만든다(app 상단 주석의 계약: 업로드와 무관하게 항상).
    page_uuid = f"{docs_id}_1"
    key = None
    if ext in IMAGE_EXTS:
        jpeg = _image_to_jpeg(file_bytes)
        key = minio.put_page_image(docs_id, page_uuid, jpeg) if (can_upload and jpeg) else None
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

    Raises ``FrontError(detail)`` on failure (``parse_failed`` for a ParserError,
    ``internal_error`` otherwise). 이미지/PDF render+upload 는 **best-effort** — 실패해도
    enriched_content/page_spans 는 정상 반환(썸네일만 누락).
    """
    docs_id = docs_id or _default_docs_id(file_bytes)
    requested_extension = fileconvert.ext_of(filename)
    requested_domain = router.domain_of(filename)
    # 모달 LLM 동시호출 상한. 프록시(LiteLLM/Cloudflare) 과부하로 인한 524 를 줄이려고
    # 기본 3 으로 낮춘다(KBP_MODAL_MAX_WORKERS 로 조정; 524 잦으면 2/1 로).
    max_workers = max(1, int(os.environ.get("KBP_MODAL_MAX_WORKERS", "3")))
    # 모달 LLM(표/이미지 검색요약) 토글. 기본 off — LLM 0 회(속도↑). off 여도 아래 wrap 이
    # 켜져 있으면 표는 〈MODAL〉 로 원자화되고, 앞 블록 끝 200자·뒤 블록 앞 100자가 span 안으로
    # **복사**된다(원본 블록은 그대로 유지 — 이동/흡수 아님). 손실은 의미요약뿐.
    # KBP_MODAL_ENRICH=1 로 재활성하면 표/이미지 의미요약 + LLM 제목/각주 판정이 붙는다.
    enrich_modals = os.environ.get("KBP_MODAL_ENRICH", "0") != "0"
    # 모달 wrap(〈MODAL〉 원자 마커) 토글 — enrich 와 **분리**. 기본 on: 표를 마커로 원자화해
    # 청커가 <td> 중간에서 쪼개지 않게 한다(마커는 facade 적재 직전 스트립). KBP_MODAL_WRAP=0
    # 이면 bare 통과(마커 없음 → 청커가 자연 그룹핑, 하위호환).
    wrap_modals = os.environ.get("KBP_MODAL_WRAP", "1") != "0"
    modal_sink: dict = {}
    convert_ms = 0.0
    drm_ms = 0.0
    try:
        # ── DRM 해제(docs/REFERENCE_DRM해제_API.md) — 변환보다 먼저다. DRM 래핑된
        # office 파일은 해제 후에도 원래 확장자별 처리(kordoc 또는 fileconvert)를 거쳐야 한다.
        # 매직바이트로 걸러 DRM 아닌 파일은 원격 호출 자체를 안 한다.
        if parse_pages is None and drm.is_drm(file_bytes):
            _td = time.perf_counter()
            try:
                file_bytes = drm.unpack(file_bytes, filename)
            except ToolError as e:
                raise ParserError(str(e)) from e
            drm_ms = (time.perf_counter() - _td) * 1000.0
        # ── 변환: 지원 밖 포맷 → PDF (docs/API_FILECONVERT_AGENT.md) ─────────────
        # **여기여야 한다.** route() 안에서 filename 을 바꾸면 값 복사라
        # _render_and_upload 가 원본 이름을 보고 단일 이미지 분기로 떨어진다
        # → page_count=1 인데 page_spans 는 N개(2026-08-06 검증에서 잡힌 회귀).
        # parse_pages 주입 경로는 이미 파싱 결과가 있으므로 원격 API 를 때리지 않는다.
        if parse_pages is None and fileconvert.needs_convert(filename):
            _tc = time.perf_counter()
            try:
                file_bytes = fileconvert.convert_to_pdf(file_bytes, filename)
            except ToolError as e:
                # ToolError 는 ParserError 서브클래스가 아니다 → 감싸지 않으면
                # except Exception 에 걸려 internal_error 가 된다(원하는 건 parse_failed).
                raise ParserError(str(e)) from e
            filename = fileconvert.swap_ext_pdf(filename)
            convert_ms = (time.perf_counter() - _tc) * 1000.0
        # 변환 대상이 아닌데 pdf 도메인으로 갈 것들(확장자 없음·.zip 등)을 여기서 가른다.
        # 없으면 ODL 이 비-PDF 를 받아 ToolError 가 아닌 예외를 내고 internal_error 로 샌다.
        if parse_pages is None and router.domain_of(filename) == "pdf":
            if b"%PDF" not in file_bytes[:1024]:
                raise ParserError(f"not a PDF (and not convertible): {filename}")
            if fileconvert.ext_of(filename) != "pdf":
                filename = fileconvert.swap_ext_pdf(filename)   # "upload" → "upload.pdf"

        _t = time.perf_counter()
        # 라우팅: 주입된 parse_pages(테스트/레거시)가 있으면 pages 경로로 그대로 쓰고,
        # 없으면 확장자 router 가 도메인 파서를 고른다(excel → kind="chunks").
        if parse_pages is not None:
            rr = RouteResult(
                kind="pages", chunk_needed=True,
                pages=parse_pages(file_bytes, filename,
                                  ocr_url=ocr_url, excel_url=excel_url),
            )
        else:
            rr = _route(file_bytes, filename, ocr_url=ocr_url, excel_url=excel_url)
        if rr.kind == "chunks":
            # excel: 자체청킹 — 모달/blockify/렌더 스킵. additive 계약 유지.
            parse_ms = (time.perf_counter() - _t) * 1000.0
            page_traces = rr.page_traces or _excel_trace(
                rr, extension=requested_extension, parse_ms=parse_ms)
            total_ms = _parser_total_ms(drm_ms, convert_ms, parse_ms)
            return {
                "enriched_content": "\n\n".join(c.get("text", "") for c in rr.chunks),
                "n_blocks": len(rr.chunks),
                "modal_spans": [],
                "chunks": rr.chunks,
                "gate_summary": rr.gate_summary,
                "chunk_needed": False,
                "docs_id": docs_id,
                "page_count": 0, "pages": [], "page_spans": [],
                "page_traces": page_traces,
                # v2(리뷰 B1): pages 경로와 동일 형태(modal_llm 포함) — 모니터링 집계자 호환.
                "timing_metrics": {"total_ms": total_ms,
                                   "parse_ms": round(parse_ms, 1),
                                   "convert_ms": round(convert_ms, 1),
                                   "drm_ms": round(drm_ms, 1),
                                   "modal_enrich_ms": 0.0, "render_upload_ms": 0.0,
                                   "counters": {"page_count": 0, "n_blocks": len(rr.chunks)},
                                   "modal_llm": {"wall_ms": None, "calls": None,
                                                 "by_type": None, "per_call_ms": None,
                                                 "max_workers": None}},
            }
        pages = rr.pages
        parse_ms = (time.perf_counter() - _t) * 1000.0  # opendataloader/OCR 단계
        if not rr.page_traces and parse_pages is None:
            rr.page_traces = _default_page_traces(
                pages or [], domain=requested_domain,
                extension=requested_extension, parse_ms=parse_ms,
            )
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
            enrich_modals=enrich_modals,  # 기본 off → LLM 0회(요약 생략)
            wrap_modals=wrap_modals,      # 기본 on → 표를 〈MODAL〉 마커로 원자화
        )
        modal_ms = (time.perf_counter() - _t) * 1000.0
    except ParserError as exc:
        log.exception("parse failed for %s", filename)
        # 원래는 카테고리 문자열("parse_failed")만 넘겨 실제 원인(kordoc 부재 등)이
        # 로그에만 남고 응답 소비자(facade→kb)까지는 안 갔다 — 실제 예외 메시지를 싣는다.
        # traces 를 함께 넘긴다 — 안 넘기면 **실패한 문서에서만** page_traces 가 사라져
        # "실패를 드러낸다" 는 목적이 정작 실패에서 무효가 된다(Phase 2b-2 D2).
        raise FrontError(f"parse_failed: {exc}",
                         traces=getattr(exc, "traces", None))
    except Exception as exc:  # noqa: BLE001
        log.exception("parse-svc front-end failed for %s", filename)
        raise FrontError(f"internal_error: {exc}")

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
    total_ms = _parser_total_ms(drm_ms, convert_ms, parse_ms, modal_ms, render_ms)

    return {
        "enriched_content": enriched,
        "n_blocks": len(blocks),
        # 정식 BI 배선(A): 파서가 이미 구조화한 table 블록을 청킹까지 노출한다.
        # element key 통일 {category/content/page_number}. 빈 본문 table 은 제외
        # (score_bi degrade 방지 — 폴백보다 나쁜 bi=None 회피). table_body 는 무변형
        # 노출해야 enriched 내 table HTML 과 byte 정렬 → score_bi 위치탐색 적중.
        "table_blocks": [
            {"category": "table", "content": b.get("table_body") or "",
             "page_number": b.get("page_idx")}
            for b in blocks
            if b.get("type") == "table" and (b.get("table_body") or "").strip()
        ],
        "modal_spans": _modal_spans(enriched),
        "chunk_needed": True,
        "docs_id": docs_id,
        "page_count": page_count,
        "pages": image_pages,
        "page_spans": page_spans,
        # paddle_gw 레인 페이지 판정(additive). 그 외 레인은 None — facade 는 모르는
        # 키를 무시하므로 하위호환이다. **색인 제외는 여기서 하지 않는다** — 게이트가
        # 이미 quarantine 페이지의 blocks 를 비워서 위 concat 에 안 잡힌다.
        "page_verdicts": getattr(rr, "page_verdicts", None),
        # Phase 2b-1 관측: **전 페이지** trace. `page_verdicts`(게이트 대상 부분집합)와
        # 공존한다 — 개명이 아니라 추가다(기존 소비자 무영향).
        "page_traces": getattr(rr, "page_traces", None),
        # 모니터링(P2, additive): 파서 단계 분해 — parse(opendataloader/OCR) vs
        # modal_enrich(표/이미지 LLM) vs render_upload. modal_llm 에 표 N개×LLM 분해.
        "timing_metrics": {
            "total_ms": total_ms,
            "parse_ms": round(parse_ms, 1),
            # 원격 변환(doc/ppt/pptx→PDF)은 parse_ms 밖이라 따로 낸다 — 최대 300초라
            # 빠뜨리면 모니터링이 "빨라졌다"고 읽는데 실제 벽시계는 는다.
            "convert_ms": round(convert_ms, 1),
            "drm_ms": round(drm_ms, 1),
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
    # OCR 실제 origin 은 VL(`MODEL_API_URL`) — in-process(Phase 2c, :18050 HTTP 제거).
    # 구 `KBP_OCR_URL`(:18050) 은 dead vestige 라 표시하지 않는다(착시 방지, 01-architecture §3).
    return {"status": "ok", "deps": {"vl_ocr": os.environ.get("MODEL_API_URL")}}


@app.post("/drm/unwrap")
async def drm_unwrap(file: UploadFile = File(...), filename: str | None = Form(None)):
    """DRM(Fasoo) 래핑 파일이면 해제된 바이트를, 아니면 원본 그대로 반환한다.

    호출자(kb-backend 등)가 `/parse` 전에 자체적으로 파일을 열어야 하는 경우
    (예: 청킹모드 셀렉터용 신호 추출)를 위한 것 — 그 단계는 DRM 을 모르고 raw 바이트를
    직접 여니, 실패 시 이 엔드포인트로 해제를 받아 재시도한다(docs/REFERENCE_DRM해제_API.md).
    매직바이트(`drm.is_drm`)로 먼저 걸러 DRM 아닌 파일은 원격 왕복 없이 그대로 echo한다.
    """
    data = await file.read()
    if drm.is_drm(data):
        try:
            data = drm.unpack(data, filename or file.filename or "upload")
        except ToolError as e:
            return Response(content=str(e), status_code=502)
    return Response(content=data, media_type="application/octet-stream")


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
            # ocr_url/excel_url 은 Phase 2c/2e in-process 전환 후 소비자(vl_api·excel_rag)가
            # 무시하는 dead 파라미터 — 시그니처 하위호환만 유지(실 OCR=MODEL_API_URL). 01-architecture §3.
            ocr_url="", excel_url="",
            docs_id=docs_id or None,
            minio=_lazy_minio(),
        )
    except FrontError as exc:
        # `detail` 은 문자열 유지(하류 계약) — 관측은 **새 키**로 싣는다.
        out = {"status": "failed", "detail": exc.detail}
        if exc.traces:
            out["page_traces"] = exc.traces
        return out
    return out
