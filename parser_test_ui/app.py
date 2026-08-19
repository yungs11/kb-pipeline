"""PARSER 테스트 — kb-backend/frontend 없이 facade 에 직접 붙는 standalone UI.

`~/workspace/7.excel-parser/scripts/parse_ui.py`(8600) 패턴 그대로: 단일 파일
FastAPI, 무인증, doc_guard 판정은 facade 응답의 `gate_summary`를 로컬 렌더링만
한다(원격 재호출 없음). facade 를 HTTP 로만 부르므로 이 서비스는 파싱 로직
의존성이 전혀 없다(fastapi+uvicorn+httpx 뿐).

잡 제출은 명시적 `POST /jobs/parse` + 폴링을 쓴다(레거시 동기 `/parse` 아님) —
`batch_key="parser-test-ui"`로 태깅한다(디버깅용 표식). `/history`는
**batch_key 로 걸러지 않는다**(사용자 요청, 2026-08-19) — 이 도구뿐 아니라
같은 facade 의 `/jobs/parse`를 직접(curl 등) 호출한 실행 기록도 `kind=parse`
전부 여기서 보인다. plan: /Users/xxx/.claude/plans/concurrent-soaring-mango.md
"""
from __future__ import annotations

import html
import os
import re
import time
from typing import Any
from uuid import uuid4

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

app = FastAPI()

FACADE_URL = os.environ.get("KBP_FACADE_URL", "http://localhost:3000").rstrip("/")
FACADE_KEY = os.environ.get("KBP_FACADE_KEY") or None
BATCH_KEY = "parser-test-ui"
POLL_TIMEOUT_SECONDS = int(os.environ.get("KBP_PARSER_TEST_UI_POLL_TIMEOUT_SECONDS", "600"))
TERMINAL = frozenset({"succeeded", "failed", "canceled"})

# doc_guard RULE_NAMES 의 한글 라벨 미러(표시 전용 — excel-parser 8600 scripts/parse_ui.py
# `_RULE_LABELS`와 동일, 원본은 doc_guard/app/excel_gate_policy.py).
_RULE_LABELS = {
    "ref_error": "참조 오류(#REF!)",
    "empty_header": "빈 헤더 칸",
    "header_leak": "헤더 값 누수",
    "conflicting_code_mapping": "약어 매핑 상충",
    "unclear_header": "표 헤더 불명확",
    "unmerged_table_banners": "병합 없는 다중 표 제목",
}

# knowledge_base frontend/components/ParsingLanesCard.tsx 의 LANE_LABEL 과 1:1 동일
# (plan v8 재확인 — markdownify 항목 포함, html_native 는 "구 로그"용으로 남은 옛 이름).
LANE_LABEL = {
    "odl": "ODL", "paddle_gw": "스캔(GW)", "vl": "VL",
    "vl_ocr_direct": "VL 직접", "markdownify": "Markdownify",
    "html_native": "HTML(구 로그)", "kordoc_native": "kordoc",
    "text_native": "텍스트", "excel_openpyxl": "Excel(openpyxl)",
    "excel_kordoc": "Excel(kordoc)", "excel_auto": "Excel(auto)",
    "excel_unknown": "Excel", "skip": "스킵",
}

# knowledge_base frontend/lib/tableHtml.ts 의 sanitizer 를 그대로 포팅(보안에 민감한
# 로직을 두 언어로 복붙 — 한쪽만 고쳐지는 걸 막기 위해 규칙을 1:1로 맞춘다).
_ALLOWED_TABLE_TAGS = {"table", "thead", "tbody", "tr", "th", "td", "br",
                       "strong", "em", "b", "i"}

_TAG_RE = re.compile(r"<\/?\s*([a-zA-Z][a-zA-Z0-9]*)\b[^>]*>")
_SCRIPT_STYLE_RE = re.compile(r"<\s*(script|style)\b[\s\S]*?<\s*/\s*\1\s*>", re.IGNORECASE)
_TABLE_SEGMENT_RE = re.compile(r"<\s*table\b[\s\S]*?<\s*/\s*table\s*>", re.IGNORECASE)

#: facade job_id 형식(uuid.uuid4()) 허용치. 사용자 입력 경로(`/result/{job_id}`)
#: URL 경로파라미터·`submitted` 쿼리파라미터 둘 다 이걸로 검증한다(반사형 XSS 방지,
#: 2026-08-19 보안 리뷰 — outbound URL 조립에도 쓰이므로 escape만으로는 부족하다).
_JOB_ID_RE = re.compile(r"[0-9a-fA-F-]{1,64}")


def _escape(s: str) -> str:
    return html.escape(s, quote=False)


def _sanitize_table_html(raw: str) -> str:
    cleaned = _SCRIPT_STYLE_RE.sub("", raw)
    out: list[str] = []
    last = 0
    for m in _TAG_RE.finditer(cleaned):
        if m.start() > last:
            out.append(_escape(cleaned[last:m.start()]))
        name = m.group(1).lower()
        is_closing = m.group(0).lstrip().startswith("</")
        if name in _ALLOWED_TABLE_TAGS:
            if name == "br":
                out.append("<br/>")
            else:
                out.append(f"</{name}>" if is_closing else f"<{name}>")
        last = m.end()
    if last < len(cleaned):
        out.append(_escape(cleaned[last:]))
    return "".join(out)


def _split_table_segments(text: str) -> list[tuple[str, str]]:
    segments: list[tuple[str, str]] = []
    last = 0
    for m in _TABLE_SEGMENT_RE.finditer(text):
        if m.start() > last:
            segments.append(("text", text[last:m.start()]))
        segments.append(("table", m.group(0)))
        last = m.end()
    if last < len(text):
        segments.append(("text", text[last:]))
    return segments


def _render_page_text(text: str) -> str:
    if not text:
        return "<span class='muted'>(추출된 텍스트 없음)</span>"
    parts = []
    for kind, value in _split_table_segments(text):
        if kind == "table":
            parts.append(f"<div class='parser-test-table'>{_sanitize_table_html(value)}</div>")
        else:
            parts.append(f"<div style='white-space:pre-wrap'>{_escape(value)}</div>")
    return "".join(parts)


def _lane_label(lane: str) -> str:
    return LANE_LABEL.get(lane, lane)


def _gate_banner(gate: dict | None) -> str:
    if gate is None:
        return ""
    finds = [(s.get("sheet", ""), f) for s in gate.get("sheets", []) for f in s.get("findings", [])]
    if not finds:
        return (
            "<div style='background:#e6f4ea;border:1px solid #188038;padding:8px 14px;"
            "margin:8px 0;border-radius:6px'><b style='color:#188038'>✓ doc_guard 통과</b>"
            " — 검출된 규칙 없음</div>"
        )
    items = []
    for sh, f in finds:
        code = f.get("code", "")
        label = _RULE_LABELS.get(code, code)
        cells = f.get("cells") or []
        cell_s = f" — 셀/대상: {_escape(', '.join(str(c) for c in cells[:8]))}" if cells else ""
        items.append(
            f"<li><b>{_escape(label)}</b>(<code>{_escape(code)}</code>)"
            f" [시트: {_escape(sh)}] {_escape(f.get('detail', ''))}{cell_s}</li>"
        )
    return (
        "<div style='background:#fdecea;border:2px solid #d93025;padding:10px 14px;"
        "margin:8px 0;border-radius:6px'>"
        f"<b style='color:#d93025;font-size:15px'>⚠️ doc_guard 검출되었습니다</b>"
        f" — 규칙 {len(finds)}건<ul style='margin:6px 0 0 18px'>{''.join(items)}</ul></div>"
    )


def _fmt_ms(value) -> str:
    """분이 있으면 "M분 S초", 없으면 "S초"만(사용자 요청, 2026-08-19) — 값 자체가
    없으면 '—'(빈 문자열 아님, "0"으로 오인하지 않게 명시적으로 표시)."""
    if value is None:
        return "—"
    total_seconds = float(value) / 1000.0
    minutes, seconds = divmod(round(total_seconds), 60)
    return f"{minutes}분 {seconds}초" if minutes else f"{seconds}초"


def _parse_iso(value: str | None):
    if not value:
        return None
    from datetime import datetime
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _job_duration_str(job: dict) -> str:
    """created_at→completed_at 경과시간을 "M분 S초"로. 아직 안 끝났으면 '—'."""
    start = _parse_iso(job.get("created_at"))
    end = _parse_iso(job.get("completed_at"))
    if start is None or end is None:
        return "—"
    return _fmt_ms((end - start).total_seconds() * 1000.0)


def _fmt_attempts(attempts) -> str:
    """`attempts`(route/triage/gw 단계별 [stage, outcome, meta] 리스트)를 한 칸에
    담는다 — parse_service/parsers/pdf/__init__.py `_fmt_attempts` 관례를 그대로
    포팅(`stage:outcome(detail)` 을 " → "로 이음, 사용자 요청 2026-08-19)."""
    parts = []
    for a in attempts or []:
        stage, outcome = a[0], a[1]
        meta = a[2] if len(a) > 2 else {}
        detail = meta.get("reason") or meta.get("error") or ""
        parts.append(f"{stage}:{outcome}" + (f"({detail})" if detail else ""))
    return " → ".join(parts) if parts else "—"


def _page_traces_table(traces: list[dict]) -> str:
    if not traces:
        return ""
    rows = []
    for t in traces:
        lane = t.get("lane") or ""
        rows.append(
            "<tr>"
            f"<td>{_escape(str(t.get('page_number', '')))}</td>"
            f"<td>{_escape(str(t.get('bucket') or '—'))}</td>"
            f"<td>{_escape(_lane_label(lane))}</td>"
            f"<td>{_escape(str(t.get('source') or ''))}</td>"
            f"<td>{_escape(str(t.get('verdict') or ''))}</td>"
            f"<td>{_escape(str(t.get('chars', '')))}</td>"
            f"<td>{_fmt_ms(t.get('processing_ms'))}</td>"
            f"<td>{_escape(_fmt_attempts(t.get('attempts')))}</td>"
            "</tr>"
        )
    return (
        "<div class='parser-test-table'><table>"
        "<tr><th>#</th><th>bucket</th><th>lane</th><th>source</th><th>verdict</th>"
        "<th>자수</th><th>처리시간</th><th>시도</th></tr>"
        + "".join(rows) + "</table></div>"
    )


def _merge_page_text(parse_result: dict[str, Any]) -> None:
    """knowledge_base backend/app/routers/parser_test.py `_merge_page_text` 와 동일 —
    Python(코드포인트) 오프셋으로 여기서 바로 슬라이스한다(UTF-16 불일치 회피)."""
    content = parse_result.get("enriched_content") or ""
    spans_by_page = {s["page_number"]: s for s in (parse_result.get("page_spans") or [])}
    for p in (parse_result.get("pages") or []):
        span = spans_by_page.get(p.get("page_number"))
        p["text"] = content[span["char_start"]:span["char_end"]] if span else ""


def _render_result(result: dict[str, Any]) -> str:
    parts = ["<a href='/'>← 뒤로</a>"]
    # 도메인 실패(2026-08-19 실사용 중 발견) — 잡 큐는 "파싱 실패는 잡 실패가
    # 아니다" 계약이라(service/jobs/runner.py is_domain_failure) job 자체는
    # status=succeeded로 끝나지만, 이 result 본문은 {"status":"failed","detail":...}
    # 형태다(page_count/chunks/pages 등 정상 응답 키가 전혀 없음). 이 표시 없이
    # 그대로 렌더하면 오류 원인이 안 보이고 빈 결과처럼만 보인다.
    if result.get("status") == "failed":
        parts.append(
            "<div style='background:#fdecea;border:2px solid #d93025;padding:10px 14px;"
            "margin:8px 0;border-radius:6px'><b style='color:#d93025'>⚠️ 파싱 실패"
            "(도메인)</b> — 잡 자체는 완료됐지만 파서가 실패를 보고했습니다."
            f"<pre style='white-space:pre-wrap;margin:6px 0 0'>{_escape(str(result.get('detail') or ''))}"
            "</pre></div>"
        )
    traces = result.get("page_traces") or []
    if traces:
        parts.append("<h3>파싱 레인 로그</h3>")
        # ODL/kordoc/markdownify 등 다수 레인은 페이지별 processing_ms가 없다(문서
        # 단위 파서라 페이지로 안 쪼개짐) — knowledge_base ParsingLanesCard.tsx와
        # 동일하게 문서 전체 파서 처리시간(timing_metrics.total_ms)을 별도로 보여준다.
        total_ms = (result.get("timing_metrics") or {}).get("total_ms")
        if total_ms is not None:
            parts.append(f"<p>문서 파싱 처리시간: {_fmt_ms(total_ms)}</p>")
        parts.append(_page_traces_table(traces))

    if result.get("chunk_needed") is False:
        # parse-svc excel RouteResult.chunks 실측 스키마(8600 excel-parser 자체 청크
        # 스키마와 다르다): chunk_index/text/titles_context/pages — chunk_type/sheet/
        # path/content_text 아님.
        chunks = result.get("chunks") or []
        parts.append(_gate_banner(result.get("gate_summary")))
        parts.append(f"<p>{len(chunks)} 청크</p>")
        rows = []
        for c in chunks:
            idx = c.get("chunk_index", "")
            titles = " &gt; ".join(_escape(str(t)) for t in (c.get("titles_context") or []))
            pages = ", ".join(str(p) for p in (c.get("pages") or []))
            content = _escape(c.get("text") or "")
            rows.append(
                f"<tr><td>{_escape(str(idx))}</td><td>{titles}</td>"
                f"<td>{_escape(pages)}</td>"
                f"<td><pre style='margin:0;white-space:pre-wrap'>{content}</pre></td></tr>"
            )
        parts.append(
            "<table border='1' cellpadding='4' style='border-collapse:collapse;font-size:13px;width:100%'>"
            "<tr style='background:#eee'><th>#</th><th>titles_context</th><th>pages</th><th>content</th></tr>"
            + "".join(rows) + "</table>"
        )
    else:
        _merge_page_text(result)
        pages = result.get("pages") or []
        if not pages:
            parts.append("<p class='muted'>이 결과에는 페이지가 없습니다.</p>")
        else:
            for p in pages:
                parts.append(
                    f"<div style='margin:0.8rem 0'><div style='color:#666;font-size:0.85rem'>"
                    f"페이지 {_escape(str(p.get('page_number', '')))}</div>"
                    f"{_render_page_text(p.get('text') or '')}</div>"
                )
    return "".join(parts)


async def _facade_headers() -> dict[str, str]:
    return {"X-Facade-Key": FACADE_KEY} if FACADE_KEY else {}


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def index(submitted: str | None = Query(None)) -> str:
    history = await _render_history_summary()
    worker_status = await _render_worker_status()
    # 반사형 XSS 방지(2026-08-19, 보안 리뷰) — submitted 는 쿼리파라미터라 사용자가
    # 임의 문자열을 실어 보낼 수 있다. job_id 는 항상 facade uuid.uuid4() 형식이므로
    # 그 형식이 아니면 배너 자체를 안 그린다(escape만으로 방어하지 않는다 — href
    # 속성값 인코딩 규칙이 텍스트 escape와 달라 실수하기 쉽다).
    banner = ""
    if submitted and _JOB_ID_RE.fullmatch(submitted):
        safe_id = _escape(submitted)
        banner = (
            f"<div style='background:#e8f0fe;border:1px solid #4285f4;padding:8px 14px;"
            f"margin:8px 0;border-radius:6px'>제출됨 — <a href='/result/{safe_id}'>"
            f"{_escape(submitted[:8])}…</a> (아래 목록에도 뜬다, 진행 중이면 잠시 후 새로고침)</div>"
        )
    return f"""<!doctype html><meta charset="utf-8"><title>PARSER 테스트</title>
<style>
body{{font-family:sans-serif;max-width:960px;margin:2rem auto;padding:0 1rem}}
.parser-test-table table{{border-collapse:collapse;width:100%;font-size:0.85rem}}
.parser-test-table th,.parser-test-table td{{border:1px solid #000;padding:0.3rem 0.5rem;text-align:left}}
.parser-test-table th{{background:#eee}}
.muted{{color:#888}}
</style>
<h2>PARSER 테스트</h2>
<p class="muted">kb-backend/frontend 없이 facade({_escape(FACADE_URL)})에 직접 붙는
standalone 테스트 화면입니다. 무인증 — 민감한 문서는 올리지 마세요.</p>
<form action="/parse" method="post" enctype="multipart/form-data">
  <p><input type="file" name="file" required></p>
  <p>모드:
    <label><input type="radio" name="mode" value="general" checked> 이미지파서(일반/PDF/OCR/HTML/kordoc)</label>
    &nbsp;<label><input type="radio" name="mode" value="excel"> 엑셀파서</label>
  </p>
  <p><button type="submit">파싱 실행</button></p>
</form>
{banner}
{worker_status}
<h3>최근 테스트 기록</h3>
{history}
"""


def _ext_of(filename: str | None) -> str:
    if not filename or "." not in filename:
        return ""
    return filename.rsplit(".", 1)[-1].lower()


async def _fetch_jobs(*, limit: int, before_created_at: str | None = None,
                      before_id: str | None = None) -> dict[str, Any]:
    """batch_key 필터 없음(사용자 요청, 2026-08-19) — 이 UI 뿐 아니라 facade
    `/jobs/parse`를 직접 호출한 배치·스크립트 결과도 여기서 같이 보인다. kind=parse
    만 걸러서 이 화면과 무관한 chunk/insert/ingest 잡이 섞이지 않게 한다."""
    params: dict[str, Any] = {"kind": "parse", "limit": limit}
    if before_created_at and before_id:
        params["before_created_at"] = before_created_at
        params["before_id"] = before_id
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{FACADE_URL}/jobs", params=params,
                                headers=await _facade_headers())
        resp.raise_for_status()
        return resp.json()


def _jobs_table_html(jobs: list[dict]) -> str:
    """lane/page_count 는 facade `_public()`이 완료 시점에 미리 뽑아 남긴 얇은
    컬럼에서 바로 온다(2026-08-19, kbp.jobs.lanes/page_count) — result blob 을
    안 읽으므로 이 표는 /history(페이징, 배치 규모)에서 써도 느려지지 않는다."""
    if not jobs:
        return "<p class='muted'>아직 실행한 테스트가 없습니다.</p>"
    rows = []
    for j in jobs:
        filename = j.get("filename") or ""
        lanes = j.get("lanes") or []
        lanes_str = ", ".join(_lane_label(lane) for lane in lanes) if lanes else "—"
        page_count = j.get("page_count")
        rows.append(
            "<tr>"
            f"<td><a href='/result/{j['id']}'>{_escape(j['id'][:8])}…</a></td>"
            f"<td>{_escape(filename) if filename else '—'}</td>"
            f"<td>{_escape(_ext_of(filename)) or '—'}</td>"
            f"<td>{_escape(j.get('status', ''))}</td>"
            f"<td>{_escape(lanes_str)}</td>"
            f"<td>{_escape(str(page_count)) if page_count is not None else '—'}</td>"
            f"<td>{_escape(j.get('created_at') or '')}</td>"
            f"<td>{_escape(j.get('completed_at') or '')}</td>"
            f"<td>{_job_duration_str(j)}</td>"
            "</tr>"
        )
    return (
        "<table border='1' cellpadding='4' style='border-collapse:collapse;font-size:13px;width:100%'>"
        "<tr style='background:#eee'><th>job_id</th><th>파일명</th><th>확장자</th>"
        "<th>상태</th><th>lane</th><th>문서 페이지수</th><th>생성</th><th>완료</th>"
        "<th>처리시간</th></tr>"
        + "".join(rows) + "</table>"
    )


async def _render_history_summary(limit: int = 10) -> str:
    try:
        jobs = (await _fetch_jobs(limit=limit)).get("jobs") or []
    except Exception as exc:  # noqa: BLE001 — 목록 조회 실패는 표시만 생략
        return f"<p class='muted'>기록을 불러오지 못했습니다: {_escape(str(exc))}</p>"
    return _jobs_table_html(jobs) + "<p><a href='/history'>전체 기록 보기</a></p>"


async def _render_worker_status() -> str:
    """facade-worker 온라인/큐 상태 요약 — 배치 대량 처리 중 진행 상황 확인용
    (사용자 요청, 2026-08-19)."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{FACADE_URL}/jobs/workers", headers=await _facade_headers())
            resp.raise_for_status()
            w = resp.json()
    except Exception as exc:  # noqa: BLE001 — 조회 실패는 표시만 생략
        return f"<p class='muted'>worker 상태를 불러오지 못했습니다: {_escape(str(exc))}</p>"
    online = w.get("online")
    dot = "🟢" if online else "🔴"
    return (
        f"<p>{dot} worker {'online' if online else 'offline'} — "
        f"capacity {_escape(str(w.get('capacity')))} · "
        f"active {_escape(str(w.get('active')))} · "
        f"available {_escape(str(w.get('available')))} · "
        f"queued {_escape(str(w.get('queued')))} · "
        f"processing {_escape(str(w.get('processing')))}"
        + (f" · 대기 최고령 {_escape(str(w.get('oldest_queued_age_seconds')))}초"
           if w.get("oldest_queued_age_seconds") is not None else "")
        + "</p>"
    )


@app.get("/history", response_class=HTMLResponse)
async def history(before_created_at: str | None = Query(None),
                  before_id: str | None = Query(None)) -> str:
    # 배치로 대량 처리(수십만~수백만 건) 예정이라 keyset 페이징(2026-08-19) —
    # OFFSET 이 아니라 "이 페이지 마지막 행보다 오래된 것"으로 이어붙인다.
    PAGE_SIZE = 50
    try:
        page = await _fetch_jobs(limit=PAGE_SIZE, before_created_at=before_created_at,
                                 before_id=before_id)
    except Exception as exc:  # noqa: BLE001
        return (f"<!doctype html><meta charset='utf-8'><title>테스트 기록</title>"
                f"<a href='/'>← 뒤로</a><p>기록을 불러오지 못했습니다: {_escape(str(exc))}</p>")
    jobs = page.get("jobs") or []
    next_cursor = page.get("next_cursor")
    nav = []
    if before_created_at or before_id:
        nav.append("<a href='/history'>← 처음으로</a>")
    if next_cursor:
        nav.append(
            f"<a href='/history?before_created_at={_escape(str(next_cursor['before_created_at']))}"
            f"&before_id={_escape(str(next_cursor['before_id']))}'>다음 페이지 →</a>"
        )
    nav_html = " · ".join(nav)
    worker_status = await _render_worker_status()
    return (
        "<!doctype html><meta charset='utf-8'><title>테스트 기록</title>"
        f"<a href='/'>← 뒤로</a><h2>최근 테스트 기록</h2>{worker_status}"
        f"{_jobs_table_html(jobs)}<p>{nav_html}</p>"
    )


@app.post("/parse")
async def parse(file: UploadFile = File(...), mode: str = Form("general")) -> RedirectResponse:
    # 이 화면 자체의 업로드 크기 제한은 없다(사용자 요청, 2026-08-19) — facade
    # 자신의 KBP_JOB_MAX_UPLOAD_BYTES(기본 50MB)가 실질적인 상한으로 남는다.
    data = await file.read()
    docs_id = f"admtest-{uuid4().hex[:12]}"
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.post(
                f"{FACADE_URL}/jobs/parse",
                files={"file": (file.filename or "upload", data, file.content_type)},
                data={"content_type": file.content_type or "", "docs_id": docs_id,
                     "batch_key": BATCH_KEY},
                headers=await _facade_headers(),
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    job_id = resp.json()["job_id"]
    # 사용자 요청(2026-08-19): 제출 즉시 /result 로 넘어가 폴링 화면을 보여주는 대신
    # 목록(/)으로 돌아가 "최근 테스트 기록"에 새 행이 뜬 것만 보면 되게 한다 — 완료된
    # 결과를 보려면 그 목록의 job_id 링크를 누른다.
    return RedirectResponse(f"/?submitted={job_id}", status_code=303)


@app.get("/result/{job_id}", response_class=HTMLResponse)
async def result(job_id: str, since: int | None = Query(None)) -> str:
    # job_id 는 URL 경로 파라미터라 사용자가 임의 문자열을 실어 보낼 수 있다(반사형
    # XSS 방지, 2026-08-19 보안 리뷰) — 아래 <meta refresh> 에 그대로 다시 실리고
    # facade 로 나가는 URL 조립에도 쓰이므로, facade uuid.uuid4() 형식이 아니면
    # 여기서 즉시 거절한다(escape만으로는 outbound URL 조립 쪽을 못 지킨다).
    if not _JOB_ID_RE.fullmatch(job_id):
        return _error_page("잘못된 job_id 형식입니다.")

    now = int(time.time())
    if since is None:
        since = now

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get(f"{FACADE_URL}/jobs/{job_id}", headers=await _facade_headers())
        except httpx.HTTPError as exc:
            return _error_page(f"조회 실패: {_escape(str(exc))}")

    if resp.status_code == 404:
        return _error_page("이 잡을 찾을 수 없습니다 — 오래된 테스트 기록은 GC로 정리됩니다.")
    resp.raise_for_status()
    row = resp.json()
    status = row.get("status")

    if status not in TERMINAL:
        if now - since > POLL_TIMEOUT_SECONDS:
            return (
                "<!doctype html><meta charset='utf-8'><title>시간 초과</title>"
                "<a href='/'>← 뒤로</a><p>시간 초과 — 잡은 계속 처리 중일 수 있습니다, "
                f"<a href='/history'>/history</a>에서 나중에 다시 확인하세요.</p>"
            )
        return (
            "<!doctype html><meta charset='utf-8'><title>진행 중…</title>"
            f"<meta http-equiv='refresh' content='2;url=/result/{job_id}?since={since}'>"
            f"<a href='/'>← 뒤로</a><p>진행 중… (status={_escape(str(status))})</p>"
        )

    if status != "succeeded":
        return _error_page(f"파싱 실패: {_escape(str(row.get('error')))}")

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            result_resp = await client.get(
                f"{FACADE_URL}/jobs/{job_id}/result", headers=await _facade_headers(),
            )
        except httpx.HTTPError as exc:
            return _error_page(f"결과 조회 실패: {_escape(str(exc))}")
    if result_resp.status_code != 200:
        return _error_page(f"결과를 가져오지 못했습니다({result_resp.status_code}): "
                           f"{_escape(result_resp.text)}")

    body = _render_result(result_resp.json())
    return f"<!doctype html><meta charset='utf-8'><title>파싱 결과</title>{_table_style()}{body}"


def _error_page(message: str) -> str:
    return f"<!doctype html><meta charset='utf-8'><title>오류</title><a href='/'>← 뒤로</a><pre>{message}</pre>"


def _table_style() -> str:
    return (
        "<style>.parser-test-table table{border-collapse:collapse;width:100%;font-size:0.85rem}"
        ".parser-test-table th,.parser-test-table td{border:1px solid #000;padding:0.3rem 0.5rem;"
        "text-align:left}.parser-test-table th{background:#eee}</style>"
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8601)
