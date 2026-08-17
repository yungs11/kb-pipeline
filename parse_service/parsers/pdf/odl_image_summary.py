"""ODL 레인 전용 이미지 요약 — §C. 페이지 전사(PAGE_HYBRID)와는 다른, 이미지 1장짜리
짧은 요약 서술 전용 경로다. 범용 modal `vision_llm` 배선(`app.py:473`, 현재 None)과는
별개 — 그 경로를 살리는 게 아니라 ODL 파서가 직접 호출하는 좁은 메커니즘이다.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os

log = logging.getLogger("kb_pipeline.parse_service.parsers.pdf.odl_image_summary")

_DEFAULT_PROMPT = (
    "이 이미지가 무엇을 보여주는지 1~3문장으로 요약하라. 주민등록번호·계좌번호 등 "
    "식별정보가 보이면 있는 그대로 받아쓰지 말고 '식별정보 포함'으로만 표시하라."
)
_SYSTEM_PROMPT = "당신은 문서에 첨부된 이미지를 한국어로 간결하게 요약하는 어시스턴트다."


def _prompt() -> str:
    return os.environ.get("KBP_ODL_IMAGE_SUMMARY_PROMPT") or _DEFAULT_PROMPT


async def _summarize_async(img_bytes: bytes) -> str | None:
    from parse_service.parsers.ocr.vl_api import call_vl_api_with_base64

    b64 = base64.b64encode(img_bytes).decode("ascii")
    text, meta = await call_vl_api_with_base64(
        b64, _prompt(), _SYSTEM_PROMPT,
        max_tokens=int(os.environ.get("KBP_ODL_IMAGE_SUMMARY_MAX_TOKENS", "300") or "300"),
        guided_json=False,
    )
    if not text or not text.strip():
        return None
    return text.strip()


def summarize_odl_image(img_bytes: bytes) -> str | None:
    """ODL이 추출한 이미지 1장 → 1~3문장 요약, 실패 시 **None**(호출부가 참조를 지운다 —
    원본 경로를 그대로 남기는 것은 이 플랜이 막으려는 leak을 재현하므로 절대 금지).
    """
    def _run() -> str | None:
        return asyncio.run(_summarize_async(img_bytes))

    try:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return _run()
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(_run).result()
    except Exception:  # noqa: BLE001 — VL 실패는 비치명, 호출부가 참조 제거로 폴백
        log.exception("ODL 이미지 요약 VL 호출 실패")
        return None
