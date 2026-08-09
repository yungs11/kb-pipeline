"""``kbp`` 스키마 DDL — idempotent, 동시 기동 안전.

설계 §2/§2.1.

`CREATE ... IF NOT EXISTS` 는 Postgres 에서 **동시 실행 안전하지 않다** — 여러
프로세스가 동시에 치면 `pg_namespace`/`pg_type` 의 unique_violation(23505) 이나
`tuple concurrently updated` 가 난다. facade 는 `gunicorn -w 4` 이고 facade-worker 까지
같은 순간에 부팅하므로 재기동마다 5개 이상이 이 DDL 을 친다. 그래서 전체를 advisory
lock 으로 감싸고, 그래도 새는 경합 에러는 삼킨다.

alembic 은 쓰지 않는다. 테이블이 두 개뿐이고 facade 에 마이그레이션 도구가 없다.
"""
from __future__ import annotations

import logging
import time

import psycopg

log = logging.getLogger("kb_pipeline.service.jobs.schema")

#: advisory lock 은 **DB 단위**다(스키마로 안 갈린다 — pg_locks 에 schema 컬럼이 없다).
#: 이 DB 는 edgequake 본체와 공유하므로 키 공간이 겹칠 수 있다.
#:
#: 그래서 **2-인자 형식** `pg_advisory_xact_lock(classid, objid)` 를 쓴다. Postgres 는
#: 1-인자(bigint) 형식과 2-인자 형식의 lock 공간을 **분리**해서 관리하므로(실측 확인),
#: bigint 형식을 쓰는 쪽(edgequake 의 sqlx 마이그레이션 lock)과는 **구조적으로 충돌하지
#: 않는다** — 값이 우연히 겹쳐도 무관하다.
#:
#: classid = 0x6B6270 = ASCII "kbp". objid 로 용도를 가른다.
LOCK_CLASSID = 0x6B6270      # 7037552
LOCK_OBJ_SCHEMA = 1          # 기동 시 DDL
LOCK_OBJ_CLAIM = 2           # claim 틱 (repo.py)
LOCK_OBJ_GC = 3              # TTL GC / 고아 스윕 (gc.py)
LOCK_OBJ_GLOBAL_SEARCH = 4   # global 검색 동시성 슬롯 (app.py)

#: 동시 DDL 경합에서만 나오는 SQLSTATE. 다른 프로세스가 먼저 만든 것이므로 무해하다.
_RACE_SQLSTATES = frozenset({
    "23505",  # unique_violation (pg_namespace/pg_type)
    "42P07",  # duplicate_table
    "42710",  # duplicate_object
    "42P06",  # duplicate_schema
})

DDL = """
CREATE SCHEMA IF NOT EXISTS kbp;

CREATE TABLE IF NOT EXISTS kbp.jobs (
  id               uuid PRIMARY KEY,
  kind             text NOT NULL,
  idem_key         text,
  status           text NOT NULL,
  stage            text,
  workspace_key    text,
  batch_key        text,
  parent_job_id    uuid,
  legacy           boolean NOT NULL DEFAULT false,
  payload          jsonb,
  payload_ref      text,
  input_ref        text,
  result           jsonb,
  result_ref       text,
  error            text,
  attempt_count    int  NOT NULL DEFAULT 0,
  cancel_requested boolean NOT NULL DEFAULT false,
  claimed_by       text,
  claimed_at       timestamptz,
  heartbeat_at     timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now(),
  started_at       timestamptz,
  completed_at     timestamptz
);

CREATE INDEX IF NOT EXISTS jobs_claim_idx   ON kbp.jobs (status, created_at, id);
CREATE INDEX IF NOT EXISTS jobs_running_idx ON kbp.jobs (status, kind, workspace_key);
CREATE INDEX IF NOT EXISTS jobs_batch_idx   ON kbp.jobs (batch_key) WHERE batch_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS jobs_parent_idx  ON kbp.jobs (parent_job_id) WHERE parent_job_id IS NOT NULL;

-- 이미 만들어진 테이블의 업그레이드 경로. CREATE TABLE IF NOT EXISTS 는 컬럼을 더해주지
-- 않으므로 ALTER 를 함께 둔다(둘 다 멱등).
ALTER TABLE kbp.jobs ADD COLUMN IF NOT EXISTS idem_key text;

-- 제출 멱등키. failed/canceled 로 끝난 잡은 idem_key 를 NULL 로 비우므로(repo.complete),
-- 부분 유니크 조건은 "NULL 이 아닌 것"만으로 충분하다. 고친 뒤 같은 파일을 다시 올리면
-- 새 잡이 만들어진다 — 실패가 영구히 캐시되지 않는다.
CREATE UNIQUE INDEX IF NOT EXISTS jobs_idem_idx ON kbp.jobs (idem_key) WHERE idem_key IS NOT NULL;

-- TTL GC 용. **표현식 인덱스**여야 한다 — GC 쿼리의 술어·정렬이
-- `coalesce(completed_at, created_at)` 식이라 단일 컬럼 `(completed_at)` 인덱스는
-- WHERE 에도 ORDER BY 에도 매칭되지 않고 seq scan + sort 로 떨어진다. 그러면 jobs 가
-- 커진 배포에서 GC 가 매 사이클 statement_timeout 에 걸려 영구히 0건이 된다.
-- (coalesce 는 IMMUTABLE, 두 컬럼 모두 timestamptz 라 식 인덱스가 가능하다.)
CREATE INDEX IF NOT EXISTS jobs_gc_idx
    ON kbp.jobs ((coalesce(completed_at, created_at)))
 WHERE status IN ('succeeded','failed','canceled');

-- ── global 검색 동시성 슬롯 ────────────────────────────────────────────────
-- facade 의 global 검색은 map N + reduce 1 의 **순차 LLM** 이라 요청 하나가 분 단위로
-- 스레드를 점유한다. 잡 큐의 KBP_JOB_LIMIT_COMMUNITY 상한은 잡 경로에만 적용되므로,
-- 동기 /search 경로에는 아무 상한도 없다 — D10 이 /communities/build 에서 없앤
-- "facade 웹 프로세스 점유" 문제를 다른 경로로 재도입하는 셈이다.
--
-- **threading.Semaphore 로는 안 된다** — Dockerfile.facade 가 `-w 4` 로 뜨므로
-- 모듈 스코프 세마포어는 프로세스마다 별도 인스턴스다. "상한 2" 를 의도해도 실제
-- 전역 상한은 최대 4배가 된다. 그래서 DB 카운터로 프로세스 경계를 넘는다.
CREATE TABLE IF NOT EXISTS kbp.global_search_slots (
  id         serial PRIMARY KEY,
  claimed_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS kbp.job_workers (
  worker_id    text PRIMARY KEY,
  capacity     int  NOT NULL,
  active_count int  NOT NULL,
  started_at   timestamptz NOT NULL,
  heartbeat_at timestamptz NOT NULL
);
"""


def ensure_schema(dsn: str, *, attempts: int = 9, max_backoff: float = 30.0) -> None:
    """스키마를 만든다(멱등). 실패 시 지수 백오프 후 재시도, 끝내 실패하면 예외.

    호출 지점은 FastAPI ``lifespan`` 과 worker ``main()`` 이다 — **모듈 import 시점이
    아니다**. import 시점에 부르면 테스트 수집만으로 DB 접속을 시도한다.

    재시도가 필요한 이유는 두 가지다. 하나는 위의 DDL 경합, 다른 하나는 compose 첫
    기동에서 postgres 가 아직 접속을 받지 않는 구간이다. 오늘 facade 는 postgres 없이도
    떠서 ``/healthz`` 200 을 주므로, 여기서 즉시 죽으면 크래시루프가 된다.

    기본 예산은 총 **≥120s**(1+2+4+8+16+30+30+30). ``depends_on`` 에 기대지 않는다 —
    배포 대상 폐쇄망 podman 은 그 조건을 무시하고(``docs/airgap-deploy.md:126``), 이
    postgres 이미지는 init 중 서버를 재시작해 ``pg_isready`` 가 과도기 서버에 붙는다.
    예산을 다 쓰고도 실패하면 컨테이너의 ``restart: unless-stopped`` 가 자가치유한다.

    :raises psycopg.Error: ``attempts`` 회 모두 실패한 경우(명확한 기동 실패).
    """
    backoff = 1.0
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            _apply(dsn)
            return
        except psycopg.Error as exc:  # noqa: PERF203 - 재시도 루프
            last = exc
            log.warning(
                "ensure_schema attempt %d/%d failed (%s); retrying in %.1fs",
                attempt, attempts, exc, backoff,
            )
            if attempt < attempts:
                time.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
    log.error("ensure_schema gave up after %d attempts", attempts)
    raise last  # type: ignore[misc]


def _apply(dsn: str) -> None:
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            # 트랜잭션 종료 시 자동 해제되는 lock. DDL 전체가 한 트랜잭션이므로
            # 여러 프로세스가 동시에 들어와도 한 번에 하나만 실행한다.
            cur.execute("SELECT pg_advisory_xact_lock(%s, %s)",
                        (LOCK_CLASSID, LOCK_OBJ_SCHEMA))
            try:
                cur.execute(DDL)
            except psycopg.Error as exc:
                if getattr(exc, "sqlstate", None) in _RACE_SQLSTATES:
                    # advisory lock 을 잡고도 새는 경우(다른 버전/외부 도구가 동시에
                    # 만든 경우). 이미 존재한다는 뜻이라 성공으로 친다.
                    log.info("ensure_schema race absorbed: %s", exc)
                    conn.rollback()
                    return
                raise
        conn.commit()


#: 스키마가 통째로 사라졌을 때 나오는 SQLSTATE.
#:
