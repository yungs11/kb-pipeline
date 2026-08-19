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
from datetime import datetime, timedelta, timezone
from typing import Any

from service.jobs.repo import LeaseLost


class InMemoryJobRepo:
    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, dict[str, Any]] = {}
        # 야간 커뮤니티 배치(A1) 상태. 실 PG 의 kbp.graph_touch / community_builds /
        # batch_runs 에 대응한다.
        self.graph_touch: dict[str, Any] = {}
        self.community_builds: dict[str, dict[str, Any]] = {}
        self.batch_runs: dict[tuple[str, Any], dict[str, Any]] = {}

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
                  kind=None, limit=100, before_created_at=None,
                  before_id=None) -> list[dict[str, Any]]:
        # 인라인 더블은 created_at 을 안 채운다(claim 이 없어 실제 DB 타임스탬프가
        # 없음) — keyset 페이징은 real JobRepo(postgres)에서만 의미가 있다. 여기선
        # 파라미터만 받아 시그니처를 맞추고 동작은 무시한다(no-op).
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
        """인라인 디스패처용 claim 상당. 승인 판정 없이 바로 running 으로 만든다.

        `JobRepo._admit` 과 **같은 계약**이어야 한다 — community 는 claim 시점에 멱등키를
        비운다(D10). 한쪽만 고치면 프로덕션과 인라인 경로의 키 수명이 갈린다.
        """
        row = self.rows[_as_uuid(job_id)]
        row["attempt_count"] += 1
        row["status"] = "running"
        row["claimed_by"] = worker_id
        if row.get("kind") == "community":
            row["idem_key"] = None
        return row["attempt_count"]

    def set_stage(self, job_id, *, worker_id, attempt, stage) -> None:
        self._fenced(job_id, worker_id, attempt)["stage"] = stage

    def complete(self, job_id, *, worker_id, attempt, status,
                 result=None, result_ref=None, error=None,
                 clear_idem: bool = False) -> None:
        row = self._fenced(job_id, worker_id, attempt)
        row.update(status=status, result=copy.deepcopy(result),
                   result_ref=result_ref, error=error, stage=None)
        if status != "succeeded" or clear_idem:
            row["idem_key"] = None   # 실패(또는 도메인 실패 본문)는 캐시하지 않는다

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

    def cancel(self, job_id):
        """실 repo 와 **같은 계약** — dict 또는 None. `JobRepo.cancel` 참조."""
        row = self.rows.get(_as_uuid(job_id))
        if row is None or row["status"] not in {"queued", "running"}:
            return None
        if row.get("stage") == "inserting" and row["status"] == "running":
            # edgequake 에 이미 제출했다 — 여기서 멈추면 부분 적재가 남는다(D6).
            return {"status": "inserting", "stage": "inserting",
                    "input_ref": None, "payload_ref": None, "result_ref": None}
        row["cancel_requested"] = True
        row["idem_key"] = None
        out = {"stage": row.get("stage"),
               "input_ref": row.get("input_ref"),
               "payload_ref": row.get("payload_ref"),
               "result_ref": row.get("result_ref")}
        if row["status"] == "queued":
            row["status"] = "canceled"
            return {"status": "canceled", **out}
        return {"status": "running", **out}

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

    # ── 야간 커뮤니티 배치 (A1) ────────────────────────────────────────────
    # 실 Postgres 판정(SQL 의미론)은 requires_pg 테스트가 맡는다. 여기서는 러너·
    # 엔드포인트 테스트가 AttributeError 로 깨지지 않게 **같은 계약**만 제공한다.

    def db_now(self):
        return datetime.now(timezone.utc)

    def touch_graph(self, workspace_key: str) -> None:
        self.graph_touch[workspace_key] = self.db_now()

    def record_attempt(self, workspace_key: str, eq_workspace_id: str | None) -> None:
        row = self.community_builds.setdefault(workspace_key, {})
        row["last_attempt_at"] = self.db_now()
        if eq_workspace_id:
            row["eq_workspace_id"] = eq_workspace_id

    def record_community_success(self, workspace_key: str, eq_workspace_id: str,
                                 snapshot_at) -> None:
        row = self.community_builds.setdefault(workspace_key, {})
        row.update(eq_workspace_id=eq_workspace_id, last_success_at=snapshot_at,
                   finished_at=self.db_now(), status="succeeded")

    def record_community_failure(self, workspace_key: str,
                                 eq_workspace_id: str | None) -> None:
        row = self.community_builds.setdefault(workspace_key, {})
        row.update(finished_at=self.db_now(), status="failed")
        if eq_workspace_id:
            row["eq_workspace_id"] = eq_workspace_id

    def workspaces_needing_community(self, cap: int) -> tuple[list[str], int]:
        epoch = datetime.fromtimestamp(0, timezone.utc)
        cand = [
            (k, t) for k, t in self.graph_touch.items()
            if t > (self.community_builds.get(k, {}).get("last_success_at") or epoch)
        ]
        cand.sort(key=lambda kt: (
            self.community_builds.get(kt[0], {}).get("last_attempt_at") or epoch, kt[1]))
        return ([k for k, _ in cand[:cap]], len(cand))

    def has_live_community_job(self, eq_workspace_id: str) -> bool:
        return any(r["kind"] == "community" and r["status"] in ("queued", "running")
                   and r.get("workspace_key") == eq_workspace_id
                   for r in self.rows.values())

    def claim_run(self, name: str, run_date, stale_minutes: int) -> bool:
        row = self.batch_runs.get((name, run_date))
        if row is None:
            self.batch_runs[(name, run_date)] = {
                "name": name, "run_date": run_date, "run_at": self.db_now(),
                "submitted": 0, "deduped": 0, "failed": 0, "backlog": 0,
                "status": "started", "error": None}
            return True
        if row["status"] == "failed" or (
                row["status"] == "started"
                and row["run_at"] < self.db_now() - timedelta(minutes=stale_minutes)):
            row.update(run_at=self.db_now(), status="started", error=None)
            return True
        return False

    def has_stale_started(self, name: str, run_date, stale_minutes: int) -> bool:
        row = self.batch_runs.get((name, run_date))
        return bool(row and row["status"] == "started"
                    and row["run_at"] < self.db_now() - timedelta(minutes=stale_minutes))

    def cancel_nightly_queued(self, *, key=None, exclude_key=None) -> int:
        n = 0
        for row in self.rows.values():
            bk = row.get("batch_key") or ""
            if row["kind"] != "community" or row["status"] != "queued":
                continue
            if not bk.startswith("community-nightly:"):
                continue
            if key is not None and bk != key:
                continue
            if exclude_key is not None and bk == exclude_key:
                continue
            row.update(status="canceled", completed_at=self.db_now(), idem_key=None)
            n += 1
        return n

    def finish_run(self, name: str, run_date, *, submitted: int, deduped: int,
                   failed: int, backlog: int, status: str, error=None) -> None:
        row = self.batch_runs.get((name, run_date))
        if row is not None:
            row.update(submitted=submitted, deduped=deduped, failed=failed,
                       backlog=backlog, status=status, error=error)

    def last_batch_run(self, name: str, run_date):
        return self.batch_runs.get((name, run_date))


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
