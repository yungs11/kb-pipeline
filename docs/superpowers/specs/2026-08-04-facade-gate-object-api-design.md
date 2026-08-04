<!-- plan-version: v2 -->
<!-- ultracode-validation: PENDING -->

# facade 게이트·오브젝트 API — 다운스트림 은닉 마무리

> 짝 문서: [`2026-08-03-facade-job-queue-design.md`](2026-08-03-facade-job-queue-design.md)

## 0. 왜

facade 의 존재 이유는 **"소비자에게 capability 만 노출하고 다운스트림을 숨긴다"** 인데
두 곳이 새고 있다. kb 가 kbp 스택의 컨테이너를 직접 찌른다:

| 자원 | kb 설정 | 실체 |
|---|---|---|
| doc_guard | `docguard_base_url = http://localhost:8000` | kbp compose. **dev 8001→8000**, airgap **3004→8000** |
| MinIO | `minio_endpoint = localhost:9000`, bucket `document-parser` | kbp compose. dev **9000→9000**(override 승격), airgap **S3 미노출**(콘솔 3003→9001만) |

kb 가 사라지고 새 소비자가 붙으면 그 소비자도 `:8000`·`:9000` 을 직접 알아야 하고,
폐쇄망에서 두 포트를 추가로 열어야 한다.

### 지금 이 배선이 **실제로 깨져 있다** (2026-08-04 실측)

```
kb .env :  DOCGUARD_BASE_URL=http://localhost:8000
실측    :  :8000 연결 실패 (아무것도 없음)
           :8001 → 200      (kbp-doc_guard-1, 8001→8000 매핑)
```

`deps.docguard.check_excel()` 이 ConnectionError 를 던지고 그 지점에 try/except 가 없다
(`pipeline.py:537`). 즉 **xlsx 적재가 게이트에서 통째로 실패**한다.

이게 은닉의 동기를 강화한다 — 소비자가 다운스트림 **주소를 직접 아는 구조**라서, 그
주소가 어긋나도 소비자 쪽에서만 터지고 facade 는 모른다. facade 뒤로 넣으면 주소를
아는 곳이 한 곳(compose 의 인트라스택 DNS)으로 줄고, 이런 종류의 어긋남이 구조적으로
사라진다.

> 참고: 이 상태에서 kb 전체 테스트의
> `test_pipeline_ragflow.py::test_ragflow_gate_block_still_rejected` 가
> `'ready' == 'rejected'` 로 실패한다(잡 큐 작업 전부터 실패하던 19건 중 하나).
> 같은 계열인지 전환 후 재확인한다.

### 앞선 조사에서 정정된 사실 두 개

계획을 세우기 전에 짚어둔다 — 초기 진단이 둘 다 틀렸다.

1. **게이트 순서는 이미 옳다.** "kb 가 업로드 시점에 게이트를 돌려 파서 게이트와 중복"
   이라고 봤는데 아니다. `pipeline.py:487` 이 `gate_options 는 더 이상 사용하지 않는다
   (파서-후단 엑셀 게이트로 전환, 13규칙 게이트 제거)` 라고 명시하고, 실제 흐름은
   **parse → `gate_summary` 수신 → `check_excel` 검증 → chunk** 다. 바꿔야 할 것은
   순서가 아니라 **전송 경로**뿐이다.
2. ~~`/obj/{key}` 프록시는 없다~~ — **이것도 틀렸다(v2 정정).** 정반대다.

   ```python
   # minio_client.py:166  public_url()
   return f"/obj/{object_key.lstrip('/')}"
   # docstring: "presign 은 localhost:9000 절대 URL 이라 외부/https 에서 깨지므로
   #             (혼합콘텐츠), 챗 답변·인용 이미지는 이 상대경로를 쓴다"
   ```
   ```js
   // frontend/next.config.mjs:23
   { source: "/obj/:path*", destination: `${minio}/document-parser/:path*` }
   ```

   - `presign` 은 **호출 지점 0건 — 데드코드**다(`grep '\.presign(' backend/app` → 0).
   - 실제 이미지 URL 생산자는 `public_url` → `/obj/{key}` **상대경로**이고,
     프론트가 Next rewrite 로 same-origin 프록시한다.
   - **presign 은 이미 한 번 기각된 방식**이다(혼합콘텐츠).
   - `docker-compose.airgap.yml:100` 이 `S3 API(9000)는 내부 DNS로만 사용 → 호스트
     노출 불필요` 라고 못박아, **폐쇄망에서 presign 은 애초에 도달 불가**다.

## 1. 범위

**한다**

- facade 에 **게이트 API** 신설 — kb 뿐 아니라 다른 소비자도 쓸 수 있는 독립 엔드포인트
- facade 에 **오브젝트 API** 신설 — MinIO 은닉
- kb 에서 `docguard_base_url`·`minio_endpoint` **직접 호출 제거**
- 폐쇄망 compose 의 `:8000`·`:9000` 노출 정책 정리

**안 한다** — §7

## 2. 게이트 API

### 2.1 현재

kb 가 doc_guard 의 세 엔드포인트를 안다(`clients/docguard_client.py`):

| doc_guard | kb 사용 | 비고 |
|---|---|---|
| `POST /v1/check` | **미사용** | `docguard.check(` 호출 지점 0건 — 13규칙 게이트 제거의 잔재 |
| `POST /v1/check-excel` | `pipeline.py:537`, `tasks.py:472` | `{filename, gate_summary}` → CheckReport |
| `GET /v1/rules` | `routers/docguard.py:32` | 프론트 룰 체크박스 카탈로그 |

### 2.2 facade 계약

```
POST /gate/check-excel     {filename, gate_summary}  → CheckReport (doc_guard 원형 통과)
GET  /gate/rules                                     → 룰 카탈로그 (doc_guard 원형 통과)
```

**doc_guard 응답을 변형하지 않는다.** kb 의 `_build_gate_popup(report)` 가 원형 필드
(`findings`·`customer_message` 등)를 그대로 읽으므로, 정규화하면 kb·프론트가 함께 깨진다.
facade 가 더하는 값은 **은닉과 재사용 가능성**뿐이다.

`POST /v1/check`(원본 파일 업로드형)는 **포팅하지 않는다** — 소비자가 없다. 필요해지면
그때 더한다.

`X-Facade-Key` 게이트 대상이다(다른 stateful 경로와 동일).

**전환 순서 주의**: kb 는 `facade_key` 미설정이면 헤더를 안 붙인다(`config.py:171`).
facade 에 `KBP_FACADE_KEY` 가 설정된 배포에서 kb 에 미설정이면 `/gate/*` 가 즉시 401 이다.
**kb 의 키 설정이 facade 게이트 활성화보다 선행**해야 한다(§6 순서 반영).

### 2.3 왜 `/parse` 에 합치지 않는가

parse-svc 가 이미 `gate_summary` 를 in-process 로 계산해 `/parse` 응답에 싣는다. 검증까지
`/parse` 안에서 끝낼 수도 있지만 **별도 엔드포인트로 둔다**:

- 소비자가 **파싱 없이** 게이트만 돌리고 싶을 수 있다(룰 변경 후 재검증, 다른 파서의
  산출물 검증).
- `gate_summary` 산출(parse-svc)과 판정(doc_guard)은 **다른 서비스**다. 하나의 응답에
  묶으면 판정 룰이 바뀔 때마다 재파싱해야 한다.

## 3. 오브젝트 API

### 3.1 현재 kb 가 쓰는 연산

`clients/minio_client.py` 의 `MinioStore`:

| 연산 | 용도 |
|---|---|
| `put_original(docs_id, name, bytes, mime)` | 적재 성공 후 원본 canonical 승격 |
| `put_bytes` / `MinioBlobStore.put`(`BlobStore` Protocol 구현) | 배치·parse-preview staging |
| `put_page_image(docs_id, page_uuid, jpeg)` | 페이지 이미지 쓰기(`pipeline.py:1228,1580`) |
| `get_object_bytes(key)` | staging 회수 |
| `delete_object(key)` / `delete_prefix(docs_id)` | 문서·KB 삭제 |
| ~~`presign(key, *, expires_seconds)`~~ | **데드코드** — 호출 0건 |
| `public_url(key)` → `/obj/{key}` | 순수 문자열. **kb 에 남는다**(MinIO 미호출) |
| `rewrite_minio_urls(text)` | `public_url` 위임. **kb 에 남는다** |
| `original_object_key` / `page_image_object_key` | 키 규칙 |

### 3.2 facade 계약

```
PUT    /objects/{scope}/{doc_id}/{name}   바이트 업로드 → {key}
GET    /objects?key=                      바이트 (제어평면 전용 — staging 회수)
DELETE /objects?key=                      단건 삭제
DELETE /objects?prefix=                   프리픽스 일괄
```

`scope` 는 `original` \| `staging` \| `page` — **키 규칙을 facade 가 소유**한다. 소비자가
`{docs_id}/original/{name}` 같은 규칙을 알 필요가 없어진다(지금은 kb·parse-svc·facade
셋이 각자 안다).

`presign` 은 넣지 않는다(데드코드, §3.3). `GET` 은 **staging 회수용**이지 썸네일 서빙용이
아니다 — 브라우저는 여전히 `/obj/*` 로 간다.

### 3.3 **제어평면만 은닉한다** — 이미지 읽기는 현행 유지

MinIO 접근을 두 갈래로 가른다.

| | 무엇 | 결정 |
|---|---|---|
| **제어평면** | staging put/get, 원본 승격, 삭제, 페이지 이미지 쓰기 | **facade 뒤로** |
| **데이터평면** | 브라우저의 썸네일·인용 이미지 읽기(`/obj/*`) | **현행 유지** |

**데이터평면을 facade 로 돌리지 않는 이유 — 실측:**

```
페이지 이미지 802개 | 중앙값 292 KB · 평균 426 KB · 최대 3,987 KB
검색 1회 인용 top_k=10 → 최대 ~4 MB 가 facade 통과
```

facade 는 `gunicorn -w 4` 에 핸들러가 동기 `def`(AnyIO 스레드풀 공유)이고, 잡 대기가
`KBP_JOB_MAX_WAITERS=4` 로 스레드를 이미 점유한다. 여기에 이미지 스트리밍이 얹히면
**썸네일을 뿌리는 동안 잡 접수·`/healthz` 가 스레드를 못 얻는다.** D11(`/healthz` async)
을 비범위로 둔 근거가 "waiter 상한으로 고갈을 막는다" 였는데 이미지 트래픽은 그 상한
밖이다. facade 를 정적 파일 서버로 만드는 셈이라 설계 전제와 충돌한다.

**그리고 데이터평면은 이미 은닉돼 있다.** 브라우저는 `/obj/{key}` same-origin 만 본다.
MinIO 주소를 아는 곳은 **프론트 서버의 `MINIO_ORIGIN` env 하나**이고, 그건 소비자
*코드*가 아니라 *배포 설정*이다. 폐쇄망에서는 S3 포트가 호스트에 노출조차 안 된다.

doc_guard 와 성격이 다르다 — doc_guard 는 **API 호출**(소비자 코드가 계약을 안다)이라
은닉에 값이 있고 트래픽도 작다. MinIO 읽기는 **바이트 전송**이고 프록시 계층이 이미 있다.

`presign` 은 데드코드이므로 facade 계약에 넣지 않는다. `public_url`·`rewrite_minio_urls`
는 **순수 문자열 함수**(MinIO 를 호출하지 않는다)라 kb 에 남는다.

## 4. kb 쪽 변경

| 파일 | 변경 |
|---|---|
| `clients/docguard_client.py` | **삭제하지 않고** base_url 을 facade 로 돌린다 → 경로만 `/gate/*` 로. 또는 `KbPipelineClient` 에 `gate_check_excel`·`gate_rules` 추가하고 이 클라이언트를 제거 |
| `clients/minio_client.py` | 같은 방식으로 facade `/objects/*` 위임 |
| `config.py` | 제거 대상 6키: `docguard_base_url` · `minio_endpoint`/`minio_access_key`/`minio_secret_key`/`minio_bucket`/`minio_secure`. 흡수처는 `kb_pipeline_base_url`(:154)+`facade_key`(:171) |
| `dependencies.py` | `MinioStore.from_settings` **7곳**(64·253·311·337·360·381·428) + `DocGuardClient` 생성 **2곳**(52·285) |
| `workers/runtime.py` | `_default_staging_store()` → `MinioBlobStore` 조립(45-54). **staging_store 팩토리** |
| `workers/batch_worker.py` | `MinioStore.from_settings`(192) — 원본 canonical 승격 |
| `workers/tasks.py` | `deps.docguard.check_excel`(472) |
| `routers/docguard.py` | `GET /docguard/rules` 유지(프론트 계약) — 내부만 facade 경유 |

**총 MinIO 생성 지점 9곳**(dependencies 7 + workers 2). v1 이 "6곳" 이라 한 것은 오집계였고
워커 2곳이 빠져 있었다 — 그대로 진행했으면 `config.py` 의 `minio_*` 제거 시 **배치 워커와
parse-preview staging 이 기동 실패**한다.

**프론트는 변경 없다** — `/obj/*` 리라이트도 `MINIO_ORIGIN` 도 그대로다(§3.3 에서 데이터
평면을 범위 밖으로 뒀으므로).

`DocGuardClient` 는 **base_url 만 facade 로 돌린다**(제거하지 않는다). 제거하려면
`DocGuardLike.check` Protocol(`pipeline.py:46-49`)·`GateOptions.to_check_kwargs`(:344)·
`test_clients.py` 의 check 계열 6케이스·`test_pipeline.py` 의 gate_options 2케이스를 함께
정리해야 해서 표면이 커진다. 그건 별도 정리 항목으로 둔다(§7).

## 5. 폐쇄망 배포

**현행 결정을 뒤집는 것임을 명시한다.** `docker-compose.airgap.yml` 의 doc_guard 블록에
주석이 박혀 있다:

```
ports: ["3004:8000"]   # 외부노출: 호스트 3004 → doc_guard 8000(자체 API).
                       # facade는 doc_guard를 프록시 안 함 → 직접 노출.
```

이 계획이 그 전제를 바꾼다(facade 가 프록시한다). 주석도 함께 갱신한다.

- kbp airgap: `doc_guard` 의 **`3004:8000` 노출 제거**(facade 프록시로 대체). 위 주석도 갱신.
- kbp airgap: **minio 는 이미 S3 미노출**이라 건드릴 게 없다(콘솔 `3003:9001` 은 버킷 관리
  UI 라 유지). 프론트는 컨테이너 DNS `minio:9000` 으로 서버사이드 프록시한다.
- kb compose: `DOCGUARD_BASE_URL` 제거. `MINIO_*` 는 **제어평면 전환 후** 제거한다.
  프론트 서비스의 `MINIO_ORIGIN` 은 **유지**(데이터평면, §3.3).
- `docs/airgap-deploy.md`·`architecture-ports.md` 갱신.

## 6. 구현 순서

1. **완료** — facade `/gate/*`(`service/doc_guard.py` + 라우터 2개, 테스트 7건, 실
   doc_guard 로 확인)
2. facade `/objects/*` — 제어평면만(put/get/delete/delete-prefix). 키 규칙 소유
3. kb 를 facade 로 전환 — 게이트 먼저(표면이 작다), 그다음 오브젝트 **9곳**
4. 라이브 검증: 엑셀 게이트 거부 흐름, **썸네일이 여전히 뜨는지**(데이터평면 무변경 확인),
   문서 삭제, 배치 staging
5. 폐쇄망 compose·문서 (§5)
6. kb 설정 제거 — `docguard_base_url` 1키 + `minio_*` 5키. `grep -c` 로 완료 판정 가능하게

## 7. 범위 밖

- **`POST /v1/check`(원본 파일 게이트) 포팅** — 소비자가 없다.
- **facade 바이트 프록시(B안)** — §3.3. 필요해지면 전환.
- **parse-svc 의 게이트 산출 로직 변경** — `gate_summary` 계약은 그대로다.
- **kb 의 배치 워커·문서 메타·커뮤니티 트리거** — kb 도메인이다(잡 큐 설계 §9 참조).
- **`DocGuardClient` 제거** — `check()` Protocol·`to_check_kwargs`·테스트 8케이스가 아직
  묶여 있다. base_url 전환으로 은닉 목적은 달성되므로 별도 정리로 남긴다.
- **데이터평면(`/obj/*`) 전환** — §3.3. facade 응답성과 맞바꿔야 해서 지금은 하지 않는다.
- **버킷 분리** — 지금 `document-parser` 하나를 kb·parse-svc·잡큐가 프리픽스로 나눠 쓴다.
  분리하면 폐쇄망 번들·기존 객체 마이그레이션이 따라온다. 프리픽스 소유 규칙만 문서화한다.
