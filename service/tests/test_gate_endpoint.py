"""facade 게이트 API — doc_guard 은닉.

지키는 계약:
  * **응답을 변형하지 않는다** — 소비자가 doc_guard 원형 필드를 직접 읽는다
  * 소비자가 doc_guard 주소를 알 필요가 없다
  * `X-Facade-Key` 게이트 대상
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from service.app import app, get_doc_guard

# doc_guard 원형 응답(실측 2026-08-04, :8001/v1/check-excel).
REPORT = {
    "result": "pass",
    "summary": {"error": 0, "warning": 0},
    "skipped_rules": [],
    "findings": [],
    "customer_message": "검출된 위반 항목이 없습니다.",
}
RULES = [
    {"rule_id": "6.3", "name": "교차형 테이블", "severity": "error",
     "formats": ["docx", "pdf", "xlsx"], "is_llm": True},
]


class FakeDocGuard:
    def __init__(self, report=None, rules=None, raises=None):
        self.report = report if report is not None else REPORT
        self.rules = rules if rules is not None else RULES
        self.raises = raises
        self.calls = []

    def check_excel(self, *, filename, gate_summary):
        self.calls.append({"filename": filename, "gate_summary": gate_summary})
        if self.raises:
            raise self.raises
        return self.report

    def list_rules(self):
        if self.raises:
            raise self.raises
        return self.rules


@pytest.fixture()
def fake():
    dg = FakeDocGuard()
    app.dependency_overrides[get_doc_guard] = lambda: dg
    yield dg
    app.dependency_overrides.pop(get_doc_guard, None)


def test_check_excel_passes_report_through_unchanged(fake):
    """정규화하면 소비자의 _build_gate_popup 과 프론트가 함께 깨진다."""
    r = TestClient(app).post("/gate/check-excel",
                             json={"filename": "t.xlsx", "gate_summary": {"a": 1}})
    assert r.status_code == 200
    assert r.json() == REPORT           # 필드 추가·제거·rename 모두 금지


def test_check_excel_forwards_arguments(fake):
    TestClient(app).post("/gate/check-excel",
                         json={"filename": "표 (1).xlsx",
                               "gate_summary": {"merged_cells": 3}})
    assert fake.calls[0]["filename"] == "표 (1).xlsx"     # 한글·공백 보존
    assert fake.calls[0]["gate_summary"] == {"merged_cells": 3}


def test_check_excel_surfaces_fail_verdict(fake):
    """차단 판정도 그대로 통과시킨다 — 소비자가 result 로 분기한다."""
    fake.report = {"result": "fail", "findings": [{"message": "취소선"}],
                   "customer_message": "수정 후 재업로드하세요."}
    body = TestClient(app).post("/gate/check-excel",
                                json={"filename": "t.xlsx", "gate_summary": {}}).json()
    assert body["result"] == "fail"
    assert body["findings"][0]["message"] == "취소선"


def test_rules_passes_catalog_through(fake):
    r = TestClient(app).get("/gate/rules")
    assert r.status_code == 200 and r.json() == RULES


def test_missing_body_is_422(fake):
    assert TestClient(app).post("/gate/check-excel", json={}).status_code == 422


def test_gate_routes_are_key_gated(monkeypatch, fake):
    """다른 stateful 경로와 동일하게 X-Facade-Key 대상이다."""
    monkeypatch.setenv("KBP_FACADE_KEY", "s3cret")
    c = TestClient(app)
    assert c.get("/gate/rules").status_code == 401
    assert c.get("/gate/rules", headers={"X-Facade-Key": "s3cret"}).status_code == 200
    assert c.post("/gate/check-excel",
                  json={"filename": "t.xlsx", "gate_summary": {}}).status_code == 401


def test_doc_guard_failure_surfaces_as_5xx(fake):
    """소비자가 '게이트를 못 돌렸다'를 알 수 있어야 한다 — 조용히 pass 로 흡수 금지."""
    import httpx

    fake.raises = httpx.ConnectError("doc_guard down")
    with pytest.raises(httpx.ConnectError):
        TestClient(app).get("/gate/rules")
