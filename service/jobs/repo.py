"""``kbp.jobs`` 저장소 — 제출·claim·lease·완료. psycopg3 raw SQL.

설계 §2.3/§3.1/§3.3.

**커넥션 풀을 쓰지 않는다.** 연산마다 ``psycopg.connect(dsn)`` 으로 열고 ``finally`` 에서
닫는다. 이유 둘:

1. ``psycopg_pool`` 은 ``psycopg[binary]`` 에 포함되지 않는 **별도 배포판**이다. 폐쇄망에
   새 휠을 넣어야 하고, 빠뜨리면 facade·worker 가 ``ModuleNotFoundError`` 로 기동조차
   못 한다.
2. 이 스키마는 **edgequake 본체가 쓰는 바로 그 postgres** 안에 있다. 대기 핸들러가
   커넥션을 붙잡고 자면 edgequake 가 커넥션을 못 얻어 적재·검색이 동반 실패하는 새
   단일 실패점이 생긴다. 연산 단위로 열고 닫으면 동시 커넥션 = 실제 진행 중인 연산 수다.

**모든 잡 쓰기에 ``(claimed_by, attempt_count)`` 술어를 건다**(§3.3). ``rowcount == 0``
이면 "내 lease 를 잃었다"는 뜻이다. 이관 원본(``batch_repository.py:196-205``)에는 이
검증이 아예 없다.

``claimed_by`` 만으로는 **부족하다**. worker 는 배포상 1개이고 ``worker_id`` 는 프로세스
수명 동안 고정이라, 잡이 회수된 뒤 **같은 worker** 가 다시 집으면 옛 스레드의 쓰기가
``claimed_by=$me AND status='running'`` 을 그대로 통과한다. 그러면 attempt 1 의 결과가
attempt 2 를 종결시키고 attempt 2 스레드는 계속 달려 edgequake 에 문서를 한 번 더
제출한다. ``attempt_count`` 를 세대 토큰으로 함께 검사하면 스키마 변경 없이 막힌다.

lease 상실의 처리는 **부작용 기준으로 갈린다**:

* 부작용 **이후** 쓰기(``complete``/``requeue``) → 결과를 폐기한다(이미 벌어진 일).
* 부작용 **직전** 쓰기(``set_stage``) → **다운스트림을 호출하지 않고 즉시 중단한다.**
  ``stage='inserting'`` 사전 커밋이 중복 적재 방어의 유일한 게이트이므로, 여기서
  "로그만 남기고 계속" 하면 방어가 통째로 무의미해진다.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from service.jobs import admission, schema

log = logging.getLogger("kb_pipeline.service.jobs.repo")

#: claim 직렬화용 advisory lock. DDL 용(`kbp.schema`)과 다른 키다.
_CLAIM_LOCK_KEY = "kbp.jobs.claim"

KINDS: tuple[str, ...] = ("parse", "chunk", "insert", "ingest")

TERMINAL = frozenset({"succeeded", "failed", "canceled"})


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def max_attempts_by_kind() -> dict[str, int]:
    """kind 별 최대 시도 횟수.

    **insert 는 1 이다** — ``EdgequakeClient.insert_chunks()`` 가 호출마다
    ``submit_document`` 로 새 문서를 제출하고 멱등키가 없어서(``edgequake.py:379``)
    재시도가 곧 중복 적재다. 재시도를 없애면 원인이 사라진다(설계 §5.2, deferred D5).
    """
    default = _env_int("KBP_JOB_MAX_ATTEMPTS", 3)
    return {
        "parse": default,
        "chunk": default,
        "insert": _env_int("KBP_JOB_MAX_ATTEMPTS_INSERT", 1),
        "ingest": default,
    }


def max_runtime_by_kind() -> dict[str, int]:
    """kind 별 1회 시도의 최대 실행 시간(초).

    산식은 ``Σ(동기 호출 timeout × 최대 호출 횟수) + Σ(poll_timeout) + in-flight 1회``.
    세 항이 다 필요하다 — ``ensure_workspace`` 는 5xx 에 최대 4회 POST 하고 4xx 면 GET 을
    한 번 더 치며(``edgequake.py:80-89,97-100``), 폴 루프는 데드라인을 HTTP 호출이 **끝난
    뒤** 검사하므로 ``poll_timeout`` 뒤에 클라이언트 타임아웃 1회가 더 붙는다.

    값이 작으면 정상 진행 중인 잡이 **반드시** 회수되고, 회수는 진행 중 호출을 끊지
    못하므로 같은 잡이 2중 실행된다(설계 §3.7).
    """
    return {
        "parse": _env_int("KBP_JOB_MAX_RUNTIME_PARSE", 2100),
        "chunk": _env_int("KBP_JOB_MAX_RUNTIME_CHUNK", 5400),
        "insert": _env_int("KBP_JOB_MAX_RUNTIME_INSERT", 6600),
        "ingest": _env_int("KBP_JOB_MAX_RUNTIME_INGEST", 14400),
    }


@dataclass(frozen=True)
class ClaimedJob:
    id: uuid.UUID
    kind: str
    #: claim 시점의 시도 번호 = 이 lease 의 세대 토큰. 이후 모든 쓰기가 이 값을 함께
    #: 검사해야 같은 worker 가 재claim 했을 때의 좀비 쓰기를 막는다.
    attempt: int


class LeaseLost(RuntimeError):
    """내 lease 로 쓰려 했는데 0행 — 회수됐거나 다른 세대가 가져갔다.

    ``set_stage`` 에서 이 예외가 나오면 **다운스트림을 호출하기 전에** 잡 실행을
    포기해야 한다. ``complete``/``requeue`` 에서 나오면 결과만 버린다.
    """


class JobRepo:
    """``kbp.jobs`` 접근자. 상태를 들고 있지 않다(연산마다 커넥션을 새로 연다)."""

    def __init__(self, dsn: str | None = None, *, connect_timeout: int = 5) -> None:
        self._dsn = dsn or os.environ["KBP_PG_DSN"]
        self._connect_timeout = connect_timeout

    # ── 연결 ───────────────────────────────────────────────────────────────

    def _connect(self) -> psycopg.Connection:
        # connect_timeout 이 없으면 postgres 가 죽었을 때 워커가 무한정 매달린다.
        return psycopg.connect(
            self._dsn, connect_timeout=self._connect_timeout, row_factory=dict_row
        )

    def _retry_missing_schema(self, fn, *args, **kwargs):
        """``42P01``/``3F000`` 이면 스키마를 다시 만들고 한 번 재시도한다.

        dev 의 edgequake 런처가 postgres 를 볼륨 없이 재생성하면 스키마가 정의째
        사라지는데, 이미 떠 있는 프로세스는 lifespan 을 다시 타지 않아 영구히 깨진다.
        """
        try:
            return fn(*args, **kwargs)
        except psycopg.Error as exc:
            if not schema.is_missing_schema(exc):
                raise
            log.warning("kbp schema missing (%s); recreating and retrying once", exc)
            schema.ensure_schema(self._dsn)
            return fn(*args, **kwargs)

    # ── 제출 ───────────────────────────────────────────────────────────────

    def submit(
        self,
        *,
        kind: str,
        payload: dict[str, Any] | None = None,
        payload_ref: str | None = None,
        input_ref: str | None = None,
        workspace_key: str | None = None,
        batch_key: str | None = None,
        parent_job_id: uuid.UUID | str | None = None,
        legacy: bool = False,
        job_id: uuid.UUID | None = None,
        idem_key: str | None = None,
    ) -> uuid.UUID:
        """잡 하나를 ``queued`` 로 넣고 id 를 돌려준다. 밀리초 안에 끝나야 한다.

        ``job_id`` 를 받는 이유: staging·payload 객체 키가 ``{prefix}/{job_id}/...`` 라
        호출자가 **INSERT 전에** id 를 알아야 한다. 안 그러면 행을 만든 뒤 UPDATE 로
        참조를 덧칠하는 우회가 생긴다.
        """
        if kind not in KINDS:
            raise ValueError(f"unknown job kind: {kind!r}")
        job_id = job_id or uuid.uuid4()
        # 멱등키 충돌이면 새 잡을 만들지 않고 **기존 job_id 를 그대로 돌려준다**.
        # 소비자(kb)가 5xx 를 재시도하므로, 이게 없으면 재시도마다 새 잡이 생겨
        # /insert·/ingest 에서 edgequake 중복 적재가 된다.
        sql = """
            INSERT INTO kbp.jobs
                (id, kind, status, workspace_key, batch_key, parent_job_id,
                 legacy, payload, payload_ref, input_ref, idem_key)
            VALUES (%s, %s, 'queued', %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (idem_key) WHERE idem_key IS NOT NULL DO NOTHING
            RETURNING id
        """

        def _run():
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, (
                        job_id, kind, workspace_key, batch_key,
                        _as_uuid(parent_job_id), legacy,
                        Jsonb(payload) if payload is not None else None,
                        payload_ref, input_ref, idem_key,
                    ))
                    row = cur.fetchone()
                    if row is None:
                        # 충돌 — 같은 키의 살아있는 잡이 이미 있다.
                        cur.execute(
                            "SELECT id FROM kbp.jobs WHERE idem_key = %s", (idem_key,)
                        )
                        existing = cur.fetchone()
                        if existing is None:
                            # 그 사이 실패로 끝나 키가 비워졌다 — 재삽입.
                            conn.rollback()
                            return self.submit(
                                kind=kind, payload=payload, payload_ref=payload_ref,
                                input_ref=input_ref, workspace_key=workspace_key,
                                batch_key=batch_key, parent_job_id=parent_job_id,
                                legacy=legacy, idem_key=None,
                            )
                        conn.commit()
                        return existing["id"]
                conn.commit()
            return job_id

        return self._retry_missing_schema(_run)

    def get(self, job_id: uuid.UUID | str) -> dict[str, Any] | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM kbp.jobs WHERE id = %s", (_as_uuid(job_id),))
                return cur.fetchone()

    def list_jobs(
        self,
        *,
        workspace_key: str | None = None,
        batch_key: str | None = None,
        status: str | None = None,
        kind: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        args: list[Any] = []
        for col, val in (("workspace_key", workspace_key), ("batch_key", batch_key),
                         ("status", status), ("kind", kind)):
            if val is not None:
                where.append(f"{col} = %s")
                args.append(val)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        args.append(max(1, min(limit, 500)))
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT * FROM kbp.jobs {clause} ORDER BY created_at DESC, id LIMIT %s",
                    args,
                )
                return list(cur.fetchall())

    def ahead_in_partition(self, job: dict[str, Any]) -> int:
        """같은 ``(kind, workspace_key)`` 안에서 이 잡보다 앞선 queued 건수.

        ``queue_position`` 을 두지 않는 대신 주는 값이다 — claim 은 kind 무관 전역 FIFO
        스캔이고 승인은 3중 조건이라 "전체에서 몇 번째" 는 대기 예측에 쓸 수 없다.
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT count(*) AS n FROM kbp.jobs
                     WHERE status = 'queued' AND cancel_requested = false
                       AND kind = %s
                       AND workspace_key IS NOT DISTINCT FROM %s
                       AND (created_at, id) < (%s, %s)
                    """,
                    (job["kind"], job["workspace_key"], job["created_at"], job["id"]),
                )
                return int(cur.fetchone()["n"])

    # ── claim ──────────────────────────────────────────────────────────────

    def claim(
        self,
        *,
        worker_id: str,
        local_free: int,
        stale_lease_seconds: int | None = None,
        worker_stale_seconds: int | None = None,
    ) -> list[ClaimedJob]:
        """유지보수 + 승인을 한 트랜잭션으로 수행한다(설계 §3.1).

        트랜잭션 전체를 advisory lock 으로 직렬화한다 — 여러 worker 가 각자 "현재 running
        수"를 읽고 각자 승인하면 전역 상한이 깨지기 때문이다. claim 안에서는 다운스트림을
        호출하지 않으므로(수 ms) 직렬화 비용이 없다.

        유지보수 (1)(1b)(1c) 는 ``local_free`` 와 **무관하게** 매 틱 실행한다. 슬롯이
        꽉 찼다고 stale 회수·취소 종결·만료 worker 삭제까지 멈추면 가장 바쁠 때
        ``GET /jobs/workers`` 가 죽은 worker 를 online 으로 보고한다.
        """
        stale = stale_lease_seconds or _env_int("KBP_JOB_STALE_LEASE_SECONDS", 300)
        wstale = worker_stale_seconds or _env_int("KBP_JOB_WORKER_STALE_SECONDS", 60)
        attempts = max_attempts_by_kind()
        runtimes = max_runtime_by_kind()
        kinds = list(KINDS)

        def _run() -> list[ClaimedJob]:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    # advisory lock 은 blocking 획득이라 상한이 없으면 한 틱이 무한정
                    # 매달린다. heartbeat 는 별도 스레드지만 claim 이 굶는 것도 막는다.
                    cur.execute("SET LOCAL lock_timeout = '5s'")
                    cur.execute("SET LOCAL statement_timeout = '30s'")
                    cur.execute(
                        "SELECT pg_advisory_xact_lock(hashtext(%s))", (_CLAIM_LOCK_KEY,)
                    )

                    self._recover(cur, kinds, attempts, runtimes, stale)
                    self._finish_cancelled_queued(cur)
                    cur.execute(
                        "DELETE FROM kbp.job_workers "
                        " WHERE heartbeat_at < now() - make_interval(secs => %s)",
                        (wstale,),
                    )

                    if local_free <= 0:
                        conn.commit()
                        return []

                    running = self._running_counts(cur)
                    candidates = self._candidates(cur, kinds, attempts)
                    buckets, by_ws = admission.expand_running_buckets(running)
                    admitted = admission.plan_admissions(
                        candidates,
                        running_by_bucket=buckets,
                        running_by_workspace=by_ws,
                        bucket_limits=admission.bucket_limits_from_env(),
                        workspace_limit=admission.workspace_limit_from_env(),
                        local_free=local_free,
                    )
                    claimed = self._admit(cur, admitted, worker_id, kinds, attempts)
                conn.commit()
                return claimed

        return self._retry_missing_schema(_run)

    @staticmethod
    def _recover(cur, kinds, attempts, runtimes, stale) -> None:
        """(1) stale lease + 실행시간 초과 회수. 상한이 kind 별이라 unnest 로 조인한다.

        ``stage='inserting'`` 이 **kind 무관 최우선** 분기다. 무재시도 판정이 runner 의
        예외 처리에만 있으면 worker 급사·OOM·실행시간 초과 경로에는 적용되지 않아,
        edgequake 에 이미 문서를 제출한 잡이 ``queued`` 로 돌아가 중복 적재된다.
        """
        cur.execute(
            """
            UPDATE kbp.jobs j
               SET status = CASE
                     WHEN j.cancel_requested          THEN 'canceled'
                     WHEN j.stage = 'inserting'       THEN 'failed'
                     WHEN j.attempt_count >= lim.max_attempts THEN 'failed'
                     ELSE 'queued' END,
                   error = CASE
                     WHEN j.cancel_requested    THEN NULL
                     WHEN j.stage = 'inserting'
                       THEN 'insert already submitted to edgequake; not retried'
                     ELSE 'stale worker lease or max runtime exceeded' END,
                   claimed_by = NULL, claimed_at = NULL, heartbeat_at = NULL,
                   completed_at = CASE
                     WHEN j.cancel_requested
                       OR j.stage = 'inserting'
                       OR j.attempt_count >= lim.max_attempts THEN now() END,
                   idem_key = CASE
                     WHEN j.cancel_requested
                       OR j.stage = 'inserting'
                       OR j.attempt_count >= lim.max_attempts THEN NULL
                     ELSE j.idem_key END
              FROM unnest(%s::text[], %s::int[], %s::int[])
                   AS lim(kind, max_attempts, max_runtime)
             WHERE j.kind = lim.kind
               AND j.status = 'running'
               AND (j.heartbeat_at < now() - make_interval(secs => %s)
                    OR j.started_at < now() - make_interval(secs => lim.max_runtime))
            """,
            (kinds, [attempts[k] for k in kinds], [runtimes[k] for k in kinds], stale),
        )

    @staticmethod
    def _finish_cancelled_queued(cur) -> None:
        """(1b) queued 인데 취소 요청된 행을 종결한다.

        후보 조회가 ``cancel_requested = false`` 로 이 행을 영구 배제하므로, 종결하지
        않으면 terminal 도 아닌 채 목록에 영원히 남는 좀비가 된다.
        """
        cur.execute(
            "UPDATE kbp.jobs SET status='canceled', completed_at=now() "
            " WHERE status='queued' AND cancel_requested"
        )

    @staticmethod
    def _running_counts(cur) -> dict[tuple[str, str | None], int]:
        cur.execute(
            "SELECT kind, workspace_key, count(*) AS n FROM kbp.jobs "
            " WHERE status='running' GROUP BY 1, 2"
        )
        return {(r["kind"], r["workspace_key"]): int(r["n"]) for r in cur.fetchall()}

    @staticmethod
    def _candidates(cur, kinds, attempts) -> list[admission.Candidate]:
        """(3) 후보 조회 — ``(kind, workspace)`` 파티션당 상위 N건.

        파티션을 workspace 만으로 잡으면 안 된다. 현행 ``/parse``·``/chunk`` 에는
        workspace 개념이 없어 트래픽 대부분이 NULL 인데, 그러면 한 파티션의 상위 N건이
        전부 chunk 잡일 때 parse 슬롯이 비어 있어도 그 틱에 아무것도 승인되지 않는다.
        """
        cur.execute(
            """
            SELECT id, kind, workspace_key FROM (
              SELECT j.id, j.kind, j.workspace_key, j.created_at,
                     -- coalesce 로 sentinel 을 만들지 않는다. PARTITION BY 는 NULL 을
                     -- 한 그룹으로 묶으므로 그대로 두면 되고, 어떤 sentinel 문자열도
                     -- 실제 workspace_key 와 충돌할 여지가 있다.
                     row_number() OVER (
                       PARTITION BY j.kind, j.workspace_key
                       ORDER BY j.created_at, j.id) AS rn
                FROM kbp.jobs j
                JOIN unnest(%s::text[], %s::int[]) AS lim(kind, max_attempts)
                  ON j.kind = lim.kind
               WHERE j.status = 'queued'
                 AND j.cancel_requested = false
                 AND j.attempt_count < lim.max_attempts
            ) c
             WHERE rn <= %s
             ORDER BY created_at, id
             LIMIT %s
            """,
            (kinds, [attempts[k] for k in kinds],
             _env_int("KBP_JOB_PER_PARTITION_SCAN", 8),
             _env_int("KBP_JOB_CLAIM_SCAN_LIMIT", 200)),
        )
        return [
            admission.Candidate(id=r["id"], kind=r["kind"],
                                workspace_key=r["workspace_key"])
            for r in cur.fetchall()
        ]

    @staticmethod
    def _admit(cur, admitted, worker_id, kinds, attempts) -> list[ClaimedJob]:
        """(5) 조건부 승인. ``RETURNING`` 집합만 실제로 실행한다.

        상태 검사가 없으면, 취소 API 는 advisory lock 을 잡지 않으므로 후보 조회 이후
        커밋된 ``canceled`` 를 이 UPDATE 가 조용히 ``running`` 으로 되살린다(취소 유실).
        ``attempt_count`` 가드도 여기 있어야 소진된 행의 재claim 루프를 막는다.
        """
        if not admitted:
            return []
        cur.execute(
            """
            UPDATE kbp.jobs j
               SET status='running', claimed_by=%s, claimed_at=now(), heartbeat_at=now(),
                   attempt_count = j.attempt_count + 1,
                   started_at=now(), error=NULL, stage=NULL
              FROM unnest(%s::text[], %s::int[]) AS lim(kind, max_attempts)
             WHERE j.kind = lim.kind
               AND j.id = ANY(%s)
               AND j.status = 'queued'
               AND j.cancel_requested = false
               AND j.attempt_count < lim.max_attempts
            RETURNING j.id, j.kind, j.attempt_count
            """,
            (worker_id, kinds, [attempts[k] for k in kinds], list(admitted)),
        )
        return [
            ClaimedJob(id=r["id"], kind=r["kind"], attempt=int(r["attempt_count"]))
            for r in cur.fetchall()
        ]

    # ── lease (모든 쓰기에 claimed_by 술어) ────────────────────────────────

    def heartbeat(
        self, *, worker_id: str, leases: Sequence[tuple[uuid.UUID, int]]
    ) -> None:
        """in-flight 잡의 lease 를 갱신한다.

        **``claimed_by=$me`` 일괄 갱신은 금지**한다 — runner 스레드가 예외로 죽어 완료
        쓰기를 못 한 잡까지 계속 갱신되어, 300s stale 경로가 아니라 ``MAX_RUNTIME``
        (수천 초)까지 슬롯을 점유한다.
        """
        if not leases:
            return
        ids = [j for j, _ in leases]
        attempts = [a for _, a in leases]
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE kbp.jobs j SET heartbeat_at = now()
                      FROM unnest(%s::uuid[], %s::int[]) AS lease(id, attempt)
                     WHERE j.id = lease.id
                       AND j.attempt_count = lease.attempt
                       AND j.claimed_by = %s
                       AND j.status = 'running'
                    """,
                    (ids, attempts, worker_id),
                )
            conn.commit()

    def mark_worker(
        self, *, worker_id: str, capacity: int, active_count: int, started_at: datetime
    ) -> None:
        """worker 레지스트리 갱신. ``active_count`` 를 안 쓰면 ``available`` 이 굳는다."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO kbp.job_workers
                        (worker_id, capacity, active_count, started_at, heartbeat_at)
                    VALUES (%s, %s, %s, %s, now())
                    ON CONFLICT (worker_id) DO UPDATE
                       SET capacity = EXCLUDED.capacity,
                           active_count = EXCLUDED.active_count,
                           heartbeat_at = now()
                    """,
                    (worker_id, capacity, active_count, started_at),
                )
            conn.commit()

    def set_stage(
        self, job_id: uuid.UUID, *, worker_id: str, attempt: int, stage: str
    ) -> None:
        """진행 단계를 기록한다. ``stage='inserting'`` 은 무재시도 신호이기도 하다(§5.2).

        **부작용 직전 쓰기다.** ``LeaseLost`` 가 나면 호출자는 다운스트림을 호출하지
        말고 즉시 중단해야 한다 — 여기서 계속 진행하면 중복 적재 방어가 무의미해진다.

        :raises LeaseLost: 회수됐거나 다른 세대가 잡을 가져갔다.
        """
        self._fenced(
            "UPDATE kbp.jobs SET stage = %s"
            " WHERE id = %s AND claimed_by = %s AND attempt_count = %s"
            "   AND status = 'running'",
            (stage, job_id, worker_id, attempt),
        )

    def complete(
        self,
        job_id: uuid.UUID,
        *,
        worker_id: str,
        attempt: int,
        status: str,
        result: dict[str, Any] | None = None,
        result_ref: str | None = None,
        error: str | None = None,
    ) -> None:
        if status not in TERMINAL:
            raise ValueError(f"not a terminal status: {status!r}")
        self._fenced(
            """
            UPDATE kbp.jobs
               SET status = %s, result = %s, result_ref = %s, error = %s,
                   completed_at = now(), heartbeat_at = NULL, stage = NULL,
                   -- 실패로 끝나면 멱등키를 비운다. 설정을 고치고 같은 파일을 다시
                   -- 올렸을 때 옛 실패 job_id 가 반환되어 영구 실패로 굳는 것을 막는다.
                   idem_key = CASE WHEN %s = 'succeeded' THEN idem_key ELSE NULL END
             WHERE id = %s AND claimed_by = %s AND attempt_count = %s
               AND status = 'running'
            """,
            (status, Jsonb(result) if result is not None else None,
             result_ref, error, status, job_id, worker_id, attempt),
        )

    def requeue(
        self, job_id: uuid.UUID, *, worker_id: str, attempt: int, error: str
    ) -> None:
        """재시도 가능한 실패 — ``queued`` 로 되돌린다(``attempt_count`` 는 유지)."""
        self._fenced(
            """
            UPDATE kbp.jobs
               SET status = 'queued', error = %s,
                   claimed_by = NULL, claimed_at = NULL, heartbeat_at = NULL, stage = NULL
             WHERE id = %s AND claimed_by = %s AND attempt_count = %s
               AND status = 'running'
            """,
            (error, job_id, worker_id, attempt),
        )

    def _fenced(self, sql: str, args: tuple) -> None:
        """``(claimed_by, attempt_count, status)`` 술어가 0행이면 ``LeaseLost``."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, args)
                lost = cur.rowcount == 0
            conn.commit()
        if lost:
            raise LeaseLost(
                f"lease lost (job={args[-3]} worker={args[-2]} attempt={args[-1]})"
            )

    # ── 취소 ───────────────────────────────────────────────────────────────

    def cancel(self, job_id: uuid.UUID | str) -> str | None:
        """취소를 **단일 UPDATE 로 원자 수행**한다.

        두 번에 나누면 그 사이에 잡이 ``running``→``queued`` 로 회수되어 양쪽 다 0행이
        되고 취소가 조용히 유실된다. 취소 API 는 advisory lock 을 잡지 않으므로 경합
        창이 실재한다.

        :returns: ``'canceled'``(즉시 취소) / ``'running'``(플래그만) / ``None``
            (없거나 이미 terminal — 호출자가 404·409 를 구분한다).
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE kbp.jobs
                       SET cancel_requested = true,
                           status = CASE WHEN status='queued' THEN 'canceled'
                                         ELSE status END,
                           completed_at = CASE WHEN status='queued' THEN now()
                                              ELSE completed_at END,
                           -- 취소된 잡의 키를 비워야 같은 요청을 다시 낼 수 있다.
                           idem_key = NULL
                     WHERE id = %s AND status IN ('queued', 'running')
                    RETURNING status
                    """,
                    (_as_uuid(job_id),),
                )
                row = cur.fetchone()
            conn.commit()
        return row["status"] if row else None

    def is_cancel_requested(self, job_id: uuid.UUID) -> bool:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT cancel_requested FROM kbp.jobs WHERE id = %s", (job_id,)
                )
                row = cur.fetchone()
        return bool(row and row["cancel_requested"])

    # ── 관측 ───────────────────────────────────────────────────────────────

    def worker_stats(self, *, alive_seconds: int | None = None) -> dict[str, Any]:
        """``GET /jobs/workers`` 본문.

        키 이름은 kb 의 ``worker_capacity()``(``batch_repository.py:306-313``)와 **동일**
        하다 — 마지막 키가 ``processing`` 이다. 바꾸면 Phase 2 에서 프론트가 조용히
        ``undefined`` 를 받는다. ``oldest_queued_age_seconds`` 는 "worker 0 + 큐 적체" 를
        단일 지표로 알람하려고 더한 것이다.
        """
        alive = alive_seconds or _env_int("KBP_JOB_WORKER_STALE_SECONDS", 60)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT coalesce(sum(capacity), 0) AS cap,"
                    "       coalesce(sum(active_count), 0) AS act"
                    "  FROM kbp.job_workers"
                    " WHERE heartbeat_at >= now() - make_interval(secs => %s)",
                    (alive,),
                )
                row = cur.fetchone()
                cap, act = int(row["cap"]), int(row["act"])
                cur.execute(
                    "SELECT status, count(*) AS n FROM kbp.jobs"
                    " WHERE status IN ('queued','running') GROUP BY 1"
                )
                counts = {r["status"]: int(r["n"]) for r in cur.fetchall()}
                cur.execute(
                    "SELECT extract(epoch FROM now() - min(created_at)) AS age"
                    "  FROM kbp.jobs WHERE status = 'queued'"
                )
                age = cur.fetchone()["age"]
        return {
            "online": cap > 0,
            "capacity": cap,
            "active": act,
            "available": max(0, cap - act),
            "queued": counts.get("queued", 0),
            "processing": counts.get("running", 0),
            "oldest_queued_age_seconds": float(age) if age is not None else None,
        }

    def live_worker_count(self, *, alive_seconds: int | None = None) -> int:
        """접수 fail-fast 용(§4.4). 0 이면 잡을 만들지 않고 503 을 낸다."""
        alive = alive_seconds or _env_int("KBP_JOB_WORKER_STALE_SECONDS", 60)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) AS n FROM kbp.job_workers"
                    " WHERE heartbeat_at >= now() - make_interval(secs => %s)",
                    (alive,),
                )
                return int(cur.fetchone()["n"])

    # ── staging 정리 (GC 없음 — §5.3) ──────────────────────────────────────

    def legacy_refs_to_purge(self, job_id: uuid.UUID) -> tuple[str | None, str | None]:
        """``legacy=true`` 잡의 terminal 즉시 삭제 대상 ``(input_ref, payload_ref)``.

        ``input_ref`` 만으로는 부족하다 — 무인증 레거시 ``/chunk`` 는 파일 업로드가 없어
        ``input_ref`` 가 없고, 대신 수 MB ``enriched_content`` 가 ``payload_ref`` 로
        MinIO 에 올라간다. 무인증으로 객체를 남기는 경로가 둘이다.
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT legacy, input_ref, payload_ref FROM kbp.jobs WHERE id = %s",
                    (job_id,),
                )
                row = cur.fetchone()
        if not row or not row["legacy"]:
            return (None, None)
        return (row["input_ref"], row["payload_ref"])


def _as_uuid(value: uuid.UUID | str | None) -> uuid.UUID | None:
    if value is None or isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))
