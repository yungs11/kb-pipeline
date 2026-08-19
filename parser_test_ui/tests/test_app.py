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


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_upload_over_size_limit_returns_413(client, monkeypatch):
    monkeypatch.setattr(app_mod, "MAX_UPLOAD_BYTES", 10)
    resp = client.post(
        "/parse", files={"file": ("big.pdf", b"x" * 100, "application/pdf")},
        data={"mode": "general"},
    )
    assert resp.status_code == 413


def test_submit_excel_job_redirects_to_index(client):
    def fake_post(url, files, data):
        assert url.endswith("/jobs/parse")
        assert data["batch_key"] == "parser-test-ui"
        return _FakeResponse(202, {"job_id": "job-excel-1", "status": "queued"})

    _HANDLER["post"] = fake_post
    resp = client.post(
        "/parse", files={"file": ("t.xlsx", b"fake-xlsx", "application/vnd.ms-excel")},
        data={"mode": "excel"}, follow_redirects=False,
    )
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location == "/?submitted=job-excel-1", "제출 후 /result 로 넘어가지 않고 목록(/)으로 돌아가야 한다(사용자 요청)"


def test_excel_success_renders_chunk_table_and_gate_banner(client):
    def fake_get(url, params):
        if url.endswith("/jobs/job-excel-1"):
            return _FakeResponse(200, {"id": "job-excel-1", "status": "succeeded"})
        if url.endswith("/jobs/job-excel-1/result"):
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
    resp = client.get("/result/job-excel-1")
    assert resp.status_code == 200
    body = resp.text
    assert "excel_openpyxl" not in body or "Excel(openpyxl)" in body  # LANE_LABEL 변환 확인
    assert "doc_guard 검출되었습니다" in body
    assert "참조 오류" in body
    assert "a,b,c" in body


def test_general_success_renders_page_traces_and_page_text(client):
    def fake_get(url, params):
        if url.endswith("/jobs/job-gen-1"):
            return _FakeResponse(200, {"id": "job-gen-1", "status": "succeeded"})
        if url.endswith("/jobs/job-gen-1/result"):
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
    resp = client.get("/result/job-gen-1")
    assert resp.status_code == 200
    body = resp.text
    assert "ODL" in body
    assert "hello page one" in body
    assert "0분 1초" in body, "문서 단위 총 처리시간이 M분 S초 형식으로 보여야 한다(페이지별 processing_ms가 없는 레인 대응)"
    assert "—" in body, "페이지별 processing_ms 없을 때 빈 칸이 아니라 대시로 표시해야 한다"


def test_result_404_shows_friendly_error(client):
    def fake_get(url, params):
        return _FakeResponse(404, {"detail": "job not found"})

    _HANDLER["get"] = fake_get
    resp = client.get("/result/does-not-exist")
    assert resp.status_code == 200
    assert "찾을 수 없습니다" in resp.text


def test_result_non_terminal_within_timeout_has_refresh_tag(client):
    def fake_get(url, params):
        return _FakeResponse(200, {"id": "job-x", "status": "running"})

    _HANDLER["get"] = fake_get
    resp = client.get("/result/job-x", params={"since": int(app_mod.time.time())})
    assert resp.status_code == 200
    assert "http-equiv='refresh'" in resp.text
    assert "since=" in resp.text


def test_result_non_terminal_past_timeout_has_no_refresh_tag(client):
    def fake_get(url, params):
        return _FakeResponse(200, {"id": "job-x", "status": "running"})

    _HANDLER["get"] = fake_get
    old_since = int(app_mod.time.time()) - app_mod.POLL_TIMEOUT_SECONDS - 10
    resp = client.get("/result/job-x", params={"since": old_since})
    assert resp.status_code == 200
    assert "시간 초과" in resp.text
    assert "refresh" not in resp.text


def test_history_lists_jobs(client):
    def fake_get(url, params):
        assert params["batch_key"] == "parser-test-ui"
        return _FakeResponse(200, {"jobs": [
            {"id": "job-abc12345", "status": "succeeded",
             "created_at": "2026-08-19T00:00:00", "completed_at": "2026-08-19T00:01:00"},
        ]})

    _HANDLER["get"] = fake_get
    resp = client.get("/history")
    assert resp.status_code == 200
    assert "job-abc" in resp.text
    assert "succeeded" in resp.text
    assert "1분 0초" in resp.text, "created_at→completed_at 경과시간이 M분 S초로 표시돼야 한다"
