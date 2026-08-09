<!-- plan-version: v5 -->
<!-- ultracode-validation: READY v5 at 2026-08-09T17:40:00Z (구현 우선 전환 — 아래 §4 참조) -->

# [A1] 커뮤니티 야간 배치 — 스케줄러 + 적재 트리거 제거

> `2026-08-06-community-nightly-batch-plan.md`(A, v11 SUPERSEDED)에서 쪼갠 문서.
> A2(그래프 변화 스킵)·A3(리포트 세대 정리)·A4(스테일 스윕)는 별도.
>
> **개정 이력**
> - v4 → **v5**: 검증 blocking 11건 중 **설계 공백 7건 반영**, 나머지 4건은 §4
>   `구현 후 검증` 으로 이관하고 **구현 우선으로 전환**한다(글로벌 룰 "검증 비용 관리" 5번).
>   근거: 검증 6라운드에 **2.62M 토큰·75분**을 썼고 must_fix 가 14→14→14→9→10→11 로
>   **수렴하지 않았다**. 남은 4건은 "한 번 실행하면 즉시 드러나는 종류"다.
>   반영: 스케줄러 본체를 **`service/community_schedule.py`(신규 모듈)** 로 명시(어느
>   변경목록에도 없었다), `record_community_failure` writer 신설(DDL 의 `status`·
>   `finished_at` 을 채우는 코드가 없었다), 재claim 을 **창이 아니라 마감까지** 허용
>   (창 종료 30분 전 이후 사망 시 **이틀 지연**이었다), 위험표의 "마감 취소" 완화 주장
>   정정(**running 은 안 자른다** — 최악 12:00 점유), 제출 루프 **시간 예산**(edgequake
>   hang 시 틱이 통째로 막혔다), `parse-only-up.sh` 가 **`.env.airgap.example` 을
>   복사한다**는 사실 반영 + 강제 수단 확정, `_run_ingest` **조기 반환 경로**(그래프 미접촉)
>   에서 touch 금지, kb 테스트 수 **6→9건**(async 3건 누락), `FakeRepo` 에 `db_now` 추가.
> - v3 → **v4**: 검증 blocking 10건 반영 + **문서 구조 변경**. v1~v3 결함의 70%가
>   "같은 사실이 §2 본문 / §3 변경목록 / §4 테스트 **세 곳에 중복 기술**되고 한 곳만 고쳐져
>   서로 어긋나는 것"이었다(v1 6/14, v2 7/9, v3 7/10). **§3·§4 를 없애고 각 절이
>   설계·변경파일·테스트를 함께 갖게** 했다 — 사실의 출처를 하나로 만든다.
>   내용 수정: psycopg3 에서 `interval '%s minutes'` 는 실행 불가(리터럴 안 플레이스홀더)
>   → `make_interval(mins => %s)`, `LIKE` 의 `%` 는 `%%` 이스케이프,
>   `record_community_success` 에 `snapshot_at` 인자 명시, dedupe 경로도 `record_attempt`,
>   `created=False` 계수, `STALE_RUN_MINUTES` 를 env 표에 등재, `record_community_success` 도
>   best-effort, `last_batch_run(name, run_date)` 로 **기대 날짜 비교**, 스케줄러 루프
>   최상위 예외 가드, `_touch_graph_safe` 주석의 틀린 주장 정정 + 1회 재시도.
> - v2 → v3: `last_success_at` 을 **빌드 시작 스냅샷**으로, `_run_ingest` 도 touch,
>   `touch_graph` best-effort, `ensure_workspace` 실패 시에도 `record_attempt`,
>   굳은 `started` 회수, `_zone()` 2단 폴백, `parse-only-up.sh` 강제, `FakeRepo` 확장.
> - v1 → v2: `kbp.graph_touch` 신설(vector-only 회귀·TTL 증거소실 해결),
>   `last_success_at`/`last_attempt_at` 분리, in-flight 검사를 파이썬으로.

---

## 0. 목표와 비범위

### 목표
1. 커뮤니티 빌드가 **문서 적재 경로에서 트리거되지 않는다**.
2. 하루 1회 정해진 밤에, **그래프가 변한** workspace 만 큐에 넣는다.
3. 수동 `POST /communities/build` 가 **야간 잡에 흡수되지 않는다**.
4. 야간 배치가 **조용히 멈춘 것을 탐지**할 수 있다.
5. 폐쇄망에서 추가 의존성 없이 동작하고 **파서 전용 배포를 깨지 않는다**.
6. **어떤 workspace 도 현행보다 빌드가 줄거나 늘지 않는다.**

### 비범위 (이걸로 NEEDS_REVISION 금지)
- 그래프 변화 **스킵 판정**(`skip_if_unchanged`·`graph_counts`·카운트·`fail_streak`·
  `max_communities`) → **A2**
- `store_reports(replace=)`·좀비 방어 → **A3** (A1 은 `store_reports` 를 안 건드린다)
- 스테일 스윕·`delete_reports` → **A4** / global 검색·D22 → **B**
- community 버킷 **전역 상한 1(직렬) 유지** — D21 종결(사용자 재확인 2026-08-09)
- 리포트 최대 하루 지연은 **수용된 트레이드오프**
- `verify-bundle.sh` `REQUIRED_ENV` 가 parse-only 에서 전체 통과하지 못하는 것
  → **기존 결함**(A1 이 만들지도 악화시키지도 않는다). deferred

---

## 1. 실측 사실 (2026-08-09, 이 워킹트리에서 직접 확인)

| # | 사실 | 근거 |
|---|---|---|
| 1 | 워커는 `JobRunner(repo=…, blobs=…)` 만 조립, eq 는 `eq_factory` 지연 | `worker.py:55` |
| 2 | 워커 폴 간격 기본 **2초** | `worker.py:58` |
| 3 | 워커 시각 관례 `datetime.now(timezone.utc)`; 컨테이너 TZ 미설정이면 **UTC** | `worker.py:63` |
| 4 | GC 는 **전용 데몬 스레드** | `worker.py:285` |
| 5 | 러너는 **오프로드된 payload 를 복원**한다 → `ctx.payload` 로 `extract_graph` 조회 가능 | `runner.py:139`, 사용처 `:205` |
| 6 | insert payload 는 **chunks 전량**이라 거의 항상 오프로드 → `jobs.payload` 컬럼 NULL | `app.py:364`, `blobs.py:29` |
| 7 | insert 잡 `workspace_key`=**kb id**, community 잡 `workspace_key`=**eq UUID** | `app.py:367`·`:408` / `:548` |
| 8 | 수동 `/communities/build` 는 `idem_key=f"community:{eq_ws}"` | `app.py:549` |
| 9 | community 는 자기 버킷, 상한 `KBP_JOB_LIMIT_COMMUNITY` 기본 **1**(전역) | `admission.py:31`·`:145` |
| 10 | 제출은 `ON CONFLICT (idem_key) DO NOTHING` → 살아있는 같은 키면 **기존 job_id 반환** | `repo.py:171` |
| 11 | **claim 시점에** community 의 `idem_key` 를 NULL 로 비운다 | `repo.py:491` |
| 12 | `JobRetryable` requeue 잡은 `status='queued'` 인데 **`idem_key` 는 이미 NULL** | `worker.py:202` |
| 13 | **claim 경로에 시간 조건이 없다** — 낮에도 queued community 를 집는다 | `repo.py:470-500` |
| 14 | 단건 `cancel` 은 `idem_key = NULL` 명시 | `repo.py:710` |
| 15 | `submit_job` 은 job_id 하나만 반환(신규/충돌 구분 불가) | `api.py:137`·`:181` |
| 16 | `run()` 의 일반 예외는 `except Exception as exc: raise classify(exc)` 로 **잡 실패로 정규화** | `runner.py:154-155` |
| 17 | `eq_ws` 는 `_run_community` **지역변수** — `run()` except 스코프에 없다 | `runner.py:279` |
| 18 | GC 가 TTL 경과 terminal 잡 행을 **DELETE** | `repo.py:833`, TTL `gc.py:37` |
| 19 | **`gc.ttl_seconds()` 는 파싱 실패 시 `None` 반환** | `gc.py:37-60` |
| 20 | kb 트리거는 `extract_graph is False` 면 **enqueue 안 함**; 호출부 `:363`, 정의 `:388`, `queue is None` 즉시 return `:408` | `backend/app/workers/tasks.py:404` |
| 21 | facade·facade-worker 가 **같은 `x-facade-env` 앵커** 공유 | compose `:10`, airgap `:27`·`:316`·`:343` |
| 22 | `verify-bundle.sh` `REQUIRED_ENV` 는 "비면 **실패**" 목록 | `scripts/airgap/verify-bundle.sh` |
| 23 | parse-only 번들에도 `verify-bundle.sh` 동봉, `parse-only-up.sh` 가 facade-worker 를 **같은 앵커로** 띄운다 | `build-bundle.sh:137`, `parse-only-up.sh` |
| 24 | **추적중** env 템플릿 3종(`.env.example`·`.env.airgap.example`·`scripts/parse-svc.env.example`). `.env.parse-only.example` 은 실재하나 **미커밋** → 착수 시 함께 커밋. **`scripts/facade.env.example` 은 없다** | `git ls-files`/`status` |
| 25 | `_run_ingest` 는 `_run_insert` 를 **호출하지 않고** 인라인 재구현 | `runner.py:209` |
| 26 | `/ingest` 는 `skip_graph` 미전달 → **항상 그래프 추출**(payload 에 `extract_graph` 키 없음) | `runner.py:248` |
| 27 | `FakeRepo`(`set_stage`·`get` 2개뿐)를 `_runner()` 헬퍼가 **모든 러너 테스트의 기본 repo** 로 쓴다 | `test_job_runner.py:34`·`:105` |
| 28 | **드라이버는 psycopg3**; interval 파라미터화는 `make_interval(…=> %s)` 관용구뿐이고 **문자열 리터럴 안 `%s` 사례는 0건** | `repo.py:42-43`·`:308`·`:368`·`:759` |
| 29 | **실측**: `kbp-facade:airgap` 에 tzdata **있음**(`ZoneInfo('Asia/Seoul')` OK) | 2026-08-09 `docker run` |
| 30 | kb `test_community_job.py` 의 테스트는 **9건**(`def` 6 + **`async def` 3**) | `grep -cE '^(async )?def test'` |
| 31 | `_run_ingest` 에 `if parsed.get("status")=="failed": return parsed` **조기 반환**이 있다(그래프 미접촉) | `runner.py:222` |
| 32 | **`parse-only-up.sh` 는 `.env` 부재 시 `.env.airgap.example`(전체 스택 템플릿)을 복사한다** | `parse-only-up.sh:93` |

---

## 2. 설계

> **각 절이 자기 변경파일·테스트를 갖는다.** 별도 "변경 목록"·"테스트" 절을 두지 않는다
> (v1~v3 결함의 70%가 그 중복에서 나왔다). §3 은 **위험·완료판정만** 다룬다.

### 2.1 `kbp.graph_touch` — 후보의 근거

```sql
CREATE TABLE IF NOT EXISTS kbp.graph_touch (
  workspace_key text PRIMARY KEY,     -- kb id (insert 잡의 축, 사실 7)
  touched_at    timestamptz NOT NULL  -- 서버 now() 로 기록(§2.3 시계 통일)
);
```

**왜 잡 테이블을 안 쓰나** — (a) `kind IN ('insert','ingest')` 만 보면 **그래프를 끈
vector-only KB 가 현행 0회 → 매일 1회**가 된다(사실 20). `payload->>'extract_graph'` 로도
못 거른다(사실 6: 오프로드로 컬럼 NULL). **러너 시점에만 볼 수 있다**(사실 5).
(b) GC 가 72h 경과 잡을 지우므로(사실 18) 야간이 3일 넘게 멈추면 **증거째 사라진다**.

```python
# runner._run_insert 성공 직후
if ctx.payload.get("extract_graph", True):
    self._touch_graph_safe(workspace_id)

# runner._run_ingest 의 **적재까지 도달한** 성공 경로 직후 — 그 경우엔 무조건.
#   ⚠️ _run_ingest 에는 `if parsed.get("status") == "failed": return parsed`(사실 31)
#     조기 반환이 있다. 그 경로는 HTTP 200 이지만 **그래프를 전혀 안 건드린다** →
#     touch 하면 안 된다. 따라서 위치는 함수 끝이 아니라 **insert_chunks 성공 직후**다.
#   _run_ingest 는 _run_insert 를 부르지 않고(사실 25) skip_graph 도 안 넘겨
#   **항상 그래프를 추출한다**(사실 26). payload 에 extract_graph 키가 없으므로 게이트 금지.
self._touch_graph_safe(workspace_id)

def _touch_graph_safe(self, workspace_key):
    """예외를 삼킨다(로그만) — **단 1회 재시도한다**.

    이 호출은 edgequake 적재가 **이미 끝난 뒤**라, 예외를 올리면 run() 의 공통 except 가
    잡아(사실 16) 잡을 failed 로 종결시키고 insert 는 재시도도 없다 → **성공한 적재가
    실패로 뒤집힌다**(현행 대비 회귀).

    ⚠️ 다만 "실패해도 그 밤 한 번 거를 뿐" 이 **아니다** — graph_touch 는 이 설계의
    **유일한 후보 증거원**이고 GC 대상도 아니라, UPSERT 가 실패하면 그 workspace 는
    **다음 적재가 올 때까지 무기한** 후보가 아니다. 마지막 업로드 직후면 영구 미빌드다.
    그래서 (a) 짧은 재시도 1회, (b) 실패 시 `log.error`(warning 아님).
    """
    for attempt in (1, 2):
        try:
            self.repo.touch_graph(workspace_key); return
        except Exception:  # noqa: BLE001 — 적재 성공을 뒤집지 않는다
            if attempt == 2:
                log.error("graph_touch 기록 실패(재시도 후) — 이 workspace 는 다음 적재까지 "
                          "야간 후보가 되지 않는다 ws=%s", workspace_key, exc_info=True)
            else:
                time.sleep(0.5)
```

**변경**: `service/jobs/schema.py`(테이블), `service/jobs/repo.py`(`touch_graph`),
`service/jobs/runner.py`(`_touch_graph_safe` + `_run_insert`·**`_run_ingest`** 양쪽 호출),
`service/jobs/memory.py`(대응).

**테스트** — `service/tests/test_job_runner.py`
- `_run_insert` 성공 + `extract_graph=True` → `touch_graph` 호출
- `_run_insert` + **`extract_graph=False` → 미호출** ← §0 목표 6 회귀
- **`_run_ingest` 성공 → 무조건 호출**(payload 에 키가 없어도) ← 사실 25·26 회귀
- **`touch_graph` 가 2회 다 실패해도 잡은 여전히 성공**, `log.error` 1건
- ⚠️ **`FakeRepo`(사실 27)에 **`touch_graph`·`record_community_success`·`db_now`**(고정 datetime 반환) stub 을
  추가해야 한다(`record_attempt` 는 스케줄러 전용이라 러너 페이크엔 불필요).
  ⚠️ `service/tests/conftest.py` 의 `InMemoryJobRepo` 도 같은 이유로 대응이 필요하다.** 안 하면 `AttributeError` → `classify`(사실 16)로
  **FakeRepo 를 쓰는 러너 테스트 전량이 빨강**이 된다(v2·v3 이 "무변경 통과"라 잘못 적었다)

### 2.2 `kbp.community_builds` — 두 축 분리

```sql
CREATE TABLE IF NOT EXISTS kbp.community_builds (
  workspace_key   text PRIMARY KEY,   -- kb id
  eq_workspace_id text,
  last_attempt_at timestamptz,        -- 제출 시점 기록. **정렬 축**
  last_success_at timestamptz,        -- 성공 시만. **후보 술어**. 값은 빌드 **시작 스냅샷**
  finished_at     timestamptz,
  status          text                -- succeeded | failed
);
```

한 컬럼으로 겸하면 **어느 쪽으로도 틀린다**: 실패 시 갱신하면 후보에서 **영구 탈락**,
미갱신하면 회수된(러너를 안 탄) workspace 가 **매 밤 영구 1순위**.

**★ `last_success_at` 은 완료 시각이 아니라 "빌드가 그래프를 읽기 시작한 시각"이다.**
빌드는 `builder()` 진입 시점 그래프만 보고 수십 분~7200s 걸린다. 완료 시각을 쓰면
**빌드 도중 성공한 적재**가 `touched_at > last_success_at` 을 못 만족해 영구 탈락한다 —
현행은 적재마다 새 잡을 만들어 뒤이은 빌드가 커버하므로 **나빠진다**(§0 목표 6).

```python
# runner._run_community
snapshot_at = self.repo.db_now()          # ★ §2.3 시계 통일 — 파이썬 시각 금지
self._stage(ctx, "building")              # 기존 호출. lease 상실 시 JobAborted (유일한 게이트)
result = builder(eq_ws, llm=get_text_llm(), dsn=os.environ["KBP_PG_DSN"])
self._record_success_safe(workspace_id, eq_ws, snapshot_at)
```

**실패 기록**: `_run_community` 안의 `try/except` 에서 `record_community_failure` 를 부른다
(**`JobAborted` 는 제외** — lease 상실은 다른 세대가 소유한 것이라 이쪽 실패가 아니다).
`run()` 의 공통 except 에는 넣지 않는다 — `eq_ws`·`snapshot_at` 이 그 스코프에 없고(사실 17)
`except (JobFailed, JobRetryable, JobAborted): raise` 가 먼저 잡는다(사실 16).
이 writer 가 없으면 DDL 의 `status`·`finished_at` 컬럼이 **영원히 안 채워진다**.

`_record_success_safe` 는 `_touch_graph_safe` 와 **같은 이유로 best-effort** 다 — 수십 분짜리
빌드가 **끝난 뒤**의 기록 실패로 그 빌드를 failed 로 뒤집으면 훨씬 비싸다. 실패 시
`log.error`(다음 밤에 재빌드된다).

**변경**: `schema.py`(테이블), `repo.py` —
`record_attempt(workspace_key, eq_workspace_id|None)`,
**`record_community_failure(workspace_key, eq_workspace_id)`**(`status='failed'`·`finished_at=now()`;
`last_success_at` 은 **건드리지 않는다** → 후보에 그대로 남는다),
**`record_community_success(workspace_key, eq_workspace_id, snapshot_at)`** ← 3인자,
`db_now() -> datetime`; `memory.py`(대응); `runner.py`(위 호출 + `_record_success_safe`).

**테스트** — `test_community_nightly.py`(`requires_pg`)
- `record_attempt`/`record_community_success` 가 **서로 다른 컬럼**을 갱신
- **`last_success_at == 넘긴 snapshot_at`**(호출 시각 `now()` 가 아님) ← v3 1순위 수정 회귀
- UPSERT 2회 동작

`test_job_runner.py`: `_run_community` 성공 → `record_community_success` 가
**`snapshot_at` 포함 3인자로** 호출 / 예외 → 미호출 / **기록 실패해도 잡은 성공**

### 2.3 후보 선정 — **시계를 통일한다**

```sql
WITH cand AS (
  SELECT gt.workspace_key, gt.touched_at, cb.last_attempt_at
    FROM kbp.graph_touch gt
    LEFT JOIN kbp.community_builds cb ON cb.workspace_key = gt.workspace_key
   WHERE gt.touched_at > COALESCE(cb.last_success_at, to_timestamp(0))
)
SELECT workspace_key, count(*) OVER () AS total
  FROM cand
 ORDER BY COALESCE(last_attempt_at, to_timestamp(0)) ASC, touched_at ASC
 LIMIT %s
```

**★ 두 축 모두 DB 시계로 쓴다.** `touched_at` 은 서버 `now()`(§2.1)인데 `last_success_at` 을
파이썬 시각으로 쓰면, PG 컨테이너 시계가 워커보다 δ 뒤질 때 스냅샷 이후 δ 이내에 완료된
적재가 **영구 탈락**한다. 그래서 §2.2 가 `repo.db_now()`(=`SELECT now()`)를 쓴다.

**반환은 `(workspace_keys, total)`** — `LIMIT` 만으론 "후보 > 캡"을 알 수 없어
`backlog` 기록도 warning 도 불가능하다.

**in-flight 제외는 SQL 이 아니라 파이썬**(§2.4). SQL 로 하면 `cb` 가 LEFT JOIN 이라
**이력 없는 workspace 에서 NULL 비교 → 검사가 통째로 no-op** 이다(신규·롤아웃 첫 밤이
정확히 그 경우). 축이 갈려 있어(사실 7) kb id 로 대체 매칭도 안 된다.

**변경**: `repo.py` — `workspaces_needing_community(cap) -> (list[str], int)`; `memory.py`.

**테스트**(`requires_pg`): `touched_at > last_success_at` 인 것만 / **이력 없는 workspace 도
포함** / **실패해도 후보에 남는다**(`last_success_at` 미갱신) / 정렬이 `last_attempt_at`
오래된 순 / `(len==cap, total==전체)`.

### 2.4 제출

```python
for kb_id in candidates:
    if time.monotonic() > loop_deadline:      # ★ 루프 전체 시간 예산
        log.warning("제출 루프 예산 초과 — 나머지 %d건은 다음 밤", len(candidates) - i)
        break
    try:
        eq_ws = self.runner.eq_client.ensure_workspace(kb_id, name=kb_id)
    except Exception:
        log.exception("ensure_workspace 실패 kb=%s", kb_id)
        repo.record_attempt(kb_id, None)   # ★ 실패해도 시도 기록 — 안 하면 깨진 workspace 가
        failed += 1; continue              #   last_attempt_at=NULL 로 **영구 1순위** 고정
    if repo.has_live_community_job(eq_ws):
        repo.record_attempt(kb_id, eq_ws)  # ★ dedupe 경로도 기록 — cap 은 후보 LIMIT 이라
        deduped += 1; continue             #   여기서 안 걸면 상단을 영구 점유한다
    job_id, created = submit_job_ex(
        repo, blobs, kind="community",
        payload={"workspace_id": kb_id},          # skip_if_unchanged 없음(A2 소관)
        workspace_key=eq_ws,
        # ★ 야간 키를 **수동 키(사실 8)와 분리**. 같은 키면 야간 잡이 queued 인 동안
        #   수동 호출이 ON CONFLICT(사실 10)로 그 job_id 를 돌려받아, 운영자가 202 를
        #   받고도 아무 일이 안 일어난다. run_date 로 **밤마다 새 키**.
        idem_key=f"community-nightly:{eq_ws}:{run_date}",
        batch_key=f"community-nightly:{run_date}")
    repo.record_attempt(kb_id, eq_ws)
    if created: submitted += 1
    else:       deduped += 1               # ★ created=False 도 반드시 계수(누락 금지)
```

**★ 루프에 시간 예산을 둔다.** `ensure_workspace` 는 동기 HTTP 다 — edgequake 가 hang 이면
`MAX_PER_NIGHT`(8) × 타임아웃(수백초) 동안 **틱 하나가 막혀** ①(지난 밤 취소)·②(마감)도
못 돈다. `loop_deadline = time.monotonic() + KBP_COMMUNITY_SUBMIT_BUDGET_SECONDS`(기본
**600**)로 끊고 나머지는 다음 밤에 맡긴다(`last_attempt_at` 미갱신이라 그대로 후보 선두).

`submit_job_ex` = `submit_job`(사실 15) 동일 본문 + `(job_id, created)` 반환.
**`live_worker_count() <= 0 → HTTPException(503)` 가드는 복제하지 않는다** — facade-worker
프로세스 **안의 스레드**라 웹 예외를 던지면 안 된다.

**변경**: `service/jobs/api.py`(**`submit_job` 무변경**, `submit_job_ex` 신규),
`repo.py` — `has_live_community_job(eq_workspace_id) -> bool`; `memory.py`.
**`service/app.py` 무변경.**

**테스트** — `test_community_schedule.py`(**PG 불요**)
- 제출 계약: `payload=={"workspace_id": kb_id}`, `workspace_key==eq_ws`,
  **`idem_key==f"community-nightly:{eq_ws}:{run_date}"`**, `batch_key` 일치, kb id/eq UUID 뒤바뀜 없음
- `has_live_community_job` True → 건너뛰고 **`deduped` 증가 + `record_attempt` 호출**
- `ensure_workspace` 예외 → 그 항목만 건너뛰고 **`record_attempt(kb_id, None)` 호출**, 루프 계속
- **`created=False` → `deduped` 증가**(어떤 카운터에도 안 잡히면 실패)
- 캡 초과 시 `backlog = total - cap` 기록 + warning

`test_community_nightly.py`(`requires_pg`): `has_live_community_job` 이 queued·running 을
잡고 terminal 은 안 잡는다

### 2.5 실행 판정

```sql
CREATE TABLE IF NOT EXISTS kbp.batch_runs (
  name text NOT NULL, run_date date NOT NULL,
  run_at timestamptz NOT NULL DEFAULT now(),
  submitted int NOT NULL DEFAULT 0, deduped int NOT NULL DEFAULT 0,
  failed int NOT NULL DEFAULT 0, backlog int NOT NULL DEFAULT 0,
  status text NOT NULL DEFAULT 'started',   -- started | ok | failed
  error  text,
  PRIMARY KEY (name, run_date)
);
```

```python
tz  = _zone()
now = datetime.now(tz)             # ★ UTC 아님 — 사실 3 관례를 따르면 창이 어긋난다
run_date = _current_run_date(now, build_at)   # BUILD_AT 이전이면 전날 밤
start    = datetime.combine(run_date, build_at, tz)   # ★ run_date 고정(자정 랩)
end, deadline = start + timedelta(minutes=window), start + timedelta(minutes=deadline_min)

repo.cancel_nightly_queued(exclude_key=f"community-nightly:{run_date}")   # ① 지난 밤 잔여
if now >= deadline:                                                       # ② 마감
    repo.cancel_nightly_queued(key=f"community-nightly:{run_date}")
if not (start <= now < end): return                                       # ③ 창
if not repo.claim_run("community-nightly", run_date, stale_minutes): return
```

**① 은 창 판정과 무관하게 매 틱 먼저.** 워커가 04:00 에 죽었다 낮 12:00 에 살아나면
`run_date` 는 '오늘'이라 어제 밤 queued 가 취소 대상이 아니고, **claim 에 시간 조건이
없어**(사실 13) 뜨자마자 **업무시간에** 캡만큼 실행한다.
**② 는 창 앞에** 둔다 — `DEADLINE`(420분)은 정의상 `WINDOW`(120분) 밖이라 뒤에 두면
영원히 도달 못 한다.

```sql
-- cancel_nightly_queued. ★ batch_key 로만 매칭한다. idem_key 는 claim 시(사실 11)·
-- requeue 시(사실 12) NULL 이 되어 샌다. ★ 키 문자열은 **파이썬이 완성해서** 넘긴다
-- (SQL 안 `'…:' || %(d)s` 는 date→text 암묵 캐스트가 DateStyle 에 의존해 매칭 0건이 된다).
-- ★ 파라미터 쿼리의 리터럴 % 는 **`%%` 로 이스케이프**한다(레포에 LIKE 선례 0건).
UPDATE kbp.jobs SET status='canceled', completed_at=now(), idem_key=NULL
 WHERE kind='community' AND status='queued'
   AND batch_key LIKE 'community-nightly:%%'
   AND (%(key)s::text  IS NULL OR batch_key =  %(key)s::text)
   AND (%(excl)s::text IS NULL OR batch_key <> %(excl)s::text)
```

```sql
-- claim_run. ★ interval 파라미터화는 make_interval 만 쓴다 — psycopg3 는 문자열 리터럴
-- 안의 %s 도 치환해(사실 28) `interval '$1 minutes'` 가 되어 매 호출 예외가 된다.
INSERT INTO kbp.batch_runs (name, run_date) VALUES (%s, %s)
ON CONFLICT (name, run_date) DO UPDATE
   SET run_at = now(), status = 'started', error = NULL
 WHERE kbp.batch_runs.status = 'failed'
    OR (kbp.batch_runs.status = 'started'
        AND kbp.batch_runs.run_at < now() - make_interval(mins => %s))
RETURNING 1
```
`failed` 만 허용하면 **`finish_run` 이 안 불리는 종료**(SIGKILL·OOM·재기동)에서 행이
`started` 로 굳어 **그 밤 남은 틱이 전부 claim 실패**한다. `KBP_COMMUNITY_STALE_RUN_MINUTES`(기본 **30**)로 회수한다.

**★ 재claim 은 창(③)이 아니라 마감까지 허용한다.** `STALE`(30) 과 `WINDOW`(120) 가 무관하면
**창 종료 30분 전 이후에 워커가 죽으면 그 밤이 통째로 사라진다** — 예: 창 03:00~05:00,
04:45 SIGKILL → 재claim 가능 05:10 인데 그때는 ③ `start <= now < end` 가 거짓이라 먼저
return 한다. 다음 창은 내일 03:00 이고 그 행은 `run_date=어제` 라 무관 → **이틀 지연**
(수용한 것은 "최대 하루"다). 따라서 ③ 을 `start <= now < end` 대신
**`start <= now < max(end, deadline_if_stale_recoverable)`** 로 두지 말고, 더 단순하게
**"창 안 이거나, 굳은 `started` 를 회수할 수 있고 아직 마감 전"** 이면 진행한다:

```python
in_window = start <= now < end
stale_recoverable = repo.has_stale_started("community-nightly", run_date, stale_minutes)
if not (in_window or (stale_recoverable and now < deadline)): return
```

**루프 전체를 `try/except` 로 감싸고 `finally` 에서 `finish_run`.** 그리고 **스케줄러 루프
최상위에도 가드**를 둔다 — `finish_run` 자체가 PG 장애로 던지면 **스레드가 죽고**, 워커
본체는 살아있어 재기동이 없으므로 "기동 시 직전 밤 로그"가 **영원히 다시 안 찍힌다**.

**탐지** — 기동 시 **`last_batch_run(name, expected_run_date)`** 로 조회한다.
`last_batch_run(name)`(가장 최근 행)이면 3일 전 `ok` 행만 있어도 `info ok` 를 찍어
**멈춤을 못 잡는다**. 기대 날짜는 `_current_run_date` 기준 직전 밤.

| 기대 밤의 행 | 로그 |
|---|---|
| 없음 | `warning` — 워커가 내려가 있었다 |
| `ok` | `info` (submitted/deduped/failed/backlog) |
| `failed` | `error` + `error` 컬럼 |
| `started` | `error` — 마커만 서고 끝나지 않은 밤 |

**변경**: **`service/community_schedule.py`(신규 모듈)** — 이 절의 실행 판정·제출 루프
(§2.4)·`_zone()`/env 파싱(§2.6)·기동 로그가 전부 여기 들어간다. `worker.py` 는 이 모듈의
`run_forever(repo, blobs, runner)` 를 데몬 스레드로 띄우기만 한다(§2.6 의 `try/except` 가드).
**PG 불요 테스트가 이 모듈을 직접 import 해 돌린다.**
`schema.py`(테이블), `repo.py` — `claim_run(name, run_date, stale_minutes)`,
`cancel_nightly_queued(*, key=None, exclude_key=None) -> int`, `finish_run(...)`,
`last_batch_run(name, run_date)`; `memory.py`; `service/worker.py`(`kbp-community` 데몬
스레드, 기동은 `_gc_thread`(사실 4) 옆 — **`try/except` 로 감싼다**: 스케줄러가 못 떠도
워커 본체는 떠야 한다).

**테스트** — `test_community_schedule.py`(PG 불요): 창 밖 → 제출 0 / **자정 랩**(23:30+120 →
23:40·00:30 이 같은 `run_date`) / **`now` 를 UTC 로 넘기면 창이 어긋난다** / ① 이 창과
무관하게 매 틱 / ② 가 창 밖에서도 / 예외 시 `finish_run(failed, error)` + **스레드 생존** /
**`finish_run` 이 던져도 스레드 생존** / 기동 로그 4분기(없음·ok·failed·**started**) /
**기대 run_date 로 조회한다**(3일 전 `ok` 행이 있어도 warning).

`test_community_nightly.py`(`requires_pg`): `claim_run` 동시 1승 / `failed` 재claim /
**`started` + `run_at` 초과 → 재claim, 미초과 → 실패** / 대상 0건이어도 행이 남는다 /
`cancel_nightly_queued` 가 **`batch_key` 로만** 매칭·`idem_key=NULL` 동반·running 보존·
`exclude_key` 동작·**`idem_key` 가 이미 NULL 인 requeue 잡도 잡는다**(사실 12) /
**두 SQL 이 psycopg3 에서 실제로 실행된다**(사실 28 회귀).

### 2.6 타임존·설정

```python
def _zone():
    name = os.environ.get("TZ") or "Asia/Seoul"
    try: return ZoneInfo(name)
    except Exception: log.warning("TZ=%r 사용 불가 — Asia/Seoul 폴백", name)
    try: return ZoneInfo("Asia/Seoul")
    except Exception:                     # ★ tzdata 없는 이미지 — 폴백도 같은 예외를 던진다
        log.warning("tzdata 없음 — 고정 UTC+9 폴백"); return timezone(timedelta(hours=9))
```

| env | 기본 | 의미 |
|---|---|---|
| `TZ` | `Asia/Seoul` | 미설정이면 UTC(사실 3) → 03:00 이 KST 12:00 |
| `KBP_COMMUNITY_BUILD_ENABLED` | `true` | `false` 면 **스레드 미기동** |
| `KBP_COMMUNITY_BUILD_AT` | `03:00` | 파싱 실패 → 기본값 + warning |
| `KBP_COMMUNITY_WINDOW_MINUTES` | `120` | 실행 창 |
| `KBP_COMMUNITY_DEADLINE_MINUTES` | `420` | 마감(=10:00) |
| `KBP_COMMUNITY_MAX_PER_NIGHT` | `8` | 한 밤 제출 상한 |
| `KBP_COMMUNITY_POLL_SECONDS` | `60` | 스케줄 틱(워커 폴 2초를 쓰면 초당 수회 쿼리) |
| **`KBP_COMMUNITY_STALE_RUN_MINUTES`** | **`30`** | 굳은 `started` 회수 지연(§2.5) |

- **실측(사실 29)**: 이미지에 tzdata 가 **있다**. 그래도 2단 폴백을 둔다 — 베이스가 바뀌면
  조용히 깨지고, 그 예외가 스레드 기동 지점에서 터지면 워커가 안 떠서 live worker 0 →
  facade 의 모든 제출이 503 이 된다.
- 기동 시 **다음 실행 시각을 절대시각으로** 로그. `ENABLED=false` 도 로그.
- **TTL 경고**: `ttl = gc.ttl_seconds()`; **`ttl is None` 이면 건너뛴다**(사실 19 —
  `None < 172800` 은 TypeError). `ttl is not None and ttl < 48*3600` 일 때만 warning.

**변경(배포)** — 프로젝트 CLAUDE.md "폐쇄망 배포는 소스 수정과 함께":
- compose ×2 의 **`x-facade-env` 앵커**(사실 21) — `TZ` + `KBP_COMMUNITY_*` **7개**
  (`STALE_RUN_MINUTES` 포함) 전부 `${VAR:-기본}`
- env 템플릿(사실 24 — **실재 파일만**): `.env.example`, `.env.airgap.example`,
  `.env.parse-only.example`(**미커밋 → 함께 커밋**).
  `scripts/parse-svc.env.example` 은 parse-svc 프로세스용이라 **스케줄러 키 불필요**(누락 아님)
- **`.env.parse-only.example`** — `TZ` + `KBP_COMMUNITY_BUILD_ENABLED=false`
- **`scripts/airgap/parse-only-up.sh` 가 `KBP_COMMUNITY_BUILD_ENABLED=false` 를 강제한다.**
  ⚠️ 템플릿 기본값에 의존하면 **반드시 뚫린다** — 이 스크립트는 `.env` 가 없으면
  **`.env.airgap.example`(전체 스택 템플릿)을 복사한다**(`parse-only-up.sh:93`, 사실 32).
  즉 `.env.parse-only.example` 에 걸어둔 기본값은 이 경로에서 아예 안 읽힌다.
  compose 앵커 기본이 `true` 라 **edgequake 없는 파서 전용 스택에서 야간 스레드가 떠**
  매 밤 도달 불가능한 edgequake 를 호출한다.
  **강제 수단(택1, 구현 시 확정)**: (a) `up` 직전에
  `export KBP_COMMUNITY_BUILD_ENABLED=false` — compose 는 셸 환경을 `--env-file` 보다
  우선하므로 어떤 `.env` 로 와도 이긴다. (b) 93행의 복사 원본을 `.env.parse-only.example`
  로 바꾼다(이쪽이 더 근본적이나 기존 운영 절차를 바꾼다).
  → **(a) 를 기본으로 하고 (b) 는 별건**으로 둔다
- **`verify-bundle.sh` `REQUIRED_ENV` 에 `TZ` 를 넣지 않는다**(사실 22·23: 넣으면 파서 전용
  배포가 검증 실패). compose 에 기본값이 있으므로 대상 아님
- `docs/airgap-deploy.md`·`docs/parse-only-guide.md` — 시각 변경·끄는 법·즉시 빌드법

**테스트**: `test_community_schedule.py` — TZ 이상값 → 스레드 생존 + 폴백 + warning /
`BUILD_AT` 파싱 실패 → 기본값 + warning / `ENABLED=false` → **스레드 미기동** /
**`ttl_seconds()` 가 `None` 이어도 TypeError 없이 건너뛴다**.

### 2.7 적재 직후 트리거 제거 (kb)

`backend/app/workers/tasks.py:363` 의 `_maybe_enqueue_community_build(...)` **호출 제거**.
정의(`:388`)·`build_communities_task`·arq 등록은 **남긴다**(수동 운영·되돌림 대비).

**테스트** — `backend/tests/`
- **스파이 큐를 주입한다.** 현 하네스는 `ctx["queue"]=None` 이고 `queue is None` 이면 즉시
  return 하므로(사실 20 `:408`) **호출부를 안 지워도 통과하는 빈 초록불**이다.
  스파이를 넣고 **삭제 전 빨강(enqueue 1회)** 을 먼저 확인한 뒤 삭제해 초록으로 바꾼다
- `test_community_job.py` **9건**(사실 30)은 함수 직접 호출이라 무변경 통과(함수는 남긴다)

**문서**: `_workspace/01-architecture.md`(§1 의 "배치(스케줄/…)" 서술이 사실이 된다),
`02-changes.md`(진입점 일원화·하루 지연 계약·A 분할 근거), `03-dev-progress.md`

---

## 3. 위험 / 완료 판정

| 위험 | 완화 |
|---|---|
| 커뮤니티 최대 하루 지연 | 사용자가 택했다. 수동 트리거로 즉시 가능 |
| vector-only 재빌드 / 낡은 리포트 | **A2·A3·A4 비범위.** `graph_touch` 로 그래프를 끈 KB 는 후보 자체가 아니다 |
| 야간이 업무시간 침범 | 캡 8 + **queued 취소**(마감·지난 밤). ⚠️ **running 빌드는 안 자른다** — 마감 직전 claim 된 빌드가 `max_runtime` 7200s(`repo.py:102`)까지 갈 수 있어 **최악 12:00 까지 점유**한다. 진행 중 빌드를 끊으면 LLM 호출을 못 되돌려 작업만 버리므로 **의도적으로 수용**한다 |
| 워커가 밤새 죽어 있음 | `graph_touch` 는 GC 대상이 아니라 **증거가 남는다** |
| 회수 경로가 러너를 안 탐 | `last_attempt_at` 은 **제출 시** 기록 → 정렬이 안 무너진다 |
| **`graph_touch` UPSERT 실패** | 재시도 1회 + `log.error`. 실패 시 **다음 적재까지 무기한 미빌드**(§2.1 주석) |
| 야간 배치가 조용히 멈춤 | `batch_runs.status/error` + **기대 run_date 기준** 기동 로그 4분기 |
| 파서 전용 배포에 야간 스레드 | 템플릿 + **`parse-only-up.sh` 가 강제** |
| tzdata 부재 | 2단 폴백(실측: 현재 이미지엔 있음) |

**완료 판정 — 증거를 남긴다**
- [ ] 착수 시 `service/tests/`·kb `backend/tests/` **기준선을 먼저 측정해 기록**, 회귀 0
- [ ] 신규 테스트가 각각 **구현을 되돌리면 실패**함을 확인(특히 kb 스파이 큐 빨강→초록)
- [ ] **`claim_run`·`cancel_nightly_queued` 두 SQL 을 실 PG 로 실행**(사실 28 — psycopg3
      리터럴 플레이스홀더·`%%` 이스케이프가 실제로 동작하는지)
- [ ] 실측: dev 에서 `BUILD_AT` 를 현재+1분 → 잡 생성 → `batch_runs(status='ok')` →
      같은 run_date 재틱 skip → 재기동 시 "직전 밤 결과" 로그
- [ ] 실측: `ENABLED=false` 로 스레드 미기동 로그
- [ ] `docker compose config` 로 신규 env **8개**(TZ + `KBP_COMMUNITY_*` 7개) 해석 확인,
      템플릿 3종 누락 0 + **`.env.parse-only.example` 커밋 여부**(사실 24)
- [ ] `verify-bundle.sh` 의 `REQUIRED_ENV` 블록에 **`TZ` 부재** 확인

---

## 4. 구현 후 검증 — **전부 닫힘 (2026-08-09)**

> 글로벌 룰 "검증 비용 관리" 5번대로, 계획서에서 더 다투지 않고 **구현 중 실측으로** 닫았다.

| # | 항목 | 결과 |
|---|---|---|
| 1 | `FakeRepo`·`InMemoryJobRepo` stub 누락 | ✅ 예측대로 `AttributeError: 'FakeRepo' object has no attribute 'db_now'` 로 **즉시 빨강** → stub 추가 후 초록 |
| 2 | 기준선 측정 | ✅ kbp `service/tests/` **269 passed**, kb `backend/tests/` **644 passed / 19 failed** |
| 3 | 성공 제출 경로의 `record_attempt` 회귀 | ✅ 제거 시 빨강 확인 |
| 4 | `last_success_at` 이 스냅샷임을 고정 | ✅ **처음엔 무의미했다** — `FakeRepo.db_now()` 가 고정값이라 완료 시각으로 바꿔도 통과했다. 호출마다 1분 전진시키고 단언을 "첫 `db_now()` 값" 으로 바꿔 **빨강 확인** |
| 5 | `claim_run`·`cancel_nightly_queued` 의 psycopg3 실행 | ✅ 일회용 PG(16-alpine) 실측 — `make_interval(mins => %s)` 동작, `LIKE '…:%%'` 이스케이프 동작, `idem_key` 동반 NULL·running 보존·`exclude_key` 확인. `test_community_nightly_pg.py` 17건으로 고정 |

### 추가로 드러난 것 (범위 밖이었으나 사용자 승인 후 처리)

**kb 테스트 하네스 노후화** — `FakeKbPipeline.chunk()` 가 실 클라이언트
(`KbPipelineClient.chunk`)에 있는 **`table_blocks` 인자를 안 받아** `TypeError` →
`kb_pipeline chunk 실패` 로 적재가 통째로 `failed` 였다. 그래서 **provider=kb_pipeline
적재가 tail 에 도달하는 하네스가 하나도 없었고**, A1 의 kb 회귀 테스트를 유효하게 만들
수 없었다(호출부를 되살려도 초록 = 무의미).

fake 시그니처를 실제와 맞춰 해결했고 **기존 실패 3건도 함께 복구**됐다.
**kb 기준선: 644 passed / 19 failed → 648 passed / 16 failed** (회귀 0).

---

## 5. 구현 결과 (2026-08-09)

**kbp**
- `service/jobs/schema.py` — `graph_touch`·`community_builds`·`batch_runs`
- `service/jobs/repo.py` — 신규 11개 메서드
- `service/jobs/memory.py` — `InMemoryJobRepo` 동일 계약
- `service/jobs/runner.py` — `_touch_graph_safe`(insert 는 `extract_graph` 게이트,
  ingest 는 **적재 도달 경로에서만**), `_run_community` 이력 기록(`JobAborted` 제외),
  전부 best-effort
- `service/jobs/api.py` — `submit_job_ex`(**`submit_job` 무변경**, 503 가드 미복제)
- **`service/community_schedule.py`(신규)** — 창·마감·지난밤 취소·claim·제출·기동 로그
- `service/worker.py` — `kbp-community` 데몬 스레드(**기동 실패가 워커를 막지 않는다**)
- compose ×2 앵커 + env 템플릿 3종 + `parse-only-up.sh` 강제 비활성

**kb**
- `backend/app/workers/tasks.py` — 적재 tail 트리거 **제거**(함수·arq 등록은 보존)
- `backend/tests/test_worker_kb_pipeline_stages.py` — fake 시그니처 동기화 + 회귀 테스트

**테스트**: kbp **303 passed**(PG 없음) / **385 passed**(PG 포함), kb **648 passed**.
회귀 시뮬레이션으로 **8종**이 실제로 빨강이 되는 것을 확인했다.
