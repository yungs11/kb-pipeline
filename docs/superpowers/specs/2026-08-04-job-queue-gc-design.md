<!-- plan-version: v2 -->
<!-- ultracode-validation: PENDING -->

# 잡 큐 TTL GC (D2) — 완료 잡 + MinIO 객체 정리

> 짝 문서: [`2026-08-03-facade-job-queue-design.md`](2026-08-03-facade-job-queue-design.md)
> (설계 §2·§5.3), [`...-deferred.md`](2026-08-03-facade-job-queue-deferred.md) D2.
>
> v1 → v2: 4렌즈 검증에서 범위 내 must-fix 11건. 그중 둘은 **GC 이전에 이미 있던 결함**
> 이라 이 작업에 포함한다 — 멱등 재삽입이 행 id 와 객체 키를 어긋나게 만드는 버그(§2.6)
> 와 `KBP_JOB_*` 을 배포에 전달할 통로가 없는 공백(§3).

## 0. 왜 지금인가

Phase 1 에서 GC 를 의도적으로 뺐다 — "만들지 않으면 참조 무결성·고아 객체 결함이
존재하지 않는다"는 판단이었고, 그때는 맞았다. **전제가 바뀌었다.**

실측(2026-08-04): e2e 를 몇 번 돌린 뒤 `kbp-jobs/` 아래에 `input.bin` 14개, 1.8MB 가
**영구 고아**로 남았다. 잡 행을 지워도 객체는 아무도 안 지운다.

원인은 구조적이다. §5.3 은 `legacy=true` 잡의 staging 만 terminal 즉시 삭제한다.
`legacy=false`(신규 `/jobs/*`) 잡은 재시도가 다시 읽어야 하고 소비자가 나중에
`/result` 를 부를 수 있어서 남긴다 — 그런데 **kb 가 플래그를 켜면 쓰는 게 정확히 그
경로다**. 업로드마다 원본이 MinIO 에 영구 적재된다(상한 50MB × 건수).

**따라서 D2 는 Phase 2 완료(kb 를 `/jobs/*` 로 상시 전환)의 선행 조건이다.**

## 1. 범위

**한다**

- terminal 잡 중 TTL 경과분 삭제 — 행 + 객체 3종(`input_ref`·`payload_ref`·`result_ref`)
- **참조 무결성**: 비종료 잡이 `parent_job_id` 로 가리키는 잡은 보존
- **고아 객체 스윕**: 어떤 행도 참조하지 않는 `{prefix}/{uuid}/` 객체 삭제
- worker 에서 낮은 빈도로 실행, advisory lock 단일화
- **선행 결함 둘**: 멱등 재삽입 키 불일치(§2.6), `KBP_JOB_*` 배포 통로(§3)

**안 한다** — §7 에 근거

## 2. 설계

### 2.1 어디서 도는가

**전용 데몬 스레드**(`kbp-gc`)에서 돈다. heartbeat 와 같은 이유다 — 틱 루프에 넣으면
GC 가 도는 동안 `claim()`·`_reap()` 이 정지해 큐가 멎고, `_inflight` 가 안 비워져
`free = capacity - len(_inflight)` 가 실제보다 작게 계산된다. 스윕은 전체 나열 +
최대 배치 삭제라 수 초~수 분이 걸릴 수 있다.

- 루프 **매 iteration 을 try/except** 로 감싼다. 어떤 예외로도 스레드가 죽지 않는다.
- 종료 신호는 heartbeat 와 같은 `_shutdown` 이벤트를 쓴다.
- API 프로세스는 GC 하지 않는다(§1 불변 규칙 + `-w 4` 중복 실행 회피).

**주기 기준시각**은 프로세스 메모리에 둔다.

- TTL 삭제: 기동 후 **첫 사이클에 1회 실행**(초기값 0), 이후 `GC_INTERVAL` 마다.
- 고아 스윕: 기동 직후엔 돌지 않고 **기동시각 + `SWEEP_INTERVAL`** 부터. 재기동이
  잦을 때 매번 전체 나열을 도는 것을 막는다.
- lock 을 못 잡아 건너뛴 경우에도 **타이머는 리셋**한다(매 사이클 try-lock 두드리기 방지).

worker 가 여럿이어도 하나만 돌게 **advisory lock `(LOCK_CLASSID, LOCK_OBJ_GC=3)`**.
`pg_try_advisory_xact_lock` 으로 **비차단** 획득 — 못 잡으면 건너뛴다. objid 가 claim(2)
과 달라 GC 가 claim 을 막지 않는다.

**GC 트랜잭션도 `SET LOCAL lock_timeout='5s'` + `statement_timeout='30s'`** 를 건다
(claim 과 동일 관행). 같은 DB 를 edgequake 와 공유하므로 상한 없이 붙잡으면 안 된다.

### 2.2 삭제 순서 — 행 먼저, 객체 나중

```
1) 행 DELETE ... RETURNING input_ref, payload_ref, result_ref
2) 반환된 키들을 MinIO 에서 삭제 (실패는 로그만)
```

행을 먼저 지운다. 반대면 객체가 사라진 행이 남아 `GET /jobs/{id}/result` 가 "결과 있음"
이라 응답해놓고 복원에 실패한다. 행을 먼저 지우면 그 잡은 404 가 되어 계약이 명확하다
(TTL 경과 = 결과 보증 없음). 2)가 실패해 객체가 남으면 §2.4 스윕이 걷어낸다.

### 2.3 TTL 삭제 SQL

**`ttl_hours <= 0` 이거나 파싱 실패면 TTL 삭제·고아 스윕을 둘 다 즉시 반환한다**
(어떤 삭제도 하지 않음). 비상 정지 레버가 전량 삭제 레버가 되면 안 된다 —
`completed_at < now() - interval '0'` 은 모든 terminal 잡을 즉시 지운다.

```sql
DELETE FROM kbp.jobs j
 WHERE j.id IN (
   SELECT c.id FROM kbp.jobs c
    WHERE c.status IN ('succeeded','failed','canceled')
      AND coalesce(c.completed_at, c.created_at) < now() - make_interval(hours => %s)
      AND NOT EXISTS (                      -- 보호 조건을 LIMIT **안**에 둔다
            SELECT 1 FROM kbp.jobs k
             WHERE k.parent_job_id = c.id
               AND k.status NOT IN ('succeeded','failed','canceled'))
    ORDER BY coalesce(c.completed_at, c.created_at)
    LIMIT %s)
RETURNING input_ref, payload_ref, result_ref;
```

**`NOT EXISTS` 가 `LIMIT` 서브쿼리 안에 있어야 한다.** 밖에 두면 보호 대상까지 포함해
상위 N건을 뽑고 바깥에서 걸러내는데, 보호 행은 정의상 가장 오래된 축이라 정렬 앞머리에
쌓인다. 배치 크기에 도달하면 매 사이클 0건 삭제로 **GC 가 영구 정체**한다. 영구 보호가
가설이 아닌 이유: `requeue` 는 `attempt_count` 를 유지한 채 `queued` 로 되돌리고
(`repo.py`), `_candidates` 는 `attempt_count < max` 로 그 행을 영구 배제하므로 소진된
자식이 비종료 상태로 남을 수 있다.

**`coalesce(completed_at, created_at)`** 을 쓰는 이유: `completed_at IS NULL` 인 terminal
행은 `<` 비교가 false 라 TTL 대상이 아니고, 행이 있으니 스윕 대상도 아니어서 **영구
잔류**한다. 현행 전이 경로는 모두 `completed_at` 을 채우지만 스키마에 `NOT NULL`·`CHECK`
가 없어 불변식이 코드 관행에만 의존한다. `created_at` 은 `NOT NULL DEFAULT now()` 다.
위반 행을 회수할 때 WARN 을 남긴다.

`parent_job_id` 는 **전용 컬럼**이라 SQL 로 보인다(Phase 1 에서 payload jsonb 대신
컬럼으로 뺀 이유가 이것이다).

### 2.4 고아 객체 스윕

**"증거의 부재"로 삭제를 결정하는 유일한 경로라 가장 위험하다.** 다음을 모두 지킨다.

**나열·파싱 계약** — `blobs.iter_job_objects()` 가 소유한다.

```python
def iter_job_objects() -> Iterator[tuple[uuid.UUID | None, str, datetime | None]]:
    """(job_id, key, last_modified). 프리픽스 해석을 여기 가둔다."""
```

- `list_objects(bucket, prefix=f"{prefix}/", recursive=True)` **고정**. `recursive=False`
  면 common-prefix 유사객체가 나오고 그 `last_modified` 는 `None` 이다.
- 키가 `^{prefix}/{uuid}/...` 로 파싱되지 않으면 `job_id=None` 을 낸다.
- `KBP_JOB_MINIO_PREFIX` 가 빈 문자열이면 **기동 시 거부**한다(버킷 전체가 스윕 대상이
  된다). 프리픽스에 슬래시가 들어도 `blobs` 안에서 일관되게 처리한다.

**삭제 조건 — 아래를 모두 만족할 때만 지운다.**

1. `job_id` 파싱 성공 (실패 = 우리 것이 아님 → 건드리지 않는다)
2. `last_modified` 가 **tz-aware** 이고 `now() - last_modified > $orphan_grace`
   (`None` 이거나 naive 면 **건너뛴다** — 삭제하지 않는다)
3. 그 `job_id` 가 `kbp.jobs` 에 **없고**
4. 그 **키**가 어떤 행의 `input_ref`/`payload_ref`/`result_ref` 와도 **일치하지 않는다**

4번이 필요한 이유는 §2.6 이다 — 행 id 와 객체 키가 어긋날 수 있는 경로가 있(었)다.
배치 조회로 확인한다: `WHERE input_ref = ANY(%s) OR payload_ref = ANY(%s) OR result_ref = ANY(%s)`.

**`$orphan_grace`(기본 6h)** 는 제출 창을 덮는다 — `submit_job` 은 객체를 먼저 올리고
행을 나중에 INSERT 하므로 그 사이에 스윕이 돌면 살아있는 잡의 입력을 지운다.

**fail-closed.** 존재 확인이 실패하면 **그 사이클 전체를 중단하고 아무것도 지우지
않는다**. `job_ids_present()` 반환형을 `set[UUID] | None` 으로 두어 "빈 결과"와 "조회
실패"를 코드상 구분한다. 이 코드베이스의 지배 스타일이 예외를 삼키는 것이라
(`blobs.delete` 는 WARN, `worker._safe` 는 조용히 return) 자연스러운 구현이 fail-open 이
되기 쉽다 — postgres 순간 장애만으로 살아있는 객체가 전량 삭제된다.

**sanity 가드.** 후보 대비 고아 비율이 `KBP_JOB_ORPHAN_MAX_RATIO`(기본 0.9)를 넘거나
`kbp.jobs` 행 수가 0 이면 WARN 만 남기고 **삭제를 보류**한다.

**나열은 끝까지, 삭제만 상한.** `KBP_JOB_GC_BATCH` 는 **삭제 수**만 제한한다. 나열까지
자르면 키 사전순 + UUIDv4 특성상 앞쪽이 살아있는 잡이면 고아가 영영 조회 범위 밖이라
스윕이 수렴하지 않는다 — 객체가 쌓이는 상황(§0 의 동기)에서 기능이 무력화된다.

### 2.5 설정

| env | 기본 | 뜻 |
|---|---|---|
| `KBP_JOB_TTL_HOURS` | 72 | terminal 잡 보존. **0 이하면 GC·스윕 전부 정지** |
| `KBP_JOB_GC_INTERVAL_SECONDS` | 3600 | TTL 삭제 주기 |
| `KBP_JOB_ORPHAN_SWEEP_INTERVAL_SECONDS` | 21600 | 고아 스윕 주기(기동 후 이 시간 뒤 첫 실행) |
| `KBP_JOB_ORPHAN_GRACE_SECONDS` | 21600 | 이보다 최근 객체는 스윕 제외 |
| `KBP_JOB_ORPHAN_MAX_RATIO` | 0.9 | 고아 비율이 이보다 크면 삭제 보류(sanity) |
| `KBP_JOB_GC_BATCH` | 500 | 한 사이클 최대 **삭제** 수(나열은 제한 안 함) |

### 2.6 선행 결함 — 멱등 재삽입이 행 id 와 객체 키를 어긋나게 한다

`repo.submit` 의 멱등 충돌 재삽입 경로가 `job_id=` 를 넘기지 않아 **새 uuid 행**을
만드는데, 그 행의 `input_ref`/`payload_ref` 는 여전히 `{prefix}/{옛 uuid}/...` 를
가리킨다. 스윕은 이를 "행 없는 고아"로 보고 **살아있는 잡의 입력을 지운다.**

GC 이전부터 있던 버그다(키와 행이 어긋나는 것 자체가 잘못). **`job_id=job_id` 를
넘기도록 고친다.** §2.4 의 4번 조건(키 직접 대조)은 그와 별개로 남긴다 — 방어를 한 겹
더 두는 비용이 싸다.

## 3. 파일

| 파일 | 변경 |
|---|---|
| `service/jobs/schema.py` | `LOCK_OBJ_GC = 3` 추가. `CREATE INDEX IF NOT EXISTS jobs_gc_idx ON kbp.jobs (completed_at) WHERE status IN ('succeeded','failed','canceled')` |
| `service/jobs/repo.py` | `purge_expired()`(§2.3), `job_ids_present()`(`set|None`), `refs_in_use()`(§2.4-4), **§2.6 버그 수정** |
| `service/jobs/blobs.py` | `iter_job_objects()`(§2.4 계약), 빈 프리픽스 거부 |
| `service/jobs/gc.py` (신규) | `run_ttl_gc()` · `run_orphan_sweep()` — 조율·판정 |
| `service/worker.py` | `kbp-gc` 데몬 스레드 + 주기 제어 |
| `service/jobs/memory.py` | 더블 보강(§5) |
| `docker-compose.yml` · `docker-compose.airgap.yml` | **`x-facade-env` 앵커에 `KBP_JOB_*` 추가** |
| `.env.airgap.example` · `scripts/facade.env` | 같은 키 |

**배포 통로가 없었다.** `KBP_JOB_*` 은 compose·facade.env 에 **0개**다(실측). 컨테이너는
`env_file` 이 아니라 명시 env 맵(`x-facade-env`)을 쓰므로 지금은 코드 기본값 외에는
설정할 방법이 없다. Phase 1 의 공백이고 여기서 함께 메운다.

`service/app.py` 는 **변경 없다** — API 는 GC 하지 않는다.

## 4. 테스트

| 대상 | 방법 |
|---|---|
| TTL 삭제 | `completed_at` 을 과거로 → 행·객체 삭제. TTL 이내는 보존 |
| `completed_at` NULL | terminal + NULL 인 행도 `created_at` 기준으로 회수된다 |
| 비종료 자식 보호 | terminal parent + queued child → parent 보존 |
| 종료된 자식 | child 가 terminal 이면 parent 도 삭제 |
| **head-of-line** | 보호 대상이 배치 크기만큼 앞에 쌓여도 뒤의 삭제 가능 행이 지워진다 |
| 배치 상한 | 대상 N > batch 면 batch 만큼만, 다음 사이클에 나머지 |
| 삭제 순서 | 객체 삭제 실패해도 행은 지워진다 |
| **TTL=0** | terminal 행 0건 삭제 + 스윕도 안 돈다 |
| 스윕 grace | 행 없는 오래된 객체 삭제, **grace 이내 객체 보존** |
| 스윕 `last_modified=None` | **삭제하지 않는다** |
| 스윕 키 파싱 실패 | 우리 형식이 아닌 키는 건드리지 않는다 |
| **스윕 fail-closed** | `job_ids_present()` 가 예외면 아무것도 안 지운다 |
| **스윕 sanity** | 고아 비율 > ratio 면 보류 |
| 스윕 키 대조 | 행 id 와 키가 어긋나도 `*_ref` 로 살아있으면 보존 |
| 스윕 수렴 | 객체 N > batch 여도 연속 사이클로 전부 수거 |
| §2.6 수정 | 멱등 재삽입 후 행 id 와 객체 키가 일치한다 |
| GC 격리 | GC 가 오래 걸려도 claim 이 계속 돈다(전용 스레드) |
| GC 예외 | GC 가 매번 던져도 worker 틱·heartbeat 가 산다 |
| advisory lock | 외부가 GC lock 을 쥐면 건너뛴다(비차단). GC 가 claim 을 안 막는다 |

## 5. 인메모리 더블 보강

§4 의 상당수가 더블 위에서 성립해야 한다. 현재 부족한 것:

- `InMemoryJobRepo.complete/cancel` 이 `completed_at` 을 **안 채운다**(항상 `None`)
- `InMemoryBlobStore.store_json()` 이 **항상 인라인** → `*_ref` 가 영원히 `None`
- `InMemoryBlobStore.key()` 가 `mem/{job_id}/{name}` → **프로덕션 키 레이아웃과 다르다**

보강: `completed_at` 실제 기록 + 조작 경로, 프로덕션과 동일한 `{prefix}/{job_id}/{name}`
키, 오프로딩 강제 스위치, `iter_job_objects()`.

**키 파싱·TTL 경계·head-of-line 은 `requires_pg` 실 DB 테스트로 못박는다** — 더블로
검증하면 실제 SQL·키 형식을 검증하지 못한다.

## 6. 구현 순서

1. **§2.6 버그 수정** + 회귀 테스트 (GC 와 독립, 먼저 고친다)
2. `schema.py` — `LOCK_OBJ_GC`, `jobs_gc_idx`
3. `repo.purge_expired()`·`job_ids_present()`·`refs_in_use()` — `requires_pg` 테스트
4. `blobs.iter_job_objects()` — fake + 실 MinIO
5. `gc.py` 판정 로직 — 순수 단위테스트(더블 보강 포함)
6. `worker.py` 전용 스레드 배선
7. compose·env 통로
8. 라이브 검증: 객체가 실제로 줄어드는지, claim 이 안 멎는지
9. 문서 반영

## 7. 범위 밖 (근거)

- **삭제 전 아카이빙** — 잡 결과는 소비자가 가져간 사본이 정본이다. 큐는 전달 수단이지
  보관소가 아니다.
- **워크스페이스별 보존정책** — 요구가 나온 적 없다. 단일 TTL 로 시작.
- **GC 지표/알람·`last_gc_at` 노출** — 로그로 시작한다. 주기 상태를 DB 에 두게 되면
  관측 수단이 자연히 생기므로 그때 재검토.
- **edgequake 문서 삭제** — 잡 큐 책임이 아니다.
- **MinIO lifecycle 규칙 대체** — 버킷을 페이지 이미지와 공유하므로 버킷 규칙은 남의
  객체를 지운다. prefix 규칙은 참조 무결성을 모른다.
- **소진 후 queued 로 정체하는 좀비 행 종결** — 큐 본체(claim/requeue) 설계 사안이지
  GC 책임이 아니다. 잡 큐 설계 문서에 별도 항목으로 남긴다.
- **멱등 dedupe 수명 = TTL 이라는 계약 상호작용** — 72h 뒤 같은 문서 재업로드가 새 잡이
  되는 것은 대체로 의도된 동작이다. 설계 §4.4 에 각주만 남긴다.
- **부모가 TTL 로 지워진 뒤 자식이 참조하는 창** — 결과가 자식 잡의 명시적 `JobFailed`
  (무결성 훼손·중복 적재 없음)라 수용한다.
- **같은 버킷+프리픽스를 공유하는 두 배포가 서로의 staging 을 지우는 시나리오** — 배포
  토폴로지 문제다. §2.4 의 fail-closed·sanity 가드와 "이 프리픽스를 단독 소유한다"
  전제로 덮는다. 스윕 opt-in env 도입은 airgap 배포 문서에서 다룬다.
- **`_purge_legacy_inputs` 가 컬럼을 NULL 로 안 비워 WARN 오탐** — 기능적으로 무해(멱등).
  "존재하지 않는 객체 삭제는 WARN 을 남기지 않는다" 정도로 구현 시 처리.
