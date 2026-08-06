"""Fasoo DRM 해제 — 원본 바이트 그대로 복원(docs/REFERENCE_DRM해제_API.md).

같은 인프라(base host)를 fileconvert 와 공유하지만 path prefix 가 다르다
(``/api/drm/agent/tool`` vs ``/api/fileconvert/agent/tool``) → 별도 env(``KBP_DRM_URL``).
토큰 값은 fileconvert 와 동일(2026-08-06 사용자 확인)하지만, 향후 서버가 분리될 수 있어
``KBP_DRM_TOKEN`` 을 별도 env 로 둔다 — ``KBP_FILECONVERT_TOKEN`` 을 재사용하지 않는다.

명세: DRM 파일이 아니면 서버가 입력 바이트를 그대로 반환한다(폴백) — 그래서 unpack() 은
호출 자체를 실패로 보지 않는다. 다만 매 파일마다 원격 왕복을 시키지 않으려고
:func:`is_drm` 매직바이트 휴리스틱으로 먼저 걸러낸다(호출부 책임 — 이 모듈은 감지도 제공).

env 는 fileconvert.py 관례대로 **호출 시점에** 읽는다(모듈 로드 시 읽으면 테스트의
``monkeypatch.setenv`` 가 안 먹는다).
"""
from __future__ import annotations

import logging
import os

import httpx

from parse_service.tools import ToolError, safe_basename

log = logging.getLogger("kb_pipeline.parse_service.tools.drm")

# 테스트 seam — fileconvert.py 와 동일 패턴(`httpx.MockTransport` 주입).
_transport = None

#: Fasoo DRM 래핑 파일의 매직 바이트(실측: 길이-프리픽스 2바이트 뒤 "DRMONE  This
#: Document is encrypted and protect..."). 앞부분 32바이트 안에서 찾으면 충분하다.
_DRM_MAGIC = b"DRMONE"


def is_drm(file_bytes: bytes) -> bool:
    """DRM 래핑 파일로 보이는가(휴리스틱). False 라고 절대 아니라는 보장은 아니다."""
    return _DRM_MAGIC in file_bytes[:32]


def unpack(file_bytes: bytes, filename: str) -> bytes:
    """DRM 해제 → 평문 원본 바이트. 실패는 :class:`ToolError`.

    URL 미설정이면 즉시 실패한다 — fileconvert.py 와 동일 원칙, 하드코딩 기본값 없음.
    """
    base = (os.environ.get("KBP_DRM_URL") or "").rstrip("/")
    if not base:
        raise ToolError("KBP_DRM_URL 미설정 — DRM 해제 불가")
    token = os.environ.get("KBP_DRM_TOKEN") or ""
    try:
        timeout = float(os.environ.get("KBP_DRM_TIMEOUT") or 300)
    except ValueError:
        log.warning("KBP_DRM_TIMEOUT 값이 잘못됨 — 기본 300 사용")
        timeout = 300.0

    name = safe_basename(filename)
    with httpx.Client(transport=_transport, timeout=timeout) as client:
        try:
            resp = client.post(
                f"{base}/unpack",
                headers={"Authorization": f"Bearer {token}"},
                files={"file": (name, file_bytes)},
            )
        except httpx.HTTPError as e:                       # noqa: BLE001
            raise ToolError(f"DRM 해제 요청 실패({type(e).__name__}): {name}") from e
        # 명세 §1 응답표: 성공만 200 + 바이너리, 그 외(400/401/422/500)는 본문 없음/JSON.
        if resp.status_code != 200:
            raise ToolError(f"DRM 해제 실패(HTTP {resp.status_code}): {name}")
    log.info("drm: %s 해제 %d bytes", name, len(resp.content))
    return resp.content
