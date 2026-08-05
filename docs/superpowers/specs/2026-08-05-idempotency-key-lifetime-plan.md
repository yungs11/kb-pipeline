<!-- plan-version: v7 -->
<!-- ultracode-validation: READY v7 at 2026-08-05T08:51:19Z -->

# 멱등키 수명 수정 (C) + 커뮤니티 빌드 큐 편입 (B)

> **범위 축소 — 취소(A)를 이 plan 에서 뺀다.**
>
> v1~v3 검증에서 취소는 라운드마다 새 blocking 이 나왔다(12 → 10 → 9). 마지막 라운드가
> 드러낸 것은 "축소범위" 로도 배선이 부족하다는 것이다 — 워커 진입 취소 가드, pipeline 의
> `job_id`/`session_factory` 운반, `status='canceled'` 를 **쓰는 주체**, `_bad()` 로 값을
> 반환하는 3개 지역 핸들러, queued 취소분의 blob 정리, 문서·배치아이템 상태 어휘까지
> 전부 신규다. 한 plan 에 묶어두면 **C 가 A 에 인질로 잡힌다.**
>
> C 는 지금 **조용한 데이터 손실**이고 사용자가 최우선으로 지정했다. 여기서 C+B 만
> 끝내고, A 는 별도 plan 으로 뺀다(§6).

> **v3 → v4 에서 뒤집은 것**: v3 §2.1 의 처방 (a)시간버킷·(b)succeeded 키 비우기를
> **둘 다 폐기**했다. 검증이 반증을 냈고 코드로 확인했다(§2.2).
>
> **v6 → v7**: 설계 동일. v6 검증의 blocking(테스트 호출부 누락)과 심볼 오기를 고쳤고,
> 범위 밖 2건을 deferred D21·D22 로 넘겼다(`fea526d`).
>
> **v5 → v6**: 설계는 그대로. v5 검증이 잡은 **정밀도 결함**을 고쳤다 — 존재하지 않는
> 심볼(`repo.start`), 빠뜨린 파급(Protocol + 테스트 더블 3개), 테스트가 skip 으로 공허히
> 통과하는 조건, 인자명 충돌.
>
> **v4 → v5 에서 또 뒤집은 것**: v4 는 parse·insert 키를 **함께** `document_id` 로 바꾸려
> 했는데, parse 호출부 3곳 중 **2곳은 documents 행이 존재하기 전에** 돈다(§2.4). 그리고
> parse 캐시는 애초에 데이터 손실이 아니다. → **insert 키만 고친다.** community 는 성공이
> 아니라 **claim 시점**에 키를 비운다(§2.3).

---

## 1. 문제

세 층이 겹쳐서 멱등키가 "재시도 합치기" 가 아니라 **최대 72h 영구 캐시**가 된다.

```
① kb 가 보내는 키가 결정적이다        kb-insert:{ws}:{doc_id}   doc_id = content_hash[:16]
                                      kb-parse:{docs_id}        ← KB 스코프도 없음
② facade 는 명시 키에 시간버킷을 안 붙인다   jobs/api.py:114  if explicit: return f"h:{explicit}"
③ complete 는 succeeded 면 키를 보존한다     jobs/repo.py:578
④ submit 은 충돌 시 **상태와 무관하게** 기존 job_id 를 돌려준다   jobs/repo.py:188
```

**증상 C-1 (데이터 손실)** — 문서를 지우고 같은 파일을 다시 올리면:
같은 content_hash → 같은 키 → facade 가 옛 succeeded insert 잡을 반환 → kb 가 그 **옛
결과**를 읽어 문서를 `ready` 로 만든다. **edgequake 에는 아무것도 없다.** 72h 동안 그렇다.

**증상 C-2 (교차 KB, 이번 범위 밖)** — `kb-parse:{docs_id}` 는 KB 스코프가 없어 같은
파일을 두 KB 가 올리면 parse 잡 하나를 공유한다. 다만 parse 결과는 내용 결정적이라 공유가
**틀리지 않는다**(페이지 이미지 키도 같은 `docs_id` 기반이다). 손실이 아니므로 §2.4 대로
건드리지 않는다.

**증상 B** — `/communities/build` 는 `community:{eq_ws}` 를 직접 넘긴다. 첫 빌드가 성공하면
그 workspace 의 재빌드가 72h 동안 no-op 이다.

> **문서 중복 검사와 무관하다.** 문서 중복은 kb 가 자기 DB 의 `content_hash` 로 판정하고
> (`pipeline.py:577` 같은 KB·같은 파일명·같은 해시·`ready` → 스킵, `batches.py:210` 배치
> 중복), facade 를 부르기 **전에** 끝난다. 이 수정은 그 경로를 건드리지 않는다.

---

## 2. 처방

### 2.1 C — **insert 키만** 작업 단위로 바꾼다

```
kb-insert:{workspace_id}:{doc_id}   →  kb-insert:{document_id}
kb-parse:{docs_id}                  →  그대로 둔다 (§2.4)
```

인자 이름은 **`kb_document_id`** 로 한다. `insert()` 안에서 `document_id` 는 이미
**edgequake 가 발급한 문서 id** 를 가리키므로(`client.py:344`), 같은 함수에서 한 이름이
두 id 공간을 가리키면 안 된다 〔v5 검증 minor〕.

값은 kb `documents` 행의 UUID 다. 호출부(`pipeline.py:2269`)는
`_ingest_kb_pipeline_tail(..., rec: DocumentRecord, ...)` 안이라 **`rec.document_id` 가
이미 스코프에 있다**.

**왜 이게 맞는가** — 멱등키가 답해야 하는 질문은 *"이게 같은 내용인가"* 가 아니라
*"이게 **같은 작업의 재시도**인가"* 다.

| 상황 | 예전(내용 해시) | 지금(작업 단위) |
|---|---|---|
| 5xx 재시도 (`_request` max 3회) | 같은 키 → 합침 ✅ | 같은 document_id → 합침 ✅ |
| arq 태스크 재시도(`max_tries`) | 같은 키 → 합침 ✅ | 같은 document_id → 합침 ✅ |
| `recover_stale`(processing→queued 회수) | 같은 키 → 합침 ✅ | 같은 document_id → 합침 ✅ |
| `retry_failed_item`(실패 아이템 재수행) | 같은 키 → 합침 ✅ | 같은 document_id → 합침 ✅ |
| **문서 삭제 후 같은 파일 재업로드** | 같은 키 → **옛 결과 반환(손실)** ❌ | **새 문서 행 → 새 UUID → 재적재** ✅ |

네 재실행 경로가 전부 **같은 `documents` 행**을 재사용하므로 합치기가 유지된다
(`batch_repository.retry_failed_item` 은 `item.document_id` 를 그대로 쓰고,
`batch_repository.recover_stale` 은 아이템 상태만 되돌린다 — 후자는 facade 의
`JobRepo._recover` 와 이름만 같고 다른 것이다).

**동작 변화 하나 — 같은 KB · 같은 내용 · 다른 파일명** 〔v5 검증 minor〕:
옛 키는 내용 해시라 두 문서를 **한 insert 잡으로 병합**했다. 새 키는 병합하지 않으므로
edgequake 문서가 하나 더 생긴다. 데이터 손실은 아니고 **오히려 옳다** — 사용자가 다른
이름으로 올린 두 문서다(kb 중복검사도 `kb_id+file_name` 기준이라 이 조합을 안 막는다).
facade 는 `doc_id` 를 edgequake 식별자로 쓰지 않고 title 폴백으로만 쓴다.

재업로드가 새 `document_id` 를 받는 근거: 문서 삭제는 **hard delete** 다
(`repositories.py` `delete_document_rows` → `sa_delete(Document)`). 업로드 라우터는 파일마다
새 `Document` 행을 만들고(`routers/kb.py:229-241`), 파이프라인의 "재사용" 은 그 새
placeholder 를 갱신하는 것이다. 내용이 같고 기존 문서가 `ready` 면 **facade 를 부르기
전에** 스킵된다(`pipeline.py:577`).

### 2.2 v3 의 두 처방을 폐기한 이유 〔v3 검증 #5·#6〕

**(a) 명시 키에 시간버킷 — 폐기.**
insert 잡의 최대 수명은 6600s 인데 버킷 폭은 300s 다(22배). **살아있는 insert 잡과의 병합**
(원래 의도한 동작)이 5분 뒤 깨져 두 번째 insert 잡이 생기고, edgequake 에는 멱등키가 없어
**중복 문서**가 된다. 고정윈도우 경계를 5xx 재시도가 넘는 경우도 같다.

**(b) `succeeded` 도 키 비우기 — 폐기.**
"terminal 이면 끝났으니 안전하다" 는 HTTP 재시도만 본 판단이었다. kb 에는 **파이프라인 레벨
재실행이 3종**(arq `max_tries`, `batch_repository.recover_stale`,
`batch_repository.retry_failed_item`) 있고, 재실행은 같은
content_hash → 같은 doc_id → 같은 키를 낸다. 지금은 그 키가 옛 succeeded insert 잡을
돌려줘 **edgequake 재적재를 막고 있다**. 키를 비우면 재실행마다 새 insert 잡이 생겨
D1 이 막으려던 중복 적재가 되살아난다.

→ **facade 의 키 수명 규칙(ingest kind)은 그대로 둔다.** 고치는 곳은 kb 의 insert 키
재료 하나다.

### 2.3 B — 커뮤니티는 **claim 시점**에 키를 비운다 〔v4 검증〕

`/communities/build` 는 `derive_idem_key` 를 거치지 않고 `submit_job` 에 키를 직접 넘기므로
어떤 버킷 처방도 적용되지 않는다.

**v4 의 "성공 시 비우기" 는 회귀다.** 빌드가 도는 동안(최대 7200s) 들어온 트리거가 전부 그
running 잡으로 흡수되는데, 그 잡은 **시작 시점의 그래프만** 본다. 10건 배치에서 1번 문서가
빌드를 시작하면 2~10번 트리거가 흡수되어 **2~10번 엔티티는 다음 업로드까지 커뮤니티가
없다.** 기존 BackgroundTask 는 트리거마다 독립 빌드라 마지막 빌드가 전량 커버했다.

→ **claim(= `running` 전이) 시점에 비운다.**

전이 지점은 **두 곳**이다 〔v5 검증 blocking〕. `repo.py` 에 `start()` 는 **없다**:

| 경로 | 함수 | 비고 |
|---|---|---|
| 프로덕션 | `JobRepo._admit`(`repo.py:461`, staticmethod) | `claim()` 트랜잭션 안의 유일한 queued→running UPDATE |
| 인라인/테스트 | `InMemoryJobRepo.start`(`memory.py:74`) | `api._run_inline` 이 부른다 |

**둘 다 고친다.** 한쪽만 고치면 프로덕션과 인라인 경로의 키 수명이 갈린다(`complete` 도
이미 두 벌로 유지되고 있다).

```
_admit  UPDATE ... SET status='running', ...,
        idem_key = CASE WHEN j.kind = 'community' THEN NULL ELSE j.idem_key END
memory.start:  같은 규칙
complete 시:   현행 유지 (succeeded 면 보존)
```

- 빌드가 **시작되기 전**(queued)에 들어온 트리거 → 합쳐진다 (버스트 흡수)
- 빌드가 **시작된 뒤** 들어온 트리거 → 새 잡이 되고, 버킷 상한 1이 **직렬화**한다
  → 앞 빌드가 끝나면 뒤 빌드가 돌아 나중 문서까지 커버한다

`parse`·`chunk`·`insert`·`ingest` 는 건드리지 않는다 — claim 시 키를 비우면 재실행 중복
적재 방어가 깨진다.

### 2.4 parse 키를 안 건드리는 이유 〔v4 검증〕

parse 호출부는 **3곳**이고 그중 둘은 `documents` 행이 존재하기 전에 돈다:

| 호출부 | document 행 | |
|---|---|---|
| `pipeline.py:524` | **없음** — xlsx 게이트 parse 는 `rec` 생성(`:588`) **전** | `existing_document_id` 도 dev 경로에선 None |
| `tasks.py:450` | **없음** — parse-preview Phase1 은 세션/staging 스코프. 행은 Phase2 에서 생긴다 | |
| `pipeline.py:1808` | `_facade_parse_and_chunk` 안 — `rec` 을 인자로 안 받는다 | 시그니처 + 호출자 2곳 변경 필요 |

여기에 `document_id` 를 강제하면 **엑셀 적재와 미리보기가 전부 죽는다**. `None` 허용하면
멱등키가 사라져 재시도마다 VL/OCR 이 전량 재실행된다.

그리고 **parse 캐시는 데이터 손실이 아니다.** parse 는 내용 결정적이라 같은 바이트면 같은
결과다. 옛 parse 결과를 받아도 틀리지 않는다. 손실은 `insert` 에서만 일어난다 — 옛 insert
결과를 받으면 kb 는 `ready` 인데 edgequake 는 비어 있다.

> **남는 것(이번 범위 밖)**: 파서를 고치고 같은 파일을 다시 올리면 최대 72h 동안 옛 parse
> 결과를 받는다. 데이터 손실은 아니지만 혼란스럽다. parse 키에 파서 버전을 섞는 것이
> 자연스러운 해법이고, 별도 항목으로 남긴다.

### 2.5 B — 나머지 (이미 작성된 코드, 워킹트리 미커밋)

| 파일 | 변경 |
|---|---|
| `jobs/admission.py` | `BUCKETS_FOR_KIND["community"]=("community",)`, `KBP_JOB_LIMIT_COMMUNITY` 기본 1 |
| `jobs/repo.py` | `KINDS` 에 `community`, `max_attempts` 기본, `max_runtime` 7200s |
| `jobs/runner.py` | `_run_community` — workspace 해석 → `_stage("building")` → 빌더 |
| `app.py` | `/communities/build` → `submit_job(kind="community")`, 응답에 `job_id` 추가 |
| compose ×2 | `KBP_JOB_LIMIT_COMMUNITY` |

**추가 처방**
- **503 계약** — 큐를 타면서 "worker 없음 → 503" 이 생긴다. 소비자(`tasks.py:368-371`)가
  `except Exception: return "failed"` 로 **완전히 삼키고 재시도도 없다**. 그 호출부에
  **경고 로그**를 남긴다(재시도는 범위 밖 — 다음 적재의 트리거가 다시 부른다).
- **workspace 축** — community 는 `workspace_key=eq_ws`(edgequake UUID), parse/insert 는
  kb id 라 `KBP_JOB_LIMIT_PER_WORKSPACE` 축을 공유하지 않는다. 유량제어는 **버킷 레벨
  (상한 1)에서만** 걸린다. 문서에 명시하되 설계는 안 바꾼다(통일하면 기존 잡의 workspace
  상한 계산이 바뀐다).

---

## 3. 변경 목록

**kb** (4파일)
- `clients/kb_pipeline_client.py:312-344` — `insert()` 에 `kb_document_id: str` 추가,
  `idem_key=f"kb-insert:{kb_document_id}"`. `parse()` 는 **안 건드린다**
- `core/pipeline.py:213` — **`KbPipelineLike` Protocol 의 `insert` 시그니처**도 함께
  갱신한다 〔v5 검증 blocking〕
- `core/pipeline.py:2269` — `kbp.insert(..., kb_document_id=str(rec.document_id))`
- `workers/tasks.py:368-371` — 커뮤니티 트리거 실패 시 **경고 로그**(지금은 완전히 삼킨다)

**kb 테스트 더블 3개** 〔v5 검증 blocking〕 — 명시 키워드만 받으므로 새 인자를 넘기는
순간 `TypeError` 가 나고, 그 예외를 `pipeline.py:2276` 의 `except Exception` 이 삼켜
**"적재 실패" 로 둔갑**한다. 회귀 기준선이 무너지므로 반드시 함께 고친다:
- `tests/test_pipeline_kb_pipeline.py:229`
- `tests/test_worker_kb_pipeline_stages.py:69`
- `tests/test_worker_kb_pipeline_stages.py:183`
- (`tests/test_pipeline_raganything.py:156` 은 `*a, **k` 라 무관)

**facade** (2곳 + 이미 작성된 B 코드)
- `jobs/repo.py:461` `_admit` — `community` 면 `idem_key = NULL`
- `jobs/memory.py:74` `InMemoryJobRepo.start` — 같은 규칙

**kb 클라이언트 테스트 — `insert()` 호출부 9곳** 〔v6 검증 blocking〕
`kb_document_id` 를 필수 키워드로 만들면 `test_kb_pipeline_client.py` 의 직접 호출부가
전부 `TypeError` 로 깨진다. 더블 3개와 **같은 종류의 누락**이다:
`373, 398, 413, 427, 440, 454, 575, 594, 655`
- `:657` 의 `kb-insert:{WS}:{DOC_ID}` 단언 → 새 키로 갱신
- `:644` 의 `kb-parse:{DOC_ID}` 는 **그대로 통과**(parse 미변경)

> 즉 이 변경으로 손대는 테스트는 **더블 3개 + 클라이언트 호출부 9곳 + 단언 1곳** 이다.
> 이걸 다 반영한 뒤에야 "kb 실패 19건 그대로" 가 의미를 가진다.

## 4. 테스트

> **skip 함정** 〔v5 검증 blocking〕 — B 의 두 핵심 회귀는 `_admit` 동작이라 **실
> Postgres 에서만** 관측된다. 그 파일(`service/tests/test_job_repo_pg.py`)은
> `KBP_PG_DSN` 미설정이면, 그리고 **살아있는 facade-worker 가 큐를 물고 있으면**
> module-level skip 한다. 인메모리 더블은 `_admit` 을 아예 안 탄다.
> → 검증 시 **worker 를 내리고 DSN 을 주어 실제로 돌린 것을 확인**한다. skip 으로 통과한
> "kbp 0건" 은 근거가 아니다.

> **use_jobs 전제** 〔v6 검증 minor〕 — 레거시 동기 경로(`use_jobs=False`)는 `_post_body`
> 가 `idem_key` 를 **아예 쓰지 않는다**(`client.py:162-171`). C 수정은 잡 경로에서만
> 효과가 있으므로, 아래 라이브 검증은 `KB_PIPELINE_USE_JOBS=true` 로 떠 있는 것을 전제로
> 한다. 그 전제 없이 통과해도 아무것도 증명하지 못한다.

**C (insert 키)**
- ★ **배선 회귀 가드** 〔v6 검증 blocking〕 — `FakeKbPipeline.insert_calls` 에 기록된
  `kb_document_id` 가 `str(rec.document_id)` 이고 **`new_docs_id`(content_hash[:16]) 와
  다르다**. `pipeline.py:2264` 에서 두 값이 같은 스코프에 공존하므로 잘못된 쪽을 넘겨도
  예외가 없고, 클라이언트 테스트는 키 **형식**만 봐서 통과한다 — 이 단언이 없으면 C-1 이
  무증상 재현되는데 스위트가 전부 초록이다. 더블이 이미 인자를 기록하므로 1줄이다
- 같은 `kb_document_id` 로 재제출 → **같은 job_id**(재시도 합치기 유지 — 핵심 회귀)
- 다른 `kb_document_id`(삭제 후 재업로드) → **새 job_id**
- `kb_document_id` 가 없으면 **예외**(조용히 키 없이 보내지 않는다)
- Protocol·더블 3개 갱신 후 **kb 실패가 19건 그대로**인지 확인
- **라이브**: 적재 → 문서 삭제 → 같은 파일 재업로드 → edgequake 에 **실제로 청크가 있다**

**B (커뮤니티)** — ★ 는 pg 필수
- `community` 잡이 큐에 들어가고 웹 프로세스에서 안 돈다(`status=queued`)
- **queued 인 동안** 트리거 3회 → 잡 1개 (버스트 흡수)
- ★ **claim 후** 트리거 → **새 잡**이 생기고 상한 1로 직렬화된다(§2.3 회귀 — 배치 뒤쪽
  문서가 커버되는지)
- ★ **`insert` 는 claim 후에도 키가 남는다**(kind 분기 회귀 — 재실행 중복 적재 방어)
- `InMemoryJobRepo.start` 도 같은 규칙인지(인라인 경로 계약 일치)
- runner: workspace 해석·빌더 인자, `LeaseLost` 면 빌드 미시작, workspace 없으면 `JobFailed`
- worker 없음 → 503 + kb 경고 로그
- 기존 `test_app.py:105` 커뮤니티 멱등 테스트는 잡을 실행하지 않고 queued 로만 두므로
  §2.3 변경 후에도 **그대로 통과**한다(회귀 아님)

**회귀 기준선**: kb 기존 실패 19건, kbp 0건(pg 포함해 실제 실행).

## 5. 리스크 / 알려진 한계

| | 항목 | 판단 |
|---|---|---|
| C | `kb_document_id` 를 못 얻는 insert 경로 | 호출부는 `rec` 스코프 안 **한 곳**뿐(`pipeline.py:2269`). 없으면 예외로 드러낸다 |
| C | 같은 KB·같은 내용·**다른 파일명** → edgequake 문서 2개 | 동작 변화지만 옳다(§2.1). 손실 아님 |
| B | claim 시 키를 비워 빌드가 쌓인다 | 상한 1이 직렬화. **나중 문서를 커버하는 게 목적**이다 |
| B | claim-clear 로 `submit` 의 경합 분기가 일상화 → 키 없는 community 잡 | 상한 1이 직렬화하므로 무해. "queued 면 항상 합쳐진다" 가 100% 는 아님을 명시 〔v5 검증 minor〕 |
| B | kind 분기가 다른 kind 에 오적용 | `insert` 보존 / `community` 비움을 **각각** pg 테스트로 고정 |

**알려진 한계 (이번 변경이 만들지 않았고, 고치지도 않는다)**

1. **kb 성공판정 실패 후 재시도 루프** 〔v5 검증 minor〕 — facade insert 는 `succeeded`
   인데 kb 의 판정(`chunk_count>0` 등)이 미충족이면 kb 가 edgequake 문서를 **지우고**
   실패로 끝낸다. facade 잡은 succeeded 라 키가 보존되므로, 재시도가 **캐시된 결과**를
   다시 읽어 같은 실패를 72h 반복한다. **지금도 동일**하다(옛 키도 결정적이라 같은 잡을
   맞는다). 근본 해법은 kb 가 cleanup 할 때 facade 키를 무효화하는 것 — 별도 항목.
2. **parse 캐시** — 파서를 고치고 같은 파일을 다시 올리면 최대 72h 옛 결과(§2.4). 손실
   아님. parse 키에 파서 버전을 섞는 것이 해법 — 별도 항목.

## 6. 이 plan 에서 뺀 것 — 취소(A)

별도 plan 으로 뺀다. v3 검증이 드러낸 **미해결 배선**을 그대로 옮겨 적는다:

1. **`status='canceled'` 를 쓰는 주체가 없다** — `tasks.py:231` 의 광범위 `except` 가
   무조건 `failed` 로 박고, `batch_worker.py:174-178` 에는 canceled 분기가 없다
2. **pipeline 의 3개 체크포인트가 값을 반환한다** — `except Exception: return _bad(...)`
   구조라 "tail 에서 한 번 재-raise" 가 성립하지 않는다
3. **워커 진입 취소 가드가 없다** — `tasks.py:160` 이 무조건 `running` 으로 덮어써
   queued 취소가 되살아난다
4. **취소 확인 콜러블의 운반 경로가 없다** — pipeline 은 `job_id`·`session_factory` 를
   모른다(`on_stage` 만 있다)
5. **queued 취소분의 blob 정리가 없다** — `blob_store.delete` 는 워커가 처리할 때의
   `finally` 에서만 돈다
6. **문서·배치아이템 상태 어휘**가 잡 상태 8곳 외에 3개 컴포넌트 더 필요하다
   (`DocumentList`·`DocumentDetailModal`·문서 상세 page)
7. `canceled` 를 배치 terminal 로 넣으면 **재시도 불가**가 된다
   (`batch_repository.py:233` 이 `{failed, gate_failed}` 하드코딩)
