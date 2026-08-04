<!-- plan-version: v6 -->
<!-- ultracode-validation: PENDING -->

# facade 잡 큐 — 동시처리·유량제어를 kbp 로 이관

> v5 → v6: 5차 검증 — 한 렌즈는 **READY**. 남은 blocking 은 **v5 가 틀린 것 하나**:
> `claimed_by` 만으로는 lease 펜싱이 안 된다(§3.3 — D4 의 논리가 거꾸로였다). worker 는
> 배포상 1개이고 `worker_id` 는 프로세스 수명 동안 고정이라, **같은 worker** 가 회수된
> 잡을 다시 집으면 옛 스레드의 쓰기가 술어를 통과한다. `attempt_count` 를 세대 토큰으로
> 함께 검사해 스키마 변경 없이 막았다(구현·테스트 완료).
>
> v4 → v5: 4차 검증 — **새 범위 확장 지적 0건**. 남은 것은 v3→v4 편집에서 생긴 문서
> 내부 모순(§6 표의 풀 잔재, §8 의 504 vs §4.5 의 409, LEGACY_WAIT 3600 vs 3300)과
> `MAX_RUNTIME` 재산정(§3.7), `attempt_count` 가드 누락(§3.1), 취소 경합(§4.2).
>
> v3 → v4: 범위 가드를 건 3차 검증에서 (A)/(B) 급 결함 20건. 큰 것: 커넥션 풀 폐기
> (`psycopg_pool` 은 별도 배포판이라 기동 실패 — §2.3), kind 별 회수 파라미터를 단일
> 스칼라로 표현 불가(§3.1), insert 무재시도가 runner 에만 있어 급사 시 중복 적재(§3.1·§5.2),
> `MAX_RUNTIME` 이 submit 타임아웃 누락(§3.7), 레거시 `/parse`·`/ingest` 가 `async def`
> 라 이벤트 루프 블로킹(§4.4).
>
> v2 → v3: 범위를 목표로 되돌렸다. v2 는 검증 지적을 전부 흡수하며 부풀었다.
> Phase 1 에 남기는 기준은 **없으면 유량제어가 동작하지 않거나 기존 동작이 깨지는 것**
> 하나뿐이다. 뺀 항목과 그 근거는 [`...-deferred.md`](2026-08-03-facade-job-queue-deferred.md).
>
> 범위를 줄이면서 **결함 자체가 사라진** 결정 다섯: insert 무재시도(D5), TTL GC 없음(D2),
> 멱등키 없음(D1), 스트리밍 없음(D3), `lease_epoch` 대신 `claimed_by` 술어(D4).

## 0. 목표와 경계

**목표**: facade 가 동시처리량·유량제어를 소유한다. kb-backend 의 DB 폴링 큐를 kbp 로
가져온다. 그 이상은 하지 않는다.

### 0.1 문제

kb-backend 는 없어지고 facade(kbp, :19000)가 유일한 API 서버로 남는다. 지금 facade 는
유량제어 수단이 **하나도 없다** — `/parse`·`/chunk`·`/insert`·`/ingest` 가 전부 동기
블로킹이고(`service/app.py:112,134,221,276`), 웹 프로세스가 직접 parse-svc·
adaptive_chunk·edgequake 를 호출하며 수 분간 응답을 기다린다. 컨테이너 배포는
`gunicorn -w 2`(`Dockerfile.facade:15`) 라 무거운 요청 2건이면 facade 전체가 멎는다.

kb-backend 는 이미 durable 큐를 갖고 있다 — `batch_ingestion_items`,
`FOR UPDATE SKIP LOCKED` claim, lease heartbeat, stale 회수, worker 레지스트리
(`knowledge_base/backend/app/batch_repository.py`, `app/workers/batch_worker.py`).
kb 가 사라지면 이 능력도 같이 사라진다.

### 0.2 기존 경로를 202 로 바꿀 수 없다

유일한 실소비자 kb-backend 의 facade 클라이언트:

```python
resp = self._request("POST", url, files=files, data=data)   # 429/5xx 재시도 래퍼
resp.raise_for_status()                                      # 202 는 예외 아님
body = resp.json() ...
return {"enriched_content": body.get("enriched_content") or "", ...}
```
(`knowledge_base/backend/app/clients/kb_pipeline_client.py:175-186`, chunk 247·insert 290 동일)

`/parse` 가 `202 {job_id}` 를 돌려주면 `raise_for_status()` 는 통과하고
`enriched_content` 는 `""` 가 된다. **예외 없이 빈 문서가 청킹·적재된다.**

그래서 경로를 나눈다:

| | Phase 1 | Phase 2 |
|---|---|---|
| `POST /jobs/{kind}` (신규) | 202 `{job_id}` — 비동기 정식 경로 | 유일 경로 |
| `POST /parse`·`/chunk`·`/insert`·`/ingest` (기존) | **응답 계약 불변**. 내부적으로 잡을 제출하고 완료까지 대기 | 제거 |

기존 경로가 동기로 남아도 **유량제어는 Phase 1 에 즉시 확보된다** — 다운스트림 호출이
worker 슬롯 안에서만 일어나기 때문이다. 대기하는 쪽이 다운스트림이 아니라 큐로 바뀐다.

### 0.3 확정 결정

| 항목 | 결정 |
|---|---|
| 정식 API | 비동기 잡(제출 + 폴링), 신규 `/jobs/*` |
| 잡화 대상 | parse · chunk · insert · ingest |
| 유지(잡 아님) | `/search` `/chunks` `/doc` `/insert/status` `/communities/build` `/healthz` |
| 기존 4경로 | Phase 1 동안 응답 계약·인증 요구 **불변**(내부만 잡 경유) |
| 슬롯 위치 | DB (in-memory 불가 — facade 가 다중 프로세스) |
| 제한 단위 | 단계별(kind 버킷) + 테넌트별(workspace) |
| 단계 연결 | 서버가 결과 보관, 다음 단계는 `job_id` 참조 |
| worker | facade API 와 별도 프로세스(같은 이미지, command 만 다름) |

## 1. 구조

```
소비자 ──POST /jobs/parse──▶ facade API :19000 (gunicorn -w 4)
                               │ 1) 업로드 → MinIO staging
                               │ 2) kbp.jobs INSERT (queued)
                               │ 3) 202 {job_id}          ← 밀리초
                               ▼
                         postgres :5433  schema kbp     ← 유일한 조율 지점
                               ▲
                               │ 폴링 + advisory lock claim
             facade-worker  python -m service.worker
                               │ 슬롯 안에서만 다운스트림 호출
                               ▼
                   parse-svc / adaptive_chunk / edgequake
```

불변 규칙.

1. **API 프로세스는 다운스트림을 호출하지 않는다** (읽기 경로 제외).
   **알려진 예외**: `/communities/build` 는 현행 `BackgroundTasks` 를 유지한다
   (`app.py:343-362`). 적재 유량과 무관하고 호출 빈도가 낮다. Phase 2 에서 편입(D10).
2. **다운스트림 호출은 worker 슬롯 안에서만 일어난다.**
3. **API↔worker 조율은 postgres 만 쓴다.** 폐쇄망 번들에 새 인프라가 없다.
4. **lease 를 잃은 worker 의 쓰기는 거부된다** (§3.3).

새 런타임 의존성 없음 — `psycopg[binary]>=3.1`·`minio>=7.2.0` 이 이미
`requirements.txt` 에 있다. **커넥션 풀은 쓰지 않는다**(§2.3) — `psycopg_pool` 은
`psycopg[binary]` 에 포함되지 않는 별도 배포판이라 폐쇄망에서 현장 복구가 불가능하다.

## 2. 데이터 모델

`KBP_PG_DSN` 이 가리키는 edgequake postgres 안에 **`kbp` 스키마**를 새로 만든다.

```sql
CREATE SCHEMA IF NOT EXISTS kbp;

CREATE TABLE IF NOT EXISTS kbp.jobs (
  id             uuid PRIMARY KEY,
  kind           text NOT NULL,          -- parse | chunk | insert | ingest
  status         text NOT NULL,          -- queued | running | succeeded | failed | canceled
  stage          text,                   -- parsing | chunking | inserting | NULL
  workspace_key  text,                   -- NULL = 테넌트 상한 미적용 (§3.4)
  batch_key      text,
  parent_job_id  uuid,                   -- parse_job_id / chunk_job_id (§4.1)
  legacy         boolean NOT NULL DEFAULT false,  -- 기존 4경로 경유 여부 (§5.3)
  payload        jsonb,
  payload_ref    text,                   -- 큰 payload MinIO 키
  input_ref      text,                   -- 업로드 staging MinIO 키
  result         jsonb,
  result_ref     text,                   -- 큰 결과 MinIO 키
  error          text,
  attempt_count  int  NOT NULL DEFAULT 0,
  cancel_requested boolean NOT NULL DEFAULT false,
  claimed_by     text,
  claimed_at     timestamptz,
  heartbeat_at   timestamptz,
  created_at     timestamptz NOT NULL DEFAULT now(),
  started_at     timestamptz,
  completed_at   timestamptz
);

CREATE INDEX IF NOT EXISTS jobs_claim_idx   ON kbp.jobs (status, created_at, id);
CREATE INDEX IF NOT EXISTS jobs_running_idx ON kbp.jobs (status, kind, workspace_key);
CREATE INDEX IF NOT EXISTS jobs_batch_idx   ON kbp.jobs (batch_key) WHERE batch_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS jobs_parent_idx  ON kbp.jobs (parent_job_id) WHERE parent_job_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS kbp.job_workers (
  worker_id     text PRIMARY KEY,
  capacity      int  NOT NULL,
  active_count  int  NOT NULL,
  started_at    timestamptz NOT NULL,
  heartbeat_at  timestamptz NOT NULL
);
```

**슬롯 테이블은 두지 않는다.** 슬롯 점유 = `status='running'` 행의 개수다. 상태가 한
곳에만 있어 "슬롯은 비었는데 잡은 돌고 있다" 는 불일치가 구조적으로 불가능하다.

`parent_job_id` 는 `payload` 안이 아니라 **전용 컬럼**이다. payload 가 MinIO 로
오프로딩되면 jsonb 안의 참조가 SQL 에서 보이지 않기 때문이다(GC 는 Phase 1 비범위지만
컬럼은 지금 두는 게 싸다).

### 2.1 DDL 동시 실행

`CREATE ... IF NOT EXISTS` 는 Postgres 에서 동시 실행 안전하지 않다(`pg_namespace`/
`pg_type` 의 23505, `tuple concurrently updated`). facade 는 gunicorn 다중 워커 +
facade-worker 가 동시에 부팅한다(재기동마다 5개 이상이 같은 DDL 을 친다).

- `ensure_schema()` 전체를 `pg_advisory_xact_lock(hashtext('kbp.schema'))` 안에서 실행.
- `23505`/`42P07`/`42710`/`tuple concurrently updated` 는 1회 재시도 후 무시.
- **호출 지점은 FastAPI `lifespan` 과 worker `main()` 이다.** 모듈 import 시점이 아니다
  (import 시점이면 테스트 수집만으로 DB 접속을 시도한다).
- facade-worker 의 `depends_on` 에 `postgres: {condition: service_healthy}`.
- **실패 정책**: 지수 백오프로 총 **120s 이상** 재시도하고 그래도 실패하면 명확한
  메시지와 함께 기동 실패시킨다. `depends_on` 에 의존하지 않는다 — 배포 대상 폐쇄망
  podman 은 이 조건을 무시하고(`docs/airgap-deploy.md:126`), 이 postgres 이미지는 init
  중 서버를 재시작해 `pg_isready` 가 과도기 서버에 붙는다. 끝내 실패하면
  `restart: unless-stopped` 가 자가치유한다.
- **런타임 복구**: repo 의 제출·claim 경로가 `42P01`(undefined_table) 또는
  `3F000`(invalid_schema_name) 을 만나면 `ensure_schema()` 를 1회 재실행하고 재시도한다.
  dev 에서 `start_dedicated_edgequake.sh` 가 postgres 를 **볼륨 없이 재생성**하면
  (`:8-11`) 행이 아니라 **스키마 자체가 사라지는데**, 이미 떠 있는 facade·worker 는
  lifespan 을 다시 타지 않아 영구히 깨진 상태로 남는다.

### 2.2 payload·result 오프로딩

직렬화 크기가 `KBP_JOB_INLINE_MAX_BYTES`(기본 262144) 이하면 jsonb 인라인, 초과하면
MinIO `{prefix}/{job_id}/{payload|result}.json` 에 넣고 `*_ref` 에 키만 남긴다.
`/chunk` 를 `enriched_content` 본문으로 제출하면 payload 가 수 MB 가 되므로 result 와
동일 규칙이 필요하다.

**읽기 측 복원은 불변식이다.** 레거시 래퍼·`GET /jobs/{id}/result`·runner(체인 해석)는
`*_ref` 가 있으면 **반드시 MinIO 에서 읽어 본문을 복원한다**. 복원 실패는 500 이고,
**빈 본문을 반환하지 않는다**. 262144 는 parse 결과에 대해 예외가 아니라 기본 경로라
(enriched_content + pages + page_spans + table_blocks), 이 배선이 빠지면 kb 는
`200 + enriched_content=""` 를 받아 `or ""` 로 조용히 흡수한다 — §0.2 가 202 전환을
포기하며 막으려던 '빈 문서 청킹·적재' 가 그대로 재현된다.

### 2.3 커넥션 예산

이 스키마는 **edgequake 본체가 쓰는 바로 그 postgres** 안에 있다(`docker-compose.yml:222`).
대기 핸들러가 커넥션을 붙잡고 sleep 하면 edgequake 가 커넥션을 얻지 못해 적재·검색이
동반 실패하는 새 단일 실패점이 생긴다.

**커넥션 풀을 쓰지 않는다.** `repo.py` 의 모든 연산은 `psycopg.connect(dsn)` 으로 열고
`finally` 에서 닫는다(작업당 1커넥션). 연결은 **TCP + SCRAM 왕복**이라 공짜가 아니므로
(`docker-compose.yml` 은 `postgres:5432`, `scripts/facade.env:3` 은 `localhost:5433`)
폴링 주기를 너무 짧게 잡지 않는다 — `KBP_JOB_WAIT_POLL_INTERVAL_SECONDS` 기본 2s 면
대기 1건당 초당 0.5 연결이다.

- 풀을 쓰면 `psycopg_pool` 이 필요한데 이는 `psycopg[binary]` 에 없는 별도 배포판이다
  (실측: `import psycopg_pool` → `ModuleNotFoundError`). 폐쇄망에 새 휠을 넣어야 한다.
- 풀을 쓰면 상한 산정이 또 하나의 결함원이 된다 — worker 는 실행 스레드 4 + 폴링·
  heartbeat 스레드 1 = 5 를 동시에 요구하므로 `max_size=3` 이면 heartbeat 스레드가
  `PoolTimeout` 으로 막혀 §6.1 불변식이 무의미해진다.
- **대기 핸들러는 커넥션을 붙잡고 자지 않는다** — 조회 1회마다 열고 닫은 뒤 sleep 한다.
  이것이 이 절의 실제 목적이다(edgequake 가 커넥션을 못 얻는 상황 방지).

동시 커넥션 상한 = 실제 진행 중인 연산 수다. 최악은 facade 4 프로세스 × waiter 4 +
worker(실행 4 + 폴링 1 + heartbeat 1) = 22 이고, Postgres 기본 `max_connections=100` 안에서
edgequake 여유를 남긴다.

## 3. claim 알고리즘

### 3.1 직렬화

PostgreSQL 은 `FOR UPDATE` 와 윈도우 함수를 한 쿼리에 못 쓴다. 여러 worker 가 각자
"현재 running 수"를 읽고 각자 승인하면 전역 상한이 깨진다. **claim 트랜잭션 전체를
advisory lock 으로 직렬화**한다. claim 안에서는 다운스트림을 호출하지 않으므로(수 ms)
직렬화 비용이 없다.

```
BEGIN;
  SELECT pg_advisory_xact_lock(hashtext('kbp.jobs.claim'));

  -- (1) 회수: stale lease + 최대 실행시간 초과.  상한은 kind 별이므로 상수 테이블 조인.
  UPDATE kbp.jobs j
     SET status = CASE
           WHEN j.cancel_requested        THEN 'canceled'
           WHEN j.stage = 'inserting'     THEN 'failed'      -- ← 최우선. §5.2 참조
           WHEN j.attempt_count >= lim.max_attempts THEN 'failed'
           ELSE 'queued' END,
         error = CASE WHEN j.cancel_requested THEN NULL
                      WHEN j.stage = 'inserting'
                        THEN 'insert already submitted to edgequake; not retried'
                      ELSE $reason END,
         claimed_by = NULL, claimed_at = NULL, heartbeat_at = NULL,
         completed_at = CASE WHEN j.cancel_requested
                               OR j.stage = 'inserting'
                               OR j.attempt_count >= lim.max_attempts THEN now() END
    FROM unnest($kinds, $max_attempts, $max_runtimes)
         AS lim(kind, max_attempts, max_runtime)   -- 값은 env 로 조립(§3.7)
   WHERE j.kind = lim.kind
     AND j.status = 'running'
     AND (j.heartbeat_at < now() - $stale_interval
          OR j.started_at  < now() - lim.max_runtime);

  -- (1b) queued 인데 취소 요청된 행 종결 (좀비 방지)
  UPDATE kbp.jobs SET status='canceled', completed_at=now()
   WHERE status='queued' AND cancel_requested;

  -- (1c) 만료 worker 레지스트리 행 삭제
  DELETE FROM kbp.job_workers WHERE heartbeat_at < now() - $worker_stale_interval;

  -- (2) 현재 점유 집계
  SELECT kind, workspace_key, count(*) FROM kbp.jobs
   WHERE status='running' GROUP BY 1,2;

  -- (3) 후보 조회 — (kind, workspace) 파티션당 상위 N건 (기아 회피, §3.6)
  SELECT id, kind, workspace_key FROM (
    SELECT id, kind, workspace_key, created_at,
           -- sentinel 금지. PARTITION BY 는 NULL 을 한 그룹으로 묶으므로 coalesce 가
           -- 필요 없고, 어떤 sentinel 문자열도 실제 workspace_key 와 충돌할 수 있다.
           -- (파이썬 소스에 SQL 을 박을 때 '\x00…' 은 진짜 NUL 이 되어 문법 오류다 —
           --  실제로 첫 라이브 실행에서 이걸로 깨졌다.)
           row_number() OVER (PARTITION BY kind, workspace_key
                              ORDER BY created_at, id) AS rn
      FROM kbp.jobs
     WHERE status='queued' AND cancel_requested = false
       AND attempt_count < $max_attempts_for(kind)   -- 소진 행 재claim 금지
  ) c
   WHERE rn <= $per_partition_scan
   ORDER BY created_at, id
   LIMIT $scan_limit;

  -- (4) plan_admissions() 순수 함수로 승인 계산

  -- (5) 조건부 승인
  UPDATE kbp.jobs
     SET status='running', claimed_by=$w, claimed_at=now(), heartbeat_at=now(),
         attempt_count=attempt_count+1,
         started_at=now(), error=NULL, stage=NULL
   WHERE id = ANY($admitted)
     AND status='queued' AND cancel_requested = false
     AND attempt_count < $max_attempts_for(kind)
  RETURNING id, kind;         -- ← RETURNING 집합만 실행한다
COMMIT;
```

회수 상한을 **kind 무관 스칼라 하나**로 두면 안 된다. 스칼라 3 이면 stale insert 가
재큐되어 §5.2 의 무재시도 보호가 무너지고(= edgequake 중복 적재), 작은 스칼라면 정상
진행 중인 ingest 가 반드시 회수되어 §3.3 이 경고한 2중 실행이 난다.

`attempt_count` 가드가 (3)·(5) 양쪽에 있어야 한다. (1)의 회수는 `attempt_count >= max`
일 때 `failed` 로 종결하지만, runner 가 실패로 종결시킨 뒤 다시 `queued` 로 돌아가는
경로(§5.1 재시도)는 회수를 거치지 않는다. 가드가 없으면 소진된 행을 claim 이 무한히
다시 집는다 — insert 는 `max=1` 이라 한 번의 requeue 만으로 재적재 루프가 된다.

kind → `(max_attempts, max_runtime)` 상수 테이블은 **`service/jobs/admission.py` 한 곳**
에 두고 env 로 로드한다(§3.7). SQL 에 리터럴로 박으면 env 를 바꿔도 회수 임계가 변하지
않아 현장에서 임계를 올릴 수 없다.

`stage='inserting'` 분기가 **kind 무관 최우선**인 이유: §5.2 의 무재시도 판정이
runner(프로세스 내 예외 처리)에만 있으면 worker 급사·OOM·`max_runtime` 초과 경로에는
적용되지 않는다. 회수 UPDATE 가 `stage` 를 보지 않으면, edgequake 에 이미 문서를 제출한
ingest 잡이 `attempt_count(1) < 3` 이라 `queued` 로 돌아가 parse→chunk→insert 를 다시
돌고 `submit_document`(`edgequake.py:379`)가 멱등키 없이 같은 문서를 2건 적재한다.

(5)의 `AND status='queued' AND cancel_requested=false` 가 없으면, 취소 API 는 advisory
lock 을 잡지 않으므로 (3) 이후 커밋된 `canceled` 를 승인 UPDATE 가 조용히 `running` 으로
되살린다(취소 유실). `RETURNING` 으로 **실제 승인된 행만** 실행한다.

`started_at` 은 매 시도마다 갱신한다(`COALESCE` 아님) — (1)의 `max_runtime` 판정이
시도 단위여야 하기 때문이다. 최초 접수 시각은 `created_at` 이 갖고 있다.

### 3.2 승인 계산 (순수 함수)

```python
def plan_admissions(candidates, running_by_bucket, running_by_workspace,
                    bucket_limits, workspace_limit, local_free) -> list[UUID]:
    """FIFO 순서로 훑으며 kind 버킷·workspace·로컬 슬롯이 모두 허용하는 잡만 승인한다.

    승인할 때마다 running_by_bucket / running_by_workspace / local_free 를 **즉시
    증가·감소시키고**, 이후 후보는 갱신된 값으로 판정한다. 스냅샷만 보고 판정하면
    한 틱에 후보 전체가 승인되어 상한이 무너진다.

    - kind → 버킷 집합 매핑은 BUCKETS_FOR_KIND (§3.5). ingest 는 세 버킷을 동시 점유.
    - **running_by_bucket 은 (2)의 (kind, count) 집계를 BUCKETS_FOR_KIND 로 전개해
      만든다.** running ingest 1건은 parse·chunk·insert 각 1을 점유한다. kind 키를
      그대로 버킷 키로 쓰면 running ingest 가 어느 버킷도 점유하지 않아(버킷 상한표에
      'ingest' 항목이 없다) §3.5 가 막으려던 과승인이 그대로 재현된다.
    - 한 후보가 막혀도 뒤의 후보 검사를 계속한다(head-of-line blocking 회피).
    - workspace_key 가 None 이면 workspace 상한을 적용하지 않는다 (§3.4).
    - 알 수 없는 kind 는 승인하지 않는다 (무제한 승인 금지).
    """
```

DB 없이 테스트 가능하다. §8 의 주요 검증 대상.

### 3.3 lease 방어

worker 의 **모든 잡 쓰기**(heartbeat, stage 갱신, complete, requeue)는
`WHERE id=$1 AND claimed_by=$me AND attempt_count=$gen AND status='running'` 를 강제한다.
`rowcount == 0` 이면 **"내 lease 를 잃었다"** 는 뜻이다.

**`claimed_by` 만으로는 부족하다.** worker 는 배포상 1개이고(§7.1 에 replicas 지정이
없다) `worker_id` 는 프로세스 수명 동안 고정이다. 그래서 잡이 회수된 뒤 **같은 worker**
가 다시 집으면 옛 스레드의 쓰기가 그대로 통과한다:

```
attempt 1 (스레드 A) 실행 중 → heartbeat 랩스 또는 MAX_RUNTIME 초과로 회수 → queued
같은 worker 가 재claim → attempt 2 (스레드 B). claimed_by 는 **동일**
스레드 A 가 complete → 술어 통과 → attempt 1 결과로 succeeded 종결
스레드 B 는 그대로 진행 → edgequake 에 두 번째 문서 제출   ← 중복 적재
```

claim 이 `RETURNING attempt_count` 로 세대를 돌려주고 모든 쓰기가 그 값을 함께 검사하면
스키마 변경 없이 막힌다. 전용 `lease_epoch` 컬럼은 여전히 불필요하다(D4 를 이 근거로
다시 썼다).

**lease 상실의 처리는 부작용 기준으로 갈린다.**

| 시점 | 대상 | 처리 |
|---|---|---|
| 부작용 **이후** | `complete` / `requeue` | 결과를 폐기한다(이미 벌어진 일) |
| 부작용 **직전** | `set_stage` | **다운스트림을 호출하지 않고 즉시 중단한다** |

`stage='inserting'` 사전 커밋이 중복 적재 방어의 유일한 게이트다. 여기서 "로그만 남기고
계속" 하면 방어가 통째로 무의미해진다 — 회수된 좀비가 edgequake 에 문서를 한 번 더
제출한다.

이관 원본 `complete_item`(`batch_repository.py:196-205`)에는 이 검증이 없다 — 옮기면서
추가하는 부분이다.

**회수의 한계**: 회수는 DB 행만 `queued` 로 되돌릴 뿐 진행 중인 HTTP 호출을 끊지 못한다.
좀비는 슬롯 밖에서 다운스트림을 계속 점유한다. 그래서 회수 임계는 정상 최악 소요보다
**넉넉해야** 한다(§3.7).

### 3.4 workspace_key 와 NULL

현행 `/parse`(`app.py:112`)·`/chunk`(`app.py:136`) 에는 workspace 개념이 아예 없고 kb
클라이언트도 안 싣는다. 이들을 한 버킷에 몰면 `KBP_JOB_LIMIT_PER_WORKSPACE=2` 가 사실상
전역 상한이 되어 **처리량이 현행보다 나빠진다.**

**규칙**: `workspace_key IS NULL` 이면 workspace 상한을 적용하지 않는다(kind 버킷 상한만).
`/jobs/parse`·`/jobs/chunk` 는 `workspace_id` 를 optional 로 받고, 주면 테넌트 공정성
혜택을 받는다. `/jobs/insert`·`/jobs/ingest` 는 `workspace_id` 가 필수라 항상 채워진다.

### 3.5 kind → 버킷 매핑

```python
BUCKETS_FOR_KIND = {
    "parse":  ("parse",),
    "chunk":  ("chunk",),
    "insert": ("insert",),
    "ingest": ("parse", "chunk", "insert"),   # 세 구간을 모두 호출하므로 셋 다 예약
}
```

ingest 는 parse-svc·adaptive_chunk·edgequake 를 모두 호출한다(`app.py:288-327`). parse
슬롯만 잡으면 `KBP_JOB_LIMIT_INSERT=2`("임베딩 서버 처리량에 종속")의 근거가 무너진다
(ingest 4건 + chunk 2건 → adaptive 동시 6, edgequake 동시 6). claim 시점에 세 버킷을
함께 예약하고 종료 시 함께 해제한다(실행 중 중첩 취득이 아니므로 데드락 없음).

**귀결(수용)**: 동시 ingest 상한 = `min(4, 2, 2) = 2` 이고, parse/chunk 부하가 지속되면
ingest 승인이 지연될 수 있다. Phase 1 트래픽은 kb 의 단계별 호출이 대부분이라 혼재가
드물다. aging/예약은 비범위(D7).

### 3.6 기아 회피

(3)의 후보 조회를 전역 FIFO 윈도로만 하면 한 파티션이 윈도를 채워 다른 파티션이 영원히
승인되지 않는다. 파티션을 **`(kind, workspace_key)`** 로 잡는다 — `workspace_key` 만으로
잡으면 Phase 1 트래픽 대부분이 NULL 이라 한 파티션에 몰리고, 후보 상위 N건이 전부 chunk
잡이면 parse 슬롯이 비어 있어도 그 틱에 아무것도 승인되지 않는다(kind 간 head-of-line).

### 3.7 설정

| env | 기본 | 근거 |
|---|---|---|
| `KBP_JOB_LIMIT_PARSE` | 4 | parse-svc `gunicorn -w 4` (`Dockerfile.parse-svc:27`) |
| `KBP_JOB_LIMIT_CHUNK` | 2 | adaptive_chunk 4방법 경쟁이라 건당 비용 큼 |
| `KBP_JOB_LIMIT_INSERT` | 2 | 임베딩 서버 처리량 종속 |
| `KBP_JOB_LIMIT_PER_WORKSPACE` | 2 | `workspace_key IS NOT NULL` 에만 적용 |
| `KBP_JOB_WORKER_CONCURRENCY` | 4 | worker 프로세스 1개의 로컬 스레드 슬롯 |
| `KBP_JOB_CLAIM_SCAN_LIMIT` | 200 | 후보 스캔 윈도 |
| `KBP_JOB_PER_PARTITION_SCAN` | 8 | `(kind, workspace)` 파티션당 후보 상한 |
| `KBP_JOB_POLL_INTERVAL_SECONDS` | 2 | worker 틱 = heartbeat 주기 |
| `KBP_JOB_WAIT_POLL_INTERVAL_SECONDS` | 2 | 대기 핸들러의 잡 상태 폴링 주기(§2.3 커넥션 예산과 연결) |
| `KBP_JOB_STALE_LEASE_SECONDS` | 300 | §3.3 (heartbeat 는 전용 heartbeat 스레드가 계속 침 — §6.1) |
| `KBP_JOB_WORKER_STALE_SECONDS` | 60 | `job_workers` 만료 판정 |
| `KBP_JOB_MAX_RUNTIME_PARSE` | 2100 | 1800(`app.py:103`) + 여유 |
| `KBP_JOB_MAX_RUNTIME_CHUNK` | 5400 | submit 600 + poll 3600 + in-flight 600(`adaptive_chunk.py:42-43`) + 여유 |
| `KBP_JOB_MAX_RUNTIME_INSERT` | 6600 | ensure_workspace 600×5 + submit 600 + poll 1200 + in-flight 600 + counts 630 + 여유 |
| `KBP_JOB_MAX_RUNTIME_INGEST` | 14400 | 세 구간 합 ≈12600 + 여유 |
| `KBP_JOB_MAX_ATTEMPTS` | 3 | parse·chunk·ingest |
| `KBP_JOB_MAX_ATTEMPTS_INSERT` | 1 | §5.2 — insert 재시도 = 중복 적재 |
| `KBP_JOB_INLINE_MAX_BYTES` | 262144 | payload/result 공통 임계 |
| `KBP_JOB_MAX_UPLOAD_BYTES` | 209715200 | 200MB. 초과 시 413 |
| `KBP_JOB_MAX_WAITERS` | 4 | 프로세스당 동시 대기 상한(레거시 포함) |
| `KBP_JOB_IDEM_WINDOW_SECONDS` | 300 | 자동 파생 멱등키의 시간 버킷 폭(§4.4) |
| `KBP_JOB_WAIT_MAX_SECONDS` | 0 | `/jobs/*?wait` 상한. **기본 비활성** |
| `KBP_JOB_LEGACY_WAIT_SECONDS` | 3300 | 기존 4경로 내부 대기 상한. **kb 소비자 타임아웃 3600.0(`config.py:159`)보다 작아야** 우리 응답이 먼저 도달한다 |
| `KBP_JOB_MINIO_PREFIX` | `kbp-jobs` | 객체 키 접두사 |
| `KBP_JOB_MINIO_BUCKET` | `${MINIO_BUCKET}` (기본 `document-parser`) | 기존 버킷 재사용 |

**불변식: `MAX_RUNTIME` 은 그 kind 의 최악 소요보다 반드시 커야 하고, 산식은**

```
Σ(각 동기 호출 client timeout × 최대 호출 횟수) + Σ(poll_timeout)
  + (데드라인 직전 in-flight 호출 1회분 client timeout)
```

**이다.** 세 항이 다 필요하다는 게 v3·v4 에서 연달아 틀린 지점이다.

- v3 는 submit 타임아웃을 뺐다 — adaptive 는 `timeout=600`(POST `/chunk/jobs`) 후
  `poll_timeout=3600` 이 **submit 이후에 시작**한다(`adaptive_chunk.py:42-43`).
- v4 는 **재시도 횟수**와 **in-flight 1회분**을 뺐다. `ensure_workspace` 는 5xx 에
  최대 4회 POST 하고 4xx 면 `_find_workspace_by_slug` GET 을 한 번 더 친다
  (`edgequake.py:80-89,97-100`) → 최악 600×5. 그리고 폴 루프는 데드라인을 **HTTP 호출이
  끝난 뒤** 검사하므로(`edgequake.py:390-427`) `poll_timeout` 뒤에 클라이언트 타임아웃
  1회가 더 붙는다. `_fetch_graph_counts` 도 같은 구조라 30+600 이 추가된다.

값이 작으면 정상 진행 중인 잡이 **반드시** 회수된다. insert 는 §3.1 의 `stage='inserting'`
최우선 분기 때문에 회수 = 즉시 `failed` 이고, §4.5 매핑상 레거시 `/insert`·`/ingest` 는
500 을 받아 kb 가 3회 재시도한다 → **edgequake 중복 적재**. §5.2 가 막으려던 결과가
그대로 재현된다. 값이 작으면 정상 진행 중인 잡이 **반드시** 회수되고,
회수는 진행 중 HTTP 호출을 끊지 못하므로(§3.3) 같은 잡이 실제로 2중 실행된다.

`MAX_ATTEMPTS`·`MAX_RUNTIME` 이 kind 별이므로 회수 UPDATE 도 kind 별이어야 한다(§3.1).

## 4. API 계약

### 4.1 신규 잡 경로

| Method | Path | Body |
|---|---|---|
| POST | `/jobs/parse` | multipart: `file`, `content_type?`, `docs_id?`, `workspace_id?`, `batch_key?` |
| POST | `/jobs/chunk` | JSON: `parse_job_id` \| (`enriched_content` + 아래), `workspace_id?`, `batch_key?` |
| POST | `/jobs/insert` | JSON: `workspace_id`, `doc_id`, `chunk_job_id` \| `chunks`, `title?`, `extract_graph?`, `batch_key?` |
| POST | `/jobs/ingest` | multipart: `file`, `workspace_id`, `doc_id`, `content_type?`, `batch_key?` |

전부 `202 {job_id, status:"queued"}`.

**kind 별 payload 필드** — 현행 핸들러 시그니처와 1:1 이어야 한다. 하나라도 흘리면
결과 스키마 비교로는 안 잡힌다.

| kind | 필드 | 현행 출처 | 비고 |
|---|---|---|---|
| parse | **effective content_type** | `app.py:128` `content_type or file.content_type` | 폼 필드가 없으면 **멀티파트 파트 헤더**로 폴백. kb 는 falsy 면 폼에 안 싣는다(`kb_pipeline_client.py:170-171`). worker 엔 `UploadFile` 이 없으므로 **접수 시점에 확정해 payload 에 저장** |
| parse | `docs_id` | `app.py:114` | 페이지 이미지 MinIO 키 합의용. 누락 시 UI 썸네일 키가 조용히 어긋난다 |
| parse | filename | `_safe_basename(file.filename or "upload")` `app.py:126` | 한글·공백 보존 |
| chunk | `doc_name` `page_spans` `pages` `table_blocks` `methods` `skip_scoring` `llm_regex_pattern` | `app.py:136-142` | **`table_blocks` → `blocks` 이름 변환**(`app.py:171`) |
| insert | `title` `extract_graph` | `app.py:224-226` | `eq.insert_chunks(skip_graph=not extract_graph)` |
| ingest | `workspace_id` `doc_id` **effective content_type** | `app.py:277-279,293` | filename 폴백은 `_safe_basename(file.filename or doc_id)` — parse 와 **다르다**(`app.py:289`) |
| ingest | — | `app.py:319-320` | `/ingest` 는 `skip_graph` 를 전달하지 않는다(현행 유지) |
| ingest 내부 chunk 호출 | `doc_name=doc_id`, `atomic_markers` **만** | `app.py:303-304` | `/chunk`(`:168-173`)와 **다르다** — `page_spans`·`pages`·`blocks`·`methods`·`skip_scoring`·`llm_regex_pattern` 미전달. 현행 유지 |
| insert/ingest | `tenant_id = _TENANT_ID` | `app.py:32` | 상수. payload 가 아니라 runner 가 그대로 쓴다 |
| insert | title 폴백 `title or doc_id` | `app.py:238` | |
| ingest | title = `doc_id` | `app.py:320` | insert 와 폴백 규칙이 **다르다** |

**결과 성형**은 kind 별로 현행 응답과 동일해야 한다:
`/parse` 의 `chunk_needed=false` → `chunk_strategy="excel_rag_parser"` 재구성(`app.py:129-130`),
`/chunk` 의 `chunk_text`→`text`·`chunk_pages`→`pages` 정규화 + 마커 스트립(`app.py:174-192`),
`/insert` 의 `edgequake_workspace_id`·`entity_count`·`relationship_count`·`phases`(`app.py:240-255`),
`/ingest` 의 `chunking_selection`·`edgequake_workspace_id`(`app.py:321-329`).

`/jobs/chunk` 는 `parse_job_id` 와 `enriched_content` 중 **정확히 하나**(둘 다/둘 다
아님 → 400). `parse_job_id` 를 주면 worker 가 그 잡 결과에서 `enriched_content`·
`page_spans`·`pages`·`table_blocks` 를 꺼낸다. 참조 잡이 `succeeded` 가 아니면 접수 시
409. 참조는 `parent_job_id` 컬럼에 저장한다.

`/jobs/insert` 의 `chunk_job_id` → `chunks` 는 **chunk 잡 result 의 `chunks[].text`**
(마커 스트립된 표시사본, `app.py:178`)를 쓴다. kb 의 현행 단계별 경로와 같다 — kb 는
`/chunk` 응답의 `text` 를 그대로 `/insert` 에 넘긴다. `/ingest` 내부 경로만 마커가 남은
`chunk_text` 를 쓰는데(`app.py:305`), `eq.insert_chunks` 가 어차피 `_strip_modal` 을
다시 적용하므로(`edgequake.py:377`) 저장물은 동일하다.

### 4.2 조회

라우팅 순서 주의: **`GET /jobs/workers` 를 `GET /jobs/{id}` 앞에 선언**하고 `{id}` 는
`uuid.UUID` 로 타입 지정한다. FastAPI 는 선언 순서로 매칭하므로 순서가 뒤면 `workers`
가 `{id}` 핸들러로 흡수된다.

- `GET /jobs/{id}` → `{id, kind, status, stage, workspace_key, batch_key, attempt_count,
  queued_seconds, eligible, created_at, started_at, completed_at, error}`
  `buckets_available` = "이 잡의 kind 버킷과 테넌트에 **지금 여유가 있는가**". 승인
  예측이 아니다 — 로컬 슬롯, FIFO 상 앞선 후보들의 카운터 소비(§3.2 는 승인마다 즉시
  차감한다), 스캔 윈도 진입 여부를 보지 않는다. `queued` 가 아니면 `null`.
  같은 `(kind, workspace)` 파티션에서 앞선 queued 건수를 `ahead_in_partition` 으로 함께
  준다 — 이쪽이 대기 예측에 실제로 쓸 수 있는 값이다.
  **`queue_position` 은 두지 않는다.** claim 은 kind 무관 전역 FIFO 스캔이고 승인은
  버킷·workspace·로컬 슬롯 3중 조건이라 "앞에 N건" 은 대기 시간을 예측하지 못한다.
- `GET /jobs/{id}/result` → **기존 동기 응답 스키마와 동일한 본문**. 미완료 409, 실패 422.
- `GET /jobs?workspace_id=&batch_key=&status=&kind=&limit=` → 목록.
- `GET /jobs/workers` → kb 의 `worker_capacity()` 와 **동일 키**
  `{online, capacity, active, available, queued, processing}` + `oldest_queued_age_seconds`.
  kb 는 마지막 키가 `processing` 이다(`batch_repository.py:306-313`) — 바꾸면 Phase 2
  프론트가 조용히 `undefined` 를 받는다. 집계는 살아있는 worker 행만 센다(만료 행은
  §3.1 (1c)가 삭제). `oldest_queued_age_seconds` 로 "worker 0 + 큐 적체" 를 단일 지표로
  알람할 수 있다.
- `DELETE /jobs/{id}` → **단일 UPDATE 로 원자화한다.** 두 번에 나누면 그 사이에 잡이
  `running`→`queued` 로 회수되어(§3.1 (1)) 양쪽 다 0행이 되고 취소가 조용히 유실된다
  (취소 API 는 advisory lock 을 잡지 않으므로 경합 창이 실재한다).

  ```sql
  UPDATE kbp.jobs
     SET cancel_requested = true,
         status       = CASE WHEN status='queued' THEN 'canceled' ELSE status END,
         completed_at = CASE WHEN status='queued' THEN now() ELSE completed_at END
   WHERE id = $1 AND status IN ('queued','running')
  RETURNING status;
  ```

  `canceled` 반환 → 200. `running` 반환 → 202(플래그만). 0행이면 잡을 조회해 없으면
  404, terminal 이면 409. **terminal 잡에는 플래그를 찍지 않는다** — 찍으면 이미 끝난
  잡에 "취소 접수"를 응답하게 된다.

  취소 반응성: `queued` 즉시. `running` 은 ingest 만 단계 경계에서 중단하고, parse·chunk·
  insert 는 진행 중 호출이 끝난 뒤 중단한다(D6).

### 4.3 인증

신규 `/jobs/*` 는 전부 `X-Facade-Key` 게이트에 넣는다. `KBP_FACADE_KEY` 미설정 시 no-op
인 dev 동작은 유지된다(`app.py:80`).

**기존 4경로의 인증 요구는 Phase 1 동안 바뀌지 않는다** — `/parse`·`/chunk` 는 계속
무인증이다. 문서가 이를 "의도적으로 열려 있다"고 명시하고 있고(`docs/facade-api.md:77`),
**kb 파사드 키가 미설정인 배포**에서 게이트를 채우면 kb 가 즉시 401 을 맞는다(키가
설정돼 있으면 kb 는 `/parse` 에도 헤더를 싣는다 — `kb_pipeline_client.py:120-124`).

무인증 경로가 stateless 에서 stateful 로 바뀌는 것은 사실이다(인증 없는 호출 하나가
최대 200MB staging 을 남긴다). GC 가 없는 Phase 1 에서는 §5.3 이 이를 막는다.

### 4.4 제출 멱등키

소비자(kb)는 429/5xx 를 최대 3회 재시도한다(`kb_pipeline_client.py:137`). 제출 경로에서
그건 잡 중복 생성이고, `/insert`·`/ingest` 에서는 곧 edgequake 중복 적재다.

`/jobs/*` 제출은 `Idempotency-Key` 헤더를 받고, 없으면
`sha256(kind + workspace + payload + file)` + **시간 버킷**으로 자동 파생한다. 같은 키의
살아있는 잡이 있으면 새로 만들지 않고 **기존 `job_id` 를 반환**한다.

| 상황 | 동작 |
|---|---|
| 같은 요청 재시도(수 초 내) | 같은 자동 키 → 기존 잡 반환 |
| 의도적 재요청(버킷 경과 뒤) | 새 키 → 새 잡 |
| 명시 헤더 | 버킷 없음 — 소비자가 수명을 정한다 |
| 잡이 `failed`/`canceled` 로 종결 | **키를 비운다** → 고친 뒤 재제출하면 새 잡 |
| 키 충돌 | 방금 올린 staging 객체를 즉시 삭제(고아 방지, GC 없음 — D2) |

**레거시 4경로는 멱등키를 쓰지 않는다.** 동기라 소비자가 최종 결과를 보고 재시도를
판단하므로, 캐시가 끼면 "설정 고치고 다시 파싱" 이 조용한 no-op 이 된다.

### 4.5 `?wait` 와 레거시 대기

`/jobs/*?wait=true` 는 제출 후 완료까지 **DB 만 폴링**해 결과를 반환한다. 기본
`KBP_JOB_WAIT_MAX_SECONDS=0`(**비활성**).

기존 4경로의 내부 대기도 같은 메커니즘을 쓰되 상한은 `KBP_JOB_LEGACY_WAIT_SECONDS`
(3300)다. **모든 기본값의 단일 출처는 §3.7 표다** — 본문에 숫자를 중복 기재하지 않는다.

**레거시 4경로 핸들러를 전부 동기 `def` 로 전환한다.** 현재 `/parse`(`app.py:113`)와
`/ingest`(`app.py:277`)는 `async def` + `await file.read()`(`:125`,`:288`)다. async 인
채로 DB 폴링을 넣으면 이벤트 루프가 최대 `LEGACY_WAIT` 만큼 블로킹되어 같은 gunicorn
워커의 `/healthz`·`/jobs/*` 가 전부 멎고 compose healthcheck 가 unhealthy 로 떨어진다.
업로드는 `file.file.read()` 로 읽는다(FastAPI 가 동기 `def` 를 threadpool 에서 실행).

**스레드풀 예산이 핵심이다.** 대기 핸들러를 동기 `def` 로 두는 것만으로는 부족하다 —
`/healthz` 도 동기 `def` 라(`app.py:107-109`) **같은 AnyIO 스레드풀**을 공유한다. 동기
`def` 는 이벤트 루프만 지킬 뿐 스레드풀 고갈을 막지 못한다. 그래서:

- 대기 중인 요청 수를 **프로세스당 `KBP_JOB_MAX_WAITERS`(기본 4)로 제한**한다.
  레거시 경로도 예외가 아니다. 초과 시 `503 + Retry-After`.
- **waiter permit 을 먼저 획득하고, 실패하면 잡을 만들지 않고 즉시 503 을 낸다.**
  제출-후-거절은 금지다 — 잡을 INSERT 한 뒤 503 을 내면 worker 는 그 잡을 정상 실행하는데
  kb 는 5xx 를 재시도해(`kb_pipeline_client.py:137`, `max_retries=3`) **두 번째 잡**을
  만든다. Phase 1 은 멱등키가 없으므로(D1) 레거시 `/insert`·`/ingest` 에서 곧 edgequake
  중복 적재다.
- 대기 핸들러는 폴링 사이에 DB 커넥션을 닫는다(§2.3).
- `/jobs/*?wait` 상한 초과 시 `202 {job_id}` 폴백(잡은 계속 진행).

**worker 가 하나도 살아있지 않으면 접수 자체를 거절한다.** 제출 시 `kbp.job_workers` 에
`heartbeat_at > now() - KBP_JOB_WORKER_STALE_SECONDS` 인 행이 0 이면 잡을 만들지 않고
즉시 `503 + Retry-After`. 대기 중에도 잡이 `queued` 인 채 live worker 0 이
`WORKER_STALE_SECONDS` 이상 지속되면 503 으로 종결한다.
facade-worker 는 **이번에 새로 생기는 프로세스**라(호스트 런처 미기동, airgap 신규 블록)
빠뜨리기 쉽고, 그 경우 오늘은 facade 만 띄우면 되던 `/parse`·`/chunk` 가 3300s 매달렸다가
실패한다. fail-fast 가 없으면 문서 1건에 최대 3시간이 든다(kb 재시도 3회).

**잡 행이 조회되지 않으면 즉시 5xx 로 종결한다.** 무한 대기 금지 — §7.3 의 dev 큐 소멸
시나리오가 여기로 온다.

### 4.6 레거시 래퍼 응답 매핑

Phase 1 의 핵심 약속이 "기존 4경로 응답 계약 불변" 이므로 실패 경로도 정의해야 한다.
kb 는 429/5xx 만 재시도한다(`kb_pipeline_client.py:126-142`).

| 잡 결과 | 레거시 경로 응답 |
|---|---|
| `succeeded` | 200 + 기존과 동일한 본문 |
| parse-svc `{status:"failed"}` | 200 + parse-svc 원본 본문 (§5.1 — 잡은 `succeeded`) |
| `failed` (다운스트림 오류) | **500** + `{"detail": error}`. 현행도 다운스트림 예외가 500 으로 샜다 |
| `canceled` | 409 |
| 대기 초과(`LEGACY_WAIT`=3300s) | **409** + `{"detail": ..., "job_id": ...}`. **잡은 계속 진행한다** |
| waiter 상한 초과 | 503 + `Retry-After` (잡 행 미생성) |
| live worker 0 | 503 + `Retry-After` (잡 행 미생성) |

대기 초과를 **4xx 로 두는 이유**: kb 는 429/5xx 만 재시도한다(`kb_pipeline_client.py:137`).
504 로 내면 kb 가 같은 요청을 최대 3회 재제출하고, Phase 1 에 멱등키가 없으므로(D1)
`/insert`·`/ingest` 에서 edgequake 중복 적재가 된다. `MAX_RUNTIME_CHUNK`·`MAX_RUNTIME_INGEST`(§3.7 표)가 `LEGACY_WAIT` 보다 크므로 대기 초과는 **정상적으로
발생할 수 있다** — 드문 사건이 아니다. 다만 현행에서도 그 시간이면 kb 가 이미
`ReadTimeout`(3600) 을 맞으므로 회귀가 아니고, 오히려 `job_id` 로 결과를 회수할 수 있어
낫다.

## 5. 실패·재시도

### 5.1 분류

| 상황 | 처리 |
|---|---|
| 다운스트림 5xx / 타임아웃 / 커넥션 오류 | 재시도(`attempt_count < max`) |
| 다운스트림 4xx, 검증 오류 | 즉시 `failed` |
| **parse-svc `{status:"failed"}`** | **job = `succeeded`.** 본문을 result 에 그대로 보존 |
| staging 객체 없음 | 즉시 `failed` |
| 참조 잡 소실 / 미완료 | 즉시 `failed` (runner 가 **실행 시점에 재확인**) |
| worker 급사 | heartbeat 끊김 → 회수 |
| API 재기동 | 무영향 |

**parse-svc 실패를 `succeeded` 로 두는 이유**: 현행 `/ingest` 는 parse-svc 가 실패해도
HTTP 200 + parse-svc 원본 dict 를 반환하는 **정상 경로**이고(`app.py:294-297`, 주석
"v2(리뷰 B10)"), 이를 고정하는 테스트가 있다
(`service/tests/test_ingest_chunk_needed.py:62 test_failed_parse_returns_immediately`).
잡의 `succeeded` 는 "파이프라인이 정상 종료했다"는 뜻이지 "문서가 잘 파싱됐다"는 뜻이
아니다. 결과 본문의 `status` 필드가 후자를 말한다.

### 5.2 insert 는 재시도하지 않는다

`EdgequakeClient.insert_chunks()` 는 호출마다 `submit_document` 로 새 문서를 제출한다
(`service/edgequake.py:379`). 멱등키가 없어 재시도가 곧 중복 적재다.

**Phase 1 은 insert kind 의 `max_attempts` 를 1 로 둔다.** 원인이 재시도이므로 재시도를
없애면 원인이 사라지고, `edgequake.py` 를 건드릴 필요가 없다. insert 실패는 소비자가
다시 호출하면 된다 — 현행과 동일하다(회귀 아님).

`ingest` 잡은 세 단계를 도는데, insert 구간에 진입한 뒤 실패하면 **재시도하지 않는다**
(같은 이유). 즉 ingest 의 재시도는 parse·chunk 구간 실패에만 적용된다.

**판정은 runner 가 아니라 DB 에 있어야 한다.** runner 의 예외 처리에만 두면 worker 급사·
OOM·`max_runtime` 초과 경로에 적용되지 않는다. 그래서 runner 는 `eq.insert_chunks()`
호출 **직전에** lease 술어로 `stage='inserting'` 을 커밋하고, 회수 UPDATE 가 이 값을
kind 무관 최우선으로 본다(§3.1). 스키마 변경은 필요 없다 — `stage` 컬럼은 진행률
표시용으로 이미 있다.

**이 커밋이 `LeaseLost` 를 던지면 runner 는 `eq.insert_chunks()` 를 호출하지 않고 잡
실행을 포기한다**(§3.3). 이 규칙이 없으면 게이트를 다 넣고도 중복 적재가 난다.

자동 재시도가 필요해지면 seam 3종을 함께 넣는다(D5).

### 5.3 staging 수명 (GC 없음)

Phase 1 은 TTL GC 를 만들지 않는다(D2). 대신 누수가 실제로 위험한 경로 하나만 막는다:

**`legacy=true` 인 잡의 `input_ref` 와 `payload_ref` 는 잡이 terminal 이 되는 즉시
삭제한다.** `input_ref` 만으로는 부족하다 — 무인증 레거시 `/chunk`(`app.py:136-142`,
게이트 없음)는 파일 업로드가 없어 `input_ref` 가 아예 없고, 대신 수 MB `enriched_content`
가 §2.2 규칙에 따라 `payload_ref` 로 MinIO 에 올라간다. 즉 무인증으로 객체를 남기는
경로가 둘이다.

`result_ref` 는 **레거시 래퍼가 본문을 읽어 응답한 뒤**(또는 대기가 409/503 으로
종결된 뒤) 삭제한다. 대기 중에 지우면 §2.2 의 "빈 본문 반환 금지" 불변식과 충돌한다.

`legacy=false`(신규 `/jobs/*`) 잡의 객체는 남긴다 — 재시도가 `input_ref` 를 다시 읽고,
소비자가 `GET /jobs/{id}/result` 를 나중에 부른다.

## 6. 파일 구조

| 파일 | 책임 |
|---|---|
| `service/jobs/schema.py` | advisory-lock 으로 감싼 idempotent DDL + `ensure_schema(dsn)` |
| `service/jobs/repo.py` | psycopg3. **연산마다 `psycopg.connect(dsn)` 열고 `finally` 에서 close — 커넥션 풀 금지**(§2.3). submit/get/list/claim/heartbeat/complete/cancel/worker_stats. **모든 잡 쓰기에 `(claimed_by, attempt_count)` 술어**(§3.3) |
| `service/jobs/admission.py` | `plan_admissions()` + `BUCKETS_FOR_KIND` + 설정 로딩 |
| `service/jobs/blobs.py` | `JobBlobStore` — MinIO put/get/delete + JSON 직렬화 |
| `service/jobs/runner.py` | kind 별 실행. 현행 `app.py` 핸들러 본문을 이동 |
| `service/worker.py` | `python -m service.worker` 루프 + ThreadPoolExecutor + SIGTERM |
| `service/app.py` | 제출/조회 + 레거시 4경로 래퍼. 다운스트림 호출 제거(읽기 경로 제외) |

`service/parse_client.py`·`adaptive_chunk.py`·`edgequake.py` 는 **변경 없이** 재사용한다
(§5.2 로 edgequake seam 이 불필요해졌고, §7.5 로 parse_client seam 도 불필요해졌다).

**불변식: `JobRepo`·`JobBlobStore` 는 모듈 스코프에 인스턴스를 만들지 않는다.**
`get_edgequake`/`get_parse_client` 처럼 지연 `Depends` 팩토리로만 만든다. 모듈 스코프에
두면 `service/tests/test_facade_auth.py` 가 `importlib.reload(service.app)` 하는 순간
실 DB·MinIO 접속을 시도해 깨진다.

### 6.1 worker 불변식

- **heartbeat 는 전용 heartbeat 스레드가 친다.** executor 에 제출하지 않는 것은 물론,
  claim 을 수행하는 틱 루프와도 분리한다. 잡별 태스크로 제출하면 `concurrency=4` 가
  장시간 잡으로 포화됐을 때 heartbeat 가 아예 돌지 않고, 틱 루프에 붙여 두면 claim 이
  `pg_advisory_xact_lock`(blocking) 에서 지연될 때 heartbeat 가 함께 멎는다 — 어느
  쪽이든 그 worker 의 전 잡이 동시에 stale 로 오판된다.
- **모든 DB 접속에 타임아웃을 건다**: DSN 에 `connect_timeout=5`, claim 트랜잭션 진입
  직후 `SET LOCAL lock_timeout='5s'` + `SET LOCAL statement_timeout='30s'`. advisory
  lock 은 blocking 획득이라 상한이 없으면 한 틱이 무한정 매달릴 수 있다.
- `_tick()` 전체를 `try/except` 로 감싼다. DB transient 로 루프가 죽지 않는다.
- 틱 순서: 완료 회수 → 유지보수 (1)(1b)(1c) → claim (3)(4)(5) → 실행 제출.
  heartbeat 는 별도 스레드가 `POLL_INTERVAL` 주기로 돈다.
- **heartbeat 스레드가 `job_workers.active_count = len(in-flight)` 를 함께 갱신한다.**
  갱신하지 않으면 `GET /jobs/workers` 의 `active`/`available` 이 항상 `0`/`capacity` 로
  굳는다(kb 원본은 heartbeat 시점에 쓴다 — `batch_repository.py:266-284` `mark_worker`).
- **heartbeat 는 현재 in-flight future 의 job id 집합에만 친다** —
  `WHERE id = ANY($inflight) AND claimed_by=$me AND status='running'`. `claimed_by=$me`
  일괄 갱신은 금지다. 일괄로 치면 runner 스레드가 예외로 죽어 완료 쓰기를 못 한 잡도
  계속 갱신되어, 300s stale 경로가 아니라 `MAX_RUNTIME`(수천 초)까지 슬롯을 점유한다.
- **유지보수 (1)(1b)(1c) 는 `local_free` 와 무관하게 매 틱 실행한다.** `local_free == 0`
  이면 (3)(4)(5) 만 건너뛴다. 슬롯이 꽉 찼다고 stale 회수·취소 종결·만료 worker 행
  삭제까지 멈추면, 가장 바쁠 때 `GET /jobs/workers` 가 죽은 worker 를 online 으로 보고한다.
- **SIGTERM 드레인 중에도 heartbeat 를 유지한다.** `shutdown(wait=False)` 후
  `while 진행중인_잡이_있다: heartbeat(); sleep(poll_interval)` 로 돈다.
  이관 원본(`batch_worker.py:262-264`)은 `shutdown(wait=True)` 로 루프를 블로킹한 뒤
  마지막에 heartbeat 를 1회 부르는데, 그대로 포팅하면 `stop_grace_period` 1800s 동안
  heartbeat 가 `STALE_LEASE`(300s)를 넘겨 드레인 중인 잡 전량이 다른 worker 에게
  회수·중복 실행된다. **의도적으로 원본과 다른 지점이다.**

### 6.2 테스트 seam

기존 테스트는 `app.dependency_overrides[get_adaptive_chunk] = lambda: fake` 로
다운스트림을 주입하고 DB/MinIO fixture 가 없다. 레거시 경로가 잡을 경유하면 이 구조가
그대로는 안 돈다. 두 가지를 넣는다:

1. **인메모리 `JobRepo`·`JobBlobStore`** — `app.dependency_overrides[get_job_repo]` 로 주입.
   인메모리 repo 는 기본적으로 **live worker 1건을 보고한다** — 그러지 않으면 §4.4 의
   worker-liveness 게이트에 걸려 §8.1 재배선 대상 테스트가 전부 503 을 받는다.
2. **인라인 디스패처** — 제출 즉시 같은 프로세스에서 runner 를 실행한다.
   `app.state.inline_dispatcher` 로 **테스트에서만 주입**하고, 프로덕션 코드 경로에는
   이를 켜는 env 나 기본값을 두지 않는다(프로덕션에서 절대 켜지지 않는 보장).

`JobRunner` 는 생성자로 다운스트림 클라이언트를 받는다:

```python
class JobRunner:
    def __init__(self, *, parse_client=None, chunk_client=None, eq_client=None, blobs=None):
        ...   # None 이면 env 로 조립 (현행 get_* 함수 재사용)
```

(이 두 seam 의 전제인 `pyproject.toml` 의 `testpaths`·`markers` 는 이미 반영돼 있다.)

## 7. 배포

### 7.1 dev compose (`docker-compose.yml`)

현재 facade 블록(216-237)에는 YAML 앵커가 없다. 없는 앵커를 참조하면 파일 전체 파싱이
깨져 **9개 서비스 전부** 기동 불가가 된다. 앵커 도입을 선행 변경으로 넣는다:

```yaml
x-facade-env: &facade_env
  KBP_PARSE_SVC_URL: http://parse-svc:19001
  KBP_ADAPTIVE_CHUNK_URL: http://adaptive_chunk:18060
  KBP_EDGEQUAKE_URL: http://edgequake:8081
  KBP_PG_DSN: postgres://edgequake:${POSTGRES_PASSWORD:-edgequake_secret}@postgres:5432/edgequake
  KBP_OPENAI_API_KEY: ${KBP_OPENAI_API_KEY}
  KBP_OPENAI_BASE_URL: ${KBP_OPENAI_BASE_URL}
  KBP_LLM_MODEL: ${KBP_LLM_MODEL}
  MINIO_ENDPOINT: minio:9000
  MINIO_ACCESS_KEY: ${MINIO_ACCESS_KEY}
  MINIO_SECRET_KEY: ${MINIO_SECRET_KEY}
  MINIO_BUCKET: ${MINIO_BUCKET:-document-parser}
  MINIO_SECURE: "false"

services:
  facade:
    build: { context: ., dockerfile: Dockerfile.facade }
    image: kbp-facade:dev            # worker 와 태그 공유
    environment: *facade_env
    depends_on:
      postgres: { condition: service_healthy }
      minio:    { condition: service_healthy }
      # (기존 parse-svc/adaptive_chunk/edgequake 조건 유지)

  facade-worker:
    # facade 와 동일 build + 동일 image 태그. 두 번째 빌드는 레이어 캐시 히트라 공짜다.
    # build 를 빼면 compose 가 kbp-facade:dev 를 pull 하려다 access denied 로 실패한다
    # (로컬 전용 태그라 어느 레지스트리에도 없다). build 금지는 airgap(§7.2)에만 적용된다.
    build: { context: ., dockerfile: Dockerfile.facade }
    image: kbp-facade:dev
    command: ["python", "-m", "service.worker"]
    environment: *facade_env
    depends_on:
      facade:   { condition: service_started }
      postgres: { condition: service_healthy }
      minio:    { condition: service_healthy }
      parse-svc: { condition: service_healthy }
      adaptive_chunk: { condition: service_healthy }
      edgequake: { condition: service_healthy }
    restart: unless-stopped
    stop_grace_period: 1800s
    networks: [kbp]
```

`stop_grace_period` 없이는 `docker stop` 기본 10초 뒤 SIGKILL 이라 §6.1 의 드레인이
성립하지 않는다.

### 7.2 airgap compose (`docker-compose.airgap.yml`)

이 파일은 헤더 주석(6행)에서 **`build:` 를 원천 금지**하고 `image:` 태그만 쓴다
(facade 블록 191-192 = `image: kbp-facade:airgap`). worker 블록도 `build:` 없이
`image: kbp-facade:airgap` + `command` 만 다르게 한다. **이미지 9종은 그대로다.**

**실제 변경 필요**: airgap facade 블록(191-214)에 `MINIO_*` 가 하나도 없고 `depends_on`
이 parse-svc/adaptive_chunk/edgequake 뿐이다(parse-svc airgap 블록 175-179 에는 MINIO_*
가 있다). facade·facade-worker 양쪽에 `MINIO_*` 와
`depends_on: {postgres: {condition: service_healthy}, minio: {condition: service_healthy}}`
를 추가한다.

facade-worker airgap 블록에도 `restart: unless-stopped` + `stop_grace_period: 1800s` 를
넣는다. 폐쇄망 podman 은 `depends_on` 조건을 무시하므로(`docs/airgap-deploy.md:126`)
첫 기동에서 postgres·minio 보다 먼저 뜰 수 있고, 그때는 §2.1 의 재시도 예산(≥120s)과
restart 정책이 유일한 방어다.

**`KBP_FACADE_KEY` 는 Phase 1 에서 건드리지 않는다**(D12). airgap 자산 어디에도 이 키가
없는데 compose 에 `${KBP_FACADE_KEY}` 를 넣으면 **빈 문자열**이 주입되고, 게이트는
`os.environ.get(...) is None` 으로만 비활성화되므로(`app.py:69,79-81`) `""` 는 게이트 ON
이다 — `/ingest`·`/insert`·`/search`·`/chunks`·`/doc` 전부가 즉시 401 이 된다.
`${KBP_FACADE_KEY:?}` 로 쓰면 반대로 스택이 기동조차 못 한다. 어느 쪽이든 기존 동작
파손이라, 이 키의 도입은 `.env.airgap.example`·`verify-bundle.sh` REQUIRED_ENV·
`app.py` 의 빈 문자열 처리를 **함께** 바꿔야 하는 별도 작업이다.

**이미 충족(확인만)**: `.env.airgap.example:80-81` 에 `MINIO_ACCESS_KEY`/`MINIO_SECRET_KEY`
가 있고 `scripts/airgap/verify-bundle.sh:31` 의 `REQUIRED_ENV` 에도 등록돼 있다.
`verify-bundle.sh` 는 `IMAGES`·`REQUIRED_ENV` 양쪽 다 변경 불필요하다(`kbp-facade` 태그
재사용). 배포 검증 스크립트(`load-and-up.sh`)의 worker 확인은 비범위(D8).

### 7.3 호스트 dev

`scripts/facade.env` 에는 `MINIO_*` 가 없다(7키만). 추가한다. **호스트에서 MinIO 는
`localhost:9000`** 이다 — `docker-compose.override.yml:22` 가
`ports: !override ["9000:9000", "9001:9001"]` 로 승격시켜 놓았고(dify-minio 은퇴 후
kbp-minio 가 THE minio), `scripts/parse-svc.env:7` 도 같은 값이다. compose 본문의
`19010:9000` 은 override 가 덮는다.

`scripts/run-facade-worker.sh` 를 신설한다. **기존 런처는 포트 또는 cmdline 패턴으로
프로세스를 죽이는데**(`run-facade.sh:36` 은 `pkill -f "uvicorn service.app:app"`)
worker 는 포트가 없다. `pgrep -f '[p]ython -m service.worker'` + PID 파일로 스코프하고,
`pkill -f service` 같은 광역 패턴은 금지한다(facade 와 worker 가 서로를 죽인다).
`run-facade.sh`·`run-facade-worker.sh` 양쪽에 `: "${MINIO_ENDPOINT:?...}"` fail-fast
가드를 넣는다. `.claude/skills/restart-kbp-stack/SKILL.md` 에 필수 서비스로 등록한다.

**dev 경고 — edgequake 런처가 큐를 소거한다.** `service/scripts/start_dedicated_edgequake.sh:8-9`
는 `docker rm -f eq-pg-kbp` 후 볼륨 없이 재생성한다. `KBP_PG_DSN` 이 바로 그 DB 라
edgequake 를 한 번 재기동하면 `kbp.jobs` 전체가 소멸한다. §4.4 의 "잡 행이 조회되지
않으면 즉시 5xx" 규칙이 이 경우 대기 중인 레거시 요청을 3600s 매달리지 않게 한다.
런처 문서와 restart 스킬에 "큐가 살아 있어야 하면 바이너리-온리 재기동" 을 적는다.

### 7.4 gunicorn 워커 수

`Dockerfile.facade:15` 의 `-w 2` → **`-w 4`**. 레거시 4경로가 잡 완료를 기다리는데 그
대기는 DB 폴링뿐이라 2 보다는 늘려야 하지만, 8 은 메모리 최악치가 폐쇄망 박스에 맞지
않는다(§7.5). kb 의 배치 동시성이 2 이므로 4 로 충분하다.

같은 줄의 `--timeout 1800` 은 `LEGACY_WAIT`(3300)보다 작으므로 **4200 으로 올린다**.
`UvicornWorker` 에서 이 값은 요청 시간이 아니라 워커 무응답 판정 기준이지만, 대기 요청이
많을 때 마스터가 워커를 죽이는 경로를 남길 이유가 없다.

### 7.5 메모리 프로필

업로드 스트리밍은 비범위(D3). 대신 `KBP_JOB_MAX_UPLOAD_BYTES` 상한을 접수 시 강제해
최악 메모리를 **실제로 계산한다**(초과 시 413). `ParseSvcClient.parse(file_bytes=...)`
시그니처는 그대로다.

업로드 1건당 상주 사본은 API 쪽 2벌(`file.file.read()` + MinIO put 버퍼), worker 쪽
2벌(`get_object` 결과 + httpx 멀티파트 인코딩)이다. 최악:

```
API    : MAX_UPLOAD × 2 × (프로세스 W × waiter 4)   ← W=8, 상한 200MB 면 12.8GB
worker : MAX_UPLOAD × 2 × concurrency 4            ← 상한 200MB 면 1.6GB
```

**API 쪽 12.8GB 는 폐쇄망 박스에 안 맞는다.** 같은 호스트에서 edgequake·postgres·minio·
parse-svc 가 함께 돈다. 그래서 기본값을 이렇게 정한다:

| 값 | 기본 | 최악 상주 |
|---|---|---|
| `KBP_JOB_MAX_UPLOAD_BYTES` | **52428800 (50MB)** | worker 400MB |
| gunicorn `-w` | **4** (2→4, 8 아님) | API 1.6GB (= 50MB × 2 × 4 × 4) |

`-w 8` 은 근거가 없었다. kb 의 배치 동시성이 2 이고(`batch_worker_concurrency` 기본 2)
레거시 대기는 DB 폴링뿐이라 4 로 충분하다. 50MB 를 넘는 문서를 다뤄야 하면 D3(스트리밍)
을 먼저 구현하고 상한을 올린다 — 상한만 올리면 OOM-kill 로 in-flight 잡이 함께 죽는다.

`JobBlobStore` 는 `make_bucket` 을 **호출하지 않는다** — 제한된 업로드 전용 자격증명에서
`AccessDenied` 가 난다(`parse_service/minio_client.py:99-103` 의 동일 근거).

**단, 기동 시 버킷 존재 확인은 한다.** 이 설계에서 버킷 부재의 심각도가 격상되기
때문이다 — 현행에서는 페이지 썸네일만 누락되고 파싱은 성공하지만(그래서
`load-and-up.sh:100` 이 warn 후 계속 진행한다), 잡 방식에서는 staging put 실패 =
`/parse`·`/ingest` **접수 전면 실패**다. 폐쇄망 운영자가 초록불로 인수한 뒤 첫 업로드에서
원인 불명 500 을 만나는 시나리오를 막아야 한다. facade·worker 기동 시 다음으로 확인한다 — `list_objects` 는 **지연 제너레이터**라
호출만 하면 HTTP 요청이 아예 나가지 않으므로 1건을 실제로 순회해야 한다. `max_keys`
파라미터는 minio-py 에 **없다**(있는 것으로 쓰면 기동 시 TypeError).

```python
next(iter(client.list_objects(bucket, prefix=f"{JOB_PREFIX}/", recursive=True)), None)
```

`S3Error(NoSuchBucket)` 만 잡아 **WARN** 로그를 남긴다(`bucket_exists`/`make_bucket` 은
여전히 금지). 첫 배포에서는 버킷이 비어 있는 게 정상이라 ERROR 로 올리면 오탐이 된다 —
실제 실패는 첫 staging put 에서 명확한 메시지로 드러난다. `load-and-up.sh:100` 의 버킷 생성 실패를
`warn`→`die` 로 바꾸는 1줄 변경과 `docs/airgap-deploy.md:132` 의 영향 서술 갱신을
작업 항목에 넣는다.

## 8. 테스트

| 대상 | 방법 |
|---|---|
| `plan_admissions()` | 순수 함수. 버킷 상한 / workspace 상한 / `None` 미적용 / FIFO / head-of-line 회피 / 로컬 슬롯 / ingest 3버킷 / 알 수 없는 kind 거부 / **후보 10건·상한 4 → 정확히 4건**(카운터 누적) |
| 기아 회피 | 앞선 후보가 전부 chunk 로 막혀도 같은 틱에 parse 가 승인되는가 |
| 버킷 전개 | running ingest 2건이면 신규 chunk 잡이 승인되지 않는가(ingest 가 3버킷 점유) |
| NULL workspace 처리량 | workspace 없는 배치 N건이 `LIMIT_PARSE` 까지 병렬 승인 (현행 대비 퇴행 없음) |
| claim 동시성 | 실 postgres(`requires_pg`). 두 커넥션 동시 claim 시 상한 초과 없음 |
| lease 방어 | 회수 후 옛 `claimed_by` 의 쓰기가 거부되는가 |
| **세대 토큰** | **같은 worker** 가 재claim 한 뒤 옛 attempt 의 complete/stage/heartbeat 가 전부 거부되는가 |
| lease 상실 시 부작용 | `set_stage('inserting')` 이 `LeaseLost` 면 edgequake 호출 카운트가 0 인가 |
| heartbeat 생존 | executor 포화 시 / **SIGTERM 드레인 중 `stale_lease` 를 넘겨도** 갱신되는가 / 실행 스레드가 전부 DB 를 점유해도 갱신되는가 / **in-flight 가 아닌 잡은 갱신하지 않는가** |
| 유지보수 독립성 | `local_free==0` 이어도 stale 회수·취소 종결·만료 worker 행 삭제가 도는가 |
| 취소 | queued 즉시 canceled / running 플래그 / 승인 UPDATE 가 canceled 를 되살리지 않음 / running 취소가 좀비로 남지 않음 / terminal·없는 id 는 409·404 |
| stale 회수 | kind 별 `max_attempts`/`max_runtime` 이 각각 적용되는가(insert 1회·ingest 14400s), **`stage='inserting'` 잡은 attempt_count 와 무관하게 failed** |
| 체인 해석 | parse 잡 result → chunk payload 4필드 추출(**`table_blocks`→`blocks` 포함**), `chunk_job_id` → `chunks[].text` 사용, 참조 미완료 접수 409, 접수 후 소실 시 실행 시점 감지 |
| 실패 분류 | 502 → queued 복귀 + `attempt_count` 증가 / 400 → 즉시 failed / **insert 는 502 여도 재시도 안 함** |
| 제출 payload 왕복 | `docs_id` 보존, **폼 `content_type` 미전달 시 파트 헤더 폴백**, parse/ingest filename 폴백 차이, `_safe_basename` 유니코드 보존, `methods`/`skip_scoring`/`llm_regex_pattern` 기본값 |
| 레거시 응답 매핑 | §4.5 표 전부 — failed→500, canceled→409, **대기초과→409(+본문 `job_id`, 잡은 계속 진행)**, waiter초과→503(잡 행 미생성), live worker 0→503. "레거시 실패 응답이 kb 재시도 조건(429/5xx)에 걸리지 않는다" 단언 포함 |
| `?wait` / waiter 상한 | `MAX_WAITERS=4` 에서 레거시 요청 40건 동시 투입 → 4건만 대기, 나머지는 503+Retry-After 이고 **그 503 은 `kbp.jobs` 행을 만들지 않는다**. 그 동안 `/healthz` 200 유지. `/jobs/*?wait` 상한 초과는 202 폴백 |
| live worker 0 | 접수 즉시 503(잡 행 미생성) / 대기 중 worker 소멸 시 503 종결 |
| 레거시 핸들러 컨텍스트 | 레거시 `/parse` 대기 중에도 `/healthz` 가 200 (동기 `def` 전환 확인) |
| `*_ref` 복원 | 임계 초과 parse 잡의 레거시 `/parse` 응답이 inline 케이스와 byte-동일(`enriched_content` 비지 않음), 복원 실패 시 500 |
| `/jobs/*` 인증 | 키 설정 시 401 / 헤더 동반 시 통과, 레거시 `/parse`·`/chunk` 는 무인증 유지 |
| `buckets_available` | 버킷 포화 시 false, 여유 시 true, `queued` 아니면 null. `ahead_in_partition` 이 앞선 queued 건수를 센다 |
| 잡 행 소실 | 대기 중 잡이 사라지면 즉시 5xx (무한 대기 안 함) |
| `GET /jobs/workers` | 집계, 만료 worker 행 삭제, kb 와 동일 키, **`/jobs/workers` 가 `{id}` 로 안 흡수됨** |
| staging 수명 | `legacy=true` 잡 terminal 시 즉시 삭제, `legacy=false` 는 보존 |
| 결과 크기 분기 | 임계 이하 inline / 초과 MinIO ref (payload 도 동일) |
| 업로드 상한 | 초과 시 413 |
| `ensure_schema` 동시성 | `requires_pg` — 두 커넥션이 동시에 호출해도 예외 없이 1회 생성, 재호출 no-op, DSN 불통 시 백오프 후 **명확한 메시지로 기동 실패**(무한 대기 아님) |
| 스키마 소실 복구 | `42P01`/`3F000` 을 만나면 `ensure_schema` 재실행 후 재시도한다 |
| 대기 커넥션 | 대기 핸들러 N건 동시 실행 시 폴링 1회당 connect/close 가 정확히 1쌍, **sleep 구간의 동시 open 커넥션 0** |
| `attempt_count` 가드 | 소진된(`>= max`) queued 행을 claim 이 다시 집지 않는다 |
| `MAX_RUNTIME` 하한 | 각 kind 의 `MAX_RUNTIME` > 그 kind 의 클라이언트 타임아웃 합(§3.7 산식) |
| 취소 원자성 | `running`→`queued` 회수와 `DELETE` 가 경합해도 취소가 유실되지 않는다 |
| **레거시 4경로 회귀** | §8.1 |

### 8.1 레거시 4경로 회귀

§6.2 의 인메모리 repo + 인라인 디스패처로 재배선한다. 각 파일에 "잡 경유 후에도 응답
본문이 동일하다" 는 단언을 추가한다.

| 파일 | 유지할 단언 |
|---|---|
| `service/tests/test_parse_endpoint.py` | `_safe_basename` 유니코드 보존, `docs_id` 전달, `chunk_strategy` 재구성(excel), `gate_summary` passthrough, page 필드 |
| `service/tests/test_chunk_endpoint.py` | `MODAL_ATOMIC_MARKERS` 전달, 마커 스트립, `chunk_text`→`text`/`chunk_pages`→`pages`, `timing_details` |
| `service/tests/test_insert_endpoint.py` | `edgequake_workspace_id`, `entity_count`/`relationship_count`, `phases`, **`title` 미전달 시 `doc_id` 가 제목** |
| `service/tests/test_ingest_chunk_needed.py` | `test_failed_parse_returns_immediately`(§5.1), `chunk_needed=false` 분기, **ingest 내부 chunk 호출 인자가 `/chunk` 와 다른 현행을 유지** |
| `service/tests/test_app.py::test_ingest_and_chunks` | end-to-end 형태 |
| `service/tests/test_facade_auth.py` | `importlib.reload(service.app)` 후에도 **DB·MinIO 접속 없이** 통과(모듈 스코프 인스턴스 금지 검증) |

## 9. Phase 2 — kb-backend 축소 (별도 착수)

1. ~~선행: 제출 멱등키 구현(D1)~~ — **완료**(§4.4).
2. kb 의 facade 클라이언트를 `/jobs/*` + 폴링으로 전환.
3. kb 의 `batch_worker` 제거. 화면은 `GET /jobs?batch_key=` 와 `GET /jobs/workers` 로
   대응(키 이름을 kb 와 맞췄으므로 프론트 매핑 변경 없음).
4. 레거시 4경로 제거 + `/parse`·`/chunk` 인증 게이트 적용.
5. kb 에만 있는 기능(doc_guard 게이트 결과, 문서 메타, MinIO 원본 승격)은 kb 에 남긴다.

## 10. 문서 갱신 (Phase 1 산출물)

- `docs/facade-api.md` — 잡 경로 절 신설, 엔드포인트 표, `?wait`, 인증 절(`:77`)에
  `/jobs/*` 가 게이트 대상임을 추가(레거시 `/parse`·`/chunk` 무인증은 Phase 1 유지),
  **레거시 4경로의 신규 상태코드(503 / 409 / 413)와 409 응답의 `job_id` 로 결과를
  회수하는 절차**, 요약표의 블로킹·응답 열 갱신.
- `docs/airgap-deploy.md` — `:132` 버킷 부재 영향 서술 갱신(§7.5), facade-worker 추가.
- `docs/facade-api.html` — 같은 내용.
- `docs/architecture-ports.md` — facade-worker 추가.
- `_workspace/01-architecture.md` — 구성도에 facade-worker, 유량제어 소유권.
- `_workspace/02-changes.md` — 결정 근거(왜 202 로 안 바꿨는지, 왜 insert 무재시도인지).
- `_workspace/03-dev-progress.md` — phase 별 진행상황(각 phase 종료 시 갱신).

## 11. 비범위

[`2026-08-03-facade-job-queue-deferred.md`](2026-08-03-facade-job-queue-deferred.md) 참조 —
TTL GC(D2) · 업로드 스트리밍(D3) · `lease_epoch`(D4) · insert 재시도
멱등(D5) · 취소 즉시반응(D6) · ingest aging(D7) · airgap 검증 스크립트(D8) · NUL 정제
(D9) · `/communities/build` 큐 편입(D10) · `/healthz` async(D11) ·
airgap `KBP_FACADE_KEY` 필수화(D12).

추가로: 잡 우선순위 큐(FIFO 고정), 잡 중간 체크포인트, `/search`·`/chunks`·`/doc` 잡화,
kb 의 `IngestionBatch` 도메인 모델 이식(`batch_key` 문자열로 갈음).

## 12. 구현 순서

0. **완료** — `pyproject.toml` 의 `testpaths`/`markers`, `service/jobs/admission.py`,
   `service/jobs/schema.py`, `service/tests/test_job_admission.py`(20 passed).
   **런타임 의존성 추가는 없다** — 커넥션 풀을 쓰지 않으므로(§2.3).
1. **완료** — `repo.py`(claim SQL, `(claimed_by, attempt_count)` 펜싱, `42P01` 복구).
2. **완료** — `blobs.py` + `runner.py`(현행 핸들러 본문 이동, 클라이언트 재사용).
3. **완료** — `worker.py`(틱 루프, heartbeat 전용 스레드, 드레인).
4. **완료** — `jobs/api.py` 라우터 + `app.py` 등록. `/jobs/*` 제출·조회·취소.
5. **완료** — 레거시 4경로를 잡 래퍼로. 테스트 재배선은 `service/tests/conftest.py`
   하나로 처리했다(인메모리 repo/blobs + 인라인 디스패처 자동 주입) — 기존 파일들의
   단언을 손대지 않고 살렸다.
6. **완료** — compose(dev/airgap `facade-worker` + `x-facade-env` 앵커 + `MINIO_*`),
   `Dockerfile.facade`(`-w 4`, `--timeout 4200`), `scripts/run-facade-worker.sh`,
   `scripts/facade.env`(MINIO_*), `restart-kbp-stack` 스킬, `docs/facade-api.md`.

**라이브 검증(2026-08-04)**: facade(:19000) + facade-worker 를 실제로 띄우고 스캔 PDF
6건을 동시 제출 → **정확히 4건만 running, 2건 queued**가 6회 관측 내내 유지됐다. 슬롯이
비는 대로 승격됐고, 결과는 현행 `/parse` 응답 스키마 그대로(OCR 한글 884자 +
`docs_id`·`page_spans`·`pages`·`table_blocks`·`timing_metrics`). 레거시 `/parse` 도
동일 본문을 반환했다.

각 단계 끝에서 `_workspace/03-dev-progress.md` 를 갱신한다.
