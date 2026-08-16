"""wait_for_job — DB 순간 장애(repo.get 예외)가 폴링 루프를 죽이지 않는지.

실측 사고(2026-08-16, facade-kbp.log): `repo.get(job_id)` 도중 psycopg
`ConnectionTimeout`이 나 `_legacy_job` 밖으로 새어, 호출자(kb-backend)는 연결이
끊긴 것으로 관측했다(`RemoteProtocolError`) — 그 사이 다운스트림(parse-svc)은 실제로
계속 처리해 결국 성공했다. `wait_for_job`이 `repo.get()` 예외를 흡수하고 다음 폴에서
재시도하면, 이런 순간 장애가 작업 전체를 고아로 만들지 않는다.

설계: `~/.claude/plans/facade-job-wait-transient-retry.md` (v2 READY).
"""
from __future__ import annotations

import time
import uuid

import psycopg
import pytest
from fastapi import HTTPException

from service.jobs.api import TERMINAL, wait_for_job


class _FlakyRepo:
    """`get()`이 지정한 횟수만큼 예외를 던진 뒤 정상 row 를 반환하는 fake repo."""

    def __init__(self, *, raises: Exception, fail_times: int, row: dict) -> None:
        self._raises = raises
        self._fail_times = fail_times
        self._row = row
        self.calls = 0

    def get(self, job_id):
        self.calls += 1
        if self.calls <= self._fail_times:
            raise self._raises
        return self._row


class _AlwaysFailingRepo:
    """`get()`이 매번 예외를 던지는 fake repo(DB 완전 다운 시뮬레이션)."""

    def __init__(self, *, raises: Exception) -> None:
        self._raises = raises
        self.calls = 0

    def get(self, job_id):
        self.calls += 1
        raise self._raises


class _VanishedRowRepo:
    """`get()`이 예외 없이 항상 None 을 반환하는 fake repo(행 실종 시뮬레이션)."""

    def get(self, job_id):
        return None


def _terminal_row(status: str = "succeeded") -> dict:
    assert status in TERMINAL
    return {"id": uuid.uuid4(), "status": status, "result": {"ok": True}}


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch):
    """폴 간격/실제 sleep 을 테스트에서 없앤다(빠르게 여러 iteration 소진)."""
    monkeypatch.setenv("KBP_JOB_WAIT_POLL_INTERVAL_SECONDS", "0")
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)


def test_psycopg_operational_error_is_absorbed_then_succeeds():
    """§검증 V3 — 실측 사고와 같은 psycopg 예외 클래스를 흡수하고 재시도 후 성공."""
    row = _terminal_row()
    repo = _FlakyRepo(
        raises=psycopg.OperationalError("connection timeout expired"),
        fail_times=2,
        row=row,
    )
    out = wait_for_job(repo, blobs=None, job_id=row["id"], timeout=5.0)
    assert out is row
    assert repo.calls == 3  # 실패 2회 + 성공 1회


def test_generic_exception_is_also_absorbed_then_succeeds():
    """넓은 `except Exception`이 psycopg 전용이 아님을 증명 — 무관한 예외도 흡수."""
    row = _terminal_row()
    repo = _FlakyRepo(raises=RuntimeError("boom"), fail_times=1, row=row)
    out = wait_for_job(repo, blobs=None, job_id=row["id"], timeout=5.0)
    assert out is row
    assert repo.calls == 2


def test_permanent_db_outage_still_ends_in_409_not_infinite_wait():
    """DB 가 정말 죽어 있으면(계속 예외) 여전히 deadline 에서 409 로 종결(무한 대기 아님)."""
    repo = _AlwaysFailingRepo(raises=psycopg.OperationalError("connection timeout expired"))
    with pytest.raises(HTTPException) as exc_info:
        wait_for_job(repo, blobs=None, job_id=uuid.uuid4(), timeout=0.0)
    assert exc_info.value.status_code == 409
    assert repo.calls >= 1


def test_vanished_row_still_raises_500_immediately_not_swallowed():
    """행 실종(None, 예외 아님)은 여전히 즉시 500 — 예외 케이스와 안 섞임(회귀 가드)."""
    repo = _VanishedRowRepo()
    with pytest.raises(HTTPException) as exc_info:
        wait_for_job(repo, blobs=None, job_id=uuid.uuid4(), timeout=5.0)
    assert exc_info.value.status_code == 500
