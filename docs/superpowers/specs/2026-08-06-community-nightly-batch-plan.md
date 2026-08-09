<!-- plan-version: v11 -->
<!-- ultracode-validation: SUPERSEDED — A1/A2/A3/A4 로 분할됨 (2026-08-09) -->

# [A] 커뮤니티 야간 배치 — **분할됨(2026-08-09). 이 문서로 구현하지 말 것**

> ## ⛔ 이 문서는 더 이상 구현 대상이 아니다
>
> v8→v9→v10 세 라운드 검증에서 blocking 이 **14 → 14 로 수렴하지 않았다**. 결함이
> §3(리포트 세대 정리)·§2.6(스테일 스윕)에 몰렸는데 **둘 다 파괴적 연산(DELETE)이면서
> §0 의 목적과 직접 관련이 없다**. v6 에서 A/B 를 쪼갠 것과 같은 이유로 한 번 더 쪼갰다.
>
> | | 내용 | 문서 |
> |---|---|---|
> | **A1** | 야간 스케줄러 + 적재 트리거 제거 + 실행 이력 | `2026-08-09-community-nightly-A1-scheduler-plan.md` |
> | A2 | 그래프 변화 스킵·카운트·`fail_streak`·`max_communities` | 미작성 |
> | A3 | 리포트 세대 정리(`store_reports(replace=)`) + 좀비 방어 | 미작성 |
> | A4 | 스테일 리포트 스윕 | 미작성 |
>
> **A1 만으로 §0 의 목적(주간 LLM 부하 회피 + 진입점 단일화)이 달성된다.**
> 아래 본문은 A2~A4 의 **설계 소재**로 보존한다 — 특히 §2.5(스킵 판정)·§3(세대 정리)·
> §2.6(스윕)의 논증과 v9/v10 검증에서 드러난 함정은 그대로 유효하다.
> 범위 밖으로 이관한 13건은 deferred **D25~D37** 에 있다.

# [A] 커뮤니티 야간 배치

> **v6 에서 범위를 쪼갰다.** v1~v5 는 "야간 배치 + global 검색 버튼" 두 기능을 한 문서에
> 담아 admission·GC·LLM·스키마·챗 계약·프론트를 동시에 건드렸고, 5회 검증에서 blocking 이
> 계속 남았다. **이 문서는 야간 배치만** 다룬다. global 검색 버튼은
> `2026-08-06-global-search-button-plan.md`(B) 로 분리했다.
>
> **A 와 B 는 코드 의존이 없다.** 어느 쪽을 먼저 구현해도 된다.
>
> **개정 이력**
> - v1 폐기: D22("검색이 웹 워커를 2시간 점유")가 거짓 — `global_search` 는 미배선이다.
> - v2(blocking 12): payload 대신 `workspace_key` 로 대상 선정.
> - v3(blocking 13): lookback 창 제거, 키 통일(§2.7) **철회**(적재 슬롯을 먹어 더 나빴다).
> - v4(blocking 8): `kbp.community_builds` 도입, fail-open/fail-closed 분리, 마감 취소.
> - v5 → v6: 범위 축소. 내용 변경 없음(§3.2~§3.6 을 B 로 이관).
> - v9 → **v10**(2026-08-09): ultracode 경쟁 검증(4렌즈+종합) **blocking 14건** 반영.
>   설계가 실제로 바뀐 것 5건 — (1) 야간 `idem_key` 를 수동과 **분리**(같은 키라 수동
>   재빌드가 야간 queued 잡에 흡수돼 §2.5 의 "수동은 항상 빌드" 가 무효였다),
>   (2) `_recover`(순수 SQL)가 러너를 안 타 `fail_streak` 가 **회수 경로에서 안 올랐다** →
>   `_recover` 안에서 직접 UPSERT, (3) `record_community_build` **컬럼별 규칙표** 신설
>   (실패가 `snapshot_at`·카운트를 덮지 않는다, 스킵 판정은 성공 이력만 본다, `JobAborted`
>   미기록), (4) `max_communities` 상한과 `replace=True` 를 함께 켜면 **살아있는 리포트가
>   소각**된다 → 상한에 걸린 빌드는 `replace=False` 로 강등, (5) `max_attempts=1` 이
>   2중 실행을 못 막으므로 **DELETE 직전 lease 재검사** 규약.
>   그 외 — `_stage("building")` 복원(v9 스니펫에서 누락), `max_communities` **배선 지점**
>   명시(없으면 상한이 영구 무제한인데 테스트는 전부 초록), `now` 를 **로컬존으로** 획득
>   (레포 관례 UTC 를 따르면 KST 12:00 발화), 마감 취소가 **이전 밤 잡까지**, `batch_runs` 에
>   **종료 상태/error**(마커만 서고 죽은 밤을 구별), **`TZ` 를 `REQUIRED_ENV` 에서 제외**
>   (파서 전용 배포 100% 실패 회귀), 스윕 정합성 가드의 **조회 경로 신설 + 첫 밤 보류**,
>   테스트를 **PG 불요/requires_pg 두 파일로 분리**(v9 배치는 dev 기본 상태에서 전부 skip),
>   kb 회귀에 **스파이 큐**(현 하네스는 코드 없이도 통과), 라인 인용 6건 재교정.
>   **범위 밖 13건은 deferred D25~D37 로 이관**했다.
> - v8 → **v9**(2026-08-09): 코드 변경 없이 **라인번호만 재검증**했다. 실질 사실 39개는
>   전부 그대로 참이다(확인: `_EXEMPT_WORKSPACE = None`, `KBP_JOB_MAX_ATTEMPTS_COMMUNITY`
>   0건, claim 시 community idem NULL, TZ 미설정, `testpaths` 에 `kb_pipeline/tests` 없음).
>   `docker-compose.airgap.yml` 이 별건(paddle_gw 배선)으로 커져 airgap 라인이 +70 밀렸다.
>   **`x-facade-env` 앵커 위치(compose `:10`, airgap `:27` — **2026-08-09 재확인, 불변**)는 불변**이라 §4 주 작업 지점은
>   그대로다. **D21 은 v8 대로 종결 유지**(직렬이 의도 — §2.7)를 사용자가 재확인했다(08-09).
> - v6 → **v7**: blocking 11건. 실행 판정 로직을 **다시 짰다**(자정 랩이 실제로는 안 됐고
>   마감 취소가 죽은 코드였다). `fail_streak`·idem 충돌 집계·`batch_runs` 이력을 실제로
>   기록하는 경로를 넣었다. 스윕의 uuid/text 타입 불일치, 수동 재빌드가 스킵 가드에 막히는
>   문제, 끝나지 않는 대형 빌드에 대한 방어를 추가했다.

---

## 0. 사용자 결정 (2026-08-06)

**커뮤니티 빌드를 야간 배치로 일원화한다.** 적재 직후 트리거를 없앤다.
목적은 웹 프로세스 보호가 아니라 **주간 LLM 부하 회피 + 진입점 단일화**.

**대가**: 적재한 문서의 커뮤니티가 **최대 하루** 뒤 반영된다. 사용자가 인지하고 택했다.

---

## 1. 실측 사실 (2026-08-06. 라인번호 3회 재검증)

| # | 사실 | 근거 |
|---|---|---|
| 1 | 러너는 DSN 을 인라인. `dsn` 지역변수 **없음** | `runner.py:288` |
| 2 | 러너는 `community_builder`·`eq_factory` 를 주입받는다 | `runner.py:94-95` |
| 3 | payload 는 256KB 초과 시 오프로드 → `payload` 컬럼 NULL | `blobs.py:29,183-187` |
| 4 | insert payload 는 chunks 전량 | `app.py:364` |
| 5 | `workspace_key` 는 insert·ingest 에서 **kb id** | `app.py:364` |
| 6 | `/communities/build` 는 **payload 에 kb id 를 그대로 담고**(`payload={"workspace_id": workspace_id}`), **`workspace_key`·`idem_key` 만 eq UUID** 로 쓴다 | `app.py:545-550` |
| 7 | `_EXEMPT_WORKSPACE = None`(**`admission.py:37`**) — community 도 workspace 상한에 계산 | `:66,111,116` |
| 8 | 버킷 상한은 **`admission.py:145`** `_env_int("KBP_JOB_LIMIT_COMMUNITY", 1)` — env 로 상향 가능. (`:31` 은 kind→버킷 매핑) | 해당 파일 |
| 9 | kind별 `max_attempts` override 는 `KBP_JOB_MAX_ATTEMPTS_INSERT` **하나뿐**. community 는 기본 3 | `repo.py:74-82` |
| 10 | `KBP_JOB_MAX_ATTEMPTS_COMMUNITY` 는 리포지토리에 **0건** | grep |
| 11 | idem 충돌 시 기존 job_id 만 반환 — `batch_key` 를 UPDATE 하지 않는다 | `repo.py:171,186-190` |
| 12 | claim 시 community 만 `idem_key` 를 NULL 로 비운다 | `repo.py:491` |
| 13 | **claim 경로에 시간 조건이 없다** — 09시에도 queued community 를 계속 집는다 | `repo.py:470-500` |
| 14 | `build_workspace_communities` 는 진입 즉시 `fetch_graph` **1회** — **시작 시점 스냅샷**만 본다 | `community.py:507` |
| 15 | 라이브: community `created 09:10:21 → completed 09:48:37`(38분), 그 사이 09:12:31 insert 성공 | 라이브 |
| 16 | `public.community_reports` 는 `store_reports` 안의 `cur.execute(_DDL)` 에서만 **lazy 생성** | `community.py:484` |
| 17 | `store_reports` 는 DELETE 없이 upsert. `community_id` = Louvain enumerate 인덱스 | `community.py:468` |
| 18 | `_NODE_SQL` 은 표현식 인덱스가 없어 **Seq Scan**(실측 16ms, Rows Removed 2325) | EXPLAIN |
| 19 | GC 는 **fail-closed** 를 명문화한다 | `gc.py:9-13` |
| 20 | TTL 은 `gc.ttl_seconds()` — `KBP_JOB_TTL_SECONDS` 를 **먼저** 읽고 없으면 `_HOURS`(72) | `gc.py:37`; compose `:52` |
| 21 | community `max_runtime` 7200s | `repo.py:102` |
| 22 | 라이브 실측 빌드 1건 = **29~38분** | 검증 측정 |
| 23 | 트리거 가드 `or extract_graph is False` | `tasks.py:404`. 호출부 `:363`, 정의 `:388` |
| 24 | `test_community_job.py` 5건은 함수 **직접 호출** 단위테스트(첫 테스트 `:36`) | 해당 파일 |
| 25 | 호출부(`tasks.py:363`)를 덮는 테스트 **0건** | grep |
| 26 | facade·facade-worker 가 같은 매핑 앵커 공유 — compose `:10`(앵커)·`:328`(restart), airgap `:27`(앵커)/`:316`/`:343` | grep |
| 27 | facade-worker `restart: unless-stopped` = compose `:293`; heartbeat 사망 시 자체 종료 | `worker.py:296-297` |
| 28 | 컨테이너 TZ 미설정 → UTC. **dev 호스트는 이미 KST** | compose·Dockerfile |
| 29 | facade `DELETE /doc` 은 잡 행을 만들지 않는다 | `app.py:417` |
| 30 | worker 는 `JobRunner(repo=…, blobs=…)` 만 만든다 — eq 는 `eq_factory` 지연 조립 | `worker.py:55` |
| 31 | dev 는 호스트 런처 | `scripts/run-facade-worker.sh`, `scripts/facade.env` |
| 32 | 단건 `cancel`(API)은 `idem_key = NULL` 을 명시하지만, **bulk 취소 `_finish_cancelled_queued` 는 비우지 않는다** | `repo.py:710`(단건 cancel 은 비움), `:375,381-384`(bulk 는 안 비움) |
| 33 | `submit_job` 은 `created`(job_id) **하나만** 반환한다 — 신규 생성인지 idem 충돌인지 호출자가 구분할 수 없다 | `api.py:137`(정의)·`:171,181`(제출·반환) |
| 34 | `public.community_reports.workspace_id` 컬럼은 **`uuid` 타입**(text 아님) | `community.py:437` |
| 35 | `_NODE_SQL`/`_EDGE_SQL` 의 비교식 좌변은 `properties::text::jsonb ->> 'workspace_id'` — **text** | `community.py:154` |
| 36 | `worker.py` 의 폴 간격은 `KBP_JOB_POLL_INTERVAL_SECONDS`(기본 **2초**) | `worker.py:58` |
| 37 | `kb_pipeline/tests/` 는 **pytest 수집 대상이 아니다**(`testpaths = ["tests", "service/tests"]`) | `pyproject.toml:30` |
| 38 | `test_job_repo_pg.py` 는 `pytest.mark.requires_pg` + `KBP_PG_DSN` 미설정·활성 워커 감지 시 **모듈 전체 skip**. `_truncate()` 는 `kbp.jobs`·`kbp.job_workers` **만** 지운다 | `test_job_repo_pg.py:26-27,42-53` |
| 39 | 라이브에 커뮤니티 231개(`e512080e…`)·111개(`f404411e…`) 보유 workspace 존재 — 그래프는 적재마다 단조 증가한다 | 라이브 |

---

## 2. 설계

### 2.1 `kbp.community_builds` — 빌드 이력을 잡 테이블에서 분리한다

v4 이전은 빌드 이력을 `kbp.jobs` 에서 유추했고 **네 가지가 동시에 깨졌다**: (a) 완료 시각
기준이라 빌드 중 도착한 적재를 영구히 잃고(사실 14·15), (b) 그래프가 안 바뀐 vector-only
적재도 매일 밤 재빌드하고, (c) in-flight 잡을 못 보고, (d) TTL GC 가 이력을 지운다.

```sql
CREATE TABLE IF NOT EXISTS kbp.community_builds (
  workspace_key   text PRIMARY KEY,        -- kb id (insert 측과 같은 축)
  eq_workspace_id text,
  snapshot_at     timestamptz,             -- ★ 그래프를 읽은 시각(빌드 시작)
  finished_at     timestamptz,
  status          text,                    -- succeeded | failed | skipped
  node_count      integer,
  edge_count      integer,
  fail_streak     integer NOT NULL DEFAULT 0
);
```
러너가 빌드 전후로 갱신한다. `ensure_schema` 에 추가.

### 2.2 대상 선정

```sql
WITH last_insert AS (
  SELECT workspace_key, MAX(COALESCE(completed_at, created_at)) AS inserted_at
  FROM kbp.jobs
  WHERE kind IN ('insert','ingest') AND status = 'succeeded'
    AND workspace_key IS NOT NULL
  GROUP BY workspace_key
)
SELECT li.workspace_key
FROM last_insert li
LEFT JOIN kbp.community_builds cb ON cb.workspace_key = li.workspace_key
WHERE (cb.snapshot_at IS NULL OR li.inserted_at > cb.snapshot_at)     -- ★ 스냅샷 시각
  AND NOT EXISTS (                                                     -- ★ in-flight 제외
        SELECT 1 FROM kbp.jobs j
        WHERE j.kind = 'community' AND j.status IN ('queued','running')
          AND j.payload->>'workspace_id' = li.workspace_key)
ORDER BY COALESCE(cb.fail_streak, 0) ASC, li.inserted_at ASC          -- ★ 실패 반복은 후순위
LIMIT %(cap)s
```

- **`snapshot_at` 비교**: 빌드는 시작 시점 그래프만 본다(사실 14). 완료 시각으로 비교하면
  38분 빌드 중 도착한 적재가 "이미 포함됨"으로 오판돼 **영구 탈락**한다(사실 15).
- **in-flight 제외**: 전날 것이 running 이면 claim 이 `idem_key` 를 비워(사실 12) **중복
  잡**이 생기고 §3 의 `replace=True` 두 트랜잭션이 겹친다. queued 면 idem 충돌로 캡만 헛소진.
- **`fail_streak` 후순위**: `max_attempts=1` 이면 2시간 넘는 workspace 가 매 밤 실패하며
  오래된-순 큐 선두를 영구 점유한다(다른 형태의 굶김).
- `payload` 대신 `workspace_key`(insert 측) — payload 는 오프로드되면 NULL 이다(사실 3·4).

**백로그**: 후보 > 캡이면 `제출/잔여` 를 `batch_runs` 에 기록하고 잔여 > 0 이면 warning.
**잔여 residual**: `last_insert` 는 여전히 `kbp.jobs` 에 의존하므로 캡에 밀린 채 TTL(72h)이
지나면 조용히 탈락한다. 캡 8 · 오래된-순이면 3일치 = 24개 여유지만 **완전 방어는 아니다**
→ 잔여 > 0 이 3일 연속이면 `log.error`, deferred 에 올린다.

### 2.3 잡 키 — `/communities/build` 와 동일 (v3 §2.7 철회)

`_EXEMPT_WORKSPACE = None`(사실 7)이라 community 도 workspace 상한 2 에 계산된다. 키를
kb id 로 통일하면 30~120분 빌드가 그 KB 의 **적재 슬롯 1개를 점유**해 업로드 처리량이
절반이 된다. 지금 네임스페이스가 갈린 것은 **의도된 격리**다.

배치가 eq UUID 를 해석해 기존 키를 그대로 쓴다. **`submit_job` 의 반환 계약은 건드리지
않는다** — v7 이전 초안은 이걸 `(job_id, created)` 튜플로 바꾸려 했으나, `submit_job` 은
legacy 동기 엔드포인트 4개(`app.py` `_legacy_job()` → `/parse`·`/chunk`·`/insert`·
`/ingest`)와 비동기 `/jobs/*` 라우터 4개(`jobs/api.py:302,335,362,390` — 응답 바디에
`str(job_id)` 를 그대로 싣는다)가 **공유하는 진입점**이라, 반환형을 바꾸면 8곳이 동시에
깨지거나(legacy 는 즉시 예외, `/jobs/*` 는 **조용히 잘못된 job_id 문자열을 응답에 실어
보낸다** — 더 위험하다) 8곳을 전부 갱신해야 한다. 야간 배치 하나를 위해 공유 진입점의
계약을 흔들 이유가 없다.

**대신 새 함수를 하나 더 둔다** — `submit_job` 내부 로직을 그대로 재사용하되 신규/충돌
여부만 함께 반환한다:

```python
# service/jobs/api.py — submit_job 은 무변경. 신규 함수만 추가.
def submit_job_ex(repo, blobs, *, kind, payload, workspace_key=None, idem_key=None,
                   batch_key=None, parent_job_id=None, legacy=False,
                   file_bytes=None) -> tuple[uuid.UUID, bool]:
    """submit_job 과 동일한 본문이지만 (job_id, created: bool) 을 반환한다.

    `submit_job` 은 공유 진입점(legacy 4경로 + /jobs/* 4라우터)이라 반환형을 못 바꾼다.
    이 함수는 그 8곳을 안 건드리고 새 소비자(야간 배치)에게만 필요한 정보를 준다.
    """
    job_id = uuid.uuid4()
    ...   # submit_job 의 본문과 동일(중복을 피하려면 submit_job 이 내부 헬퍼로 위임하도록
          # 리팩터할 수도 있으나, v7 은 안전하게 복제로 시작한다 — 리팩터는 별건)
    created_id = repo.submit(job_id=job_id, kind=kind, ...)
    if created_id != job_id:
        blobs.delete(input_ref)          # 기존 submit_job 과 동일한 고아 정리
        if payload_ref: blobs.delete(payload_ref)
    return created_id, created_id == job_id
```

```python
eq_ws = self.runner.eq_client.ensure_workspace(kb_id, name=kb_id)   # 사실 30(지연 조립)
job_id, created = submit_job_ex(repo, blobs, kind="community",
           payload={"workspace_id": kb_id, "skip_if_unchanged": True},
           workspace_key=eq_ws,
           # ★ v10: 야간 키를 **수동 키와 분리한다**. 같은 키(`community:{eq_ws}`)를 쓰면
           # 야간 잡이 queued 인 동안(버킷 상한 1 직렬 → 캡 8건이면 03:00~08:00 내내)
           # 수동 /communities/build 가 `ON CONFLICT DO NOTHING` 으로 **그 야간 잡의
           # job_id 를 그대로 돌려받는다**(repo.py:171,186-190). 그 잡은 payload 에
           # skip_if_unchanged=True 를 갖고 있어, 운영자는 202 {status:"started"} 를
           # 받고도 `graph unchanged` no-op 을 얻는다 — §2.5 가 명문화한 "수동은 항상
           # 빌드"(v6 이 막았던 유일한 탈출구)가 그대로 무효가 된다.
           # run_date 를 키에 넣어 **밤마다 새 키**가 되게 한다(같은 밤 중복만 억제).
           idem_key=f"community-nightly:{eq_ws}:{run_date}",
           batch_key=f"community-nightly:{run_date}")               # ★ run_date, §2.4
if created: submitted += 1
else:       deduped += 1
```

→ **`service/app.py` 무변경, `test_app.py:99-101` 무변경, admission 영향 없음, 기존
`submit_job` 호출부 8곳(legacy 4 + `/jobs/*` 4) 전부 무변경.** `/communities/build` 는
계속 `submit_job`(단일 반환값)을 쓴다 — 이 잡 kind 는 여러 함수에서 제출될 수 있으므로
어느 함수를 쓰든 같은 `kbp.jobs` 행 규약을 따른다.

### 2.4 실행 판정 — 마커 이력 테이블 + 실행 창 + 마감 취소 (재설계)

v6 의 시각 판정은 **세 가지가 동시에 깨졌다**: (a) 매 틱 `now.date()` 로 창을 재계산해
`BUILD_AT=23:30` 같은 자정 랩 설정에서 창의 후반이 통째로 죽고, (b) 마감 취소를 창 판정
**뒤**에 둬 `DEADLINE(420분)` 이 정의상 `WINDOW(120분)` 밖이라 **영원히 도달 불가능한 죽은
코드**였고, (c) `batch_runs` 가 `name` 단일 PK 라 매일 덮어써 "3일 연속 잔여" 를 셀 수 없었다.

```sql
CREATE TABLE IF NOT EXISTS kbp.batch_runs (
  name        text NOT NULL,
  run_date    date NOT NULL,
  run_at      timestamptz NOT NULL DEFAULT now(),
  submitted   integer NOT NULL DEFAULT 0,
  deduped     integer NOT NULL DEFAULT 0,
  backlog     integer NOT NULL DEFAULT 0,
  -- ★ v10 [10]: claim_run 은 "실행을 **시도**했다" 마커다. 마커만 서고 스윕/후보/제출
  -- 루프가 예외로 죽으면 submitted=deduped=backlog=0 으로 남아 **"후보가 원래 0건이던
  -- 밤"과 구별되지 않는다**. 야간 배치가 유일한 빌드 경로가 되므로, 조용히 멈춘 것을
  -- 탐지할 수단이 없으면 커뮤니티가 영영 안 만들어지는 구간을 아무도 모른다.
  -- 루프 종료 시 반드시 갱신한다: 'ok' | 'failed'. 예외면 error 에 요약을 남긴다.
  status      text NOT NULL DEFAULT 'started',
  error       text,
  PRIMARY KEY (name, run_date)                    -- ★ 이력 보존(v6 은 단일행이라 못 셌다)
);
```

**"밤(run_date)" 을 매 틱 재계산하지 않고 한 번 고정한다** — 자정 랩의 근본 원인은 창의
시작·끝을 그 순간의 날짜로 다시 계산하는 것이었다:

```python
def _current_run_date(now: datetime, build_at: time) -> date:
    """지금이 어느 '밤'에 속하는지. BUILD_AT 이전이면 전날 밤이 아직 안 끝난 것으로 본다."""
    return now.date() if now.time() >= build_at else now.date() - timedelta(days=1)

# ★ v10: `now` 를 **로컬존(tz)으로** 얻는다. 이 줄이 없으면 구현자가 레포 관례
#   (worker.py:63 `datetime.now(timezone.utc)`)를 따르게 되고, run_date·창 판정이 UTC
#   벽시계가 되어 BUILD_AT=03:00 창이 **KST 12:00~14:00** 에 발화한다 — §0 의 유일한
#   목적(주간 LLM 부하 회피)이 정확히 뒤집힌다.
#   계약: `_current_run_date` 는 **tz-aware 로컬존 now 만** 받는다(UTC now 를 넘기면 안 된다).
tz  = _zone()                      # §2.7
now = datetime.now(tz)             # ★ UTC 아님
run_date = _current_run_date(now, build_at)
start = datetime.combine(run_date, build_at, tz)     # ★ run_date 고정 — 재계산 안 함
end   = start + timedelta(minutes=window)             # end 가 자정을 넘어도 start 는 안 바뀐다
deadline = start + timedelta(minutes=deadline_minutes)

# ── 마감 취소: 창 판정과 무관하게, 매 틱 무조건 먼저 확인한다 ──
# ★ v10 [9]: **이전 밤 잡까지 자른다.** v9 는 현재 run_date 의 batch_key 만 잘랐다.
#   워커가 04:00 에 죽었다가 다음 날 12:00 에 살아나면 그 시점 run_date 는 '오늘'이라
#   어제 밤 queued(batch_key=...:D-1)는 취소 대상이 아니고, **claim 경로에 시간 조건이
#   없어**(사실 13, repo.py:470-500) 워커가 뜨자마자 **업무시간 한복판에서 최대 8건을
#   순차 실행**한다 — §0 의 유일한 목적을 정면으로 깬다.
#   → 접두사로 훑되 **현재 run_date 는 제외**한다(오늘 밤 것은 아직 창 안일 수 있다).
n = cancel_stale_nightly(prefix="community-nightly:", keep_run_date=run_date)
if n: log.warning("stale nightly community jobs canceled: %d", n)
if now >= deadline:
    n = cancel_queued_by_batch_key(f"community-nightly:{run_date}")   # §2.4.1
    if n: log.warning("nightly deadline passed; canceled %d queued community jobs", n)

# ── 실행 창 ──
if not (start <= now < end):
    return
if not claim_run("community-nightly", run_date):     # PK (name, run_date) 로 원자적 삽입
    return
```

- **`claim_run`**: `INSERT INTO kbp.batch_runs (name, run_date) VALUES (%s, %s) ON CONFLICT (name, run_date) DO NOTHING RETURNING 1`.
  대상 0건이어도 이 INSERT 는 일어난다 → **마커가 항상 남는다.**
- **자정 랩**: `run_date` 를 한 번 고정하므로 `BUILD_AT=23:30, WINDOW=120` 이면 `start=23:30`,
  `end=01:30(다음날)` 이 되고, 00:30 틱에도 `run_date` 는 그대로라 창 판정이 그대로 유지된다.
- **마감 취소가 실제로 도달 가능**: 창 판정 앞에 무조건 두었으므로 `DEADLINE > WINDOW` 여도
  실행된다. `KBP_COMMUNITY_DEADLINE_MINUTES` 기본 **420**(10:00).

#### 2.4.1 `cancel_queued_by_batch_key` — idem_key 를 함께 비운다

마감 취소 대상은 **한 번도 claim 되지 않은 queued** 라 claim 시의 idem_key NULL 화(사실 12)를
거치지 않는다. 단건 `cancel` API 는 명시적으로 비우지만(사실 32) bulk 취소 경로를 새로
쓰는 것이므로 **처음부터 맞게 짠다**:

```sql
UPDATE kbp.jobs
   SET status = 'canceled', completed_at = now(), idem_key = NULL   -- ★ 반드시 함께
 WHERE kind = 'community' AND status = 'queued' AND batch_key = %s
```
안 하면 취소된 잡이 `idem_key='community:{eq_ws}'` 를 쥔 채 남아, 다음 밤 제출이
`ON CONFLICT DO NOTHING` 으로 그 취소된 job_id 를 돌려주고(사실 33 이 고친 반환 계약에서
`created=False`) `deduped` 로만 세어져 **경고 없이 그 KB 만 GC TTL(72h)까지 조용히 멈춘다.**

---

### 2.5 그래프 변화 판정 — vector-only 를 실제로 거른다

`has_graph()`(있나/없나)로 `extract_graph` 가드(사실 23)를 대체하는 것은 **동치가 아니다**:
이미 그래프가 있는 KB 에 vector-only 문서를 적재하면 후보에 오르고 `has_graph` 는 True 라
**그래프가 안 바뀌었는데 매일 밤 30~38분 전체 재빌드**가 돈다 — §0 목적에 정면 위배.

```python
# kb_pipeline/community.py — 신규 공개 헬퍼
def graph_counts(workspace_id: str, dsn: str) -> tuple[int, int]:
    """(node_count, edge_count). **DB 오류를 삼키지 않는다**(fail-closed)."""
def delete_reports(workspace_id: str, dsn: str, *, level: int = 0) -> int:
```

**야간 배치가 스킵을 판단하고, 수동 재빌드는 항상 강제한다.** v6 은 `_run_community` 안에
스킵 판정을 넣어 `/communities/build`(같은 `kind='community'`)도 똑같이 스킵됐다 — 리포트가
깨져 수동으로 다시 돌리려는 운영자에게 **유일한 탈출구가 no-op** 이 되는 문제였다. 판정을
잡 payload 의 명시적 플래그로 옮긴다:

```python
# service/worker.py — 야간 배치 제출 시에만 스킵을 요청한다(§2.3 의 submit_job_ex 사용)
submit_job_ex(..., payload={"workspace_id": kb_id, "skip_if_unchanged": True}, ...)
# app.py:527-531 /communities/build 는 submit_job 을 쓰고 이 키를 안 넣는다 → 기본 False → 항상 빌드
```

```python
# runner._run_community
dsn = os.environ["KBP_PG_DSN"]                 # ← v6 스케치엔 이 줄이 없어 NameError(사실 1)
snapshot_at = datetime.now(timezone.utc)       # ← now_utc() 는 존재하지 않는다
skip_if_unchanged = bool(ctx.payload.get("skip_if_unchanged"))
try:
    nodes, edges = self.graph_probe(eq_ws, dsn)
except Exception:
    raise JobRetryable("graph probe failed")   # fail-closed — 삭제도 스킵도 하지 않는다

if nodes == 0:
    removed = self.report_cleaner(eq_ws, dsn)
    self._record_build(workspace_id, eq_ws, snapshot_at, "skipped", 0, 0, failed=False)
    return {"workspace_id": eq_ws, "skipped": "empty graph", "reports_removed": removed}

# ★ v10 [3]: 스킵 판정은 **성공 세대 이력만** 본다. 실패 기록이 카운트를 보존하면
#   실패한 workspace 가 다음 밤 (prev.count == nodes,edges) 로 "graph unchanged" 스킵되고
#   status='skipped' 로 fail_streak 까지 리셋돼 **영구히 안 만들어진다**.
prev = self.repo.last_community_build(workspace_id, status="succeeded")
if skip_if_unchanged and prev and (prev.node_count, prev.edge_count) == (nodes, edges):
    self._record_build(workspace_id, eq_ws, snapshot_at, "skipped", nodes, edges, failed=False)
    return {"workspace_id": eq_ws, "skipped": "graph unchanged"}   # ★ 야간 배치만 여기로 온다

# ★ v10 [6]: **기존 `self._stage(ctx, "building")` 을 여기서 호출한다**(graph_probe 이후·
#   builder 이전). v9 스니펫은 이 줄을 통째로 빠뜨렸는데, 이것이 lease 상실 시 JobAborted 를
#   던지는 **유일한 게이트**이고 `test_community_commits_the_stage_before_building` 이 이를
#   검증한다(§5 의 "기존 2건 무변경 통과" 단언이 여기에 걸린다).
#   ★ v10 [5]: 이 게이트가 §3 의 `replace=True` 와 직결된다 — _recover 가 max_runtime 초과
#   잡을 failed 로 종결해도 **실행 중 스레드는 안 끊긴다**(순수 SQL). 종결 즉시 버킷(상한 1)
#   점유가 풀려 새 잡이 승인되고, 좀비가 그대로 store_reports 를 부르면 두 세대가 서로의
#   리포트를 DELETE 한다. `_stage` 는 여기서 한 번 막아주지만 builder 는 수십 분이므로
#   **DELETE 직전에 한 번 더 확인해야 한다** → §3 의 lease 재검사 규약을 참조.
self._stage(ctx, "building")

# ★ v10: 상한을 **러너가 읽어 넘긴다**. v9 는 community.py 에 시그니처만 추가하고
#   호출부에 인자를 안 넘겨, 상한이 영구히 None(무제한)인데 §5 테스트는
#   build_workspace_communities 를 직접 호출해 자르기만 검증하므로 **전부 초록**이었다
#   (§2.5.1 의 방어가 존재하지 않는 상태). 야간·수동 **양쪽 경로가 같은 러너**를 타므로
#   여기 한 곳에서 읽으면 둘 다 적용된다.
max_comm = _env_int("KBP_COMMUNITY_MAX_COMMUNITIES_PER_BUILD", 150) or None
result = builder(eq_ws, llm=get_text_llm(), dsn=dsn, max_communities=max_comm)
self._record_build(workspace_id, eq_ws, snapshot_at, "succeeded", nodes, edges, failed=False)
return {"workspace_id": eq_ws, "result": result if isinstance(result, (dict, list, str, int)) else None}
```

> ### ★ v10 [2][3] — `record_community_build` 의 컬럼별 UPSERT 규칙 (미정이었다)
>
> **[2] 회수 경로가 러너를 안 탄다.** `max_runtime` 초과·stale lease 회수·워커 급사는
> `_recover`(`repo.py:329-372`)의 **순수 SQL UPDATE** 로 처리되고 `runner.run` 의 예외
> 처리부(`runner.py:151-155`)가 **전혀 실행되지 않는다** → `record_community_build(failed=True)`
> 가 호출되지 않는다. 그런데 이 경로가 정확히 §2.5.1 이 방어 대상으로 지목한 "끝나지 않는
> 대형 빌드"다. `fail_streak` 가 0 에 머물면 §2.2 의 `ORDER BY fail_streak ASC` 가 **no-op**
> 이 되어, 같은 workspace 가 매 밤 2시간치 LLM 을 태우며 캡 선두를 영구 점유한다.
> → **`_recover` 안에서 직접 UPSERT 한다**(같은 트랜잭션):
> ```sql
> INSERT INTO kbp.community_builds (workspace_key, status, fail_streak)
> SELECT j.payload->>'workspace_id', 'failed', 1
>   FROM recovered j WHERE j.kind = 'community'
> ON CONFLICT (workspace_key) DO UPDATE
>   SET status = 'failed', fail_streak = kbp.community_builds.fail_streak + 1
> ```
> `payload` 가 오프로드로 NULL 인 경우는 `workspace_key`(= eq UUID)로는 PK(kb id)를 못
> 찾으므로 **건너뛴다** — community 잡의 payload 는 2키뿐이라 256KB 오프로드가 일어나지
> 않는다(사실 3·4 는 insert 잡 이야기다). 이 전제를 §5 테스트로 고정한다.
>
> **[3] 컬럼별 규칙을 못 박는다.** v9 는 "실패 시 `snapshot_at=None`" 만 적어 네 가지가 미정이었다.
>
> | 컬럼 | 성공 | 스킵 | **실패** |
> |---|---|---|---|
> | `snapshot_at` | 빌드 시작 시각으로 갱신 | 갱신 | **덮지 않는다**(기존 보존) |
> | `node_count`/`edge_count` | 갱신 | 갱신 | **덮지 않는다**(§2.6 정합성 가드의 입력) |
> | `status` | `succeeded` | `skipped` | `failed` |
> | `fail_streak` | **0 리셋** | **0 리셋** | **+1** |
>
> - **실패가 `snapshot_at` 을 덮지 않는 이유**: 덮으면(NULL) 그 workspace 가 매 밤 영구
>   재빌드 후보가 되고, 보존하면 §2.2 의 `li.inserted_at > cb.snapshot_at` 이 여전히 참이라
>   **후보에는 남되** `fail_streak` 로 후순위가 된다 — 의도한 동작이다.
> - **스킵 판정은 `status='succeeded'` 이력만 참조**(위 러너 스니펫의
>   `last_community_build(workspace_id, status="succeeded")`). 실패 기록이 카운트를 보존하는데
>   스킵 판정이 그걸 보면 실패한 workspace 가 "graph unchanged" 로 **영구 스킵**된다.
> - **`JobAborted` 는 기록하지 않는다** — lease 상실은 다른 세대가 그 workspace 를 소유한
>   것이라 이쪽의 실패가 아니다. `runner.run` 의 except 에서 `JobAborted` 를 먼저 분기한다.
> - **§5 테스트**: mock 호출 인자 단언만으로는 UPSERT 가 항상 0 으로 덮어써도 초록이다.
>   **실 Postgres 라운드트립**으로 (연속 2회 increment → 2), (reset → 0),
>   (실패 기록이 기존 `snapshot_at`·카운트를 덮지 않음)을 검증한다.

**`_record_build` 가 `fail_streak` 를 실제로 관리한다** — v6 은 이 필드를 스키마에만
선언하고 갱신하는 코드가 없어 `ORDER BY fail_streak` 가 항상 0 인 no-op 이었다:

```python
def _record_build(self, workspace_key, eq_ws, snapshot_at, status, nodes, edges, *, failed):
    self.repo.record_community_build(
        workspace_key, eq_ws, snapshot_at, status, nodes, edges,
        fail_streak_delta="reset" if not failed else "increment",
    )
```
`record_community_build` 는 UPSERT 로 `fail_streak = 0`(reset) 또는
`fail_streak = kbp.community_builds.fail_streak + 1`(increment) 를 적용한다.
**러너가 예외로 빠지는 경로(`JobRetryable`/`JobFailed`)도 잡아야 한다** — `runner.run()` 의
공통 예외 처리부에서 `kind == "community"` 이면 `record_community_build(..., failed=True,
snapshot_at=None)` 을 호출한다(스냅샷을 안 남겨 §2.2 의 `snapshot_at IS NULL` 로 계속
후보에 남되, `fail_streak` 증가로 후순위로 밀린다).

- **`graph_probe`·`report_cleaner` 를 주입 가능하게** 한다(`community_builder` 와 같은 방식,
  사실 2). 안 그러면 가짜 DSN 을 쓰는 `test_job_runner.py:345,366` 이 실제 접속으로 깨진다.
- **카운트 동일 = 생략은 근사다.** 수가 같은데 내용만 바뀐 경우를 놓친다. 적재는 거의 항상
  노드를 늘리므로 실용적으로 충분하고 **놓쳐도 다음 적재 때 잡힌다**. 대안(그래프 해시)은
  Seq Scan 전량 읽기라 더 비싸다 — 의도적 트레이드오프로 기록한다.

#### 2.5.1 끝날 수 없는 빌드에 대한 방어

`build_workspace_communities`(`community.py:507`)는 커뮤니티마다 LLM 1회를 순차로 부르고
`store_reports` 는 **루프가 끝난 뒤 한 번만** 실행돼 중간 진척이 안 남는다. 라이브에 이미
231개(사실 39) 보유 workspace 가 있고 111개가 29~38분이므로 **231개는 60~80분** — 그래프는
적재마다 단조 증가하니 이 숫자는 앞으로도 는다. `max_runtime`(7200s) 을 넘으면 회수되고
`max_attempts=1`(§2.7) 로 **즉시 failed** → 예외 경로 기록(위)으로 `fail_streak` 는 늘지만
**성공 없이 매 밤 2시간치 LLM 비용을 반복 태우는 상태가 조용히 지속될 수 있다.**

```python
# kb_pipeline/community.py — 커뮤니티 수 상한
def build_workspace_communities(..., max_communities: int | None = None):
    ...
    if max_communities and len(communities) > max_communities:
        log.warning("workspace %s has %d communities, capping to %d",
                    workspace_id, len(communities), max_communities)
        communities = sorted(communities, key=len, reverse=True)[:max_communities]  # 큰 것 우선
```
> ### ★ v10 [4] — 상한과 `replace=True` 를 **함께 켜면 안 된다**
>
> §3 의 `replace=True` 는 "reports 가 비어있지 않으면 workspace+level **전량 DELETE** 후
> 삽입"이다. 상한이 걸린 빌드는 상위 N개만 reports 에 담으므로, 라이브 231개 workspace 는
> **첫 야간 빌드에서 81행이 영구 소실**된다(사실 39). §3 의 목적(존재하지 않는 커뮤니티
> 요약 제거)이 정반대로 **존재하는 커뮤니티의 리포트 소각**이 된다.
>
> 게다가 상한은 `sorted(key=len)` 로 결정적이고 카운트가 그대로면 `skip_if_unchanged` 로
> 다음 빌드가 **아예 안 돌므로**, 아래 "잘린 커뮤니티는 다음 빌드에서 다시 후보가 될 수
> 있다" 는 **성립하지 않는다**(그 문장은 v10 에서 철회한다).
>
> **규약**: `build_workspace_communities` 는 **상한에 실제로 걸린 빌드에서 `replace=False`
> 로 강등한다**(부분 갱신 = upsert 만). 상한에 안 걸린 빌드만 `replace=True` 로 세대를
> 정리한다. 즉 `replace_effective = replace and not truncated`. 상한에 걸린 workspace 는
> 낡은 리포트가 남을 수 있지만, **살아있는 리포트를 지우는 것보다 낫다**(§6 에 기록).
>
> `truncated` 여부는 반환값에 실어 러너가 `community_builds` 에 남긴다(운영 가시성).

`KBP_COMMUNITY_MAX_COMMUNITIES_PER_BUILD`(기본 **150** — 111개 38분 기준 150개는 ~50분,
7200s 상한에 여유가 크다)를 야간 배치·수동 빌드 양쪽에 적용한다. 잘린 커뮤니티는 다음
빌드에서 다시 후보가 될 수 있다(작은 커뮤니티가 영구히 안 만들어질 수 있음을 §6 에 남긴다).

### 2.6 스테일 리포트 스윕 — fail-closed, **타입 일치**

`DELETE /doc` 은 잡 행을 안 남기므로(사실 29) 문서를 전량 지운 workspace 는 후보에 안 잡히고
리포트만 남아 **삭제된 문서 기반으로 답한다.**

야간 배치 앞에 LLM 없는 스윕을 붙인다:
```sql
SELECT DISTINCT workspace_id FROM public.community_reports   -- 테이블 부재 시 no-op(사실 16)
```
`workspace_id` 는 **`uuid` 컬럼**(사실 34)이라 드라이버가 `uuid.UUID` 로 반환한다. 그런데
`graph_counts` 가 쓰는 `_NODE_SQL`/`_EDGE_SQL` 의 비교식 좌변은 **text**(사실 35) — 그대로
넘기면 `operator does not exist: text = uuid` 로 매 workspace 가 예외를 내고 fail-closed 로
전부 건너뛰어 **스윕이 영구 no-op** 이 된다.

```python
for row in rows:
    ws = str(row["workspace_id"])          # ★ uuid → text 명시 캐스트
    nodes, edges = graph_counts(ws, dsn)
    if (nodes, edges) == (0, 0):
        delete_reports(ws, dsn)
```

**fail-closed 를 명문화한다**(사실 19 의 GC 원칙과 동일):
- `graph_counts` 가 **예외를 올리면 그 workspace 를 건너뛴다.** "모른다"로 삭제하지 않는다.
  이 레포의 기존 관례(`search.py:143-146` 의 `except psycopg.Error: return False`)가 정확히
  위험한 방향이라 **반복하지 않는다.**
- 1회 스윕당 삭제 상한 `KBP_COMMUNITY_SWEEP_MAX_DELETES`(기본 **5**). 초과분은 로그만 남기고
  다음 밤으로 — 스키마 사고로 전량 삭제되는 것을 막는다.
- **★ v10 [8] 조회 경로를 만든다.** 스윕 후보는 `public.community_reports.workspace_id`
  = **eq UUID** 인데, `kbp.community_builds` 의 PK 는 `workspace_key` = **kb id** 다.
  `eq_workspace_id` 는 유니크·인덱스 없는 부가 컬럼이라 **역인덱스가 안 되고**, §4 가 정의한
  API 도 `last_community_build(workspace_key)` 뿐이라 **아래 정합성 가드를 실행할 방법이 없다.**
  → `kbp.community_builds.eq_workspace_id` 에 **`UNIQUE` 제약 + 인덱스**를 두고
  `last_community_build_by_eq(eq_workspace_id)` 를 추가한다.
- **★ v10 [8] 이력 0행이면 삭제를 보류한다.** 신규 테이블이라 **배포 첫 밤에는 전 workspace 가
  이력 0행** → 가드가 정의상 미발화이고, 그 밤 `graph_counts` 가 `(0,0)` 인 workspace 는
  상한 5개까지 리포트가 **실제로 지워진다**. 스윕의 유일한 안전장치가 가장 위험한 밤에
  꺼져 있는 셈이다. → **직전 이력이 없으면 삭제하지 않고 `log.warning` 만** 남긴다
  (다음 밤부터는 이력이 쌓여 정상 판정된다).
- **정합성 체크**: 직전 `community_builds.node_count > 0` 인데 이번 `graph_counts` 가
  `(0, 0)` 이면 **한 밤 사이 그래프 전체가 사라진 것** — 정상적 문서 삭제 흐름보다 스키마
  사고를 의심할 근거가 크다. 이 경우 삭제를 보류하고 `log.error` 만 남긴다(1회 스윕당
  이런 보류가 발생해도 상한 계산에는 포함하지 않는다 — 삭제 자체를 안 했으므로).
- **비용**: `graph_counts` 는 표현식 인덱스가 없어 **Seq Scan** 이다(사실 18, 실측 16ms).
  현재 리포트 보유 5개 workspace 기준 무시할 만하고, 커지면 표현식 인덱스를 검토한다
  (**지금은 만들지 않는다** — 범위 밖).

> **잔여**: **일부** 문서만 지운 workspace 는 다음 적재까지 낡은 리포트를 쓴다. 완전 해법
> (삭제도 잡을 남긴다)은 **범위 밖 → deferred**.

### 2.7 실행 예산·타임존

실측 상단 38분(사실 22), 버킷 상한 1(사실 8) → 직렬.

- `KBP_COMMUNITY_MAX_PER_NIGHT` 기본 **8** → 8 × 38분 = **5시간 04분**(03:00~08:04).
  하단값 30분으로 계산하면 캡 10 이 6시간 20분(09:20)이 된다 — **상단값으로 잡는다.**
- **`repo.py` 를 고쳐** `"community": _env_int("KBP_JOB_MAX_ATTEMPTS_COMMUNITY", 1)` 추가.
  지금은 override 가 없어 기본 3 이고(사실 9·10) compose 에 env 만 넣으면 **조용한 no-op**
  이다. 3 × 7200s = 워크스페이스당 최악 6h 이고, `max_runtime` 회수는 진행 중 호출을 못 끊어
  **2중 실행**된다 — §3 의 `replace=True` 와 겹치면 두 트랜잭션이 서로의 리포트를 지운다.
  **부수효과**: `max_attempts_by_kind()` 는 kind 단위(사실 9)라 이 값은 **수동
  `/communities/build` 에도 적용된다** — 일시적 DB·LLM 오류 한 번이 재시도 없이 그 밤(또는
  수동 재빌드 1회 호출)을 통째로 실패시킨다. §2.5 의 `JobRetryable` 은 `attempt_count <
  max_attempts` 가 성립할 때만 재시도되므로, `max_attempts=1` 에서는 사실상 `JobFailed` 와
  동치다 — 감수한다(다음 밤이 어차피 재시도한다).
- **진짜 최악**: 7200s 짜리가 연달아 나면 8 × 2h = 16h. §2.4 의 **마감 취소**가 queued 를
  자른다. running 1건은 최대 2h 더 갈 수 있다 — 감수한다. §2.5.1 의 커뮤니티 수 상한이
  7200s 도달 자체를 줄인다.
- 버킷 상한을 올리는 것은 답이 아니다(동시 LLM 이 임베딩·추출과 경합).

**틱 간격** — 기존 폴 간격(사실 36, 기본 2초)을 그대로 쓰면 2시간 창 안에서 초당 수회 DB
쿼리가 된다. `kbp-gc` 와 같은 방식으로 **별도 간격**을 둔다: `KBP_COMMUNITY_POLL_SECONDS`
기본 **60**(창 판정·마감 취소 정확도에 60초면 충분하고, 후보 쿼리 비용도 분당 1회로 낮다).

**타임존** — `worker.py` 는 전부 `datetime.now(timezone.utc)` 관례라(사실 28) 그대로 따라
쓰면 03:00 UTC = **12:00 KST** 로 목적이 정반대가 된다.
```python
def _zone():
    name = os.environ.get("TZ") or "Asia/Seoul"
    try: return ZoneInfo(name)
    except Exception:                       # POSIX 'KST-9' · 오타 → 스레드 즉사 방지
        log.warning("TZ=%r unusable; falling back to Asia/Seoul", name)
        return ZoneInfo("Asia/Seoul")
```
- **`TZ` 를 `facade_env` 앵커에 넣는다**(사실 26 — 서비스별 분리는 앵커를 쪼개야 하고,
  facade 가 `TZ` 를 갖는 것은 무해하다). `TZ: ${TZ:-Asia/Seoul}`.
- `KBP_COMMUNITY_BUILD_AT` 기본 `"03:00"`. 파싱 실패 → 기본값 + warning.
- 기동 로그에 **다음 실행 시각을 절대시각으로** 찍는다.
- `KBP_COMMUNITY_BUILD_ENABLED` 기본 `true`. `false` 면 스레드를 안 띄운다.
- **TTL 경고**: `gc.ttl_seconds()`(사실 20 — `_SECONDS` 를 먼저 읽는다)가 48h 미만이면 기동 시
  warning. 기본 72h 에선 발화하지 않으므로 백로그 잔여 경고(§2.2)가 주 감지 수단이다.

**부분 실패 복구** — 후보 제출 루프 중 `ensure_workspace`(edgequake HTTP) 가 5xx 로 끊기면
그 밤은 `submitted < cap` 인 채로 끝난다. **재시도하지 않는다** — `claim_run` 마커가 이미
섰고, 다음 밤에 같은 후보가 (스냅샷이 안 바뀌었으므로) 다시 최우선으로 잡힌다. 이 경우
`backlog` 카운트는 실제보다 낮게 잡히지만, §2.2 의 오래된-순 정렬이 결국 따라잡는다 —
당장 별도 재시도 로직을 추가하지 않는다(의도적으로 범위를 좁힌다).

### 2.8 적재 직후 트리거 제거 (kb)

`tasks.py:363` 의 호출을 제거한다. 함수 정의(`:385`)·`build_communities_task`·arq 등록은
남긴다. 주석: *"커뮤니티 빌드는 facade-worker 야간 배치가 소유한다(진입점 단일화). 수동
재빌드는 facade `/communities/build` 직접 호출뿐이다."*

> `test_community_job.py` 5건은 함수를 **직접 호출**하는 단위테스트라(사실 24) 무변경으로
> 통과한다. 문제는 호출부를 덮는 테스트가 **0건**이라는 것이다(사실 25).

---

## 3. 리포트 세대 정리 (재빌드가 원인이라 A 에 속한다)

`store_reports` 는 DELETE 없이 upsert 하고(사실 17) `community_id` 는 enumerate 인덱스다.
재빌드로 커뮤니티가 줄면 낡은 리포트가 영구히 남아 **존재하지 않는 커뮤니티의 요약으로
답한다.** 야간 재빌드가 규칙화되면 확정적으로 터진다.

```python
def store_reports(..., *, replace: bool = False) -> int:
    # replace=True 이고 reports 가 **비어 있지 않으면** 같은 트랜잭션에서
    #   DELETE FROM public.community_reports WHERE workspace_id=%s AND level=%s
    # 를 먼저 실행. 실패하면 롤백되어 옛 리포트가 살아남는다.
```
- **★ v10 [5] DELETE 직전 lease 재검사 (좀비 방어).** `max_attempts=1` 은 2중 실행을 막지
  **못한다** — `_recover`(`repo.py:329-372`)는 `max_runtime`(7200s)·heartbeat 초과 잡을
  `failed` 로 종결하고 `idem_key` 를 비우지만 **실행 중 스레드는 끊지 못한다**(순수 SQL).
  종결 즉시 community 버킷(상한 1, `admission.py:145`) 점유가 풀려 새 잡이 승인되고,
  좀비가 완주해 `store_reports` 를 부르면 **두 세대가 서로의 리포트를 DELETE** 한다.
  지금까지는 순수 upsert 라 무해했지만 `replace=True` 가 이걸 **전량 소실**로 바꾼다.
  → `store_reports(replace=True)` 는 **DELETE 를 실행하기 직전 같은 트랜잭션에서**
  `SELECT status, claimed_by FROM kbp.jobs WHERE id=%s FOR SHARE` 로 자기 잡이 여전히
  `running` 이고 `claimed_by` 가 자신인지 확인한다. 아니면 **DELETE·INSERT 를 모두 건너뛰고**
  `JobAborted` 를 올린다(작업은 버리되 살아있는 세대를 훼손하지 않는다).
  러너가 `job_id`·`worker_id` 를 `store_reports` 에 넘길 수 있도록 시그니처를 확장한다.
  (대안인 workspace 단위 advisory lock 은 7200s 를 잡고 있어 회수 자체를 막아 채택하지 않는다.)
- **`reports == []` 면 DELETE 생략.** 안 그러면 `min_community_size` 로 전부 걸러진 빌드가
  리포트 전량(라이브 231·111·92·39행)을 **정상 종료로 소각**한다. 빈 그래프 정리는
  §2.5·§2.6 이 **명시적으로** 담당한다.
- 기본값 `False` → 기존 호출자·테스트 무변경. `build_workspace_communities` 만 `True`.

---

## 4. 변경 목록

**kbp — 코드**
- `service/worker.py` — `kbp-community` 스레드(60s 폴 간격·창·`claim_run`·스윕·후보·제출·
  마감 취소·백로그/TTL 경고). 스레드 로직은 §2.4 의 `_current_run_date` 고정 방식을 따른다
- `service/jobs/schema.py` — `kbp.batch_runs`(PK `(name, run_date)`), `kbp.community_builds`
- `service/jobs/api.py` — **`submit_job` 은 무변경.** 신규 `submit_job_ex(...) -> tuple[uuid.UUID, bool]`
  추가(§2.3) — `submit_job` 의 공유 호출부 8곳(legacy 4경로 + `/jobs/*` 라우터 4개,
  `app.py:200` `_legacy_job()` 및 `jobs/api.py:302,335,362,390`)은 **전부 무변경**
- `service/jobs/repo.py` — `"community": _env_int("KBP_JOB_MAX_ATTEMPTS_COMMUNITY", 1)`;
  `claim_run(name, run_date)`; `record_community_build(workspace_key, eq_ws, snapshot_at,
  status, nodes, edges, *, fail_streak_delta)`; `last_community_build(workspace_key)`;
  `cancel_queued_by_batch_key(batch_key)`(§2.4.1 — **`idem_key=NULL` 을 함께 세팅**)
- `service/jobs/runner.py` — `graph_probe`·`report_cleaner` 주입 seam + `_run_community`
  재작성(§2.5) + 공통 예외 처리부에서 `kind=="community"` 실패를 `record_community_build
  (failed=True)` 로 기록, `dsn` 지역변수
- `service/jobs/memory.py` — `InMemoryJobRepo` 에 신규 메서드 대응
- `kb_pipeline/community.py` — `graph_counts()`·`delete_reports()` 신규;
  `store_reports(replace=)`; `build_workspace_communities(max_communities=)`(§2.5.1)
- **`service/app.py` 무변경** — `/communities/build` 는 계속 `submit_job`(단일 반환값)을
  쓴다(§2.3)

**kbp — 테스트**
- `service/tests/test_job_runner.py` — 주입 seam·`graph_counts` 분기·예외 시 `fail_streak`
  증가 기록
- `service/tests/test_job_repo_pg.py` — `_truncate()` 에 `kbp.batch_runs`·
  `kbp.community_builds` 추가(사실 38 — 안 하면 `claim_run` 테스트가 같은 날 재실행에서
  실패); `claim_run`·`max_attempts["community"]`·`cancel_queued_by_batch_key` 의
  `idem_key=NULL` 검증
- **★ v10 [12] 테스트를 두 파일로 쪼갠다.** v9 는 실행 판정 전부를 `requires_pg` 단일 파일에
  몰아넣었는데, `test_job_repo_pg.py:26-27`(DSN 미설정)·`:42-53`(살아있는 facade-worker 감지)
  규약을 따르면 **이 프로젝트의 기본 dev 상태에서 한 줄도 실행되지 않는다** — 창 판정·자정 랩·
  TZ 폴백·제출 계약 같은 **순수 파이썬 로직의 유일한 회귀 방어가 상시 skip** 된다.
  - **신규** `service/tests/test_community_schedule.py` — **PG 불요**. 창 안/밖, 자정 랩,
    `_current_run_date`, TZ 폴백·`BUILD_AT` 파싱 실패, `ENABLED=false` 스레드 미기동,
    제출 계약(payload·`workspace_key`·**야간 `idem_key`**·`batch_key`), `submit_job_ex` 의
    `(job_id, created)` 반환, 예외가 스레드를 안 죽임 — 전부 페이크 repo 로 검증한다.
  - **신규** `service/tests/test_community_nightly.py` — `pytestmark =
    pytest.mark.requires_pg`. **SQL 의미론만** 남긴다: 후보 쿼리, `claim_run` 경합,
    `cancel_queued_by_batch_key` 의 `idem_key=NULL`, `record_community_build` 라운드트립
    (increment 2회 → 2, reset → 0, 실패가 `snapshot_at`·카운트를 안 덮음), `_recover` 가
    community 실패를 `fail_streak` 로 기록함
- **★ v10 [12] kb 회귀는 스파이 큐를 주입한다.** `test_worker_kb_pipeline_stages.py:121-130` 의
  `_ctx` 가 `"queue": None` 이고 `_maybe_enqueue_community_build` 는 `queue is None` 이면
  즉시 return 하므로(`tasks.py:406-408`), **호출부를 안 지워도 통과하는 빈 초록불**이다.
  → `ctx["queue"]` 에 **enqueue 를 기록하는 스파이**를 넣고, **삭제 전에 빨강(enqueue 1회)**
  임을 먼저 확인한 뒤 삭제해 초록으로 바꾼다(빨강→초록을 못 보면 그 테스트는 무효다).
- **★ v10 [12] `store_reports(replace=)` 테스트 하네스를 못 박는다.** `tests/test_community.py`
  는 psycopg 사용이 0건인 순수 인메모리 파일이고 대상 함수는 `psycopg.connect(dsn)` 을 쓴다.
  mock 으로 때우면 "같은 트랜잭션 DELETE·실패 시 롤백" 주장이 **검증되지 않는다**.
  → `replace` 의 트랜잭션 의미론은 `service/tests/test_community_nightly.py`(requires_pg)로
  옮기고, `tests/test_community.py` 에는 `max_communities` 자르기 같은 **순수 로직만** 남긴다
- ~~**신규** `service/tests/test_community_nightly.py` — `pytestmark =
  pytest.mark.requires_pg`~~(사실 38, `test_job_repo_pg.py` 와 동일 skip 규약). 창·마커·후보
  쿼리·마감 취소·스윕·자정 랩을 **실 Postgres** 로 검증한다(순수 SQL 의미론이라 인메모리
  모킹으로는 못 잡는다)
- `tests/test_community.py`(**기존 파일**, `kb_pipeline/tests/` 아님 — 사실 37, 그 디렉터리는
  `testpaths` 에 없어 수집되지 않는다) — `store_reports(replace=)`·`graph_counts`·
  `delete_reports`·`build_workspace_communities(max_communities=)`

**kbp — 설정/배포**
- compose ×2 `facade_env` **앵커**(compose `:10`, airgap `:27` — **2026-08-09 재확인, 불변**) — `TZ: ${TZ:-Asia/Seoul}`,
  `KBP_COMMUNITY_BUILD_AT/ENABLED/WINDOW_MINUTES/DEADLINE_MINUTES/MAX_PER_NIGHT`,
  `KBP_COMMUNITY_SWEEP_MAX_DELETES`, `KBP_JOB_MAX_ATTEMPTS_COMMUNITY`,
  `KBP_COMMUNITY_POLL_SECONDS`, `KBP_COMMUNITY_MAX_COMMUNITIES_PER_BUILD`
  (전부 `${VAR:-기본}`)
- `scripts/facade.env` — 같은 변수(dev 는 호스트 런처, 사실 31)
- `.env.airgap.example` — **`TZ` 만 A 섹션**(배포지 시각은 운영자 결정). 나머지는 compose
  기본값 관례를 따라 B-3 튜닝 섹션에 넣지 않는다
- `scripts/airgap/verify-bundle.sh` — **`REQUIRED_ENV` 에 `TZ` 를 넣지 않는다**(v9 계획 철회).
  `REQUIRED_ENV` 는 "비어 있으면 **실패**" 목록이라(`check_env`: 값 없으면 `miss=1` → `return 1`),
  `TZ` 를 넣으면 **파서 전용 배포가 100% 검증 실패**한다 — `build-bundle.sh` 는 parse-only
  번들에도 `verify-bundle.sh` 를 동봉하고 `.env.parse-only.example` 에는 `TZ` 가 없다.
  `TZ` 는 compose 에 `${TZ:-Asia/Seoul}` 기본값이 있으므로 **빈값 금지 대상이 아니다.**
  → 대신 **`.env` 템플릿 2종(`.env.airgap.example`·`.env.parse-only.example`)에 값과 함께 선언**한다.
- **`.env.parse-only.example`** — `TZ` + `KBP_COMMUNITY_BUILD_ENABLED=false` 를 명시한다.
  `parse-only-up.sh` 는 facade-worker 를 **같은 `x-facade-env` 앵커로 띄우므로** 야간 스레드가
  그 배포에서도 기동한다. 파서 전용 배포는 커뮤니티를 안 쓰므로 **끄는 것이 기본**이어야 하고,
  끌 수단이 템플릿에 없으면 운영자가 존재조차 모른다.
- **`docs/parse-only-guide.md`** — 위 두 키를 사용 가이드에 반영(§4 문서 목록에 누락돼 있었다)

**kb**
- `workers/tasks.py:363`(실제 경로 `backend/app/workers/tasks.py:363`) — 트리거 호출 제거 +
  주석 (§2.8)
- **신규 회귀** — `backend/tests/test_worker_kb_pipeline_stages.py`(기존 하네스, 이미
  `ingest_document_task` 를 직접 구동한다)에 "성공해도 `BUILD_COMMUNITIES_TASK` 를
  enqueue 하지 않는다" 케이스 추가
- `test_community_job.py` 5건은 **호출부 삭제 후에도 남긴다** — 함수 자체(단위 동작)는
  여전히 유효하고, 수동 재빌드 경로(§2.8 주석이 가리키는 `/communities/build`)가 내부적으로
  이 함수를 다시 쓸 가능성에 대비한 회귀다. "죽은 코드의 초록불" 이 아니라 "함수 계약 보존"
  으로 문서에 명시한다

**문서**
- `_workspace/02-changes.md` — 진입점 일원화, "최대 하루 지연" 계약, 세대 정리,
  키 통일을 **기각한 근거**, `submit_job_ex` 신규 도입(공유 `submit_job` 은 무변경)
- `_workspace/03-dev-progress.md` — phase 진행
- `docs/airgap-deploy.md` — 야간 LLM 부하·`TZ`
- deferred — D21 종결, 신규 3건: **문서 삭제가 재빌드를 트리거하지 않는다**,
  **캡 백로그가 TTL 로 탈락할 수 있다**, **커뮤니티 수 상한을 넘긴 workspace 는 작은
  커뮤니티가 영구히 안 만들어질 수 있다**(§2.5.1)

---

## 5. 테스트

**대상 선정**
- `workspace_key` 로 뽑는다: `payload` NULL(오프로드) 행도 **포함** ← 회귀 핵심
- **`snapshot_at` 비교**: 빌드 중(09:10 시작·09:48 완료) 도착한 09:12 insert 가 **다음 밤
  후보에 든다** ← 영구 탈락 회귀
- 빌드 이력 없음 → 포함 / `snapshot_at` 이 더 최신 → 제외
- **queued·running community 가 있으면 제외** ← 중복 빌드 회귀
- **실패 후(`snapshot_at IS NULL`, `fail_streak > 0`)에도 후보에는 남되 정렬에서 뒤로 간다**
  ← `fail_streak` 가 실제로 갱신되는지(§2.5 예외 처리부) 확인하는 게 이 테스트의 핵심이다.
  갱신 코드 없이는 이 항목이 항상 통과하므로 **`record_community_build` mock 의 호출 인자를
  단언**해야 한다
- 캡 초과 시 오래 밀린 순, 잔여 warning, 3일 연속이면 error(`batch_runs` 가 `(name,
  run_date)` 이력을 보존하는지 확인 — 단일행이면 이 테스트 자체가 3일치를 못 만든다)
- 실패 상태·`workspace_key IS NULL` 제외

**제출 계약**
- `payload == {"workspace_id": kb_id, "skip_if_unchanged": True}`(야간 배치만),
  `workspace_key == eq_ws`, `idem_key == f"community:{eq_ws}"`,
  `batch_key == f"community-nightly:{run_date}"`(§2.4 의 고정된 `run_date`, 틱 시각 아님)
- kb id 와 eq UUID 를 **뒤바꾸지 않는다**
- **`submit_job_ex` 가 `(job_id, created)` 를 반환하고, idem 충돌은 `created=False` →
  `deduped` 로 센다** ← 반환 계약 확장이 실제로 있는지의 회귀
- `/communities/build` 는 `skip_if_unchanged` 를 **넣지 않는다**(기본 False → 항상 빌드)

**실행 판정**
- 창 안 + 오늘 마커 없음 → 실행 / **창 밖(14:00 기동) → 실행 안 함**
- **대상 0건이어도 `claim_run` INSERT 가 일어나고** 같은 `run_date` 의 다음 틱은 skip
  ← 밤새 루프 회귀
- `claim_run` 동시 호출에서 하나만 이긴다(`(name, run_date)` UNIQUE 충돌)
- **자정 랩**(`BUILD_AT=23:30, WINDOW=120`): 23:40 과 00:30 두 틱 모두 **같은 `run_date`**
  로 판정되고, 00:30 틱에서 창이 살아있다 ← v6 은 이 시나리오에서 창 후반이 죽었다
- **마감 취소가 실제로 실행된다**: `DEADLINE`(기본 420분, `WINDOW` 120분보다 큼) 이후
  틱에서 창 판정과 **무관하게** `cancel_queued_by_batch_key` 가 호출된다 ← v6 은 창 판정
  뒤에 둬 도달 불가능했다. 취소 대상은 오늘 batch_key 의 **queued 만**, running 은 보존
- **`cancel_queued_by_batch_key` 가 `idem_key` 를 NULL 로 세팅한다** — 세팅 안 하면 다음 밤
  제출이 취소된 job_id 를 재사용해 `deduped` 로 잘못 세어지는 것을 재현하는 테스트
- `ZoneInfo` 기준(TZ=Asia/Seoul 에서 03:00 KST) / `TZ` 가 `KST-9`·오타여도 **스레드가 안 죽고**
  Asia/Seoul 폴백 + warning
- `BUILD_AT` 파싱 실패 → 기본값 + warning
- `KBP_COMMUNITY_BUILD_ENABLED=false` → 스레드 미기동
- DB 조회·큐 제출 실패가 스레드를 죽이지 않는다
- `ttl_seconds() < 48h` → warning (`KBP_JOB_TTL_SECONDS` 경로 포함)
- **`test_community_nightly.py` 는 `pytest.mark.requires_pg` 로 마킹돼 있고, `KBP_PG_DSN`
  미설정 또는 활성 워커 감지 시 skip 된다** ← 소유 위치·실행 조건이 명시돼 있는지의 회귀

**러너**
- `nodes == 0` → builder 미호출, `report_cleaner` 호출, `skipped: empty graph`
- **야간 배치 제출(`skip_if_unchanged=True`)이고 카운트가 직전과 같으면** builder 미호출
  `skipped: graph unchanged` ← vector-only 회귀
- **수동 `/communities/build`(`skip_if_unchanged` 없음)는 카운트가 같아도 항상 빌드한다**
  ← v6 이 막았던 수동 재빌드 탈출구 회귀
- 카운트가 다르면 빌드 + `community_builds` 갱신(`snapshot_at` = **빌드 시작 시각**,
  `fail_streak` **리셋**)
- **`graph_probe` 예외 → `JobRetryable`, `delete_reports` 미호출** ← fail-closed
- **러너가 예외로 종료하면(`JobRetryable`/`JobFailed`) `community_builds.fail_streak` 가
  1 증가하고 `snapshot_at` 은 그대로(NULL 이면 NULL)** ← fail_streak 실제 기록 회귀
- 기존 2건(`test_job_runner.py:345,366`)이 가짜 DSN 으로 통과(주입 seam)
- `max_attempts_by_kind()["community"] == 1`, env 로 상향 가능
- **`build_workspace_communities(max_communities=150)`**: 커뮤니티가 200개면 150개로 잘리고
  나머지는 `log.warning` ← 끝나지 않는 빌드 방어 회귀

**스테일 스윕**
- 리포트 있음 + 그래프 비었음 → 삭제, LLM 미호출
- 그래프 있음 → 무변경
- **`workspace_id`(uuid) 를 `str()` 캐스트해 `graph_counts` 에 넘긴다 — 캐스트 없이 호출하면
  `text = uuid` 오류가 나는 것을 재현하는 테스트를 포함한다** ← 타입 불일치 회귀(v6 의 스윕은
  이 캐스트가 없어 영구 no-op 이었다)
- **`graph_counts` 예외 → 그 workspace 를 건너뛴다(삭제 안 함)** ← fail-closed
- **정합성 보류**: 직전 `node_count > 0` 인데 이번이 `(0,0)` 이면 삭제하지 않고 `log.error`
- 삭제 상한 초과 시 나머지는 다음 밤
- **`community_reports` 테이블 부재 → no-op**(예외 아님)

**리포트 세대**
- `replace=True` 로 커뮤니티가 줄면 낡은 행이 사라진다
- **`reports == []` → DELETE 생략** ← 전량 소각 회귀
- 빌드 실패 시 롤백되어 옛 리포트가 살아남는다
- `replace=False`(기본)는 기존 동작

**kb**
- **신규**: `backend/tests/test_worker_kb_pipeline_stages.py` 에 `ingest_document_task`
  성공 후 `BUILD_COMMUNITIES_TASK` **미 enqueue** 케이스(사실 25)
- `test_community_job.py` 5건 **무변경 통과**(사실 24)
- 회귀 기준선: **착수 시 `cd backend && pytest -q` 로 기존 실패 건수를 측정해 이 문서에
  기록하고** 그 수를 늘리지 않는다(직전 관측 19건은 미확정으로 취급).

---

## 6. 리스크

| 리스크 | 완화 |
|---|---|
| 커뮤니티가 최대 하루 지연 | 사용자가 인지하고 택했다(§0) |
| 야간이 업무시간 침범 | 캡 8 × **상단값 38분** = 5h04(§2.7) + **마감 취소**(§2.4, 도달 가능하게 재설계) + `MAX_ATTEMPTS=1`(**repo.py 코드 변경**) |
| running 1건이 마감 후 2h 더 감 | 중단해도 LLM 호출을 못 끊어 작업만 버린다 — 감수 |
| `MAX_ATTEMPTS=1` 이 일시 오류를 재시도 없이 실패시킨다(수동 빌드 포함) | 다음 밤이 재시도한다. §2.7 에 부수효과 명시 |
| 캡 초과 굶김 | 오래 밀린 순 + `fail_streak` 후순위(§2.2) — **`fail_streak` 를 실제로 기록하는 경로 필요**(§2.5) |
| **백로그가 TTL 로 조용히 탈락** | 잔여 로그(`(name, run_date)` 이력) + 3일 연속 error. **완전 방어 아님 → deferred** |
| **끝나지 않는 대형 빌드(커뮤니티 200+개)** | `max_communities` 상한(§2.5.1). 잘린 것은 다음 빌드로 미루되 영구히 못 만들어질 수 있음 → deferred |
| 카운트 같지만 내용이 바뀐 그래프 | 재빌드를 놓친다. 다음 적재 때 잡힌다 — 의도적 근사(§2.5) |
| **수동 재빌드가 스킵 가드에 막힌다** | `skip_if_unchanged` 를 야간 배치 payload 에만 명시(§2.5) — 수동은 항상 빌드 |
| 낡은 리포트로 답한다 | `replace=True`(§3) + 빈 그래프 정리(§2.5) + 스윕(§2.6) |
| **일부만 삭제한 workspace 의 낡은 리포트** | 다음 적재까지 남는다 → **deferred**(§2.6) |
| 스윕이 살아있는 리포트를 지운다 | **fail-closed** + uuid/text 캐스트(§2.6) + 1회 삭제 상한 5 + 정합성 보류 |
| **취소된 queued 잡이 idem_key 를 쥔 채 남아 그 KB 만 조용히 멈춘다** | `cancel_queued_by_batch_key` 가 `idem_key=NULL` 을 함께 세팅(§2.4.1) |
| **자정을 넘는 `BUILD_AT` 설정에서 창 후반이 죽는다** | `run_date` 를 한 번 고정(§2.4) |
| `TZ` 누락·이상값 | 앵커 기본값 + `ZoneInfo` 폴백 + 기동 로그에 다음 실행 절대시각 |
| 초당 수회 DB 폴링 | `KBP_COMMUNITY_POLL_SECONDS=60`(§2.7) |
| **상한에 걸린 workspace 의 낡은 리포트가 남는다** | v10 [4]: 상한 적용 시 `replace=False` 로 강등 — 살아있는 리포트를 지키는 쪽을 택했다. 잘린 커뮤니티는 그래프가 바뀔 때까지 리포트를 못 갖는다 → deferred D26 |
| **좀비 빌드가 살아있는 세대를 지운다** | v10 [5]: `store_reports(replace=True)` 가 DELETE 직전 lease 재검사, 실패 시 `JobAborted` |
| **야간 배치가 조용히 멈춘 것을 모른다** | v10 [10]: `batch_runs.status/error` 로 "마커만 서고 죽은 밤" 을 구별 |
| **파서 전용 배포에서 야간 스레드가 뜬다** | v10 [11]: `.env.parse-only.example` 에 `KBP_COMMUNITY_BUILD_ENABLED=false` 명시 |
