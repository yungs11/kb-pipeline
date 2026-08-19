"""parser_test_ui/app.py 회귀 테스트 — facade 는 전부 mock(실제 네트워크 호출 없음).

httpx.AsyncClient(...) 를 fake 컨텍스트매니저로 치환해 facade 응답을 조작한다.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as app_mod  # noqa: E402


class _FakeResponse:
    def __init__(self, status_code: int, json_data=None, text_data: str = ""):
        self.status_code = status_code
        self._json = json_data
        self.text = text_data or (str(json_data) if json_data is not None else "")

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError("error", request=None, response=self)


class _FakeAsyncClient:
    """모듈 전역 핸들러(``_HANDLER``)로 위임하는 fake httpx.AsyncClient."""

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, params=None, headers=None):
        return _HANDLER["get"](url, params)

    async def post(self, url, files=None, data=None, headers=None):
        return _HANDLER["post"](url, files, data)


_HANDLER = {"get": None, "post": None}


@pytest.fixture(autouse=True)
def _patch_httpx(monkeypatch):
    monkeypatch.setattr(app_mod.httpx, "AsyncClient", _FakeAsyncClient)
    yield
    _HANDLER["get"] = None
    _HANDLER["post"] = None


@pytest.fixture
def client():
    return TestClient(app_mod.app)


def test_fmt_ms_omits_minutes_when_zero():
    assert app_mod._fmt_ms(None) == "—"
    assert app_mod._fmt_ms(1234.5) == "1초", "1분 미만이면 '분' 없이 초만"
    assert app_mod._fmt_ms(60000) == "1분 0초", "1분 이상이면 M분 S초"
    assert app_mod._fmt_ms(65000) == "1분 5초"


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_index_ignores_malformed_submitted_param(client):
    """반사형 XSS 방지(2026-08-19 보안 리뷰) — submitted 쿼리파라미터가 job_id
    형식이 아니면 배너 자체를 안 그린다(href 속성에 그대로 실리는 값이라
    escape만으로는 부족)."""
    resp = client.get('/?submitted=%22%3E%3Cscript%3Ealert(1)%3C/script%3E')
    assert resp.status_code == 200
    assert "<script>" not in resp.text
    assert "제출됨" not in resp.text


def test_large_upload_is_not_rejected_by_this_ui(client):
    """이 화면 자체의 업로드 크기 제한은 없다(사용자 요청, 2026-08-19) — facade
    자신의 KBP_JOB_MAX_UPLOAD_BYTES 가 실질적인 상한으로 남는다."""
    def fake_post(url, files, data):
        return _FakeResponse(202, {"job_id": "eeeeeeee-0000-0000-0000-000000000006",
                                   "status": "queued"})

    _HANDLER["post"] = fake_post
    resp = client.post(
        "/parse", files={"file": ("big.pdf", b"x" * (20 * 1024 * 1024), "application/pdf")},
        data={"mode": "general"}, follow_redirects=False,
    )
    assert resp.status_code == 303


def test_submit_excel_job_redirects_to_index(client):
    def fake_post(url, files, data):
        assert url.endswith("/jobs/parse")
        assert data["batch_key"] == "parser-test-ui"
        return _FakeResponse(202, {"job_id": "aaaaaaaa-0000-0000-0000-000000000001", "status": "queued"})

    _HANDLER["post"] = fake_post
    resp = client.post(
        "/parse", files={"file": ("t.xlsx", b"fake-xlsx", "application/vnd.ms-excel")},
        data={"mode": "excel"}, follow_redirects=False,
    )
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location == "/?submitted=aaaaaaaa-0000-0000-0000-000000000001", "제출 후 /result 로 넘어가지 않고 목록(/)으로 돌아가야 한다(사용자 요청)"


def test_excel_success_renders_chunk_table_and_gate_banner(client):
    def fake_get(url, params):
        if url.endswith("/jobs/aaaaaaaa-0000-0000-0000-000000000001"):
            return _FakeResponse(200, {"id": "aaaaaaaa-0000-0000-0000-000000000001", "status": "succeeded"})
        if url.endswith("/jobs/aaaaaaaa-0000-0000-0000-000000000001/result"):
            return _FakeResponse(200, {
                "chunk_needed": False,
                "chunks": [{"chunk_index": 0, "titles_context": ["Sheet1"],
                           "pages": [], "text": "a,b,c"}],
                "gate_summary": {"sheets": [{"sheet": "Sheet1", "findings": [
                    {"code": "ref_error", "detail": "REF!", "cells": ["A1"]},
                ]}]},
                "page_traces": [{"page_number": 1, "lane": "excel_openpyxl",
                                "source": "excel_rag_parser", "chars": 5}],
            })
        raise AssertionError(f"unexpected GET {url}")

    _HANDLER["get"] = fake_get
    resp = client.get("/result/aaaaaaaa-0000-0000-0000-000000000001")
    assert resp.status_code == 200
    body = resp.text
    assert "excel_openpyxl" not in body or "Excel(openpyxl)" in body  # LANE_LABEL 변환 확인
    assert "doc_guard 검출되었습니다" in body
    assert "참조 오류" in body
    assert "a,b,c" in body


def test_general_success_renders_page_traces_and_page_text(client):
    def fake_get(url, params):
        if url.endswith("/jobs/bbbbbbbb-0000-0000-0000-000000000002"):
            return _FakeResponse(200, {"id": "bbbbbbbb-0000-0000-0000-000000000002", "status": "succeeded"})
        if url.endswith("/jobs/bbbbbbbb-0000-0000-0000-000000000002/result"):
            return _FakeResponse(200, {
                "chunk_needed": True,
                "enriched_content": "hello page one",
                "page_spans": [{"page_number": 1, "char_start": 0, "char_end": 15}],
                "pages": [{"page_number": 1, "page_uuid": "x_1", "minio_object": None}],
                "page_traces": [
                    {"page_number": 1, "lane": "odl", "source": "odl_md", "chars": 15,
                     "processing_ms": None},
                ],
                "timing_metrics": {"total_ms": 1234.5},
            })
        raise AssertionError(f"unexpected GET {url}")

    _HANDLER["get"] = fake_get
    resp = client.get("/result/bbbbbbbb-0000-0000-0000-000000000002")
    assert resp.status_code == 200
    body = resp.text
    assert "ODL" in body
    assert "hello page one" in body
    assert "1초" in body, "문서 단위 총 처리시간이 보여야 한다(페이지별 processing_ms가 없는 레인 대응)"
    assert "—" in body, "페이지별 processing_ms 없을 때 빈 칸이 아니라 대시로 표시해야 한다"


def test_page_traces_table_shows_bucket_and_attempts_trace(client):
    """사용자 요청(2026-08-19) — 로그 표에 bucket·시도(attempts trace) 컬럼."""
    def fake_get(url, params):
        if url.endswith("/jobs/cccccccc-1111-0000-0000-000000000099"):
            return _FakeResponse(200, {"id": "cccccccc-1111-0000-0000-000000000099", "status": "succeeded"})
        if url.endswith("/jobs/cccccccc-1111-0000-0000-000000000099/result"):
            return _FakeResponse(200, {
                "chunk_needed": True,
                "enriched_content": "x", "page_spans": [], "pages": [],
                "page_traces": [{
                    "page_number": 7, "bucket": "OCR_NEEDED", "lane": "paddle_gw",
                    "source": "gw", "verdict": "accept_gw", "chars": 1405,
                    "processing_ms": None,
                    "attempts": [
                        ["triage", "OCR_NEEDED",
                         {"reason": "텍스트없는 콘텐츠 (이미지=55, content=0B) → OCR/VL"}],
                        ["gw", "ok", {"error": ""}],
                    ],
                }],
            })
        raise AssertionError(f"unexpected GET {url}")

    _HANDLER["get"] = fake_get
    resp = client.get("/result/cccccccc-1111-0000-0000-000000000099")
    assert resp.status_code == 200
    body = resp.text
    assert "OCR_NEEDED" in body
    assert "triage:OCR_NEEDED(텍스트없는 콘텐츠 (이미지=55, content=0B) → OCR/VL) → gw:ok" in body


def test_result_404_shows_friendly_error(client):
    def fake_get(url, params):
        return _FakeResponse(404, {"detail": "job not found"})

    _HANDLER["get"] = fake_get
    resp = client.get("/result/dddddddd-0000-0000-0000-000000000004")
    assert resp.status_code == 200
    assert "찾을 수 없습니다" in resp.text


def test_result_rejects_malformed_job_id_without_calling_facade(client):
    """반사형 XSS 방지(2026-08-19 보안 리뷰) — job_id 가 uuid 형식이 아니면 facade를
    부르지도 않고 즉시 거절해야 한다(<meta refresh>/outbound URL 조립에 그대로
    실리는 값이라 escape만으로는 부족)."""
    def fake_get(url, params):
        raise AssertionError("malformed job_id 인데 facade 를 불렀다")

    _HANDLER["get"] = fake_get
    resp = client.get("/result/%22%3E%3Cimg%20src=x%20onerror=alert(1)%3E")
    assert resp.status_code == 200
    assert "잘못된 job_id" in resp.text
    assert "<img" not in resp.text


def test_result_non_terminal_within_timeout_has_refresh_tag(client):
    def fake_get(url, params):
        return _FakeResponse(200, {"id": "cccccccc-0000-0000-0000-000000000003", "status": "running"})

    _HANDLER["get"] = fake_get
    resp = client.get("/result/cccccccc-0000-0000-0000-000000000003", params={"since": int(app_mod.time.time())})
    assert resp.status_code == 200
    assert "http-equiv='refresh'" in resp.text
    assert "since=" in resp.text


def test_result_non_terminal_past_timeout_has_no_refresh_tag(client):
    def fake_get(url, params):
        return _FakeResponse(200, {"id": "cccccccc-0000-0000-0000-000000000003", "status": "running"})

    _HANDLER["get"] = fake_get
    old_since = int(app_mod.time.time()) - app_mod.POLL_TIMEOUT_SECONDS - 10
    resp = client.get("/result/cccccccc-0000-0000-0000-000000000003", params={"since": old_since})
    assert resp.status_code == 200
    assert "시간 초과" in resp.text
    assert "refresh" not in resp.text


def test_history_lists_jobs(client):
    def fake_get(url, params):
        # batch_key 로 안 거른다(사용자 요청) — 이 UI 밖에서 curl 등으로 직접
        # /jobs/parse 를 호출한 실행 기록도 kind=parse 전부 보여야 한다.
        assert "batch_key" not in params
        assert params["kind"] == "parse"
        return _FakeResponse(200, {"jobs": [
            {"id": "job-abc12345", "status": "succeeded", "filename": "보고서.pdf",
             "created_at": "2026-08-19T00:00:00", "completed_at": "2026-08-19T00:01:00"},
        ]})

    _HANDLER["get"] = fake_get
    resp = client.get("/history")
    assert resp.status_code == 200
    assert "job-abc" in resp.text
    assert "succeeded" in resp.text
    assert "1분 0초" in resp.text, "created_at→completed_at 경과시간이 M분 S초로 표시돼야 한다"
    assert "보고서.pdf" in resp.text, "파일명이 표에 보여야 한다"
    assert "pdf" in resp.text, "확장자가 표에 보여야 한다"


def test_index_shows_worker_status(client):
    def fake_get(url, params):
        if url.endswith("/jobs/workers"):
            return _FakeResponse(200, {"online": True, "capacity": 4, "active": 1,
                                       "available": 3, "queued": 2, "processing": 1,
                                       "oldest_queued_age_seconds": 12.5})
        return _FakeResponse(200, {"jobs": []})

    _HANDLER["get"] = fake_get
    resp = client.get("/")
    assert resp.status_code == 200
    assert "capacity 4" in resp.text
    assert "queued 2" in resp.text
    assert "12.5" in resp.text


def test_index_recent_history_shows_lane_and_page_count(client):
    """lane/page_count는 facade _public()이 완료 시점에 미리 뽑아 남긴 얇은 컬럼에서
    바로 온다(2026-08-19) — 목록 조회 시 잡마다 result를 따로 열어보지 않는다."""
    def fake_get(url, params):
        if url.endswith("/jobs/workers"):
            return _FakeResponse(200, {"online": True})
        if url.endswith("/jobs"):
            return _FakeResponse(200, {"jobs": [
                {"id": "dddddddd-2222-0000-0000-000000000005", "status": "succeeded",
                 "filename": "a.pdf", "created_at": "2026-08-19T00:00:00",
                 "completed_at": "2026-08-19T00:00:10",
                 "lanes": ["odl", "paddle_gw"], "page_count": 3},
            ]})
        raise AssertionError(f"unexpected GET {url}")

    _HANDLER["get"] = fake_get
    resp = client.get("/")
    assert resp.status_code == 200
    assert "ODL" in resp.text
    assert "스캔(GW)" in resp.text
    assert ">3<" in resp.text, "문서 페이지수 3이 표에 보여야 한다"


def test_history_pagination_shows_next_page_link_only_when_cursor_present(client):
    def fake_get(url, params):
        assert "before_created_at" not in params
        return _FakeResponse(200, {
            "jobs": [{"id": "job-abc12345", "status": "succeeded",
                     "created_at": "2026-08-19T00:00:00", "completed_at": None}],
            "next_cursor": {"before_created_at": "2026-08-19T00:00:00", "before_id": "job-abc12345"},
        })

    _HANDLER["get"] = fake_get
    resp = client.get("/history")
    assert resp.status_code == 200
    assert "다음 페이지" in resp.text
    assert "before_created_at=2026-08-19T00%3A00%3A00" in resp.text or \
           "before_created_at=2026-08-19T00:00:00" in resp.text

    def fake_get_no_more(url, params):
        return _FakeResponse(200, {"jobs": [], "next_cursor": None})

    _HANDLER["get"] = fake_get_no_more
    resp2 = client.get("/history", params={"before_created_at": "2026-08-19T00:00:00",
                                           "before_id": "job-abc12345"})
    assert resp2.status_code == 200
    assert "다음 페이지" not in resp2.text
    assert "처음으로" in resp2.text
