"""인메모리 잡 저장소·객체저장소 — 테스트와 dev 인라인 실행용.

설계 §6.2.

기존 엔드포인트 테스트는 `TestClient` + `dependency_overrides` 로 돌고 DB·MinIO fixture 가
없다. 레거시 4경로가 잡을 경유하게 되면 그 구조가 그대로는 안 도는데, 여기 있는 더블을
`get_job_repo`/`get_job_blobs` 에 주입하고 인라인 디스패처를 켜면 기존 단언이 살아난다.

``JobRepo`` 와 동작이 다른 곳(의도적):
  * claim/heartbeat/worker 레지스트리는 구현하지 않는다 — 인라인 실행에는 필요 없다.
  * ``live_worker_count()`` 는 항상 1 을 보고한다. 0 이면 §4.4 의 fail-fast 게이트에
    걸려 모든 레거시 테스트가 503 을 받는다.
"""
from __future__ import annotations

import copy
import uuid
from typing import Any

from service.jobs.repo import LeaseLost


class InMemoryJobRepo:
    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, dict[str, Any]] = {}

    # ── 제출·조회 ──────────────────────────────────────────────────────────

    def submit(self, *, kind, payload=None, payload_ref=None, input_ref=None,
               workspace_key=None, batch_key=None, parent_job_id=None,
               legacy=False, job_id=None, idem_key=None) -> uuid.UUID:
        if idem_key is not None:
            for row in self.rows.values():
                if row.get("idem_key") == idem_key:
                    return row["id"]     # 충돌 → 기존 잡 재사용
        job_id = job_id or uuid.uuid4()
        self.rows[job_id] = {
            "id": job_id, "kind": kind, "status": "queued", "stage": None,
            "idem_key": idem_key,
            "workspace_key": workspace_key, "batch_key": batch_key,
            "parent_job_id": _as_uuid(parent_job_id), "legacy": legacy,
            "payload": copy.deepcopy(payload), "payload_ref": payload_ref,
            "input_ref": input_ref, "result": None, "result_ref": None,
            "error": None, "attempt_count": 0, "cancel_requested": False,
            "claimed_by": None, "claimed_at": None, "heartbeat_at": None,
            "created_at": None, "started_at": None, "completed_at": None,
        }
        return job_id

    def get(self, job_id) -> dict[str, Any] | None:
        row = self.rows.get(_as_uuid(job_id))
        return copy.deepcopy(row) if row else None

    def list_jobs(self, *, workspace_key=None, batch_key=None, status=None,
                  kind=None, limit=100) -> list[dict[str, Any]]:
        out = []
        for row in self.rows.values():
            if workspace_key is not None and row["workspace_key"] != workspace_key:
                continue
            if batch_key is not None and row["batch_key"] != batch_key:
                continue
            if status is not None and row["status"] != status:
                continue
            if kind is not None and row["kind"] != kind:
                continue
            out.append(copy.deepcopy(row))
        return out[:limit]

    def ahead_in_partition(self, job) -> int:
        return 0

    # ── 인라인 실행이 쓰는 lease 전이 ──────────────────────────────────────

    def start(self, job_id, *, worker_id: str) -> int:
        """인라인 디스패처용 claim 상당. 승인 판정 없이 바로 running 으로 만든다."""
        row = self.rows[_as_uuid(job_id)]
        row["attempt_count"] += 1
        row["status"] = "running"
        row["claimed_by"] = worker_id
        return row["attempt_count"]

    def set_stage(self, job_id, *, worker_id, attempt, stage) -> None:
        self._fenced(job_id, worker_id, attempt)["stage"] = stage

    def complete(self, job_id, *, worker_id, attempt, status,
                 result=None, result_ref=None, error=None) -> None:
        row = self._fenced(job_id, worker_id, attempt)
        row.update(status=status, result=copy.deepcopy(result),
                   result_ref=result_ref, error=error, stage=None)
        if status != "succeeded":
            row["idem_key"] = None   # 실패는 캐시하지 않는다

    def requeue(self, job_id, *, worker_id, attempt, error) -> None:
        row = self._fenced(job_id, worker_id, attempt)
        row.update(status="queued", error=error, claimed_by=None, stage=None)

    def _fenced(self, job_id, worker_id, attempt) -> dict[str, Any]:
        row = self.rows.get(_as_uuid(job_id))
        if (row is None or row["claimed_by"] != worker_id
                or row["attempt_count"] != attempt or row["status"] != "running"):
            raise LeaseLost(f"lease lost (job={job_id})")
        return row

    # ── 취소 ───────────────────────────────────────────────────────────────

    def cancel(self, job_id) -> str | None:
        row = self.rows.get(_as_uuid(job_id))
        if row is None or row["status"] not in {"queued", "running"}:
            return None
        row["cancel_requested"] = True
        row["idem_key"] = None
        if row["status"] == "queued":
            row["status"] = "canceled"
            return "canceled"
        return "running"

    def is_cancel_requested(self, job_id) -> bool:
        row = self.rows.get(_as_uuid(job_id))
        return bool(row and row["cancel_requested"])

    # ── 관측 ───────────────────────────────────────────────────────────────

    def worker_stats(self, *, alive_seconds=None) -> dict[str, Any]:
        queued = sum(1 for r in self.rows.values() if r["status"] == "queued")
        running = sum(1 for r in self.rows.values() if r["status"] == "running")
        return {"online": True, "capacity": 1, "active": running,
                "available": max(0, 1 - running), "queued": queued,
                "processing": running, "oldest_queued_age_seconds": None}

    def live_worker_count(self, *, alive_seconds=None) -> int:
        # 항상 1. 0 이면 §4.4 fail-fast 에 걸려 레거시 테스트가 전부 503 이 된다.
        return 1

    def legacy_refs_to_purge(self, job_id) -> tuple[str | None, str | None]:
        row = self.rows.get(_as_uuid(job_id))
        if not row or not row["legacy"]:
            return (None, None)
        return (row["input_ref"], row["payload_ref"])


class InMemoryBlobStore:
    """MinIO 대역. 임계를 넘어도 인라인으로 두어 테스트가 바이트를 안 다루게 한다."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []

    def check_bucket(self) -> bool:
        return True

    def key(self, job_id, name) -> str:
        return f"mem/{job_id}/{name}"

    def put_bytes(self, key, data, *, content_type) -> str:
        self.objects[key] = data
        return key

    def get_bytes(self, key) -> bytes:
        if key not in self.objects:
            raise KeyError(key)
        return self.objects[key]

    def delete(self, key) -> None:
        if key:
            self.deleted.append(key)
            self.objects.pop(key, None)

    def store_json(self, job_id, name, obj):
        return (obj, None)

    def load_json(self, inline, ref):
        if ref:
            import json
            return json.loads(self.get_bytes(ref).decode("utf-8"))
        return inline


def _as_uuid(value):
    if value is None or isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))
