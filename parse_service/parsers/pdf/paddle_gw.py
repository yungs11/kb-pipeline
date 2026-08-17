"""paddle_gw 레인 — 스캔 PDF 를 PaddleOCR-VL 게이트웨이(GPU)로 페이지별 병렬 파싱.

게이트웨이(:8000 `/ocr/paddleocr_vl`, 2026-07-15 실측)가 layout(PP-DocLayoutV2)+VL 인식+표 조립을
**전부 GPU 서버에서** 수행하고 "마크다운 + 인라인 HTML 표"(= 우리 표준 중간표현, ODL 과 동일)를
반환한다 → parse-svc 로컬 의존 0(httpx 만), 기존 `hybrid_to_blocks` 재사용.

문서 통짜 대신 **페이지별 호출**(이미지 1장 multipart)로: ① page_number 계약 보존
(page_spans/페이지이미지/chunks_meta), ② 병렬 가속(GPU continuous batching).
스캔 PDF의 layout·VL 인식·표 조립은 외부 GPU 게이트웨이가 전담한다.

실패 정책: 페이지 단위 비치명(빈 blocks) — 전 페이지 실패 시 blocks 전무가 되고,
parse() 의 빈결과 검사가 ODL/in-process VL 폴백으로 잡는다(사용자 결정: 폴백은 in-process VL).
"""
from __future__ import annotations

import collections
import concurrent.futures
import json
import logging
import os
import re

import httpx

from parse_service.parsers.pdf.image_refs import find_image_refs, replace_image_refs, strip_image_refs

log = logging.getLogger("kb_pipeline.parse_service.parsers.pdf.paddle_gw")

# gw2(§A.2) 트리거 라벨 — image/figure/chart(사용자 결정, v7)/header_image/footer_image.
# `_VISUAL_LABELS`(__init__.py, 전면VL교체 소비)와는 **별개 상수**다 — header_image/
# footer_image 를 거기 추가하면 대형 헤더/푸터 이미지가 있는 기존 문서의 전면VL교체 동작이
# 바뀌는 회귀가 생긴다. chart 는 `_VISUAL_LABELS` 에도 있어 이 상위집합 관계는 안전하다.
_IMAGE_TRIGGER_LABELS = frozenset({"image", "figure", "chart", "header_image", "footer_image"})

# 페이지당 총 시한 600s(사용자 결정 — 복잡한 페이지/일시 부하 여유). 행(hang) 게이트웨이
# 조기 포기는 타임아웃이 아니라 **첫 페이지 프로브**가 담당(실패 시 즉시 폴백).
_DEFAULT_TIMEOUT = float(os.environ.get("KBP_PADDLE_GW_TIMEOUT", "600"))
# 비동기(tasks) 폴링 간격 — 각 폴링 호출은 즉시 응답이라 CF 100s 무관.
_POLL_INTERVAL = float(os.environ.get("KBP_PADDLE_GW_POLL_INTERVAL", "5"))
# 개별 HTTP 호출(submit/poll/result) 타임아웃 — 짧아도 됨(작업 대기는 폴링 루프가 담당).
_HTTP_TIMEOUT = float(os.environ.get("KBP_PADDLE_GW_HTTP_TIMEOUT", "60"))


# 게이트웨이(paddleocr_vl)가 표/그림에 넣는 상대경로 이미지 참조 — 실제 파일은 게이트웨이
# 서버에만 있어 우리 MinIO/UI 엔 없음(2026-07-16 실관측 img_in_image_box_*.jpg 404). 페이지
# 이미지는 parse-svc 가 따로 렌더·MinIO 업로드하므로 이 참조는 불필요·유해 → 제거.
# **v1~v5 결함 정정**: 실제 재현 사례가 `imgs/` 고정 접두어가 아니라
# `<파일명>_images/imageFileN.png`(gw 자체 산출물, 2026-08-16 잡 e7646e6d 실측)였다 —
# 접두어 고정 정규식은 이걸 못 잡는다. `image_refs.strip_image_refs`(임의 상대경로 + `<...>`
# 꺾쇠괄호 destination 처리, ODL §B.3 과 공유)로 일반화한다.
_BARE_IMG_RE = re.compile(r'^[ \t]*[^\s<>()]+\.(?:jpg|jpeg|png|webp|bmp|tiff?)[ \t]*$',
                          re.I | re.M)


def _splice_gw2_block_content(md: str, layout: list[dict]) -> str:
    """gw2(§A.2) 성공 시 `block_content`(이미지 블록 VL 서술)를 원래 이미지참조 자리에
    인라인 치환한다(2026-08-18 사용자 결정 — "원위치 대체").

    gw2 이전엔 top-level `text`가 image/figure 류 블록을 크롭 경로 참조로만 냈고,
    `use_ocr_for_image_block=True`로 얻은 VL 서술은 `layout[].blocks[].block_content`에만
    실려 온다(`text`는 opts와 무관하게 동일 — 2026-08-17 실측). `_parse_layout`이 layout
    블록 dict를 그대로 보존하므로(§ 위 docstring) `block_content`는 이미 살아있다 —
    여기서 그걸 md 안 참조 위치에 꽂아 넣는다.

    `_IMAGE_TRIGGER_LABELS` 블록 개수와 md 안 이미지참조 개수가 안 맞으면(예: 표에
    흡수된 이미지처럼 참조 자체가 없는 경우) 위치 매칭을 포기하고 md를 그대로 반환한다 —
    이어지는 `_strip_gateway_image_refs`가 안전하게 참조만 지운다(leak 없음, 서술 손실만).
    """
    refs = find_image_refs(md)
    if not refs:
        return md
    blocks = [b for b in layout
              if (b.get("block_label") or "").strip().lower() in _IMAGE_TRIGGER_LABELS]
    if len(blocks) != len(refs):
        return md
    contents = [(b.get("block_content") or "").strip() for b in blocks]
    try:
        return replace_image_refs(md, contents)
    except ValueError:
        return md


def _strip_gateway_image_refs(md: str) -> str:
    """게이트웨이 상대경로 이미지 참조 제거 — <img>·마크다운(꺾쇠괄호 포함)·맨몸 경로. 내용 보존."""
    if not md:
        return md
    md = strip_image_refs(md)
    md = _BARE_IMG_RE.sub("", md)
    return md


def _render_pages(file_bytes: bytes, page_numbers: set[int] | None = None):
    """게이트웨이 전송용 렌더 — MinIO 페이지이미지(dpi300)와 별개로 낮은 dpi 사용.

    실측(2026-07-15): dpi150 으로도 한국어/표 품질 충분(스모크 검증). dpi300 은 픽셀 4배 →
    업로드 큼 + 게이트웨이 VL 입력 커져 페이지당 처리시간 증가. KBP_PADDLE_GW_DPI 로 조절.
    """
    from parse_service.pdf_pages import render_pdf_pages
    dpi = int(os.environ.get("KBP_PADDLE_GW_DPI", "150"))
    if page_numbers is None:
        return render_pdf_pages(file_bytes, dpi=dpi)
    return render_pdf_pages(file_bytes, dpi=dpi, page_numbers=page_numbers)


def _looks_like_raw_layout_json(text: str) -> bool:
    """dots 간헐 형식 오류: markdown 대신 raw layout JSON('[{"bbox":..,"category":..')
    을 반환하는 케이스(2026-07-15 p10 실관측 — 재호출은 정상이었음)."""
    head = (text or "").lstrip()[:200]
    return head.startswith("[{") and '"bbox"' in head and '"category"' in head


def _parse_layout(body: dict) -> tuple[list[dict], tuple[int, int] | None]:
    """게이트웨이 응답의 layout → (blocks, page_size). 없거나 형식이 다르면 ([], None).

    body["layout"] 은 ``[{page_index, width, height, detection[], blocks[]}]`` 이고 페이지를
    1장씩 보내므로 원소는 항상 1개다(2026-08-02 실측). 소비하는 것은 두 가지뿐이다 —
    ``blocks[]``(라벨 권위)와 ``(width, height)``(면적 하한 판정용, 전송 JPEG 픽셀 크기와 일치 확인함).

    ``blocks`` 가 비었는데 ``detection`` 이 있으면 detection 원소를
    ``{"block_label": label, "block_bbox": coordinate}`` 로 정규화해 대체한다(읽기순서 단계 실패 대비).
    **bbox 도 함께 정규화해야** 상류의 면적 하한이 무음으로 꺼지지 않는다.
    구버전 게이트웨이(layout 미제공)면 ([], None) → 상류 hybrid 처리가 통째로 no-op 이 된다(하위호환).
    """
    lay = body.get("layout")
    if not isinstance(lay, list) or not lay or not isinstance(lay[0], dict):
        return [], None
    first = lay[0]
    w, h = first.get("width"), first.get("height")
    page_size = (w, h) if isinstance(w, int) and isinstance(h, int) else None
    blocks = first.get("blocks")
    if isinstance(blocks, list) and blocks:
        return [b for b in blocks if isinstance(b, dict)], page_size
    det = first.get("detection")
    if isinstance(det, list) and det:
        return [
            {"block_label": d.get("label"), "block_bbox": d.get("coordinate")}
            for d in det if isinstance(d, dict)
        ], page_size
    return [], page_size


def _post_page(jpeg: bytes, name: str, opts: dict | None = None) -> tuple[str, list[dict], tuple[int, int] | None]:
    """게이트웨이 페이지 호출(1회 재시도 래퍼) — raw JSON 형식 오류 시 한 번 더.

    반환 tuple 의 **markdown 요소로만** raw-JSON 판정한다. 재시도 성공 시 layout·page_size 도
    재시도 응답 것으로 함께 교체된다. ``opts`` — gw2(§A.2) 전용, 예:
    ``{"use_ocr_for_image_block": True, "use_chart_recognition": True}``.
    """
    md, layout, page_size = _post_page_once(jpeg, name, opts)
    if _looks_like_raw_layout_json(md):
        log.warning("gateway returned raw layout JSON for %s — retrying once", name)
        md, layout, page_size = _post_page_once(jpeg, name, opts)
        if _looks_like_raw_layout_json(md):
            raise RuntimeError(f"gateway raw-JSON output persisted for {name}")
    return md, layout, page_size


def _post_page_once(jpeg: bytes, name: str, opts: dict | None = None) -> tuple[str, list[dict], tuple[int, int] | None]:
    """게이트웨이에 페이지 이미지 1장 → (markdown, layout blocks, page_size). **비동기(tasks) 방식**.

    submit(POST {url}/tasks → task_id) → poll(GET /tasks/{id}, 즉시응답) → result(GET .../result).
    각 HTTP 호출이 즉시 응답이라 Cloudflare 100s 제한을 우회 — dots 처럼 페이지당 70s+ 걸리는
    생성형 VLM 도 안전(2026-07-15 실측: 동기 방식은 밀집 페이지 p7/p10 이 CF 524 로 실패했음).
    """
    import time
    base = os.environ["KBP_PADDLE_OCR_GATEWAY_URL"].rstrip("/")
    lang = os.environ.get("KBP_PADDLE_GW_LANG", "korean")

    # 1) submit — 즉시 task_id
    data = {"lang": lang}
    if opts:
        data["opts"] = json.dumps(opts)
    resp = httpx.post(
        f"{base}/tasks",
        files={"file": (name, jpeg, "image/jpeg")},
        data=data,
        timeout=_HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    task_id = resp.json().get("task_id")
    if not task_id:
        raise RuntimeError(f"gateway submit: task_id 없음 — {resp.json()}")

    # 2) poll — completed/failed 까지 (총 시한 _DEFAULT_TIMEOUT)
    deadline = time.monotonic() + _DEFAULT_TIMEOUT
    while True:
        st_resp = httpx.get(f"{base}/tasks/{task_id}", timeout=_HTTP_TIMEOUT)
        st_resp.raise_for_status()
        status = st_resp.json().get("status")
        if status == "completed":
            break
        if status == "failed":
            raise RuntimeError(f"gateway task failed: {st_resp.json().get('error')}")
        if time.monotonic() >= deadline:
            raise RuntimeError(f"gateway task {task_id} poll timeout ({_DEFAULT_TIMEOUT}s)")
        time.sleep(_POLL_INTERVAL)

    # 3) result
    r = httpx.get(f"{base}/tasks/{task_id}/result", timeout=_HTTP_TIMEOUT)
    r.raise_for_status()
    body = r.json()
    if body.get("status") != "ok":
        raise RuntimeError(f"gateway result status={body.get('status')} error={body.get('error')}")
    layout, page_size = _parse_layout(body)
    return body.get("text") or "", layout, page_size


def run_paddle_gateway(pdf_bytes: bytes, filename: str,
                       page_numbers: set[int] | None = None) -> list[dict]:
    """스캔 PDF → 페이지 렌더 → 게이트웨이 병렬 호출 → pages[{page_number, blocks, layout, page_size}].

    fail-fast(2026-07-15): **첫 페이지를 프로브**로 먼저 보낸다 — 실패하면 게이트웨이가
    죽은/행 상태이므로 즉시 raise 해 parse() 가 ODL/VL 로 폴백한다(나머지 페이지가
    타임아웃을 각자 기다리며 문서 전체를 붙잡는 것 방지). 프로브 성공 시 나머지는 병렬,
    개별 페이지 실패는 비치명(빈 blocks).

    ``page_numbers``(1-based, Plan B-2)를 주면 **그 페이지만** 렌더·전송한다. None 이면 전 페이지
    (현행 동작). 프로브는 렌더된 **첫 원소**를 쓰므로 부분집합이어도 그대로 성립한다.
    ``page_number`` 는 렌더가 문서 절대값을 유지하므로(pdf_pages) 반환 키도 절대값이다.
    """
    if not os.environ.get("KBP_PADDLE_OCR_GATEWAY_URL"):
        raise RuntimeError("KBP_PADDLE_OCR_GATEWAY_URL 미설정 — paddle_gw 레인 사용 불가")
    from kb_pipeline.blockify import hybrid_to_blocks

    rendered = _render_pages(pdf_bytes, page_numbers)
    if not rendered:
        return []
    max_workers = max(1, int(os.environ.get("KBP_VL_MAX_CONCURRENT", "8")))

    gw2_enabled = os.environ.get("KBP_GW_IMAGE_OCR_ENABLE", "1") != "0"

    def _needs_gw2(layout: list, page_size) -> tuple[bool, str]:
        """§A.2/A.3 — (트리거여부, 사유) — 사유는 항상 채워서 gw2_meta 로그에 남긴다
        (2026-08-18 사용자 지시 — "왜 gw2가 안 불렸는지 로그에 없다").

        실측(같은 문서, 이미지가 표 셀 안에 40~60개 있던 사고 케이스): 게이트웨이
        layout 이 그 사진들을 개별 image/figure 블록으로 안 내고 **table 블록 하나에
        통째로 흡수**한다 — `_IMAGE_TRIGGER_LABELS` 라벨 매칭이 구조적으로 못 잡는 경우가
        실재한다. 그래서 라벨 유무와 무관하게 **사유를 항상 남겨서** 이런 사각지대가
        조용히 지나가지 않게 한다(트리거 로직 자체는 유지 — 사유 노출이 이번 변경).

        대형 이미지류 블록(면적≥`KBP_VL_VISUAL_MIN_AREA`)이 있으면 기존
        `_hybrid_scan_pages` 전면VL교체가 처리하므로 gw2 는 스킵. 그런 대형 블록은 없지만
        `_IMAGE_TRIGGER_LABELS` 블록이 하나라도 있으면 gw2 트리거.
        """
        if not layout:
            return False, "no_layout"
        from parse_service.parsers.pdf import _contributes  # lazy — 순환 회피 겸 명시적
        counters: dict = collections.Counter()
        if any(_contributes(b, page_size, counters, labels=_IMAGE_TRIGGER_LABELS) for b in layout):
            return False, "skipped_large_block_delegated_to_hybrid_vl"
        if any((b.get("block_label") or "").strip().lower() in _IMAGE_TRIGGER_LABELS
               for b in layout):
            return True, "triggered"
        return False, "skipped_no_image_labeled_blocks_in_layout"

    def one(rp, probe: bool = False) -> tuple[int, list, list, tuple | None, str, str, dict | None]:
        """(page_number, blocks, layout, page_size, status, error, gw2_meta) — **7-key 계약**
        (v7 — gw2_meta 추가, 이전 6-key: page_number/blocks/layout/page_size/status/error).

        두 브랜치가 서로 다른 4-tuple 을 냈다(2026-08-12 재통합에서 합쳤다):
        HEAD `(n, blocks, status, error)` / scan-lane `(n, blocks, layout, page_size)`.
        **어느 한쪽만 취하면 조용히 기능이 죽는다** —
          · `layout`·`page_size` 를 빼면 `_hybrid_scan_pages` 의 `pg.get("layout")`·
            `_has_visual` 이 **영구 거짓**이 되어 hybrid 가 통째로 죽는다(발화 0을 정상으로 오독).
          · `status` 를 빼면 `_parse_routed` 의 `.get("status")` 가 항상 None 이라 demote
            조건이 축약되고, 게이트웨이 개별 페이지 실패가 demote 도 VL 도 못 받은 채
            게이트 EMPTY→quarantine 으로 **빈 페이지**가 된다.

        status 를 싣는 이유(2026-08-11): `blocks == []` 하나가 **세 가지 전혀 다른 사건**을
        합치고 있었다 — ① 게이트웨이 일시 장애·타임아웃·5xx(아래 except 가 삼킨다)
        ② 진짜 빈 간지 ③ md 는 왔는데 블록화 0. ①을 하류에서 terminal quarantine 으로
        확정하면 **부분 장애 한 번에 정상 스캔 페이지가 영구히 색인에서 빠지고 그 사실이
        "붕괴 페이지" 로 기록된다.** 판정(quarantine)과 사고(engine error)를 가른다.
        """
        name = f"page-{rp.page_number}.jpeg"
        try:
            md, layout, page_size = _post_page(rp.jpeg, name)
        except Exception as exc:  # noqa: BLE001
            if probe:
                raise  # 첫 페이지 실패 = 게이트웨이 불능 → 레인 포기(즉시 폴백)
            log.exception("paddle_gw page %d failed (%s)", rp.page_number, filename)
            return (rp.page_number, [], [], None,
                    "error", f"{type(exc).__name__}: {str(exc)[:200]}", None)

        # gw2_meta 는 **항상** 채운다(2026-08-18 사용자 지시) — 트리거 안 됐어도 왜
        # 안 됐는지가 로그(page_traces.attempts)에 남아야 한다. 이전엔 트리거 안 되면
        # `None`이라 아무 흔적도 안 남았다.
        needs_gw2, gw2_reason = _needs_gw2(layout, page_size)
        if not gw2_enabled:
            gw2_meta = {"outcome": "skipped", "reason": "disabled_by_env"}
        elif not needs_gw2:
            gw2_meta = {"outcome": "skipped", "reason": gw2_reason}
        else:
            # §A.3 — 2차 호출은 자체 try/except로 격리한다. 실패해도 1차 결과(md/layout/
            # page_size/status)는 그대로 유지 — 불필요한 전면 VL 재변환(demoted_pnos)을
            # 유발하지 않는다.
            try:
                md2, layout2, page_size2 = _post_page(
                    rp.jpeg, name,
                    opts={"use_ocr_for_image_block": True, "use_chart_recognition": True})
                md, layout, page_size = md2, layout2, page_size2
                gw2_meta = {"outcome": "ok"}
            except Exception as exc:  # noqa: BLE001
                log.warning("paddle_gw gw2 page %d failed (%s): %s",
                            rp.page_number, filename, exc)
                gw2_meta = {"outcome": "error", "error": f"{type(exc).__name__}: {str(exc)[:200]}"}

        if gw2_meta.get("outcome") == "ok":
            md = _splice_gw2_block_content(md, layout)
        md = _strip_gateway_image_refs(md)   # 미해석 이미지 참조 제거(gw2 실패/비활성/미매칭 시 폴백망)
        blocks = hybrid_to_blocks(md, page_idx=rp.page_number)
        return rp.page_number, blocks, layout, page_size, "ok", "", gw2_meta

    results = [one(rendered[0], probe=True)]   # 프로브 — 실패 시 여기서 raise
    if len(rendered) > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            results += list(ex.map(one, rendered[1:]))

    # layout/page_size/status/error/gw2_meta 는 **순수 추가** — 기존 두 키(page_number,
    # blocks)는 그대로다. 모르는 소비자는 무시한다(blocks 계약 불변).
    return [{"page_number": n, "blocks": blocks, "layout": layout, "page_size": page_size,
             "status": status, "error": err, "gw2_meta": gw2_meta}
            for n, blocks, layout, page_size, status, err, gw2_meta
            in sorted(results, key=lambda t: t[0])]
