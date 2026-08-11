"""파일 변환 API — 원본 → PDF. 한컴 도큐먼트툴즈 기반(docs/API_FILECONVERT_AGENT.md).

동기 변환이라 폴링이 없다: ``POST /convert-sync`` → ``cnvId`` → ``GET /download/{cnvId}``.

**성공 판정은 HTTP 상태가 아니라 응답 본문의 success/errorCode 로 한다** — 명세 §2.4:
파일 미첨부 같은 오류가 HTTP 200 으로 온다.

env 는 **호출 시점에** 읽는다(모듈 로드 시 읽으면 `monkeypatch.setenv` 가 안 먹는다).
repo 관례도 호출 시점이다 — `tools/kordoc.py` 의 `KORDOC_BIN`, `parsers/ocr` 의 `_sem()`.
"""
from __future__ import annotations

import logging
import os

import httpx

from parse_service.tools import ToolError, safe_basename

log = logging.getLogger("kb_pipeline.parse_service.tools.fileconvert")

# 테스트 seam — `httpx.MockTransport` 를 주입해 request 헤더/URL 을 캡처한다.
# 모듈 내부 함수 monkeypatch 로는 헤더가 관측되지 않아 "POST 에만 Authorization" 계약을
# 검증할 수 없다. None 이면 httpx 기본 전송을 쓴다.
_transport = None

#: 명세 §3.1.3 지원 목록 ∩ (비-excel · 비-이미지 · 비-pdf · 비-html). **여기가 유일한 정의다.**
#: odt/odp/ods/rtf 는 명세에 없다 — 넣으면 원격 422 로 문서 전체가 실패한다.
#: html/htm 은 2026-08-11 제외 — `parsers/html` 이 형변환 없이 처리한다(표 <table> 보존).
CONVERTIBLE_EXTS = {"hwp", "hwpx", "doc", "docx", "ppt", "pptx"}

#: 변환도 파싱도 불필요한 평문. 그대로 블록화한다(router 의 text 도메인).
#: xml 은 2026-08-11 편입 — 그 전엔 어느 집합에도 없어 pdf 도메인으로 떨어졌고
#: `app.py` 의 `%PDF` 가드에서 `not a PDF (and not convertible)` 로 죽었다.
TEXT_EXTS = {"txt", "md", "markdown", "csv", "json", "log", "xml"}


def ext_of(filename: str) -> str:
    """확장자(소문자). **router._domain 이 이 함수를 import 해 쓴다** — 식을 복제하지 않는다.

    소문자화를 빠뜨리면 ``A.HWP`` 가 ``needs_convert=False`` 인데 ``_domain`` 은 ``pdf`` 를
    돌려줘 ODL 이 HWP 를 받는다(조용한 회귀).
    """
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def needs_convert(filename: str) -> bool:
    """변환 API 로 보낼 확장자인가. **화이트리스트 단독** 판정."""
    return ext_of(filename) in CONVERTIBLE_EXTS


def swap_ext_pdf(filename: str) -> str:
    """변환 산출물 이름.

    하류에서 확장자를 소비하는 곳은 둘이다 —
      1. ``tools/opendataloader.py`` 가 원본 이름으로 임시파일을 만들고 ODL CLI 가
         확장자로 PDF 여부를 판정한다(실측 2026-08-06: ``.docx`` 이름이면 거부).
      2. ``app.py`` 의 ``ext == "pdf"`` 분기(페이지 이미지·page_count).

    점 술어는 :func:`ext_of` 와 통일한다(정의가 둘이면 드리프트가 생긴다).
    ``"a."`` → ``"a..pdf"`` 다 — ``ext_of("a.")`` 가 ``""`` 이라 else 가지를 탄다.
    파싱에는 문제없다(ODL 이 보는 것은 마지막 확장자뿐).
    """
    base = filename.rsplit(".", 1)[0] if ext_of(filename) else filename
    return (base or "upload") + ".pdf"


def _is_pdf(body: bytes) -> bool:
    """PDF 본문인가. **startswith 금지** — 헤더 앞 preamble 이 붙은 PDF(메일 추출본·일부
    스캐너)를 PyMuPDF·ODL 은 관용적으로 여는데 엄격한 검사만 새로 죽인다."""
    return b"%PDF" in body[:1024]


def convert_to_pdf(file_bytes: bytes, filename: str) -> bytes:
    """원본 → PDF bytes. 실패는 :class:`ToolError`.

    URL 미설정이면 즉시 실패한다 — **하드코딩 기본값을 두지 않는다.** 기본값이 있으면
    운영 오배선이 조용히 개발 서버로 나간다.
    """
    base = (os.environ.get("KBP_FILECONVERT_URL") or "").rstrip("/")
    if not base:
        raise ToolError("KBP_FILECONVERT_URL 미설정 — 파일 변환 불가")
    token = os.environ.get("KBP_FILECONVERT_TOKEN") or ""
    crt_id = os.environ.get("KBP_FILECONVERT_CRT_ID") or "kb-pipeline"
    try:
        timeout = float(os.environ.get("KBP_FILECONVERT_TIMEOUT") or 300)
    except ValueError:
        log.warning("KBP_FILECONVERT_TIMEOUT 값이 잘못됨 — 기본 300 사용")
        timeout = 300.0

    name = safe_basename(filename)
    with httpx.Client(transport=_transport, timeout=timeout) as client:
        # 1) 제출 — 파트명 `file` 고정(명세 §3.1.2). Authorization 은 여기에만.
        try:
            resp = client.post(
                f"{base}/convert-sync",
                headers={"Authorization": f"Bearer {token}"},
                files={"file": (name, file_bytes)},
                data={"crtId": crt_id},
            )
        except httpx.HTTPError as e:                       # noqa: BLE001
            raise ToolError(f"변환 요청 실패({type(e).__name__}): {name}") from e
        # HTTP 상태로 판정하지 않는다 — 명세 §2.4(오류가 200 으로 온다).
        try:
            body = resp.json()
        except ValueError as e:
            raise ToolError(
                f"변환 응답이 JSON 이 아니다(HTTP {resp.status_code}): {name}") from e
        if body.get("errorCode"):
            raise ToolError(
                f"변환 실패 [{body['errorCode']}] {body.get('errorMsg')}: {name}")
        if body.get("success") is not True:
            # 422(미지원 확장자)·401(토큰) 을 구분하려면 message 가 필요하다.
            raise ToolError(
                f"변환 실패(HTTP {resp.status_code}) {body.get('message')}: {name}")
        cnv_id = body.get("cnvId")
        if not cnv_id:
            raise ToolError(f"변환 응답에 cnvId 없음: {name}")

        # 2) 다운로드 — **인증 불필요**(명세 §3.2).
        try:
            dl = client.get(f"{base}/download/{cnv_id}")
        except httpx.HTTPError as e:                       # noqa: BLE001
            raise ToolError(f"변환 결과 다운로드 실패({type(e).__name__}): {name}") from e
        if dl.status_code != 200 or not _is_pdf(dl.content):
            # 실패 시 PDF 가 아니라 JSON 500 이 온다(명세 §3.2.3).
            raise ToolError(
                f"변환 결과가 PDF 가 아니다(HTTP {dl.status_code}, cnvId={cnv_id}): {name}")
    log.info("fileconvert: %s → PDF %d bytes (cnvId=%s)", name, len(dl.content), cnv_id)
    return dl.content
