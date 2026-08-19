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

#: claim 직렬화용 advisory lock. DDL 용과 objid 로 갈린다(`schema.LOCK_OBJ_*`).
#: 2-인자 형식을 쓰는 이유는 schema.py 주석 참조 — edgequake 와 같은 DB 라 1-인자
#: bigint 공간을 피한다.

KINDS: tuple[str, ...] = ("parse", "chunk", "insert", "ingest", "community")

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
        # 커뮤니티 빌드는 멱등이다(같은 workspace 를 다시 빌드하면 결과가 같다) —
        # insert 와 달리 재시도가 중복을 만들지 않는다.
        "community": default,
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
        # Louvain + 커뮤니티당 LLM 요약. 그래프가 커질수록 선형으로 늘어난다.
        "community": _env_int("KBP_JOB_MAX_RUNTIME_COMMUNITY", 7200),
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
                            # INSERT 는 충돌했는데 SELECT 시점엔 키가 없다 = 그 사이
                            # 다른 세션이 그 잡을 실패로 종결시켜 키를 비웠다(경합).
                            # 멱등 없이 재삽입한다.
                            #
                            # **job_id 를 반드시 그대로 넘긴다.** 호출자(api.submit_job)는
                            # 이미 `{prefix}/{job_id}/...` 로 객체를 올려둔 상태다. 여기서
                            # 새 uuid 를 만들면 행 id 와 객체 키가 어긋나고, 고아 스윕이
                            # "이 job_id 로 된 행이 없다"고 판단해 **살아있는 잡의 입력을
                            # 지운다**.
                            conn.rollback()
                            return self.submit(
                                kind=kind, payload=payload, payload_ref=payload_ref,
                                input_ref=input_ref, workspace_key=workspace_key,
                                batch_key=batch_key, parent_job_id=parent_job_id,
                                legacy=legacy, idem_key=None, job_id=job_id,
                            )
                        conn.commit()
                        return existing["id"]
                conn.commit()
            return job_id

        return _run()

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
        before_created_at=None,
        before_id: uuid.UUID | str | None = None,
    ) -> list[dict[str, Any]]:
        """최신순 페이지 조회. ``before_created_at``/``before_id`` 를 함께 주면(둘 다
        필요 — 동률 시각 tie-break) 그 행보다 **오래된** 페이지를 이어서 준다(keyset).

        배치 규모(수십만~수백만 행)에서 ``OFFSET`` 은 건너뛴 행까지 전부 스캔해
        페이지가 뒤로 갈수록 선형으로 느려진다 — keyset 은 인덱스(created_at, id)만
        타므로 몇 페이지째든 동일한 비용이다(2026-08-19, parser_test_ui /history
        페이징 요구로 도입).
        """
        where: list[str] = []
        args: list[Any] = []
        for col, val in (("workspace_key", workspace_key), ("batch_key", batch_key),
                         ("status", status), ("kind", kind)):
            if val is not None:
                where.append(f"{col} = %s")
                args.append(val)
        if before_created_at is not None and before_id is not None:
            # ORDER BY 가 (created_at DESC, id DESC) 라 튜플비교가 그대로 "더 오래된 행"과 맞는다.
            where.append("(created_at, id) < (%s, %s)")
            args.append(before_created_at)
            args.append(_as_uuid(before_id))
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        args.append(max(1, min(limit, 500)))
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT * FROM kbp.jobs {clause} ORDER BY created_at DESC, id DESC LIMIT %s",
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
                    cur.execute("SELECT pg_advisory_xact_lock(%s, %s)",
                                (schema.LOCK_CLASSID, schema.LOCK_OBJ_CLAIM))

                    self._recover(cur, kinds, attempts, runtimes, stale)
                    self._finish_cancelled_queued(cur)
                    self._finish_exhausted_queued(cur, kinds, attempts)
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

        return _run()

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
    def _finish_exhausted_queued(cur, kinds, attempts) -> None:
        """(1d) 시도를 다 쓴 ``queued`` 행을 종결한다 (D13).

        ``_candidates`` 가 ``attempt_count < max_attempts`` 로 이 행을 **영구 배제**하므로,
        종결하지 않으면 terminal 도 아니고 GC 대상도 아닌 채 큐에 남는다. 자식이면 부모의
        TTL 삭제까지 영구 차단한다(부모는 자식이 참조하는 한 안 지워진다).

        ``requeue`` 가 이미 소진을 판정하므로 새로 생기지는 않는다. 이건 **이미 갇힌 행**
        을 걷어내는 안전망이고, 상한을 낮춰서(``KBP_JOB_MAX_ATTEMPTS`` 하향) 기존 행이
        소급 소진되는 경우도 여기서 잡힌다.
        """
        cur.execute(
            """
            UPDATE kbp.jobs j
               SET status = 'failed', completed_at = now(), idem_key = NULL,
                   error = coalesce(j.error, '') ||
                           CASE WHEN j.error IS NULL THEN '' ELSE ' | ' END ||
                           'attempts exhausted while queued'
              FROM unnest(%s::text[], %s::int[]) AS lim(kind, max_attempts)
             WHERE j.kind = lim.kind
               AND j.status = 'queued'
               AND j.attempt_count >= lim.max_attempts
            """,
            (kinds, [attempts[k] for k in kinds]),
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
                   started_at=now(), error=NULL, stage=NULL,
                   -- community 는 **claim 시점에** 멱등키를 비운다(D10).
                   --
                   -- 커뮤니티 빌드는 시작 시점의 그래프만 본다. 키를 성공까지 들고 있으면
                   -- 빌드가 도는 동안(최대 7200s) 들어온 트리거가 전부 그 running 잡으로
                   -- 흡수돼, 10건 배치에서 1번 문서가 빌드를 시작하면 2~10번 엔티티는
                   -- 다음 업로드까지 커뮤니티가 없다. 기존 BackgroundTask 는 트리거마다
                   -- 독립 빌드라 마지막 빌드가 전량 커버했으므로 그게 회귀다.
                   --
                   -- 비우면: queued 동안의 버스트는 여전히 합쳐지고(실측 한 배치 3회),
                   -- 시작 후 트리거는 새 잡이 되어 버킷 상한 1 이 **직렬화**한다 →
                   -- 앞 빌드가 끝나면 뒤 빌드가 돌아 나중 문서까지 커버한다.
                   --
                   -- 다른 kind 는 절대 비우지 않는다 — 재실행(arq max_tries·recover_stale·
                   -- retry_failed_item)이 옛 succeeded insert 잡을 맞아야 edgequake
                   -- 중복 적재가 막힌다.
                   idem_key = CASE WHEN j.kind = 'community' THEN NULL ELSE j.idem_key END
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
        clear_idem: bool = False,
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
                   -- clear_idem(2026-08-06): job 자체는 succeeded 지만 본문이 도메인
                   -- 실패(parse-svc {"status":"failed"})면 명시적 idem_key 는
                   -- 시간창이 없어 영구 캐싱된다 — 그 경우도 비워서 재시도가 재실행되게 한다.
                   idem_key = CASE WHEN %s = 'succeeded' AND NOT %s
                                   THEN idem_key ELSE NULL END
             WHERE id = %s AND claimed_by = %s AND attempt_count = %s
               AND status = 'running'
            """,
            (status, Jsonb(result) if result is not None else None,
             result_ref, error, status, clear_idem, job_id, worker_id, attempt),
        )

    def requeue(
        self, job_id: uuid.UUID, *, worker_id: str, attempt: int, error: str
    ) -> None:
        """재시도 가능한 실패 — ``queued`` 로 되돌린다(``attempt_count`` 는 유지).

        **``stage='inserting'`` 이면 되돌리지 않고 ``failed`` 로 끝낸다.** ``_recover``
        (회수 경로)와 같은 규칙이다 — 한쪽만 막으면 다른 쪽으로 중복 적재가 샌다.

        구체적으로: ``insert_chunks`` 는 edgequake 에 문서를 제출한 뒤 완료를 폴링한다.
        제출은 성공했는데 폴링 중 5xx·타임아웃이 나면 ``classify`` 가 이를 **재시도 가능**
        으로 분류한다(§5.1). 예전에는 여기서 ``stage`` 를 무조건 NULL 로 지우고 ``queued``
        로 되돌려, ``ingest``(``max_attempts=3``)가 parse 부터 다시 돌며 **같은 문서를 또
        제출**했다. ``insert`` kind 는 ``max_attempts=1`` 이라 중복 대신 아무도 안 집는
        ``queued`` 좀비가 됐다(D13). 둘 다 여기서 끊는다.

        **시도를 다 썼으면(``attempt_count >= max_attempts``) 되돌리지 않고 ``failed`` 로
        끝낸다** (D13). 되돌리면 ``_candidates`` 가 ``attempt_count < max_attempts`` 로 그
        행을 영구 배제해, terminal 도 아니고 GC 대상도 아닌 채 큐에 남는 좀비가 된다.
        자식이면 부모의 TTL 삭제까지 영구 차단한다(부모는 자식이 참조하는 한 안 지워진다).
        이미 갇힌 행은 claim 유지보수 ``_finish_exhausted_queued``(1d)가 걷어낸다.

        Postgres 는 ``SET`` 의 모든 식을 **갱신 전 행**으로 평가하므로, 같은 문장에서
        ``stage = NULL`` 을 함께 써도 위 ``CASE`` 는 옛 값을 본다.
        """
        # kind 별 상한이 인자로 안 들어오므로 `_recover` 와 같은 방식으로 조인한다.
        attempts = max_attempts_by_kind()
        kinds = list(KINDS)
        self._fenced(
            """
            UPDATE kbp.jobs j
               SET status = CASE
                     WHEN j.stage = 'inserting'               THEN 'failed'
                     WHEN j.attempt_count >= lim.max_attempts THEN 'failed'
                     ELSE 'queued' END,
                   error = CASE
                     WHEN j.stage = 'inserting'
                       THEN 'insert already submitted to edgequake; not retried'
                     WHEN j.attempt_count >= lim.max_attempts
                       THEN %s || ' | attempts exhausted'
                     ELSE %s END,
                   completed_at = CASE
                     WHEN j.stage = 'inserting'
                       OR j.attempt_count >= lim.max_attempts THEN now() END,
                   idem_key = CASE
                     WHEN j.stage = 'inserting'
                       OR j.attempt_count >= lim.max_attempts THEN NULL
                     ELSE j.idem_key END,
                   claimed_by = NULL, claimed_at = NULL, heartbeat_at = NULL, stage = NULL
              FROM unnest(%s::text[], %s::int[]) AS lim(kind, max_attempts)
             WHERE j.id = %s AND j.kind = lim.kind
               AND j.claimed_by = %s AND j.attempt_count = %s
               AND j.status = 'running'
            """,
            (error, error, kinds, [attempts[k] for k in kinds],
             job_id, worker_id, attempt),
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

    def cancel(self, job_id: uuid.UUID | str) -> dict[str, Any] | None:
        """취소를 **단일 UPDATE 로 원자 수행**한다.

        두 번에 나누면 그 사이에 잡이 ``running``→``queued`` 로 회수되어 양쪽 다 0행이
        되고 취소가 조용히 유실된다. 취소 API 는 advisory lock 을 잡지 않으므로 경합
        창이 실재한다.

        **``stage='inserting'`` 이면 취소하지 않는다**(D6). edgequake 에 문서를 이미
        제출한 뒤라 여기서 멈추면 **부분 적재**가 남는다 — 잡은 취소로 끝났는데 문서는
        절반 들어가 있는 상태가 가장 나쁘다. D5 에서 이 단계를 재시도·회수 대상에서
        뺀 것과 같은 판정이다. 소비자는 반환된 ``stage`` 를 보고 취소 버튼을 감춘다.

        객체(``*_ref``)를 함께 돌려주는 이유: **즉시 취소된 잡의 staging 을 곧장 지우기
        위해서다.** 예전에는 TTL GC(기본 72h)까지 남아, 큰 스캔 PDF 를 잘못 올려 취소해도
        그 수십 MB 가 사흘을 버텼다. 삭제는 **커밋 뒤 트랜잭션 밖에서** 호출자가 한다
        (``purge_expired`` 와 같은 이유 — 롤백 시 행은 살고 객체만 사라지는 걸 막는다).

        :returns: ``{"status", "stage", "input_ref", "payload_ref", "result_ref"}`` 또는
            ``None``(없거나 이미 terminal). ``status`` 는 ``'canceled'``(즉시) /
            ``'running'``(플래그만) / ``'inserting'``(거부).
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
                       AND coalesce(stage, '') <> 'inserting'
                    RETURNING status, stage, input_ref, payload_ref, result_ref
                    """,
                    (_as_uuid(job_id),),
                )
                row = cur.fetchone()
            conn.commit()
        if row:
            return dict(row)
        # 0행 — 없거나, terminal 이거나, inserting 이라 거부됐다. 셋을 구분한다.
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status, stage FROM kbp.jobs WHERE id = %s", (_as_uuid(job_id),)
                )
                cur_row = cur.fetchone()
        if cur_row and cur_row["stage"] == "inserting" and cur_row["status"] == "running":
            return {"status": "inserting", "stage": "inserting",
                    "input_ref": None, "payload_ref": None, "result_ref": None}
        return None

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

    # ── GC (D2) ────────────────────────────────────────────────────────────

    def purge_expired(self, *, ttl_seconds: int, batch: int) -> list[dict[str, Any]] | None:
        """TTL 경과 terminal 잡을 지우고 **커밋된 뒤** 그 행들을 돌려준다.

        반환값의 ``*_ref`` 를 호출자가 트랜잭션 **밖에서** MinIO 에서 지운다. 한
        트랜잭션 안에서 객체까지 지우면, 커밋이 실패·롤백했을 때 행은 살아남고 객체만
        사라져 ``GET /jobs/{id}/result`` 가 "결과 있음" 이라 응답해놓고 복원에 실패한다.

        ``NOT EXISTS`` 는 반드시 ``LIMIT`` 서브쿼리 **안**에 둔다. 밖에 두면 보호 대상
        (비종료 자식을 가진 부모)까지 포함해 상위 N건을 뽑고 바깥에서 걸러내는데, 보호
        행은 정의상 가장 오래된 축이라 정렬 앞머리에 쌓여 매 사이클 0건 삭제로 **GC 가
        영구 정체**한다.

        ``coalesce(completed_at, created_at)`` 인 이유: ``completed_at IS NULL`` 인
        terminal 행은 ``<`` 비교가 false 라 TTL 대상이 아니고, 행이 있으니 스윕 대상도
        아니어서 영구 잔류한다.

        :returns: 삭제된 행들(``id``·``kind``·``legacy``·``completed_at``·refs 3종).
            ``ttl_seconds <= 0`` 이면 ``None``(GC 정지 신호 — 아무것도 지우지 않았다).
        """
        if ttl_seconds is None or ttl_seconds <= 0:
            return None

        def _run() -> list[dict[str, Any]]:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SET LOCAL lock_timeout = '5s'")
                    cur.execute("SET LOCAL statement_timeout = '30s'")
                    got = cur.execute(
                        "SELECT pg_try_advisory_xact_lock(%s, %s)",
                        (schema.LOCK_CLASSID, schema.LOCK_OBJ_GC),
                    ).fetchone()
                    if not got["pg_try_advisory_xact_lock"]:
                        return []          # 다른 worker 가 도는 중
                    cur.execute(
                        """
                        DELETE FROM kbp.jobs j
                         WHERE j.id IN (
                           SELECT c.id FROM kbp.jobs c
                            WHERE c.status IN ('succeeded','failed','canceled')
                              AND coalesce(c.completed_at, c.created_at)
                                  < now() - make_interval(secs => %s)
                              AND NOT EXISTS (
                                    SELECT 1 FROM kbp.jobs k
                                     WHERE k.parent_job_id = c.id
                                       AND k.status NOT IN
                                           ('succeeded','failed','canceled'))
                            ORDER BY coalesce(c.completed_at, c.created_at)
                            LIMIT %s)
                        RETURNING id, kind, legacy, completed_at,
                                  input_ref, payload_ref, result_ref
                        """,
                        (ttl_seconds, max(1, batch)),
                    )
                    rows = list(cur.fetchall())
                conn.commit()          # ← 여기까지 성공해야 키를 넘긴다
            return rows

        return _run()

    def job_ids_present(self, job_ids: Sequence[uuid.UUID]) -> set[uuid.UUID] | None:
        """주어진 id 중 실제 존재하는 것. **조회 실패는 ``None``**(fail-closed 신호).

        "빈 결과"와 "조회 실패"를 코드상 구분해야 한다 — 스윕은 증거의 부재로 삭제를
        결정하므로, 예외를 삼켜 빈 set 을 돌려주면 postgres 순간 장애만으로 살아있는
        객체가 전량 삭제된다.
        """
        if not job_ids:
            return set()
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SET LOCAL statement_timeout = '30s'")
                    cur.execute(
                        "SELECT id FROM kbp.jobs WHERE id = ANY(%s)", (list(job_ids),)
                    )
                    return {r["id"] for r in cur.fetchall()}
        except psycopg.Error:
            log.exception("job_ids_present failed — sweep must abort (fail-closed)")
            return None

    def refs_in_use(self, keys: Sequence[str]) -> set[str] | None:
        """주어진 키 중 어떤 행이든 참조 중인 것. **조회 실패는 ``None``**.

        job_id 존재 확인만으로는 부족하다 — 행 id 와 객체 키가 어긋난 행이 과거에
        만들어졌을 수 있어(멱등 재삽입 버그) 키를 직접 대조하는 방어를 한 겹 더 둔다.
        """
        if not keys:
            return set()
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SET LOCAL statement_timeout = '30s'")
                    cur.execute(
                        """
                        SELECT input_ref, payload_ref, result_ref FROM kbp.jobs
                         WHERE input_ref = ANY(%s) OR payload_ref = ANY(%s)
                            OR result_ref = ANY(%s)
                        """,
                        (list(keys), list(keys), list(keys)),
                    )
                    used: set[str] = set()
                    for r in cur.fetchall():
                        used.update(
                            v for v in (r["input_ref"], r["payload_ref"], r["result_ref"])
                            if v
                        )
                    return used
        except psycopg.Error:
            log.exception("refs_in_use failed — sweep must abort (fail-closed)")
            return None

    def count_jobs(self) -> int | None:
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    return int(cur.execute("SELECT count(*) n FROM kbp.jobs").fetchone()["n"])
        except psycopg.Error:
            return None

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

    # ── 야간 커뮤니티 배치 (A1) ────────────────────────────────────────────

    def db_now(self) -> Any:
        """서버 시각. 후보 술어의 두 축을 **한 시계로** 맞추기 위해 쓴다.

        ``graph_touch.touched_at`` 은 서버 ``now()`` 인데 ``last_success_at`` 을 워커의
        파이썬 시각으로 쓰면, PG 컨테이너 시계가 δ 뒤질 때 빌드 스냅샷 이후 δ 이내에
        완료된 적재가 ``touched_at > last_success_at`` 을 만족하지 못해 **영구 탈락**한다.
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT now() AS ts")
                return cur.fetchone()["ts"]

    def touch_graph(self, workspace_key: str) -> None:
        """그래프를 실제로 건드린 적재를 기록한다(야간 후보의 유일한 증거원)."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO kbp.graph_touch (workspace_key, touched_at)"
                    " VALUES (%s, now())"
                    " ON CONFLICT (workspace_key) DO UPDATE SET touched_at = now()",
                    (workspace_key,),
                )

    def record_attempt(self, workspace_key: str, eq_workspace_id: str | None) -> None:
        """제출을 **시도**했음을 기록한다 — 정렬 축(``last_attempt_at``).

        성공/실패/dedupe/ensure_workspace 실패 **전부** 여기를 지나야 한다. 한 경로라도
        빠지면 그 workspace 가 ``last_attempt_at IS NULL``(= epoch) 로 남아
        ``ORDER BY COALESCE(last_attempt_at, epoch) ASC`` 상단을 **영구 점유**한다.
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO kbp.community_builds"
                    "   (workspace_key, eq_workspace_id, last_attempt_at)"
                    " VALUES (%s, %s, now())"
                    " ON CONFLICT (workspace_key) DO UPDATE"
                    "   SET last_attempt_at = now(),"
                    # eq id 는 알아냈을 때만 덮는다(실패 경로는 None 을 넘긴다).
                    "       eq_workspace_id = COALESCE(EXCLUDED.eq_workspace_id,"
                    "                                  kbp.community_builds.eq_workspace_id)",
                    (workspace_key, eq_workspace_id),
                )

    def record_community_success(self, workspace_key: str, eq_workspace_id: str,
                                 snapshot_at: Any) -> None:
        """빌드 성공. ``last_success_at`` 에 **빌드 시작 스냅샷**을 넣는다(완료 시각 아님).

        완료 시각을 쓰면 수십 분짜리 빌드 **도중** 성공한 적재의 ``touched_at`` 이
        스냅샷보다 뒤·완료보다 앞이라 다음 밤 후보에서 **영구 탈락**한다.
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO kbp.community_builds"
                    "   (workspace_key, eq_workspace_id, last_success_at,"
                    "    finished_at, status)"
                    " VALUES (%s, %s, %s, now(), 'succeeded')"
                    " ON CONFLICT (workspace_key) DO UPDATE"
                    "   SET eq_workspace_id = EXCLUDED.eq_workspace_id,"
                    "       last_success_at = EXCLUDED.last_success_at,"
                    "       finished_at = now(), status = 'succeeded'",
                    (workspace_key, eq_workspace_id, snapshot_at),
                )

    def record_community_failure(self, workspace_key: str,
                                 eq_workspace_id: str | None) -> None:
        """빌드 실패. **``last_success_at`` 은 건드리지 않는다** → 후보에 그대로 남는다.

        이 writer 가 없으면 ``status``·``finished_at`` 컬럼이 영원히 안 채워진다.
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO kbp.community_builds"
                    "   (workspace_key, eq_workspace_id, finished_at, status)"
                    " VALUES (%s, %s, now(), 'failed')"
                    " ON CONFLICT (workspace_key) DO UPDATE"
                    "   SET finished_at = now(), status = 'failed',"
                    "       eq_workspace_id = COALESCE(EXCLUDED.eq_workspace_id,"
                    "                                  kbp.community_builds.eq_workspace_id)",
                    (workspace_key, eq_workspace_id),
                )

    def workspaces_needing_community(self, cap: int) -> tuple[list[str], int]:
        """(캡까지 자른 후보, **자르기 전 총 후보 수**).

        총 수를 함께 주지 않으면 ``batch_runs.backlog`` 기록도 warning 도 못 한다.
        정렬은 "가장 오래전에 시도한 것 먼저" — ``fail_streak``(A2) 없이 굶김을 막는다.
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    WITH cand AS (
                      SELECT gt.workspace_key, gt.touched_at, cb.last_attempt_at
                        FROM kbp.graph_touch gt
                        LEFT JOIN kbp.community_builds cb
                               ON cb.workspace_key = gt.workspace_key
                       WHERE gt.touched_at > COALESCE(cb.last_success_at, to_timestamp(0))
                    )
                    SELECT workspace_key, count(*) OVER () AS total
                      FROM cand
                     ORDER BY COALESCE(last_attempt_at, to_timestamp(0)) ASC,
                              touched_at ASC
                     LIMIT %s
                    """,
                    (cap,),
                )
                rows = cur.fetchall()
        if not rows:
            return ([], 0)
        return ([r["workspace_key"] for r in rows], int(rows[0]["total"]))

    def has_live_community_job(self, eq_workspace_id: str) -> bool:
        """queued|running community 잡이 있나. SQL 후보 쿼리에서 하지 않는 이유는
        축이 갈려 있어서다 — insert 잡은 kb id, community 잡은 eq UUID 이고, eq id 는
        빌드 이력이 있어야만 알 수 있어 **신규 workspace 에서 검사가 no-op** 이 된다.
        제출 루프가 ``ensure_workspace`` 로 eq id 를 얻은 **직후** 부른다.
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM kbp.jobs"
                    " WHERE kind = 'community' AND status IN ('queued','running')"
                    "   AND workspace_key = %s LIMIT 1",
                    (eq_workspace_id,),
                )
                return cur.fetchone() is not None

    def claim_run(self, name: str, run_date: Any, stale_minutes: int) -> bool:
        """그 밤을 이 프로세스가 맡는다(원자적). 이미 처리된 밤이면 False.

        ``failed`` 뿐 아니라 **굳은 ``started`` 도 회수**한다 — ``finish_run`` 이 안 불리는
        종료(SIGKILL·OOM·컨테이너 재기동)에서 행이 ``started`` 로 남으면 그 밤의 남은 틱이
        전부 claim 실패해 제출 0건이 되기 때문이다.

        ⚠️ ``interval '%s minutes'`` 로 쓰면 안 된다 — psycopg3 의 플레이스홀더 스캐너는
        SQL 문자열 리터럴 안도 치환해 ``interval '$1 minutes'`` 가 되고 파라미터 개수가
        어긋난다. 이 레포의 관용구는 ``make_interval(…=> %s)`` 뿐이다.
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO kbp.batch_runs (name, run_date) VALUES (%s, %s)
                    ON CONFLICT (name, run_date) DO UPDATE
                       SET run_at = now(), status = 'started', error = NULL
                     WHERE kbp.batch_runs.status = 'failed'
                        OR (kbp.batch_runs.status = 'started'
                            AND kbp.batch_runs.run_at
                                < now() - make_interval(mins => %s))
                    RETURNING 1
                    """,
                    (name, run_date, stale_minutes),
                )
                return cur.fetchone() is not None

    def has_stale_started(self, name: str, run_date: Any, stale_minutes: int) -> bool:
        """굳은 ``started`` 행이 있나. 창(WINDOW)이 끝난 뒤에도 마감 전이면 회수해
        재시도할 수 있게, 스케줄러가 창 판정에 이 값을 함께 쓴다.
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM kbp.batch_runs"
                    " WHERE name = %s AND run_date = %s AND status = 'started'"
                    "   AND run_at < now() - make_interval(mins => %s) LIMIT 1",
                    (name, run_date, stale_minutes),
                )
                return cur.fetchone() is not None

    def cancel_nightly_queued(self, *, key: str | None = None,
                              exclude_key: str | None = None) -> int:
        """야간 배치가 남긴 **queued** 잡을 취소한다. running 은 건드리지 않는다.

        ``batch_key`` 로만 매칭한다 — ``idem_key`` 는 claim 시점에도, ``JobRetryable``
        requeue 시에도 NULL 이 되어 정작 막아야 할 잡이 샌다.

        키 문자열은 **호출자가 완성해서** 넘긴다. SQL 안에서 ``'…:' || %(d)s`` 로 조립하면
        date→text 암묵 캐스트가 서버 ``DateStyle`` 에 의존해 매칭 0건이 될 수 있다.

        ⚠️ 파라미터가 있는 쿼리라 리터럴 ``%`` 는 ``%%`` 로 이스케이프한다.
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE kbp.jobs
                       SET status = 'canceled', completed_at = now(), idem_key = NULL
                     WHERE kind = 'community' AND status = 'queued'
                       AND batch_key LIKE 'community-nightly:%%'
                       AND (%(key)s::text  IS NULL OR batch_key =  %(key)s::text)
                       AND (%(excl)s::text IS NULL OR batch_key <> %(excl)s::text)
                    """,
                    {"key": key, "excl": exclude_key},
                )
                return cur.rowcount or 0

    def finish_run(self, name: str, run_date: Any, *, submitted: int, deduped: int,
                   failed: int, backlog: int, status: str,
                   error: str | None = None) -> None:
        """그 밤의 결과를 확정한다. 이게 안 불리면 행이 ``started`` 로 굳는다."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE kbp.batch_runs"
                    "   SET submitted=%s, deduped=%s, failed=%s, backlog=%s,"
                    "       status=%s, error=%s"
                    " WHERE name=%s AND run_date=%s",
                    (submitted, deduped, failed, backlog, status, error, name, run_date),
                )

    def last_batch_run(self, name: str, run_date: Any) -> dict[str, Any] | None:
        """**기대하는 밤**의 행을 돌려준다(가장 최근 행이 아니다).

        '가장 최근'을 쓰면 워커가 3일간 창을 놓쳐 3일 전 ``ok`` 행만 남아 있어도
        정상으로 보여 **멈춤을 못 잡는다**.
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM kbp.batch_runs WHERE name=%s AND run_date=%s",
                    (name, run_date),
                )
                return cur.fetchone()


def _as_uuid(value: uuid.UUID | str | None) -> uuid.UUID | None:
    if value is None or isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))
