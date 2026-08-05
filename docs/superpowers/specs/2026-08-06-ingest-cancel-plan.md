<!-- plan-version: v7 -->
<!-- ultracode-validation: READY v7 at 2026-08-05T17:47:14Z -->

# 적재 잡 취소 (D6) + 잡 경로 기본화

## ✅ 구현 진행 (2026-08-06)

**1/3 완료 — 백엔드 핵심** (kb `4bd434e`)
- `schemas/ingestion.py` — `IngestResult.status` 에 `canceled`
- `models/ingestion_job.py` + `migrations/a3b5d7f9c1e2` — `cancel_requested`
  (upgrade/downgrade 실기동 확인)
- `repositories.py` — `is_cancel_requested`, `request_cancel_in_kb`(단일 원자 UPDATE)
- `core/pipeline.py` — `should_cancel` 운반(ingest_document → tail → 헬퍼),
  헬퍼 7튜플 계약, 체크포인트 3곳, `_canceled`(delete_doc 미호출)
- `workers/tasks.py` — 진입 가드, `_should_cancel` 클로저(새 세션·fail-open·`own` 가드),
  `canceled` 분기
- 회귀 0건(19건 그대로)

> 구현 중 발견: `_should_cancel` 을 `if _staged:` 안에 정의하면 dify 경로에서
> `UnboundLocalError` 다(호출은 분기 밖). 분기 **앞**에 정의해야 한다.

**2/3 남음 — API·배치**
- `routers/jobs.py` — `POST /kb/{kb_id}/jobs/{job_id}/cancel`. §2.5 동작 순서대로:
  provider 409 → `request_cancel_in_kb` → `canceled` 면 문서·배치아이템 전이 →
  0행이면 `session.expire(job)` 후 재조회해 `stage=='insert'`/terminal 구분 → 409
  (`SqlDocumentRepo` import 필요)
- `schemas/jobs.py` — `JobStatus.provider` + `_to_status(job, doc, kb)` 시그니처
- `batch_repository.py` — `TERMINAL_STATUSES` 에 `canceled`,
  `retry_failed_item` 이 `cancel_requested` 비움, `find_item_by_job_id` 신설
- `batch_worker.py` — `canceled` 분기(+ `error` 는 비운다)
- `models/batch_ingestion.py:108` 주석
- `config.py:163` `use_jobs=True` + `.env.example`·`.env.airgap.example`

**3/3 남음 — 프론트** (§2.7 8+3곳, §2.8 버튼)
- `lib/types.ts` — `JobStatusValue`·`BatchItemStatus.status`·`JobStatus.provider`
- `JobList.tsx` — `known`·`label`·`TERMINAL`·버튼·:29 주석
- `JobProgressInline.tsx:14` `TERMINAL`
- `BatchStatusPanel.tsx` `statusLabel`
- `DocumentList.tsx` — 배지 + **`HIDDEN` 필터에 `canceled`**
- `DocumentDetailModal.tsx`, `app/kb/[kbId]/documents/[docId]/page.tsx`
- `app/globals.css` `.badge.canceled`
- `api.ts` `cancelKbJob`

**테스트·문서** — §6 전부 + `_workspace/` 반영 + alembic 스모크

> 선행 plan `2026-08-05-idempotency-key-lifetime-plan.md` §6 이 이관한 미해결 배선 7건을
> 여기서 푼다. 그 plan 은 v7 READY 로 구현·커밋 완료(kb `222a477`, kbp `5981769`).
>
> **v6 → v7**: blocking 1건 — 배치 아이템 UPDATE 가 **`RETURNING='canceled'` 일 때만**
> 실행돼야 한다. 무조건 실행하면 잡이 영구 `running` 으로 고착된다(§2.5-5).
>
> **v5 → v6**: blocking 1건 — §2.5 가 **running 취소를 죽였다**. 조건부 UPDATE 를
> `WHERE status='queued'` 로만 걸고 "0행이면 409" 라 해서, running 잡은 **항상 0행 →
> 항상 409** 인데 그 전에 `cancel_requested=true` 는 이미 커밋된다. 즉 "취소할 수
> 없습니다" 를 본 잡이 몇 분 뒤 canceled 로 끝난다. D6 의 핵심 유스케이스가 통째로 죽는다.
> **facade 가 이미 단일 원자 UPDATE 로 푼 문제**라 그 형태를 그대로 가져온다.
>
> **v4 → v5**: blocking 2건. (a) `should_cancel` 이 **fail-open** 이어야 한다 — 잡 기록
> 삭제 API 가 running 잡도 지우는데 `_get` 이 `ValueError` 를 던져 적재가 통째로 죽는다.
> (b) 워커 진입 가드가 **문서 상태를 방치**해 `ingesting` 으로 영구 고착된다.
>
> **v3 → v4**: 검증 blocking 2건. 하나는 **파괴적 처방**이었다 — "취소도 `_fail` 과 같은
> 정리를 한다"고 썼는데, 그 정리가 `delete_doc(workspace, content_hash[:16])` 이라
> **같은 내용·다른 파일명으로 이미 적재된 남의 문서를 지운다**(§2.1). 취소 경로는
> `delete_doc` 을 아예 호출하지 않는다. §6 의 "blob 삭제" 잔재도 제거.
>
> **v2 → v3**: 검증 blocking 3건 반영. 취소 API 의 책임을 **상태 전이 하나로 줄였다** —
> staging 정리를 직접 하려다 배치 경로에서 취소가 `failed` 로 뒤집히는 구멍을 만들었다
> (§2.5). `use_jobs=False` → 501 규칙도 폐기했다(전제가 틀렸다, §4).
>
> **v1 → v2**: 검증 blocking 4건 반영. 셋은 배선 누락이었고 하나는 **설계 구멍**이다 —
> 취소 UI/API 가 provider 를 구분하지 않아 kb_pipeline 이 아닌 KB 에서 버튼이 뜨고
> 조용히 무동작이 된다(§2.9).

---

## 0. 결정 사항 (사용자, 2026-08-06)

1. **facade 잡 경로가 무조건 기본이다.** `kb_pipeline_use_jobs` 기본값을 `True` 로
   바꾸고 `.env` 예시에 명시한다. 레거시 동기 경로는 롤백 레버로만 남긴다.
2. **취소 범위는 (a) 축소범위** (2026-08-05 결정 유지):
   - `queued` → 즉시 취소
   - `running` → **진행 중인 그 단계는 완주**하고, 다음 단계를 제출하지 않는다
   - `inserting` 이후 → 취소 불가(부분 적재 방지)

   다운스트림 폴링 훅(진행 중 단계 즉시 중단)은 여전히 비목표다.

---

## 1. 현재 사실 (실측 2026-08-06)

### 1.1 facade — 이미 되어 있는 것

| | |
|---|---|
| `kbp.jobs.cancel_requested` 컬럼 | 있음 |
| `DELETE /jobs/{id}` | `queued` 즉시 취소 **+ staging 객체 즉시 삭제** / `running` 플래그 / `inserting` 이면 **409** / 취소 시 `idem_key` 비움 |
| `_recover` | `cancel_requested` → `canceled` 로 종결 |

**kb 쪽 배선만 남았다.** facade 는 이번에 **아무것도 안 건드린다**.

### 1.2 kb — 없는 것 / 막힌 것

| # | 사실 | 근거 |
|---|---|---|
| 1 | 취소 API·버튼 **없음**. `DELETE /kb/{id}/jobs/{job_id}` 는 "잡 **기록** 삭제" | `routers/jobs.py:99` |
| 2 | 워커 진입 시 **취소를 안 본다** — 무조건 `running` 으로 덮어씀 | `tasks.py:165` `jobs.set_state(job_id, status="running", stage="gate")` |
| 3 | pipeline 체크포인트 3곳이 **예외가 아니라 값을 반환** | `:1816 return _bad(...)`, `:1866 return _bad(...)`, `:2282 return _fail(..., cleanup_doc_id=...)` |
| 4 | `_fail` 이 **edgequake staging 삭제 + 문서 상태**를 함께 한다 | `:2228-2234` `kbp.delete_doc(...)` + `set_status(rec.document_id, "failed")` |
| 5 | 워커가 `result.status` 로 분기한다 (`rejected`→`gate_failed`, `failed`→`failed`) | `tasks.py:258-275` |
| 6 | `status='canceled'` 를 쓰는 주체 **없음** | 위 분기에 canceled 없음 |
| 7 | 배치 워커가 결과를 재매핑 — `succeeded`/`gate_failed` 아니면 **전부 `failed`** | `batch_worker.py:174-178` |
| 8 | `TERMINAL_STATUSES` 에 canceled 없음 → 넣지 않으면 배치가 `completed` 못 감 | `batch_repository.py:21` |
| 9 | pipeline 은 `job_id`·`session_factory` 를 **모른다**(`on_stage` 만 받음) | `tasks.py:220-229` 호출부 |
| 10 | `use_jobs` 기본 `False` | `config.py:163` |
| 11 | **`IngestResult.status` 가 `Literal` 로 고정** — `canceled` 를 넣으면 pydantic ValidationError | `schemas/ingestion.py:113` `Literal["ready","rejected","failed","skipped"]` |
| 12 | parse·chunk 는 tail 이 아니라 **공유 헬퍼** 안. 반환은 `(ok, detail, …)` 튜플이라 취소를 실패와 구분 못 함. **raganything tail 도 같은 헬퍼를 쓴다** | `pipeline.py:1764` 정의, `:1986`(raganything)·`:2249`(kb_pipeline) 호출 |
| 13 | **`JobStatus` 에 `provider` 가 없다** — 프론트가 provider 를 모른다 | `schemas/jobs.py` `JobStatus` 필드에 없음(`KbSummary` 에만 있음) |
| 14 | `retry_failed_item` 이 `cancel_requested` 를 **안 비운다** | `batch_repository.py:226-245` |
| 15 | xlsx 게이트 parse 는 tail **이전에** 이미 facade 를 부른다 | `pipeline.py:509-530` → `:568 kb.pre_parsed = parsed` |
| 16 | 배치 `claim` 은 **아이템 status 만** 본다(잡 상태를 안 봄) | `batch_repository.py:132` `where(BatchIngestionItem.status == "queued")` |
| 17 | staging 이 없으면 워커가 `ValueError` → `_finish_unexpected` 가 잡·문서·아이템을 **전부 `failed`** 로 덮는다 | `batch_worker.py:146-159`, `:110-124` |
| 18 | `use_jobs` 는 **transport 플래그일 뿐**이다 — 소비처가 `dependencies.py:173` → 클라이언트 하나뿐. kb 의 잡·워커·체크포인트와 무관 | `config.py:163`, `dependencies.py:173` |
| 19 | `default_kb_provider` 기본값은 **`dify`** (`DEFAULT_KB_PROVIDER` 라는 심볼은 없다) | `config.py:120` |
| 20 | 헬퍼의 조기반환(`return _bad(`)은 2곳이 아니라 **5곳** | `pipeline.py:1816,1830,1846,1866,1870` |
| 21 | `JobStatus` 를 폴링하는 소비자는 **2곳**(BatchStatusPanel 은 배치 상태를 폴링한다) | `JobList.tsx:320`, `JobProgressInline.tsx:43` |

---

## 2. 설계

### 2.1 예외가 아니라 **결과 상태**로 흘린다 〔핵심〕

이전 plan(v2~v3)은 `JobCanceled` 예외를 쓰려다 두 번 막혔다 — 체크포인트가 값을
반환하고(#3), `_fail` 을 건너뛰면 정리가 안 된다(#4).

**이미 있는 축을 쓴다.** 워커는 `result.status` 로 분기한다(#5). 거기에 `canceled` 를
더한다:

```
pipeline:  취소 감지 → _canceled(detail) → IngestResult(status="canceled", ...)
                        └ 문서 상태만 "canceled" 로. **delete_doc 은 부르지 않는다**
tasks.py:  if result.status == "canceled": jobs.set_state(status="canceled") → return "canceled"
```

새 예외도, 재-raise 도, 광범위 except 우회도 필요 없다.

**⚠️ 취소는 `delete_doc` 을 호출하지 않는다** 〔v3 검증 blocking — 파괴적〕

v3 은 "`_fail` 과 같은 정리(delete_doc + 문서 상태)" 라고 썼는데 **위험하다**:

- 취소는 계약상 **insert 제출 전**에만 성립한다(§0.2) → edgequake 에 정리할 게 **없다**
- 그런데 `_fail` 의 정리는 `kbp.delete_doc(kb_id, new_docs_id)` 이고
  `new_docs_id = content_hash[:16]` 다(`pipeline.py:2216`)
- 중복 스킵 가드는 **파일명 기준**이라(`find_by_logical_identity(kb_id, file_name)`,
  `:574`) 같은 내용을 **다른 이름**으로 올리면 통과한다
- → 그 두 번째 업로드를 취소하면 **이미 `ready` 로 적재된 첫 번째 문서를 edgequake 에서
  지운다**. kb `documents` 행은 `ready` 로 남아 **조용한 손실**이다

따라서 `_finish` 일반화는 **문서 상태 전이만** 공유한다. `cleanup_doc_id` 는 취소 경로에서
넘기지 않는다. (`_fail` 이 `:1963`(raganything)·`:2227`(kb_pipeline) 두 곳에 동명으로
있는데, 일반화 대상은 **kb_pipeline 쪽**이다.)

**선행 조건 〔#11〕** — `IngestResult.status` 가 `Literal["ready","rejected","failed",
"skipped"]` 로 고정돼 있다. `"canceled"` 를 더하지 않으면 이 설계 전체가 pydantic
`ValidationError` 로 터진다. `schemas/ingestion.py:113` 을 **먼저** 넓힌다.

**`_fail` 이 둘이다** 〔검증 minor〕 — `:1963`(raganything tail)과 `:2227`(kb_pipeline
tail)에 동명으로 각각 있다. 이번에 일반화하는 것은 **kb_pipeline tail 의 것**이다.

### 2.2 취소 확인 지점 — 세 곳, 두 함수 〔#12〕

v1 은 세 곳이 모두 `_ingest_kb_pipeline_tail` 안이라고 했는데 **틀렸다.** parse·chunk 는
공유 헬퍼 `_facade_parse_and_chunk`(`:1764`) 안에 있고, tail 은 그 헬퍼를 **한 번**
호출할 뿐이다. 게다가 헬퍼의 반환은 `(ok, detail, …)` 튜플이라 취소를 실패와 구분할 수
없어 그대로 `_fail`(=failed)로 흘러간다.

| 지점 | 위치 |
|---|---|
| **헬퍼 진입 시** | `_facade_parse_and_chunk` 맨 앞 — `if pre_parsed is None:`(`:1803`) **앞**. 그 블록 안(`:1806`)에 두면 `pre_parsed` 가 채워지는 경로(xlsx 게이트·2단계 적재 Phase2)에서 **죽은 코드**가 된다 〔v3 검증〕 |
| chunk 직전 | `_facade_parse_and_chunk` (`:1833` `_emit("chunk")` 앞) |
| insert 직전 | `_ingest_kb_pipeline_tail` — **`_emit("insert")`(`:2267`) 앞** 〔v4 검증〕. 뒤에 두면 취소로 끝난 잡의 `stage` 가 `insert` 로 남아 §2.5-3(=409)·§2.8(버튼 미노출)과 겹쳐 어휘가 지저분해진다 |

**헬퍼 반환 계약을 넓힌다.** 튜플 첫 원소 `ok: bool` 을 그대로 두고, `detail` 옆에
취소 여부를 실어 tail 이 `_fail` 과 `_canceled` 를 가를 수 있게 한다:

```python
def _bad(detail, *, canceled: bool = False) -> tuple[bool, str, bool, ...]:
    return (False, detail, canceled, [], [], {}, {})
```

**raganything tail(`:1986`)도 같은 헬퍼를 쓴다** — 그쪽은 `should_cancel=None` 으로
호출해 동작이 불변임을 테스트로 고정한다.

`inserting` 진입 후에는 확인하지 않는다 — 부분 적재를 만들지 않기 위해서다.

**xlsx 게이트 parse 는 대상이 아니다** 〔#15〕 — `ingest_document` 본문(`:509-530`)이
tail 이전에 facade parse 를 한 번 부른다(게이트용, 결과를 `kb.pre_parsed` 로 재사용).
여기까지는 워커 진입 가드(§2.4)가 큐 취소를 잡고, running 중 취소는 못 잡는다.
그 창은 파싱 1회분이고 **부작용이 없다**(edgequake 미접촉) — 수용한다.

### 2.3 취소 확인 콜러블의 운반 〔#9〕

pipeline 은 `job_id` 를 모른다. `on_stage` 와 **같은 방식**으로 주입한다:

```python
# core/pipeline.py  ingest_document(..., on_stage=None, should_cancel=None)
#   should_cancel: Callable[[], bool] | None — None 이면 취소 확인 없음(기존 동작)
```

호출부(`tasks.py:220`)가 클로저를 만든다:

```python
def _should_cancel() -> bool:
    # **새 세션**으로 읽는다. 워커 세션은 expire_on_commit=False + identity map 이라
    # 다른 세션(API)의 UPDATE 를 영원히 못 본다.
    #
    # **fail-open** 〔v4 검증 blocking〕 — 예외나 행 부재는 "취소 아님"으로 흡수한다.
    # `SqlJobRepo._get` 은 행이 없으면 ValueError 를 던지는데(`repositories.py:327`),
    # 잡 **기록** 삭제 API 가 running 잡도 그대로 지운다
    # (`DELETE /kb/{id}/jobs` 전체삭제 · `DELETE /kb/{id}/jobs/{job_id}`).
    # 그러면 다음 체크포인트에서 예외가 파이프라인 한복판으로 튀어 10분짜리 적재를
    # 날리고, 배치는 `_finish_unexpected` 가 문서·아이템을 failed 로 덮는다.
    # 지금은 파이프라인 중간에 DB 조회가 없어서 기록 삭제가 적재를 안 깬다 —
    # 이 설계가 **새 실패 모드를 만들면 안 된다**. 일시적 DB 오류도 같다.
    try:
        s = ctx["session_factory"]()
    except Exception:  # noqa: BLE001
        return False
    # **호출자 세션과 같은 객체면 닫지 않는다** 〔v6 검증〕. 기존 테스트 하네스 7곳이
    # `lambda: db_session` 으로 **공유 세션을 그대로** 돌려주는데, 그걸 닫으면 워커가
    # 쓰고 있던 트랜잭션이 rollback 되고 인스턴스가 expunge 된다 — 파이프라인 한복판에서.
    # 운영(`runtime._default_session_factory`)은 매번 새 세션이라 이 가드가 no-op 다.
    own = s is not session
    try:
        return bool(SqlJobRepo(s).is_cancel_requested(job_id))
    except Exception:  # noqa: BLE001 - 행 부재·DB 오류 → 취소 아님으로 흡수
        return False
    finally:
        if own:
            try:
                s.close()
            except Exception:  # noqa: BLE001
                pass
```

> **테스트 하네스** — 공유 세션을 돌려주는 하네스가 **7곳**이다
> (`test_parse_preview_gate.py:81`, `test_worker_chunk_preview.py:75`,
> `test_worker_kb_pipeline_stages.py:124`, `test_jobs_multifile.py:91`,
> `test_batch_ingestion.py:55`, `test_job_status.py:79`, `test_worker_parse_preview.py:75`).
> `should_cancel` 은 tasks.py 가 **항상** 주입하므로 기존 kb_pipeline 테스트에서도
> 체크포인트마다 실행된다 — **"취소 테스트만 전용 팩토리" 로는 못 막는다.** 위 `own`
> 가드가 회귀 기준선 19건을 지키는 조건이다.

호출 횟수는 **문서당 최대 3회**다(폴링마다가 아니다) — 커넥션 부담 없음.

### 2.4 워커 진입 가드 〔#2〕

`tasks.py:165` 가 무조건 `running` 으로 덮어써서, `queued` 에서 취소해도 큐에 이미 들어간
태스크가 나중에 실행되면 `canceled` 가 되살아난다.

```python
job = jobs.get_job(job_id)
if job.status == "canceled" or job.cancel_requested:
    # **멱등하게 canceled 를 쓴다** 〔v2 검증 minor〕. 아무것도 안 쓰면
    # `cancel_requested=true` 인데 status 가 running 인 채 재실행되는 경로
    # (워커 크래시 → recover_stale 이 아이템만 queued 로 되돌림)에서 잡이
    # 영구히 running 으로 남아 폴링이 안 멈춘다.
    # stage 는 **그대로 둔다**(`set_state` 는 None 을 '변경 없음' 으로 취급한다) —
    # 어느 단계에서 취소됐는지 보존한다. tasks.py:244 의 "마지막 관측 단계 보존" 과 같은 규약.
    jobs.set_state(job_id, status="canceled")
    # **문서 상태도 함께 전이한다** 〔v4 검증 blocking〕. 이 가드가 실제로 발동하는
    # 비-API 경로(워커 크래시 → recover_stale 이 아이템만 queued 로 되돌림 → 재claim)
    # 에서는 파이프라인이 이미 `set_status(document_id, "ingesting")`(`:618`) 을 해 둔
    # 상태다. 잡·아이템만 canceled 로 끝내면 **문서는 영구히 'ingesting'** 이라
    # DocumentList 가 "적재 중" 을 영원히 표시한다(다른 정리 주체가 없다).
    try:
        SqlDocumentRepo(session).set_status(document_id, "canceled")
    except Exception:  # noqa: BLE001 - 문서 상태 반영 실패는 비치명
        pass
    return "canceled"
jobs.set_state(job_id, status="running", stage="gate")
```

### 2.5 취소 API — **상태 전이만 한다** 〔v2 검증 blocking〕

v2 는 취소 API 가 staging 까지 지우게 했는데, 배치 경로에서 **취소가 `failed` 로
뒤집히는** 구멍이 생겼다:

```
API 가 staging 삭제 + 잡 canceled
  → 배치 claim 은 **아이템 status 만** 본다(#16) → queued 아이템을 그대로 집어감
  → 워커가 staging 을 먼저 읽는다 → 없음 → ValueError
  → _finish_unexpected 가 잡·문서·아이템을 전부 failed 로 덮는다(#17)
  → 방금 세운 canceled 가 사라지고 배치는 completed_with_errors
```

§2.4 진입 가드에 **도달조차 못 한다**(staging 읽기가 먼저다).

**그래서 API 는 상태만 바꾼다. 정리는 각자의 기존 메커니즘에 맡긴다.**

```
POST /kb/{kb_id}/jobs/{job_id}/cancel     (owner 전용)
  202 {"job_id", "status": "canceled" | "cancel_requested"}
  409  이미 terminal / provider != kb_pipeline / kb stage == 'insert'
  404  없음 / 타 KB (IDOR — job→document→kb_id 대조)
```

동작:
1. **KB provider 가 `kb_pipeline` 이 아니면 409** 〔§2.9〕 — 이건 잡이 아니라 KB 속성이라
   먼저 본다
2. terminal·`stage=='insert'` 판정은 **아래 UPDATE 의 술어가 겸한다**(따로 조회하지 않는다)
3. (v5 의 '읽고-판단' 단계는 삭제됐다 — 4번이 원자적으로 대신한다)
4. **단일 원자 UPDATE** 〔v5 검증 blocking〕 — 읽고-판단-쓰기를 하지 않는다.
   facade `JobRepo.cancel`(`service/jobs/repo.py:699`)과 같은 형태다:

   ```sql
   UPDATE ingestion_jobs
      SET cancel_requested = true,
          status = CASE WHEN status='queued' THEN 'canceled' ELSE status END
    WHERE id = :job_id
      AND status IN ('queued', 'running')      -- terminal 이면 0행
      AND coalesce(stage, '') <> 'insert'      -- 부분 적재 방지
   RETURNING status
   ```

   | RETURNING | 응답 |
   |---|---|
   | `'canceled'` | 202 `{"status": "canceled"}` — queued 였다. 문서 상태도 `canceled`, 배치 아이템이면 아래 5 |
   | `'running'` | 202 `{"status": "cancel_requested"}` — **진행 중인 단계는 완주하고 다음 단계가 막힌다**(§0.2) |
   | 0행 | 재조회해서 `stage=='insert'` 면 409(적재 마무리 중), 아니면 409(이미 종료) |

   두 분기를 나눠 쓰면 그 사이에 잡이 종결돼 **취소가 유실되거나 끝난 잡을 뒤집는다**.
   0행일 때만 재조회하므로 정상 경로에 추가 쿼리가 없다.

   > **`queued` 창은 짧다** 〔v5 검증 minor〕 — `AsyncioJobQueue.enqueue` 가 즉시 데몬
   > 스레드를 띄우므로 단건 잡은 밀리초 안에 running 이 된다. 게다가 진입 가드(§2.4)를
   > **이미 통과한** 스레드는 `set_state(status="running")` 을 무조건 쓰므로, API 가 방금
   > 커밋한 `canceled` 를 잠깐 `running` 으로 되돌릴 수 있다. `cancel_requested` 플래그
   > 덕에 다음 체크포인트에서 수렴하지만, **"취소했는데 다시 running 으로 보인다"** 는
   > 화면이 잠깐 존재한다. 이건 인정하고 §7 에 적는다.
   >
   > v5 는 `WHERE status='queued'` 만 걸고 "0행이면 409" 라 했는데, running 잡은
   > `tasks.py:165` 가 이미 `status='running'` 을 써 둬서 **항상 0행**이다.
   > 게다가 그 전에 `cancel_requested` 를 따로 커밋하면 409 를 돌려주고도 취소가
   > 진행돼 **응답과 실제가 어긋난다**. 한 문장으로 합치면 둘 다 사라진다.

5. **`RETURNING` 이 `'canceled'` 일 때만** — 배치 아이템도 조건부 `canceled` +
   `refresh_batch_status()` 〔v6 검증 blocking + v5 minor〕:

   ```sql
   UPDATE batch_ingestion_items SET status='canceled'
    WHERE id = :item_id AND status = 'queued'
   ```

   `processing` 을 덮으면 `recover_stale`(`status=='processing'` 한정)의 회수 대상에서
   빠지고, 이어 부르는 `refresh_batch_status` 가 **아직 적재 중인 배치를 completed 로**
   표기한다. `queued` 였을 때만 바꾸면 워커가 애초에 안 집는다는 목적도 그대로 달성된다.

   **`RETURNING='running'`(=`cancel_requested` 만 세운 경우)에는 아이템을 건드리지
   않는다** 〔v6 검증 blocking〕. §2.4 가 인정한 상태조합 — 워커 크래시 → `recover_stale`
   이 **아이템만** `queued` 로 되돌리고 잡은 `running` 으로 남는 조합 — 에서 아이템까지
   `canceled` 로 만들면:

   ```
   아이템 canceled → claim 이 영영 안 집음(queued 만 집는다)
     → §2.4 진입 가드가 실행될 기회가 사라짐
     → 잡을 terminal 로 옮길 주체가 코드베이스에 **아무도 없다**
     → 잡 영구 `running` + JobList 무한 폴링
   ```

   kb 에는 `ingestion_jobs` 를 회수·타임아웃 종결하는 코드가 없다(facade 의 `_recover`
   같은 것이 없다). 아이템을 `queued` 로 남겨야 claim → 가드 → `canceled` 로 수렴한다.

6. 202

**staging/blob 은 지우지 않는다.**

| 경로 | 남는 것 | 수거 주체 |
|---|---|---|
| 배치 | `staging_store`(`parse-staging/batch/…`) | facade staging 스윕 — batch TTL 7일 (D20) |
| 단건 | `blob_store`(document_id 키) | **없다.** `blob_store.delete` 호출은 리포 전체에서 `batch_worker.py:168` 한 곳뿐이고 그것도 **배치** 경로다. 단건은 성공해도 남는다 — 취소가 만든 문제가 아니다 |

단건 `blob_store` 잔여는 **이번 변경과 무관한 기존 상태**다(성공해도 남는다). 별도 항목으로
남긴다(§7).

**배치 아이템 역조회가 필요하다** 〔v2 검증 blocking〕 — API 는 `job_id` 만 받는다.
`batch_repository` 에 `find_item_by_job_id(job_id)` 를 신설한다
(`BatchIngestionItem.ingestion_job_id` 는 `models/batch_ingestion.py:84` 에 있다).

**facade 에는 위임하지 않는다.** kb 가 다음 단계 제출을 막으면 facade 잡은 자기 수명대로
끝난다. facade 잡 id 를 kb 가 들고 있지 않으므로 위임할 수단도 없다.

### 2.6 배치 경로 〔#7 #8〕

- `batch_repository.TERMINAL_STATUSES` 에 `"canceled"` 추가 — **안 넣으면 배치가
  `completed` 로 못 가고 `queued` 로 되돌아간다**
- `batch_worker` 결과 매핑에 `canceled` 분기 추가(지금은 `failed` 로 덮인다).
  **`error` 도 함께** — 현재 `status != "succeeded"` 면 무조건 `_failure_reason` 을 불러
  `job.error` 를 채우므로, 취소인데 잔여 오류 문자열이 '오류' 로 표기될 수 있다
- staging 은 **지우지 않는다**(§2.5). facade 스윕이 batch TTL 7일로 수거한다(D20)
- `batch_ingestion.py:109` 모델 주석 갱신
- **재시도 불가** — `batch_repository.py:233` **과 `routers/batches.py:322`** 두 곳이
  `{failed, gate_failed}` 를 각각 하드코딩한다. 취소한 아이템은 재수행 버튼이 안 뜬다.
  의도된 동작으로 두고 문서에 적는다(재업로드로 해결)
- **`retry_failed_item` 이 `cancel_requested` 를 비워야 한다** 〔#14, blocking〕.
  안 비우면: 실행 중 취소 → 그 단계가 실패해 `failed`(플래그는 남음) → 재수행이
  `queued` 로 되돌림 → **워커 진입 가드가 즉시 `canceled` 반환** → 잡은 `queued` 인 채
  남아 **폴링이 영원히 안 멈춘다**. §2.7 이 고치려는 바로 그 증상이 다른 경로로 재현된다
- `has_error` 계산(`batch_repository.py:116`)에 `canceled` 를 넣지 **않는다** — 취소는
  오류가 아니다. 취소가 섞인 배치는 `completed` 로 표기된다(의도)

### 2.7 상태 어휘 — 11곳

하나라도 빠지면 배지가 깨지거나 **폴링이 안 멈춘다**.

**잡 상태 `canceled`** (8곳)
1. `models/ingestion_job.py` docstring
2. `schemas/jobs.py` `JobStatus` docstring
3. `frontend/lib/types.ts` `JobStatusValue`
4. `JobList.tsx:13` `known` 배열
5. `JobList.tsx:14-20` `label` 맵 — 없으면 영문 원문 노출
6. `JobList.tsx:9` 폴링 `TERMINAL`
7. `JobProgressInline.tsx:14` 자체 `TERMINAL` Set
8. `BatchStatusPanel.tsx` `statusLabel` 맵 — **배치/아이템 상태 맵이다**. 잡 상태를
   폴링하지는 않는다(#21). 아이템에 `canceled` 가 생기므로 여전히 필요

**문서 상태 `canceled`** (3곳) — §2.1 이 문서도 `canceled` 로 두므로
9. `DocumentList.tsx`
10. `DocumentDetailModal.tsx`
11. `app/kb/[kbId]/documents/[docId]/page.tsx`
(+ `models/document.py:74` 주석, `.badge.canceled` CSS)

### 2.8 프론트 버튼

- `status ∈ {queued, running}` **이고** `stage !== 'insert'` 일 때만 노출
- 툴팁: *"진행 중인 단계는 끝까지 실행되고, 그다음 단계부터 중단됩니다"*
- 409 면 "적재 마무리 중이라 취소할 수 없습니다" + 목록 새로고침
- 기존 `✕ 기록 삭제` 는 그대로(의미가 다르다)

### 2.9 provider 구멍 〔v1 검증 blocking — 설계 결함〕

취소는 **kb_pipeline tail 에만** 들어간다. 그런데 v1 의 취소 API·버튼은 provider 를
구분하지 않았다. dify·edgequake·raganything·ragflow KB 의 잡은 stage 가
`dify`/`select`/`persist_meta` 라 §2.8 노출조건(`stage !== 'insert'`)을 **항상 만족**해서:

- 버튼이 뜨고 → `cancel_requested` 만 세워지고 → 잡은 그대로 `succeeded` 로 끝난다
  (**조용한 무동작**)
- `stage=='insert'` 409 가드도 그 provider 들에선 **영원히 발동하지 않아** insert 중
  취소를 못 막는다

**처방 — 두 층 모두 막는다.**

1. **서버** — 취소 API 가 KB provider 를 확인해 `kb_pipeline` 이 아니면 **409**
   ("이 KB 의 적재 방식은 취소를 지원하지 않습니다"). UI 를 우회한 호출도 막힌다.
2. **프론트** — `JobStatus` 에 **`provider` 를 싣는다** 〔#13〕. 지금은 없어서 프론트가
   provider 를 알 수 없다(`schemas/jobs.py` 의 `JobStatus` 에 필드 없음 — `KbSummary`
   에만 있다). 버튼 노출조건에 `provider === 'kb_pipeline'` 을 더한다.

`JobStatus` 에 필드를 더하는 것이므로 `_to_status()` 투영도 함께 고친다.

---

## 3. 마이그레이션

`f2a4c6e8b0d2` 뒤 1건. **선례(`f2a4`)와 같은 plain `add_column`** 을 쓴다 〔v6 검증〕 —
sqlite 도 상수 `server_default` 의 ADD COLUMN 은 네이티브로 지원한다. `batch_alter_table`
은 테이블을 재생성하므로 `ingestion_jobs` 의 FK(`documents.id`, `ondelete=CASCADE`)를
반사에 맡기는 불필요한 위험을 진다.

```python
def upgrade():
    op.add_column("ingestion_jobs",
                  sa.Column("cancel_requested", sa.Boolean(),
                            nullable=False, server_default=sa.false()))

def downgrade():
    op.drop_column("ingestion_jobs", "cancel_requested")
```

---

## 4. 잡 경로 기본화

- `config.py:163` `kb_pipeline_use_jobs: bool = True`
- `.env.example`·`.env.airgap.example` 에 항목 + "롤백은 `false`" 주석
**`use_jobs=False` 여도 취소는 동작한다** 〔v2 검증 blocking — v2 의 501 규칙 폐기〕.
`kb_pipeline_use_jobs` 는 **kb→facade transport 플래그**일 뿐이다(#18, 소비처가
`dependencies.py:173` → 클라이언트 하나뿐). kb 자신의 `ingestion_jobs`·워커·§2.2
체크포인트는 이 값과 무관하고, §2.5 가 facade 에 위임하지 않으므로 취소 경로는 transport
를 아예 타지 않는다. 롤백 레버를 당겨도 멀쩡히 도는 기능을 501 로 막을 이유가 없다.

> 근거: 2026-08-05 전 구간 라이브 검증(단건 `ready` 556s, 배치 4건 `completed` 508s,
> 유량제어·게이트차단·원본승격 확인) + 2026-08-06 재업로드 검증.

---

## 5. 변경 목록

**kb 백엔드**
- `schemas/ingestion.py:113` — **`IngestResult.status` Literal 에 `"canceled"` 추가**
  〔선행 조건. 없으면 전체가 ValidationError〕
- `models/ingestion_job.py` — 컬럼 + docstring. **`default=False` 를 반드시 넣는다**
  〔v7 검증〕 — 테스트 스키마는 alembic 이 아니라 `Base.metadata.create_all` 로 만들어져
  `server_default` 가 적용되지 않는다. python 측 default 가 없으면 모든 `create_job`
  INSERT 가 NOT NULL 위반으로 죽어 **전 스위트가 빨개진다**(기준선 19건이 아니라)
- `migrations/versions/` — 신규 1건
- `repositories.py` — `is_cancel_requested`, `request_cancel`
- `routers/jobs.py` — `POST .../cancel` (+ provider 가드, **`SqlDocumentRepo.set_status`
  로 문서 상태 전이** — 이 파일은 지금 `SqlDocumentRepo` 를 import 하지 않는다.
  0행 재조회 전에는 `session.expire(job)` — `expire_on_commit=False` + identity map 이라
  raw UPDATE 뒤에도 stale 인스턴스를 돌려준다 〔v7 검증〕) / `_to_status(job, doc, kb)` 로
  **kb 인자 추가** 〔v4 검증〕 — provider 는 job·document 에 없고 `KnowledgeBase` 에만 있다.
  두 라우트 모두 kb 를 이미 들고 있다(`:62` `session.get(KnowledgeBase,…)`,
  `list_kb_jobs` 는 `Depends(get_readable_kb)`)
- `schemas/jobs.py` — `JobStatus.provider` 필드 추가 〔§2.9〕
- `schemas/jobs.py` — docstring
- `core/pipeline.py` — 네 곳:
  - `ingest_document(:475)` 시그니처에 `should_cancel` + `_ingest_kb_pipeline_tail(:656)`
    호출부로 전달
  - `_ingest_kb_pipeline_tail(:2159)` 시그니처 + insert 직전 체크포인트 + `_fail(:2227)`
    → `_finish` 일반화
  - `_facade_parse_and_chunk(:1764)` 시그니처 + `_bad` 반환 계약(취소 플래그) +
    parse·chunk 직전 체크포인트 2곳. **`_bad` 조기반환은 5곳**(`:1816,1830,1846,1866,1870`),
    성공 반환(`:1906`)과 **두 호출부 언팩**(`:1985`, `:2248`)도 6→7 튜플로 함께 바꾼다 〔#20〕
  - raganything tail(`:1986`)의 헬퍼 호출 — `should_cancel=None` 로 동작 불변
- `workers/tasks.py` — 진입 가드, `should_cancel` 클로저, `canceled` 분기
- `workers/batch_worker.py` — `canceled` 분기
- `batch_repository.py` — `TERMINAL_STATUSES` + `retry_failed_item` 이 `cancel_requested`
  를 비운다 〔#14〕 + **`find_item_by_job_id(job_id)` 신설**(취소 API 가 배치 아이템을
  찾는 수단, §2.5)
- `models/batch_ingestion.py` — 주석
- `config.py` — `use_jobs` 기본값
- `.env.example` · `.env.airgap.example`

**kb 프론트** — §2.7 의 8+3곳 + `api.ts` `cancelKbJob` + `JobList.tsx` 버튼
+ **`app/globals.css`** 에 `.badge.canceled` + `lib/types.ts:393` 문서 status 주석
+ `JobList.tsx:29-31` 주석("JobStatus 에 provider 가 노출되지 않으므로") — §2.9 이후 거짓이 된다
+ **`DocumentList.tsx:48-53` 기본 필터** — `HIDDEN = {failed, rejected}` 에 `canceled` 추가
  〔v6 검증〕. 안 넣으면 취소된 문서가 "실제 적재된 문서" 기본 목록에 계속 남는다
+ **`lib/types.ts:110-117` `BatchItemStatus`** 유니언에 `canceled`(§2.5-5 가 실제로 쓴다) 〔v4 검증 — 현재 queued/running/succeeded/
failed/gate_failed 계열만 있다〕
+ **`lib/types.ts` 의 `JobStatus` 인터페이스에 `provider` 추가** 〔§2.9 — 없으면 버튼
노출조건 `provider === 'kb_pipeline'` 이 타입에러〕

**문서** — `_workspace/` 에 취소 계약(진행 중 단계 완주 / `inserting` 이후 불가 /
취소 아이템 재수행 불가 → 재업로드)을 반영한다. 프로젝트 규칙상 필수 단계다.

**facade** — 변경 없음. `DELETE /jobs/{id}` 는 kb 가 부르지 않는다(§2.5)

---

## 6. 테스트

**pipeline**
- `IngestResult(status="canceled")` 가 **ValidationError 없이** 만들어진다 〔#11 회귀〕
- `should_cancel` 이 True 면 parse 를 **제출하지 않는다**
- chunk 직전 취소 → insert 가 제출되지 않는다
- 취소 결과가 `status="canceled"` 이고 **`delete_doc` 을 호출하지 않는다** 〔파괴적 삭제 회귀〕
- 같은 내용·다른 파일명 문서가 이미 `ready` 인 상태에서 취소해도 그 문서가 살아남는다
- `should_cancel=None`(기존 호출자) → 동작 불변
- **raganything tail 이 같은 헬퍼를 써도 동작 불변**(§2.2 회귀)
- 헬퍼가 취소를 `_fail`(failed) 아니라 `_canceled` 로 가른다

**worker**
- 진입 시 `cancel_requested` 면 `running` 으로 덮어쓰지 않고 **`canceled` 를 기록하고** 반환
  〔§2.4 — 멱등. 안 쓰면 running 고착 + 무한 폴링〕
- `result.status=="canceled"` → 잡·문서 상태 `canceled`
- `should_cancel` 클로저가 **새 세션**으로 다른 세션의 UPDATE 를 본다(stale 회귀)
- **`should_cancel` 이 fail-open 이다** 〔v4 blocking 회귀〕 — 잡 행이 삭제됐거나 DB 오류면
  예외를 밖으로 내지 않고 `False` 를 돌려준다. 실행 중 `DELETE /kb/{id}/jobs` 로 기록을
  지워도 적재가 죽지 않는다
- 진입 가드가 **문서 상태도 `canceled`** 로 만든다 〔v4 blocking 회귀 — `ingesting` 고착〕

**API**
- terminal 409 / `stage=='insert'` 409 / 타 KB 404
- **running 취소가 202 `cancel_requested` 다** 〔v5 blocking 회귀 — 409 가 아니다〕
- `queued` 취소는 202 `canceled`
- terminal 이면 409 이고 `cancel_requested` 가 **남지 않는다**(단일 UPDATE 라 0행)
- 배치 아이템 UPDATE 가 `processing` 을 덮지 않는다 〔v5 minor 회귀〕
- **`RETURNING='running'` 이면 아이템을 안 건드린다** 〔v6 blocking 회귀 — 건드리면
  claim 이 영영 안 되어 잡이 영구 running 으로 고착〕
- **`use_jobs=False` 여도 취소가 동작한다** 〔§4 — v2 의 501 규칙 폐기 회귀〕
- **provider != kb_pipeline → 409** 〔§2.9 회귀 — 조용한 무동작 방지〕
- `JobStatus` 응답에 `provider` 가 실린다
- `queued` 취소 → 즉시 `canceled`(+ 배치면 아이템도). **blob/staging 은 안 지운다**(§2.5)

**배치**
- 취소 아이템이 있어도 배치가 **`completed` 로 전이한다**(§2.6 회귀)
- 취소가 `failed` 로 덮이지 않는다
- **재수행이 `cancel_requested` 를 비운다** 〔#14 회귀 — 안 비우면 queued 고착 + 무한 폴링〕
- **queued 배치 항목 취소 → 아이템이 `canceled` 가 되어 워커가 애초에 집지 않는다**
  〔§2.5 핵심 회귀 — 안 하면 staging 부재 ValueError 로 `failed` 로 뒤집힌다〕
- 취소 후 `refresh_batch_status` 로 배치가 `completed` 로 전이한다

**프론트**
- `stage==='insert'` 면 버튼 미노출
- `canceled` 배지 렌더 + **잡 폴링 소비자 2곳이 멈춘다**(`JobList`·`JobProgressInline` — #21)
- **provider != kb_pipeline 이면 버튼이 안 뜬다** 〔§2.9〕

**마이그레이션** 〔v6 검증〕 — 테스트 하네스는 `Base.metadata.create_all` 로 스키마를
만들어서, 이번 변경에서 **운영에만 적용되는 유일한 산출물**(alembic 리비전)이 전 테스트를
통과해도 한 번도 실행되지 않는다. `alembic upgrade head` + `downgrade` 스모크를 수동으로
돌린다.

**회귀 기준선**: kb 기존 실패 19건, kbp 0건. 특히 `should_cancel` 의 `own` 가드가 없으면
kb_pipeline tail 을 타는 기존 테스트들이 공유 세션 close 로 무너진다 — 그게 첫 신호다.

---

## 7. 리스크

| | 리스크 | 완화 |
|---|---|---|
| | 취소했는데 진행 중 단계가 안 멈춘다 | **계약대로다**(§0.2). UI 문구로 명시 |
| | 취소 직후 잡이 잠깐 `running` 으로 되돌아 보인다 | 진입 가드를 이미 통과한 스레드가 `set_state(running)` 을 쓴다. 플래그로 다음 체크포인트에서 수렴한다. UI 문구가 "다음 단계부터 중단" 이므로 오해를 덜 산다 |
| | succeeded 잡에 `cancel_requested` 가 남는다 | insert 체크포인트 통과 직후 취소하면 202 를 받고도 잡은 succeeded 로 끝난다. 소비자가 가드·체크포인트뿐이라 무해하지만 청소 주체가 없다 — 별도 항목 |
| | TOCTOU — 확인 직후 취소가 커밋되면 그 단계는 제출된다 | 인정. 다음 단계에서 걸린다. **창이 밀리초는 아니다** — 그 단계가 parse(최대 35분)·chunk(90분)면 그만큼이다 |
| | `use_jobs` 기본 전환이 배포 동작을 바꾼다 | 라이브 전 구간 검증 완료. 롤백 레버 하나 |
| | `canceled` 어휘 누락으로 폴링이 안 멈춤 | 11곳을 §2.7 에 열거. 프론트 테스트로 고정 |
| | 취소된 배치 아이템 재수행 불가 | 의도. 재업로드로 해결(문서에 명시) |
| | provider 별 취소 지원 차이가 UI 에 드러난다 | `default_kb_provider` 기본값은 사실 `dify` 다(#19). 그래서 **서버 409 + 버튼 미노출** 두 층으로 막는다(§2.9) — "지원 안 하는데 버튼이 보이는" 상태를 안 만든다 |
| | 단건 `blob_store` 객체가 남는다 | **기존 상태**다 — 단건 경로엔 삭제 코드가 아예 없어 성공해도 남는다(`blob_store.delete` 는 배치 워커 한 곳뿐). 취소가 만든 문제가 아니므로 별도 항목 |
| | xlsx 게이트 parse 는 running 중 취소를 못 잡는다 | 파싱 1회분이고 **부작용 없음**(edgequake 미접촉). §2.2 에 명시 |
