"""Plan B §B0 — parsers/ocr 동시성 정리.

세 문제를 고친다:
  (1) `_sem()` 이 loop-aware 가 아니라 다른 루프에서 RuntimeError 를 낼 수 있었다
  (2) 페이지 루프가 sequential await 라 세마포어가 무의미했다(pptx/이미지도 직렬)
  (3) 호출마다 `asyncio.run` → 루프·HTTP 클라이언트가 매번 재생성
"""
import asyncio
import json

import pytest

import parse_service.parsers.ocr as ocr
from parse_service.parsers.ocr import image_utils, vl_api


def _resp(md):
    return json.dumps({"elements": [
        {"category": "figure", "content": {"html": "", "markdown": md, "text": ""},
         "coordinates": [], "id": 0, "page": 1}]})


@pytest.fixture
def three_pages(monkeypatch):
    """3페이지 이미지 파일처럼 보이게 하고 VL 을 fake 로 잡는다."""
    monkeypatch.setattr(image_utils, "image_file_to_base64_list",
                        lambda p: ["QUJD", "QUJE", "QUJF"])


# ── (1) _sem() 루프 인식 ─────────────────────────────────────────────────────
def test_sem_rebinds_across_event_loops():
    """루프가 바뀌어도 RuntimeError 없이 그 루프용 세마포어를 준다."""
    async def grab():
        s = ocr._sem()
        async with s:           # 실제로 획득까지 해봐야 loop-bound 문제가 드러난다
            pass
        return s

    a = asyncio.run(grab())
    b = asyncio.run(grab())
    assert a is not b, "루프가 바뀌면 세마포어도 새로 만들어야 한다"


def test_sem_is_shared_within_one_loop():
    async def grab_twice():
        return ocr._sem(), ocr._sem()
    a, b = asyncio.run(grab_twice())
    assert a is b


# ── (2) 페이지 동시 실행 ─────────────────────────────────────────────────────
def test_pages_run_concurrently(three_pages, monkeypatch):
    """3페이지가 동시에 진행한다 — 배리어로 확인(직렬이면 데드락 후 타임아웃)."""
    monkeypatch.setenv("KBP_VL_MAX_CONCURRENT", "3")
    started = asyncio.Semaphore(0)
    release = asyncio.Event()
    seen = {"max": 0, "cur": 0}

    async def fake_call(b64, user_p, system_p, max_tokens=None):
        seen["cur"] += 1
        seen["max"] = max(seen["max"], seen["cur"])
        started.release()
        await release.wait()      # 세 개가 다 들어올 때까지 잡아둔다
        seen["cur"] -= 1
        return _resp("ok"), vl_api.VLCallMeta(elapsed=0.1)

    monkeypatch.setattr(vl_api, "call_vl_api_with_base64", fake_call)

    async def drive():
        task = asyncio.create_task(ocr.ocr_file_to_elements(b"x", "a.png"))
        for _ in range(3):
            await asyncio.wait_for(started.acquire(), timeout=2)
        release.set()
        return await task

    res = asyncio.run(drive())
    assert seen["max"] == 3, "세 페이지가 동시에 진행해야 한다(직렬이면 1)"
    assert len(res["elements"]) == 3


def test_concurrency_is_capped(three_pages, monkeypatch):
    monkeypatch.setenv("KBP_VL_MAX_CONCURRENT", "2")
    seen = {"max": 0, "cur": 0}

    async def fake_call(b64, user_p, system_p, max_tokens=None):
        seen["cur"] += 1
        seen["max"] = max(seen["max"], seen["cur"])
        await asyncio.sleep(0.01)
        seen["cur"] -= 1
        return _resp("ok"), vl_api.VLCallMeta(elapsed=0.1)

    monkeypatch.setattr(vl_api, "call_vl_api_with_base64", fake_call)
    asyncio.run(ocr.ocr_file_to_elements(b"x", "a.png"))
    assert seen["max"] == 2, "KBP_VL_MAX_CONCURRENT 가 실제 동시 실행을 제한해야 한다"


def test_page_failure_is_nonfatal_and_order_preserved(three_pages, monkeypatch):
    """가운데 페이지가 실패해도 나머지가 살고 page 번호가 어긋나지 않는다."""
    async def fake_call(b64, user_p, system_p, max_tokens=None):
        if b64 == "QUJE":                      # 2번째 페이지
            raise RuntimeError("VL down")
        return _resp("p" + b64[-1]), vl_api.VLCallMeta(elapsed=0.1)

    monkeypatch.setattr(vl_api, "call_vl_api_with_base64", fake_call)
    res = asyncio.run(ocr.ocr_file_to_elements(b"x", "a.png"))
    pages = [e["page"] for e in res["elements"]]
    assert pages == [1, 3], "실패 페이지만 빠지고 나머지 번호는 유지"


# ── (3) 배치 진입점 ──────────────────────────────────────────────────────────
def test_many_sync_uses_one_event_loop(monkeypatch):
    """job 이 몇 개든 asyncio.run 은 1회 — 루프·HTTP 클라이언트가 하나로 유지된다."""
    monkeypatch.setattr(image_utils, "image_file_to_base64_list", lambda p: ["QUJD"])

    async def fake_call(b64, user_p, system_p, max_tokens=None):
        return _resp("ok"), vl_api.VLCallMeta(elapsed=0.1)
    monkeypatch.setattr(vl_api, "call_vl_api_with_base64", fake_call)

    runs = {"n": 0}
    real_run = asyncio.run

    def counting_run(coro, **kw):
        runs["n"] += 1
        return real_run(coro, **kw)
    monkeypatch.setattr(ocr.asyncio, "run", counting_run)

    jobs = [(b"x", f"p{i}.png", None, None) for i in range(4)]
    out = ocr.ocr_elements_many_sync(jobs)
    assert runs["n"] == 1, "배치 전체에 asyncio.run 1회"
    assert len(out) == 4 and all(len(els) == 1 for els, _m in out)


def test_many_job_failure_keeps_index_alignment(monkeypatch):
    """한 job 이 실패해도 나머지 인덱스가 밀리지 않는다."""
    def fake_b64(path):
        raise RuntimeError("bad image") if "bad" in path else None
    monkeypatch.setattr(image_utils, "image_file_to_base64_list",
                        lambda p: ["QUJD"])

    async def fake_call(b64, user_p, system_p, max_tokens=None):
        return _resp("ok"), vl_api.VLCallMeta(elapsed=0.1)
    monkeypatch.setattr(vl_api, "call_vl_api_with_base64", fake_call)

    async def boom(*a, **k):
        raise RuntimeError("job failed")

    real = ocr.ocr_file_to_elements

    async def maybe_boom(file_bytes, filename, prompt_override=None, max_tokens=None):
        if filename == "bad.png":
            return await boom()
        # max_tokens 는 **keyword-only** 다(Phase 1) — positional 로 부르면 TypeError.
        return await real(file_bytes, filename, prompt_override, max_tokens=max_tokens)
    monkeypatch.setattr(ocr, "ocr_file_to_elements", maybe_boom)

    jobs = [(b"x", "ok1.png", None, None), (b"x", "bad.png", None, None),
            (b"x", "ok2.png", None, None)]
    out = ocr.ocr_elements_many_sync(jobs)
    assert [len(els) for els, _m in out] == [1, 0, 1]
    # 2b-1: 실패 job 은 meta.error 로 사유가 올라온다(빈 응답과 구분)
    assert any(getattr(m, "error", None) for m in out[1][1]), "실패 사유가 meta 에 남는다"


def test_many_passes_prompt_and_max_tokens(monkeypatch):
    monkeypatch.setattr(image_utils, "image_file_to_base64_list", lambda p: ["QUJD"])
    seen = []

    async def fake_call(b64, user_p, system_p, max_tokens=None):
        seen.append((system_p, user_p, max_tokens))
        return _resp("ok"), vl_api.VLCallMeta(elapsed=0.1)
    monkeypatch.setattr(vl_api, "call_vl_api_with_base64", fake_call)

    ocr.ocr_elements_many_sync([(b"x", "a.png", ("SYS", "USR"), 8000)])
    assert seen == [("SYS", "USR", 8000)]


# ── Plan B-3 (2026-08-04): pdf 레인이 배치 seam 을 실제로 쓰는가 ──────────────────
def test_pdf_batch_seam_runs_pages_concurrently(monkeypatch):
    """`_ocr_elements_for_pages` 가 여러 페이지를 **한 루프에서 동시** 처리한다.

    v3 설계는 배치 진입점을 만들어 놓고 아무도 호출하지 않아 VL 이 완전 직렬이었다.
    이 테스트는 배선이 실제로 동시성을 내는지 고정한다 — 직렬이면 배리어에서 타임아웃.
    """
    import parse_service.parsers.pdf as pdf_parser
    monkeypatch.setenv("KBP_VL_MAX_CONCURRENT", "3")
    monkeypatch.setattr(image_utils, "image_file_to_base64_list", lambda p: ["QUJD"])
    seen = {"max": 0, "cur": 0}

    async def fake_call(b64, user_p, system_p, max_tokens=None):
        seen["cur"] += 1
        seen["max"] = max(seen["max"], seen["cur"])
        await asyncio.sleep(0.02)          # 겹칠 시간을 준다
        seen["cur"] -= 1
        return _resp("page"), vl_api.VLCallMeta(elapsed=0.1)

    monkeypatch.setattr(vl_api, "call_vl_api_with_base64", fake_call)
    jobs = [(b"jpeg", f"page-{n}.jpeg") for n in (1, 2, 3)]
    out = pdf_parser._ocr_elements_for_pages(jobs, None, diagram=True)
    # 2b-1: (elements, metas) 쌍
    assert len(out) == 3 and all(len(els) == 1 for els, _m in out)
    assert seen["max"] == 3, "3페이지가 동시에 진행해야 한다(직렬이면 1)"


def test_pdf_batch_seam_uses_one_event_loop(monkeypatch):
    """페이지가 N 장이어도 `asyncio.run` 은 1회 — 루프·HTTP 클라이언트가 하나로 유지된다."""
    import parse_service.parsers.pdf as pdf_parser
    monkeypatch.setattr(image_utils, "image_file_to_base64_list", lambda p: ["QUJD"])

    async def fake_call(b64, user_p, system_p, max_tokens=None):
        return _resp("ok"), vl_api.VLCallMeta(elapsed=0.1)
    monkeypatch.setattr(vl_api, "call_vl_api_with_base64", fake_call)

    runs = {"n": 0}
    real_run = asyncio.run

    def counting_run(coro, **kw):
        runs["n"] += 1
        return real_run(coro, **kw)
    monkeypatch.setattr(ocr.asyncio, "run", counting_run)

    pdf_parser._ocr_elements_for_pages(
        [(b"jpeg", f"page-{n}.jpeg") for n in range(5)], None)
    assert runs["n"] == 1


def test_pdf_batch_seam_keeps_prompt_rules(monkeypatch):
    """diagram=True 가 prompt_override 보다 우선 — 단수 함수와 같은 규칙."""
    import parse_service.parsers.pdf as pdf_parser
    from parse_service.parsers.ocr import prompts
    seen = []
    monkeypatch.setattr(ocr, "ocr_elements_many_sync",
                        lambda jobs: seen.extend(jobs) or [([], []) for _ in jobs])
    pdf_parser._ocr_elements_for_pages([(b"j", "p1.jpeg")], None, diagram=True,
                                       prompt_override=("X", "Y"))
    assert seen[0][2] == (prompts.DIAGRAM_SYSTEM_PROMPT, prompts.DIAGRAM_USER_PROMPT)


def test_pdf_batch_seam_empty_jobs_no_call(monkeypatch):
    import parse_service.parsers.pdf as pdf_parser
    called = []
    monkeypatch.setattr(ocr, "ocr_elements_many_sync", lambda jobs: called.append(jobs))
    assert pdf_parser._ocr_elements_for_pages([], None) == []
    assert called == []
