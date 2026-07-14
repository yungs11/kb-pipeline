"""paddle_gw 레인 — 스캔 PDF 를 PaddleOCR-VL 게이트웨이(GPU)로 페이지별 병렬 파싱.

게이트웨이(:8000 `/ocr/paddleocr_vl`, 2026-07-15 실측)가 layout(PP-DocLayoutV2)+VL 인식+표 조립을
**전부 GPU 서버에서** 수행하고 "마크다운 + 인라인 HTML 표"(= 우리 표준 중간표현, ODL 과 동일)를
반환한다 → parse-svc 로컬 의존 0(httpx 만), 기존 `hybrid_to_blocks` 재사용.

문서 통짜 대신 **페이지별 호출**(이미지 1장 multipart)로: ① page_number 계약 보존
(page_spans/페이지이미지/chunks_meta), ② 병렬 가속(GPU continuous batching).
실측(신탁 3p 스캔): 게이트웨이 48s vs MinerU pipeline(CPU) 181s vs hybrid 166s. 한국어·표 HTML 정상.

실패 정책: 페이지 단위 비치명(빈 blocks) — 전 페이지 실패 시 blocks 전무가 되고,
parse() 의 빈결과 검사가 ODL/in-process VL 폴백으로 잡는다(사용자 결정: 폴백은 in-process VL).
"""
from __future__ import annotations

import concurrent.futures
import logging
import os

import httpx

log = logging.getLogger("kb_pipeline.parse_service.parsers.pdf.paddle_gw")

# 페이지당 타임아웃 600s(사용자 결정 — 복잡한 페이지/일시 부하 여유). 행(hang) 게이트웨이
# 조기 포기는 타임아웃이 아니라 **첫 페이지 프로브**가 담당(실패 시 즉시 폴백).
_DEFAULT_TIMEOUT = float(os.environ.get("KBP_PADDLE_GW_TIMEOUT", "600"))


def _render_pages(file_bytes: bytes):
    """게이트웨이 전송용 렌더 — MinIO 페이지이미지(dpi300)와 별개로 낮은 dpi 사용.

    실측(2026-07-15): dpi150 으로도 한국어/표 품질 충분(스모크 검증). dpi300 은 픽셀 4배 →
    업로드 큼 + 게이트웨이 VL 입력 커져 페이지당 처리시간 증가. KBP_PADDLE_GW_DPI 로 조절.
    """
    from parse_service.pdf_pages import render_pdf_pages
    dpi = int(os.environ.get("KBP_PADDLE_GW_DPI", "150"))
    return render_pdf_pages(file_bytes, dpi=dpi)


def _post_page(jpeg: bytes, name: str) -> str:
    """게이트웨이에 페이지 이미지 1장 POST → markdown(+HTML 표) 텍스트 반환."""
    url = os.environ["KBP_PADDLE_OCR_GATEWAY_URL"]
    lang = os.environ.get("KBP_PADDLE_GW_LANG", "korean")
    resp = httpx.post(
        url,
        files={"file": (name, jpeg, "image/jpeg")},
        data={"lang": lang},
        timeout=_DEFAULT_TIMEOUT,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("status") != "ok":
        raise RuntimeError(f"gateway status={body.get('status')} error={body.get('error')}")
    return body.get("text") or ""


def run_paddle_gateway(pdf_bytes: bytes, filename: str) -> list[dict]:
    """스캔 PDF → 페이지 렌더 → 게이트웨이 병렬 호출 → pages[{page_number, blocks}].

    fail-fast(2026-07-15): **첫 페이지를 프로브**로 먼저 보낸다 — 실패하면 게이트웨이가
    죽은/행 상태이므로 즉시 raise 해 parse() 가 ODL/VL 로 폴백한다(나머지 페이지가
    타임아웃을 각자 기다리며 문서 전체를 붙잡는 것 방지). 프로브 성공 시 나머지는 병렬,
    개별 페이지 실패는 비치명(빈 blocks).
    """
    if not os.environ.get("KBP_PADDLE_OCR_GATEWAY_URL"):
        raise RuntimeError("KBP_PADDLE_OCR_GATEWAY_URL 미설정 — paddle_gw 레인 사용 불가")
    from kb_pipeline.blockify import hybrid_to_blocks

    rendered = _render_pages(pdf_bytes)
    if not rendered:
        return []
    max_workers = max(1, int(os.environ.get("KBP_VL_MAX_CONCURRENT", "3")))

    def one(rp, probe: bool = False) -> tuple[int, list]:
        name = f"page-{rp.page_number}.jpeg"
        try:
            md = _post_page(rp.jpeg, name)
        except Exception:  # noqa: BLE001
            if probe:
                raise  # 첫 페이지 실패 = 게이트웨이 불능 → 레인 포기(즉시 폴백)
            log.exception("paddle_gw page %d failed (%s)", rp.page_number, filename)
            return rp.page_number, []
        blocks = hybrid_to_blocks(md, page_idx=rp.page_number)
        return rp.page_number, blocks

    results = [one(rendered[0], probe=True)]   # 프로브 — 실패 시 여기서 raise
    if len(rendered) > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            results += list(ex.map(one, rendered[1:]))

    return [{"page_number": n, "blocks": blocks}
            for n, blocks in sorted(results, key=lambda t: t[0])]
