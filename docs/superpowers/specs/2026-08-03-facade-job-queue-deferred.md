# facade 잡 큐 — Phase 1 범위 밖으로 뺀 항목

> 짝 문서: [`2026-08-03-facade-job-queue-design.md`](2026-08-03-facade-job-queue-design.md)
>
> ultracode 경쟁 검증(v1 23건 + v2 20건 + v3 20건)이 지적한 것 중, **Phase 1 목표에 필수가 아닌**
> 것을 여기 모았다. 전부 타당한 지적이지만 지금 대응하면 범위가 목표를 넘어선다.
>
> **Phase 1 목표**: facade 가 동시처리량·유량제어를 소유한다. kb 의 DB 폴링 큐를 kbp 로
> 가져온다. 그 이상은 하지 않는다.

## 판정 기준

Phase 1 에 넣는 것은 둘 중 하나다.

1. 없으면 **유량제어가 동작하지 않는다** (큐가 멈춤·상한이 안 지켜짐·처리량 퇴행)
2. 없으면 **기존 동작이 깨진다** (kb 파손·테스트 회귀·기동 실패)

나머지는 여기로 온다. "있으면 더 좋다"는 Phase 1 근거가 아니다.

---

## D1. 제출 멱등키 (`Idempotency-Key`) — ✅ **구현 완료 (2026-08-04)**

Phase 2 의 선행 조건이라 먼저 구현했다. 설계 §4.4 참조.

- `kbp.jobs.idem_key` + 부분 유니크 인덱스(`WHERE idem_key IS NOT NULL`).
- 충돌하면 새 잡을 만들지 않고 **기존 `job_id` 를 반환**한다.
- **failed·canceled 로 끝나면 키를 비운다**(`complete`/`cancel`/회수 UPDATE). 실패를
  캐시하면 설정을 고치고 같은 파일을 다시 올려도 옛 failed id 가 반환돼 영구 실패로
  굳는다.
- 자동 파생 키는 `sha256(kind+workspace+payload+file)` 에 **시간 버킷**을 붙인다
  (`KBP_JOB_IDEM_WINDOW_SECONDS`, 기본 300s). 재시도 버스트(수 초)는 같은 버킷에
  들어가고, 의도적 재요청(수 분 뒤)은 새 잡이 된다. 명시 헤더에는 버킷을 안 붙인다 —
  소비자가 수명을 정한 것이므로.
- **레거시 4경로는 멱등키를 쓰지 않는다.** 동기라 소비자가 최종 결과를 보고 재시도를
  판단하므로, 캐시가 끼면 "다시 파싱" 이 조용한 no-op 이 된다.
- 충돌 시 방금 올린 staging 객체는 즉시 삭제한다 — 어떤 행도 참조하지 않아 GC 가 없는
  지금(D2) 그냥 두면 영구 고아다.

## D2. TTL GC (완료 잡 + MinIO 객체 정리) — ✅ **구현 완료 (2026-08-04)**

> 설계: [`2026-08-04-job-queue-gc-design.md`](2026-08-04-job-queue-gc-design.md).
> `service/jobs/gc.py`(`run_ttl_gc`·`run_orphan_sweep`) + worker 전용 `kbp-gc` 스레드.
> 라이브 확인: TTL 삭제가 행+객체를 함께 지우고, 스윕이 고아 14건을 전량 수거하며,
> grace 안의 새 객체는 보존했다. 아래 원래 판단 근거는 기록으로 남긴다.
>
> **범위 주의**: 스윕은 `kbp-jobs/` 프리픽스만 본다. kb 가 쓰는 `parse-staging/` 은
> 대상이 아니다(D20).


**지적**: GC 가 참조 중인 선행 잡 결과를 지워 후속 잡을 깬다. 고아 staging 객체가
누적된다.

**왜 뺐나**: **GC 를 만들지 않으면 이 결함들이 존재하지 않는다.** 잡 행이 며칠 쌓이는
것은 무해하다(행 하나가 수 KB, 큰 것은 MinIO 로 나감). Phase 1 에서 실제 축적 속도를
관측한 뒤 만드는 게 옳다.

**Phase 1 이 대신 하는 것**: 무인증 legacy 경로(`/parse`·`/chunk`)의 staging 객체는
잡이 terminal 이 되는 즉시 삭제한다. 인증 없이 200MB 를 무한정 남길 수 있는 경로만
막는 최소 조치다(설계 §5.3).

**언제 필요해지나**: MinIO 볼륨 사용량이 관측 가능하게 늘거나, `kbp.jobs` 행이 수만 건이
될 때.

**설계 메모**: 지울 때는 `parent_job_id` 컬럼(설계 §2 에 이미 있음)만 보면 된다 —
`payload` jsonb 안을 뒤지면 payload 가 MinIO 로 오프로딩된 잡에서 참조가 안 보인다.
추가로 어떤 `jobs` 행도 참조하지 않는 `{prefix}/{uuid}/` 객체를 쓸어내는 orphan sweep 이
필요하다.

## D3. 업로드 스트리밍 (메모리 프로필)

**지적**: API 가 전량 read → MinIO put, worker 가 다시 전량 다운로드 → httpx 멀티파트로
또 한 벌. `concurrency=4` 면 파일 크기 × 4~8배가 상주한다.

**왜 뺐나**: `KBP_JOB_MAX_UPLOAD_BYTES`(200MB) 상한을 두면 최악 메모리가 **계산 가능해
진다**. 그게 Phase 1 에 필요한 전부다. 스트리밍은 `ParseSvcClient` 시그니처 변경
(`file_bytes` → 파일-like)을 동반해 파급이 크다.

**언제 필요해지나**: worker OOM 이 실제로 관측되거나, 200MB 상한을 올려야 할 때.

**설계 메모**: API 는 `UploadFile.file`(SpooledTemporaryFile)을
`put_object(..., length=size)` 로 흘리고, worker 는 `get_object` 스트림을 임시파일로
받아 `ParseSvcClient` 에 파일-like 를 넘긴다.

## D4. 전용 `lease_epoch` **컬럼** (세대 토큰 자체는 Phase 1 에 구현했다)

**지적**: stale 오판 시 좀비 worker 가 새 lease 의 결과를 덮어쓴다.

> **정정(v6)**: 이 항목의 최초 근거는 **틀렸다**. "`claimed_by` 술어만으로 충분하고,
> 같은 프로세스가 자기 잡을 재claim 하는 경우는 worker 가 하나일 때 발생하지 않는다"
> 고 썼는데 정반대다. worker 가 **하나뿐일 때** 회수된 잡을 다시 집는 주체는 필연적으로
> 그 하나뿐인 worker 이고, `worker_id` 는 프로세스 수명 동안 고정이라 술어가 좀비를
> 전혀 막지 못한다. dev·compose·airgap 모두 facade-worker 는 1개다.
>
> ```
> attempt 1 (스레드 A) 실행 중 → 회수 → queued
> 같은 worker 가 재claim → attempt 2 (스레드 B). claimed_by 동일
> 스레드 A 의 complete 가 술어를 통과 → attempt 1 결과로 종결
> 스레드 B 는 계속 진행 → edgequake 에 두 번째 문서 제출   ← 중복 적재
> ```
>
> 그래서 **세대 토큰은 Phase 1 에 구현했다** — 다만 새 컬럼 없이 기존
> `attempt_count` 를 세대로 쓴다. claim 이 `RETURNING attempt_count` 로 세대를 주고,
> 모든 잡 쓰기가 `AND attempt_count = $gen` 을 함께 검사한다(설계 §3.3).

**여전히 범위 밖인 것**: 전용 `lease_epoch` **컬럼**. `attempt_count` 로 충분하다 —
claim 마다 단조 증가하고, 회수 없이 세대만 바뀌는 경로가 없다.

**언제 필요해지나**: 재시도가 아닌 이유로 lease 만 교체해야 할 때(예: 잡을 중단 없이
다른 worker 로 이관). 그때는 `attempt_count` 가 세대와 시도 횟수 두 의미를 겸할 수 없다.

## D5. insert 재시도 멱등 처리 — ✅ **구멍 봉합 (2026-08-05)** / 전체 멱등화는 보류

> Phase 1 의 전제("insert 는 재시도하지 않으니 중복 적재가 없다")에 **구멍이 있었다.**
>
> - `insert` kind 는 `max_attempts=1` 이 맞다. 그런데 **`ingest` kind 는 3** 이고
>   내부에서 `insert_chunks` 를 부른다.
> - `_recover`(회수 경로)는 `stage='inserting'` 을 최우선 분기로 막고 있었지만,
>   `requeue`(runner 예외 경로)는 **stage 를 무조건 지우고 `queued` 로 되돌렸다.**
> - `insert_chunks` 는 제출 후 완료를 폴링한다. 제출은 됐는데 폴링에서 5xx·타임아웃이
>   나면 `classify` 가 **재시도 가능**으로 분류한다(§5.1). 그래서 ingest 가 parse 부터
>   다시 돌며 같은 문서를 또 제출했다. `insert` kind 는 중복 대신 아무도 안 집는
>   `queued` 좀비가 됐다(D13 의 한 갈래).
>
> **고친 것**: `requeue` 가 `_recover` 와 같은 규칙을 쓴다 — `stage='inserting'` 이면
> `failed`(+`completed_at`, `idem_key` 비움), 아니면 종전대로 `queued`. 두 경로의 대칭을
> 테스트로 고정했다(`test_insert_stage_guard_holds_on_both_paths`).
>
> **여전히 보류**: 아래 원안(submit/poll 분해 + `resume_document_id`·`should_cancel`·
> `on_submitted` seam)은 그대로 범위 밖이다. 지금은 "중복 적재 대신 실패" 이지
> "재시도해도 안전" 이 아니다. insert 자동 재시도를 원하게 될 때 seam 3종을 함께 넣는다.


**지적**: `EdgequakeClient.insert_chunks()` 는 호출마다 새 문서를 제출한다
(`service/edgequake.py:379`). 멱등키가 없어 재시도가 중복 적재다. 제대로 하려면
`insert_chunks` 를 submit/poll 로 분해하고 `resume_document_id`·`should_cancel`·
`on_submitted` seam 을 추가해야 한다.

**왜 뺐나**: Phase 1 은 **insert kind 를 재시도하지 않는다**(`max_attempts=1`).
중복 적재의 원인이 재시도이므로 재시도를 없애면 원인이 사라지고, `edgequake.py` 를
건드릴 필요도 없다. insert 실패는 소비자가 다시 호출하면 된다 — 현행과 같다.

**대가**: 일시적 5xx 로 실패한 insert 가 자동 복구되지 않는다. 현행도 그렇다(회귀 아님).

**언제 필요해지나**: insert 자동 재시도를 원하게 될 때. 그때 seam 3종을 함께 넣는다.

**설계 메모**: 부분 기록에 `document_id`·`track_id` 와 **그 문서에 넣은 chunk_texts
해시**를 남긴다. 해시가 다르면(= ingest 재시도로 청크가 바뀜) 새로 제출해야 한다.
폴링 재개는 문서가 아직 non-terminal 일 때만이고, terminal failed 면 재제출한다.

## D6. insert / parse / chunk 의 취소 반응성

**지적**: 단일 다운스트림 호출 잡은 취소 경계가 없어 최대 타임아웃만큼(parse 1800s,
chunk 3600s, insert 1200s) 무동작이다.

**왜 뺐나**: 취소는 Phase 1 의 목표가 아니다. `queued` 잡의 즉시 취소만으로 "잘못 올린
배치를 멈춘다"는 실사용은 충족된다. `running` 잡의 즉시 중단은 `edgequake.py`·
`parse_client.py` 에 취소 훅을 뚫어야 한다.

**Phase 1 동작**: `queued` → 즉시 `canceled`. `running` → `cancel_requested=true` 만
세우고, ingest 는 단계 경계에서, 나머지는 진행 중 호출이 끝난 뒤 중단.

## D7. ingest 의 다중자원 기아 (aging / 예약)

**지적**: ingest 는 parse·chunk·insert 세 버킷이 **동시에** 비어야 승인된다. parse·chunk
잡이 꾸준하면 그 순간이 오지 않아 ingest 가 무기한 대기한다. 반대로 ingest 는 parse
구간 내내 chunk/insert 버킷을 잡아 단독 chunk/insert 를 굶긴다.

**왜 뺐나**: aging/예약 로직은 claim 알고리즘의 복잡도를 크게 올린다. Phase 1 의 실제
트래픽은 kb 가 보내는 **단계별 호출(parse→chunk→insert)** 이 대부분이고 `/ingest` 는
one-shot 소비자용이라 혼재가 드물다.

**Phase 1 이 대신 하는 것**: 사실을 문서화한다 — "동시 ingest 상한 = min(버킷 상한) = 2"
이고 parse/chunk 부하가 지속되면 ingest 가 지연될 수 있다(설계 §3.5).

**언제 필요해지나**: `/ingest` 와 단계별 호출이 실제로 섞여 들어오는 소비자가 생길 때.

**설계 메모**: 최장 대기 후보가 ingest 면 그 잡이 필요한 버킷에 대해 뒤 후보의 신규
승인을 보류(reserve)하고, 대기 임계를 넘으면 우선 승인한다.

## D8. airgap 배포 검증 스크립트 수정

**지적**: `scripts/airgap/load-and-up.sh` 의 `CHECKS` 배열은 헤더 주입도 본문 조건도
표현할 수 없어 `/jobs/workers` 의 `online:true` 를 검증할 수 없다. 게다가 현행 배열의
호스트 포트가 airgap compose 매핑과 어긋난다.

**왜 뺐나**: 폐쇄망 배포 시점의 작업이고, Phase 1 은 dev 에서 동작 검증을 마치는 것이
목표다. 지금 고치면 검증할 대상이 없는 스크립트를 고치는 셈이다.

**언제 필요해지나**: 폐쇄망 배포를 실제로 수행하기 전.

**설계 메모**: `CHECKS` 배열이 아니라 별도 단계로
`podman exec <facade> curl -fsS -H "X-Facade-Key: $KBP_FACADE_KEY" localhost:19000/jobs/workers`
를 파싱한다(컨테이너 내부 포트라 호스트 매핑과 무관). 기존 `CHECKS` 의 호스트 포트
불일치도 함께 정정한다. `verify-bundle.sh` 는 `IMAGES`·`REQUIRED_ENV` 양쪽 다 변경
불필요하다(`kbp-facade` 태그 재사용, MINIO 키는 `:31` 에 이미 등록).

## D9. U+0000 정제

**지적**: Postgres `jsonb`/`text` 는 NUL 을 저장할 수 없어 파서 출력에 섞이면 `22P05`
하드 실패다.

**왜 뺐나**: 실제 발생 사례가 없다. 현행 파이프라인도 `enriched_content` 를 그대로 kb 의
Postgres 에 넣고 있어(kb `documents`/`chunks_meta`) 이미 같은 제약 아래 돌아간 지 오래다.

**언제 필요해지나**: `22P05` 가 실제로 관측될 때. 그때 `blobs.py` 직렬화 진입점 한 곳에
정제를 넣는다.

## D10. `/communities/build` 를 큐로 편입

**지적**: 설계 §1 의 "API 프로세스는 다운스트림을 호출하지 않는다" 가 첫날부터 예외를
갖는다 — `/communities/build` 는 `BackgroundTasks` 안에서 edgequake·LLM 호출 + DB 직결
쓰기를 한다(`service/app.py:343-362`). `-w 8` 이면 이런 백그라운드 작업이 최대 8배로
늘고 재기동 시 조용히 유실된다.

**왜 뺐나**: 이 경로는 문서 적재 유량과 무관하고(커뮤니티 빌드는 적재 후 별도 트리거),
호출 빈도가 낮다. 큐 편입 자체는 runner 하나 추가라 비싸지 않지만, Phase 1 의 검증
표면을 넓힌다.

**Phase 1 이 대신 하는 것**: 불변 규칙 1 에 "알려진 예외"로 명시한다(설계 §1).

**언제 필요해지나**: Phase 2. 큐가 안정화되면 runner 하나 추가로 편입한다.

## D11. `/healthz` 를 async 로 전환

**지적**: 현행 `/healthz` 는 동기 `def`(`service/app.py:107-109`)라 대기 핸들러와 같은
AnyIO 스레드풀을 공유한다. 스레드풀이 고갈되면 `/healthz` 도 큐에 갇혀 compose
healthcheck 가 unhealthy 로 넘어간다.

**왜 뺐나**: Phase 1 은 **waiter 상한**으로 스레드풀 고갈 자체를 막는다(설계 §4.5).
고갈되지 않으면 `/healthz` 를 바꿀 이유가 없다. 엔드포인트 하나를 async 로 바꾸는 건
싸지만, "왜 이것만 async 인가"라는 일관성 부채가 남는다.

**언제 필요해지나**: waiter 상한을 크게 올리거나 스레드풀 고갈이 실제로 관측될 때.

## D12. airgap `KBP_FACADE_KEY` 필수화 — ✅ **구현 완료 (2026-08-05)**

> 네 곳을 함께 바꿨다.
> - `service/app.py` — 게이트가 빈 문자열·**공백뿐인 값**도 미설정과 동일 취급한다.
>   공백을 진짜 키로 보면 게이트 대상 전 경로가 401 이 되어 스택이 전면 정지한다.
>   값을 strip 해서 쓰지는 않는다(소비자가 자기 env 값을 그대로 보내므로 양쪽이 같은
>   문자열이어야 한다). 기동 경고도 같은 기준으로 바꿔 `blank`/`unset` 을 구분한다.
> - `docker-compose.airgap.yml` · `docker-compose.yml` — `x-facade-env` 에
>   `KBP_FACADE_KEY: ${KBP_FACADE_KEY:-}`. facade 와 facade-worker 가 같은 앵커를 쓴다.
> - `.env.airgap.example` — A-0 블록 신설(kb 스택 값과 일치해야 함, `openssl rand -hex 32`).
> - `scripts/airgap/verify-bundle.sh` — `REQUIRED_ENV` 에 등록. **비면 배포 전에 막힌다.**
>
> 즉 코드는 관대하게(기동 실패 없음), 배포는 엄격하게(빈 값이면 검증 실패) 나눴다.
> 아래 원래 판단 근거는 기록으로 남긴다.


**지적**: airgap facade 블록에 `KBP_FACADE_KEY` 가 없어 게이트가 no-op 이다. 무인증으로
`/ingest`·`/insert`·`/search`·`/chunks`·`/doc` 가 열려 있고, 호스트 포트로 노출된다.

**왜 뺐나**: 이 키를 도입하려면 **세 곳을 함께** 바꿔야 하는데 어느 하나만 해도 기존
동작이 깨진다.

- compose 에 `${KBP_FACADE_KEY}` 를 그냥 넣으면 **빈 문자열**이 주입된다. 게이트는
  `os.environ.get("KBP_FACADE_KEY") is None` 으로만 비활성화되므로(`service/app.py:69,79-81`)
  `""` 는 게이트 **ON** 이고, 게이트 대상 엔드포인트 전부가 즉시 401 이 된다.
- `${KBP_FACADE_KEY:?}` 로 쓰면 반대로 스택이 기동조차 못 한다.

즉 (a) `.env.airgap.example` 에 항목 추가, (b) `scripts/airgap/verify-bundle.sh:31` 의
`REQUIRED_ENV` 에 등록, (c) `app.py` 의 게이트를 `if not _FACADE_KEY:` 로 바꿔 빈 문자열을
미설정과 동일 취급 — 이 셋을 한 번에 해야 한다. 폐쇄망 보안 정책 결정이 섞여 있어
유량제어 작업에 끼워 넣을 일이 아니다.

**Phase 1 동작**: airgap 의 `KBP_FACADE_KEY` 를 건드리지 않는다(현행 유지 = 게이트 no-op).
신규 `/jobs/*` 도 같은 게이트를 쓰므로 airgap 에서는 함께 무인증이다.

**언제 필요해지나**: 폐쇄망 배포 보안 검토 시. D8(배포 검증 스크립트)과 같은 시점에
묶어서 하는 게 자연스럽다.

---

## GC(D2) 검증에서 나온 범위 밖 항목 (2026-08-04)

[`2026-08-04-job-queue-gc-design.md`](2026-08-04-job-queue-gc-design.md) v1 검증에서
타당하지만 GC 범위를 넘는다고 판정된 것들. GC plan §7 에도 요약돼 있고 여기 상세를 남긴다.

### D13. 소진 후 `queued` 로 정체하는 좀비 행

**지적**: `worker._execute` 는 `JobRetryable` 이면 `attempt_count` 를 유지한 채 `requeue`
하는데, `attempt_count >= max` 인지 보지 않는다. 그러면 `_candidates` 가
`attempt_count < max` 로 그 행을 영구 배제해 **`queued` 인 채 아무도 안 집는 좀비**가
된다. terminal 이 아니라 GC 대상도 아니고, 자식이면 부모의 TTL 삭제까지 영구 차단한다.

**왜 범위 밖**: 큐 본체(claim/requeue) 설계 결함이지 GC 책임이 아니다. GC 는 §2.3 에서
`NOT EXISTS` 를 `LIMIT` 안으로 옮겨 **정체는 피하도록** 했다(head-of-line 방지).

**고칠 곳**: `worker._execute` 의 `JobRetryable` 분기에서 `attempt_count >= max` 면
`requeue` 대신 `failed` 로 종결. 또는 claim 유지보수에 "소진된 queued 행 종결" 추가.

### D14. 멱등 dedupe 수명 = `KBP_JOB_TTL_HOURS`

**지적**: `succeeded` 잡이 TTL 로 지워지면 `idem_key` 도 함께 사라져, 명시
`Idempotency-Key` 로 72h 뒤 재제출하면 새 잡이 된다. 설계 §4.4 는 "명시 헤더는 소비자가
수명을 정한다" 고 적어 두 서술이 어긋난다.

**왜 범위 밖**: 72h 뒤 같은 문서 재업로드가 새 잡이 되는 것은 대체로 의도된 동작이다.
계약 재정의는 잡 큐 설계 §4.4 소관.

**할 일**: §4.4 에 "멱등 dedupe 수명 = TTL" 각주 한 줄.

### D15. 부모가 TTL 로 지워진 뒤 자식이 참조하는 창

`_validate_parent`(접수)와 자식 INSERT 사이에 GC 가 부모를 지우면 `parent_job_id` 가
dangling 이 된다. 결과는 자식 잡의 명시적 `JobFailed`(§5.1 "참조 잡 소실") — 무결성
훼손도 중복 적재도 없다. TTL(72h) 경과 부모를 새로 참조하는 것 자체가 드물어 수용한다.

### D16. 같은 MinIO 버킷+프리픽스를 공유하는 두 배포

서로 다른 `kbp.jobs` DB 를 보면서 같은 버킷·프리픽스를 쓰면 스윕이 서로의 살아있는
staging 을 지운다. 기본값 조합(호스트 dev `localhost:9000`/`:5433`, compose
`minio:9000`/`postgres:5432`)은 분리돼 있지만, airgap 문서가 외부 공유 MinIO 전환을 정식
절차로 안내하고 버킷·프리픽스 기본값은 배포 불변이다.

**왜 범위 밖**: 배포 토폴로지 문제다. GC 의 fail-closed·sanity 가드와 "이 프리픽스를
단독 소유한다" 전제로 실질 위험을 덮는다.

**할 일**: 스윕을 opt-in env(`KBP_JOB_ORPHAN_SWEEP_ENABLED`)로 둘지는 폐쇄망 배포 검토
시점에 결정한다(D8 과 같은 시점).

### D17. GC 관측 — 연속 실패 ERROR 승격 / `last_gc_at` 노출

GC 가 조용히 안 도는 상태를 감지할 수단이 없다. 로그로 시작하고, 주기 상태를 DB 에
두게 되면(`kbp.job_gc_state`) 관측 수단이 자연히 생기므로 그때 `/jobs/workers` 에
`last_gc_at` 을 얹는 것을 재검토한다.

### D18. `_purge_legacy_inputs` 가 참조 컬럼을 비우지 않는다

`legacy=true` 잡의 staging 을 즉시 지운 뒤 `input_ref`/`payload_ref` 를 NULL 로 안 비워,
TTL GC 가 이미 없는 키를 다시 지우며 WARN 오탐이 상시 발생한다. 기능적으로는 무해(멱등).
"존재하지 않는 객체 삭제는 WARN 을 남기지 않는다" 정도로 구현 시 처리한다.

### D19. facade `/gate/check` — pdf·docx 게이트 (전 포맷 multipart) — ❌ **안 함 (2026-08-05 결정)**

> 사용자 결정: **적용할 룰이 아직 정해지지 않았다. 지금은 비활성이 맞다.**
> pdf·docx 게이트를 켜지 않는다. 아래 분석은 룰이 확정될 때 다시 볼 근거로 남긴다.
>
> **함께 확인한 사실 — 룰 체계가 둘이다.**
>
> | | 룰 id | 상태 |
> |---|---|---|
> | 엑셀 게이트(현행) | `conflicting_code_mapping`·`empty_header`·`header_leak`·`ref_error`·`unclear_header`·`unmerged_table_banners` | ✅ 돈다. parse-svc `gate_summary` → doc_guard `/v1/check-excel` |
> | multipart `/v1/check` | `3.1`~`6.3b` 14종 | ❌ 호출부 0건 |
>
> 그래서 kb 의 `excel_gate_default_disabled_rules = ["3.1","3.2","3.3","6.1"]` 은
> **죽은 카탈로그의 id** 였다. `ExcelCheckRequest` 는 `{filename, gate_summary}` 뿐이라
> `disabled_rules` 를 받지도 않는다 — 배선했어도 판정이 안 바뀐다. "셀병합·취소선을 껐다"
> 고 읽히지만 실제로는 그 룰들이 그대로 도는 상태였다. 제거했다(kb `75e8c74`).
>
> 프론트 룰 선택 패널도 이미 없다(`disabledRules={[]}` 고정). 즉 UI→백엔드→doc_guard 가
> 전 구간 죽어 있었다. 운반 경로(폼 → 잡 payload → `GateOptions`)만 남겼다 — 룰이 정해지면
> 그 자리에 붙인다.


doc_guard 는 엑셀 전용이 아니다. 실측(2026-08-04, `:8001/v1/rules`) 14개 룰 중
docx 13 · pdf 11 · xlsx 10 이고, 3.1 띄어쓰기 · 3.2 특수문자 · 3.4 생략표현 ·
5.1/5.2 금액 · 3.6 별지는 세 포맷 공통이다. 엔드포인트도 둘이다 —
`POST /v1/check`(multipart 원본, 전 포맷) 와 `POST /v1/check-excel`(JSON gate_summary).

그런데 소비자는 `check_excel` 만 부른다. kb `pipeline.py:504` 의 게이트 분기가
`ext in ("xlsx","xlsm") and provider=="kb_pipeline"` 이고, multipart 를 쓰는
`DocGuardClient.check()` 는 정의만 있고 **호출부가 없다**(dead code). 즉 pdf·docx 는
게이트를 통과하는 게 아니라 아예 거치지 않는다.

**왜 범위 밖**: 이번 범위는 "kb 가 doc_guard 를 직접 부르지 않게 한다"는 **전송 경로
은닉**이다. pdf·docx 게이트를 켜는 건 새 기능이고, 판정 실패 시 문서를 rejected 로
떨어뜨리는 정책·팝업 문구·기존 적재분 소급 여부까지 따라온다.

**할 일**: pdf·docx 게이트를 실제로 켤 때 `/gate/check` 패스스루를 함께 설계한다.
설계 시 짚을 것 — (a) multipart 원본 전송이라 `/gate/check-excel`(JSON) 과 성격이
다르다(파일 바이트가 facade 를 통과한다), (b) `/v1/check` 는 LLM 룰을 포함해 지연이
초 단위이므로 잡 큐 kind 로 넣을지 동기로 둘지 결정이 필요하다, (c) 엑셀은 파서가
계산한 `gate_summary` 로 판정하는데 pdf·docx 는 doc_guard 가 원본을 직접 파싱하므로
파싱이 두 번 일어난다.

### D20. `parse-staging/` 누적 — 미리보기 이탈분 + 배치 차단분 — ✅ **구현 완료 (2026-08-05)**

> **kb 가 지우고, facade 가 남은 걸 나이로 쓸어낸다.**
>
> kb — 적재 시 네 종류를 모두 지운다(`routers/kb.py`). 예전엔 `original`·`sidecar` 만
> 지워 `chunk_preview`·`preview_latest` 가 남았다. `chunk_preview` 는 **preview_session_id**
> 로 키가 잡히므로 `preview_latest` 포인터를 읽어 실제 키를 찾는다. 배치는
> `status == "succeeded"` 면 지운다(`and canonical_path` 조건 제거 — 승격이 실패한
> 성공 항목의 원본이 남던 구멍).
>
> **배치 실패분은 일부러 남긴다.** 재수행(`POST /batches/{id}/items/{id}/retry`)이 이
> 객체를 그대로 다시 쓰고 없으면 409 로 거절한다(`routers/batches.py:333`). 처음엔
> "terminal 이면 성패 무관 삭제" 로 고쳤다가 재수행이 통째로 죽는 걸 발견하고 되돌렸다.
>
> facade — `service/jobs/staging_gc.py` 신규. 잡 큐 GC 와 **프리픽스도 판정도 다르다**:
> 참조가 kb DB 에 있어 "행이 없으면 고아" 를 쓸 수 없고 **나이로만** 지운다. 두 갈래에
> 다른 TTL 을 준다 — preview `KBP_STAGING_TTL_SECONDS`(1h), batch
> `KBP_STAGING_BATCH_TTL_SECONDS`(7d, 재수행 창). 모르는 키 형식은 건드리지 않는다.
> worker 의 GC 스레드가 고아 스윕과 같은 주기로 호출한다.
>
> 실행 결과(2026-08-05): 323건 → 3건, **320건 회수**. 남은 3건은 재수행 창 안의 배치 원본.
>
> 아래 원래 분석은 기록으로 남긴다.


실측(2026-08-05, `document-parser` 버킷): `parse-staging/` 아래 **323건 214.7MB**,
2026-07-07 ~ 08-04 에 걸쳐 쌓였다. 원인이 둘이다.

| 갈래 | 건수 | 용량 | 원인 |
|---|---|---|---|
| 미리보기 이탈 | 318 | 214.2MB | `routers/kb.py:469-470` 이 **적재 큐잉 시점에만** `original`·`sidecar` 를 지운다. 미리보기만 하고 적재를 안 누르면 영구히 남는다 |
| 배치 차단·실패 | 5 | 0.5MB | `workers/batch_worker.py:217` 이 `status == "succeeded" and canonical_path` 일 때만 지운다. 게이트 차단·실패 항목은 남는다 |

용량의 95% 는 미리보기 이탈분이다(`original` 107건 203.8MB). 배치 차단분은 부수적이다.

facade GC 는 이걸 못 치운다 — 고아 스윕이 `kbp-jobs/` 프리픽스만 보고, `parse-staging/`
객체는 `kbp.jobs` 행이 아니라 kb 의 `documents`/배치 테이블이 수명을 안다.

**왜 범위 밖**: 수명 주인이 kb 다. facade 가 남의 프리픽스를 TTL 로 쓸면 진행 중인
미리보기 세션을 지운다 — "누가 아직 쓰는가" 를 facade 가 알 수 없다.

**할 일**(둘 중 하나, 또는 병행):
1. kb 가 지운다 — 배치는 terminal 이면 성공·실패 무관하게 staging 삭제, 미리보기는
   세션 만료 시 정리. 수명을 아는 쪽이 지우는 게 옳다.
2. facade 가 지운다 — `parse-staging/` 전용 TTL(미리보기 세션 수명보다 넉넉하게, 예
   7일)을 별도 env 로 두고 스윕 대상에 추가. kb 를 안 고쳐도 되지만 TTL 이 너무
   짧으면 살아있는 세션을 깬다.

먼저 (1)이 맞고, (2)는 이미 쌓인 214MB 를 한 번 걷어내는 용도로 유용하다.
