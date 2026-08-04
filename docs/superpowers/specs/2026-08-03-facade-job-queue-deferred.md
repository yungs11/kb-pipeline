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

## D1. 제출 멱등키 (`Idempotency-Key`)

**지적**: kb 클라이언트가 429/5xx 를 재시도하므로(`kb_pipeline_client.py:126-142`) 잡
제출이 중복 생성된다. 자동 파생 키는 terminal 실패까지 캐시해 재파싱을 막는다.

**왜 뺐나**: Phase 1 에서 기존 4경로는 **동기 계약을 유지**한다(잡 제출 후 완료까지 대기).
즉 kb 는 여전히 최종 결과를 보고 재시도를 판단하며, 잡 제출 자체를 재시도하는 소비자가
존재하지 않는다. 신규 `/jobs/*` 는 Phase 2 전까지 소비자가 없다.

**언제 필요해지나**: Phase 2 에서 kb 를 `/jobs/*` 로 옮기는 순간. 그때 D1 은 **선행
조건**이다 — 옮기기 전에 반드시 구현한다.

**설계 메모**: 유니크 인덱스를 `WHERE status IN ('queued','running','succeeded')` 로
좁혀 failed/canceled 와 충돌하면 새 잡을 만든다. 자동 파생 키는 짧은 재시도 창(~300s)
에만 유효하게 하고, 장기 재사용은 명시 헤더가 있을 때만 허용한다.

## D2. TTL GC (완료 잡 + MinIO 객체 정리)

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

## D5. insert 재시도 멱등 처리

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

## D12. airgap `KBP_FACADE_KEY` 필수화

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
