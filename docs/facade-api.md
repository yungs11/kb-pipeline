# kb-pipeline facade (:3000) — API · 운영 매뉴얼

> **근거**: 이 문서의 모든 계약·수치는 코드에서 직접 대조했다. 문서(`_workspace/`)와 코드가 어긋나면 코드가 권위다.
> 주 출처: `service/app.py`, `service/parse_client.py`, `service/adaptive_chunk.py`, `service/edgequake.py`,
> `scripts/run-facade.sh`, `scripts/facade.env`, `docker-compose.yml`.
> 최종 대조: 2026-08-03 (branch `feat/paddle-gw-scan-lane`).

---

## 1. facade 는 무엇인가

`service/app.py` 하나로 이루어진 FastAPI 앱(`FastAPI(title="kb-pipeline")`, `service/app.py:86`).
외부 소비자(kb-backend 등)에게 **capability 만 노출**하고 다운스트림(parse-svc / adaptive_chunk / edgequake)을 숨기는
얇은 오케스트레이터다. facade 자신은 파싱도 청킹도 하지 않는다 — 대신 **청킹 정책과 모달 원자성의 소유자**다.

facade 가 실제로 더하는 값(단순 프록시가 아닌 이유):

| 책임 | 코드 |
|---|---|
| **워크스페이스 해석** — 소비자의 kb id → edgequake workspace UUID (모든 stateful 핸들러) | `eq.ensure_workspace()` (`service/edgequake.py:65`) |
| **모달 원자성 강제** — `〈MODAL…〈/MODAL〉` 스팬을 `atomic_markers` 로 청커에 전달 | `MODAL_ATOMIC_MARKERS` (`service/adaptive_chunk.py:33`) |
| **passthrough 묶기** — 청크 텍스트를 U+001E 로 join → edgequake 가 같은 바이트로 분할(1:1 정합) | `PASSTHROUGH_SEP` (`service/edgequake.py:39`) |
| **마커 스트립** — 저장/표시 직전 마커 제거(내용은 보존) | `_strip_modal()` (`service/app.py:39`, `service/edgequake.py:44`) |
| **응답 정규화** — 허브의 `chunk_text`/`chunk_pages` → 계약의 `text`/`pages` | `service/app.py:174-183` |
| **비동기 잡 폴링 은폐** — adaptive 잡·edgequake task 폴링을 동기 호출 뒤로 숨김 | `AdaptiveChunkClient.chunk()`, `EdgequakeClient.insert_chunks()` |

### 구성도

```
                        ┌──────────────── consumer ────────────────┐
                        │  kb-backend(:8088) / 프론트(:4000)       │
                        └───────────────┬──────────────────────────┘
                                        │ HTTP  (+X-Facade-Key)
                        ┌───────────────▼──────────────────────────┐
                        │  facade  :3000   service/app.py         │
                        │  /parse /chunk /insert /search /ingest   │
                        │  /chunks /doc /communities/build         │
                        └──┬─────────────┬──────────────┬──────────┘
       KBP_PARSE_SVC_URL   │             │              │  KBP_EDGEQUAKE_URL
                           │  KBP_ADAPTIVE_CHUNK_URL    │
        ┌──────────────────▼──┐  ┌───────▼──────────┐  ┌▼───────────────────────┐
        │ parse-svc  :19001   │  │ adaptive_chunk   │  │ edgequake  :3001       │
        │ parse+blockify+     │  │ :18060           │  │ 추출·임베딩·AGE그래프  │
        │ modal enrich        │  │ 4방법 경쟁 청킹  │  │ ·검색  (passthrough)   │
        └──┬──────────────┬───┘  └──────────────────┘  └───────────┬────────────┘
           │ 변환 API     │ VL/paddle_gw (원격)                    │
           │ :3000        │ MODEL_API_URL /                        │
           │              │ KBP_PADDLE_OCR_GATEWAY_URL             │
           ▼              ▼                          ┌─────────────▼────────────┐
        MinIO :9000 (페이지 이미지)                  │ postgres :5433           │
                                                     │ pgvector + Apache AGE    │
   facade 직결: KBP_PG_DSN ────────────────────────▶ │ (community_reports)      │
                                                     └──────────────────────────┘
```

facade 가 **직접 DB 를 만지는 유일한 경로**는 `/communities/build` 다(`KBP_PG_DSN`, `service/app.py:348`).
나머지는 전부 HTTP 위임.

### 오케스트레이션 두 경로

- **표준(단계별)** — 소비자가 `/parse` → `/chunk` → `/insert`(+ `/insert/status` 폴링)를 순차 호출.
  단계별 진행률·청킹 근거를 UI 에 노출할 수 있다. kb-backend 가 쓰는 경로.
- **블로킹 one-shot** — `/ingest` 한 번으로 parse→chunk→insert. 결과는 동일하되 중간 제어 없음.

> `/ingest/submit`·`/ingest/status`(비동기 변형)는 **Phase 2d 에서 제거**됐다. 폴링은 `/insert/status` 로 갈음한다.

---

## 1.5 잡 큐 — 유량제어는 facade 가 소유한다

`/parse`·`/chunk`·`/insert`·`/ingest` 는 이제 facade 웹 프로세스가 직접 처리하지 않는다.
잡을 postgres `kbp.jobs` 에 넣고, **별도 프로세스** `facade-worker` 가 슬롯 안에서 집어
다운스트림을 호출한다.

```
소비자 ──▶ facade (접수만, 밀리초) ──▶ kbp.jobs ◀── facade-worker (슬롯 안에서만 실행)
                                                        │
                                          parse-svc / adaptive_chunk / edgequake
```

동시 실행 상한은 **단계별 + 테넌트별**이다.

| env | 기본 | 근거 |
|---|---|---|
| `KBP_JOB_LIMIT_PARSE` | 4 | parse-svc `gunicorn -w 4` |
| `KBP_JOB_LIMIT_CHUNK` | 2 | adaptive_chunk 4방법 경쟁 비용 |
| `KBP_JOB_LIMIT_INSERT` | 2 | 임베딩 서버 처리량 |
| `KBP_JOB_LIMIT_PER_WORKSPACE` | 2 | `workspace_id` 가 있는 요청에만 적용 |

`/ingest` 는 세 구간을 다 호출하므로 **세 버킷을 동시 점유**한다(동시 ingest = min = 2).

### 기존 4경로는 계약이 바뀌지 않았다

내부적으로 잡을 경유하지만 **요청·응답 본문과 인증 요구는 그대로**다. 소비자 코드를 고칠
필요가 없다. 다만 다음 상태코드가 **새로 나올 수 있다**:

| 코드 | 언제 | 대응 |
|---|---|---|
| `503` + `Retry-After` | facade-worker 가 하나도 안 떠 있음, 또는 프로세스당 동시 대기 상한(`KBP_JOB_MAX_WAITERS`, 기본 4) 초과 | 잠시 후 재시도. **잡은 생성되지 않는다** |
| `409` + `{job_id}` | 대기 상한(`KBP_JOB_LEGACY_WAIT_SECONDS`, 기본 3300s) 초과 | **잡은 계속 진행 중이다.** `GET /jobs/{job_id}` 로 폴링하고 `/jobs/{job_id}/result` 로 결과를 회수한다 |
| `413` | 업로드가 `KBP_JOB_MAX_UPLOAD_BYTES`(기본 50MB) 초과 | 파일을 줄이거나 상한을 올린다 |

`409` 를 4xx 로 둔 것은 의도적이다 — 504 로 내면 5xx 를 재시도하는 소비자가 같은 문서로
**두 번째 잡**을 만든다(Phase 1 에 제출 멱등키가 없다).

### 신규 비동기 경로

```bash
# 제출 → 202
JOB=$(curl -s -X POST http://localhost:3000/jobs/parse \
        -H "X-Facade-Key: $KEY" -F "file=@doc.pdf" -F "batch_key=b1" | jq -r .job_id)

# 상태 폴링
curl -s -H "X-Facade-Key: $KEY" http://localhost:3000/jobs/$JOB
# {"status":"running","stage":"parsing","attempt_count":1,"ahead_in_partition":null,...}

# 결과 — 기존 동기 응답과 **같은 본문**
curl -s -H "X-Facade-Key: $KEY" http://localhost:3000/jobs/$JOB/result
```

단계 연결은 `job_id` 참조로 한다(수 MB 왕복 제거):
`POST /jobs/chunk {"parse_job_id": "..."}` → `POST /jobs/insert {"chunk_job_id": "..."}`.

`GET /jobs/workers` 는 worker 가용량을 준다 — 키 이름은 kb-backend 의 배치 worker 표시와
동일하다(`online`/`capacity`/`active`/`available`/`queued`/`processing`) + `oldest_queued_age_seconds`.
**`online:false` 면 접수가 전부 503 이다** — facade-worker 를 먼저 띄운다.

`queue_position` 은 제공하지 않는다. claim 이 kind 무관 전역 FIFO 스캔이고 승인은 버킷·
테넌트·로컬 슬롯 3중 조건이라 "앞에 N건" 이 대기 시간을 예측하지 못하기 때문이다. 대신
같은 `(kind, workspace)` 안의 `ahead_in_partition` 을 준다.

---

## 2. 인증 — `X-Facade-Key`

`service/app.py:63-83`. 환경변수 `KBP_FACADE_KEY` 와 요청 헤더 `X-Facade-Key` 를 비교한다.

- **`KBP_FACADE_KEY` 미설정 → 게이트 전면 비활성(dev)**. 기동 시 WARNING 로그 1줄만 남는다.
  현재 `scripts/facade.env` 에는 이 키가 **없다** → 로컬 dev 는 무인증이다.
- 불일치/누락 시 `401 {"detail": "invalid or missing X-Facade-Key"}`.
- **게이트 대상**: `/search`, `/insert`, `/insert/status`, `/ingest`, `/chunks`, `/doc`,
  `/communities/build`, **그리고 신규 `/jobs/*` 전부**(파일 staging·DB 행·worker 시간을 소비).
- **비대상(무인증)**: `/healthz`, `/parse`, `/chunk` — stateless(워크스페이스 없음)라 의도적으로
  열려 있다. Phase 1 에서 이 둘은 **그대로 무인증**이다(kb 파사드 키가 미설정인 배포에서
  게이트를 채우면 kb 가 즉시 401 을 맞는다). Phase 2 에서 레거시 경로 제거와 함께 정리한다.
- 빈 문자열(`KBP_FACADE_KEY=`)은 **미설정과 동일 취급**한다. compose 에서 `${KBP_FACADE_KEY}`
  가 미정의면 빈 값이 주입되는데, 이를 게이트 ON 으로 보면 전 엔드포인트가 401 이 된다.

---

## 3. API 레퍼런스

Base URL: `http://localhost:3000` (런처가 `--host 127.0.0.1` 로 바인드 — **루프백 전용**, 외부 노출 아님).

### 요약

| Method | Path | 파라미터 위치 | 인증 | 블로킹 |
|---|---|---|---|---|
| GET | `/healthz` | — | ✗ | 즉시 |
| POST | `/parse` | multipart form | ✗ | ~수십초–수분 |
| POST | `/chunk` | JSON body | ✗ | ~수분 (4방법 경쟁) |
| POST | `/insert` | JSON body | ✓ | ~수분 (터미널까지 폴링) |
| GET | `/insert/status` | **query** | ✓ | 즉시 |
| POST | `/ingest` | multipart form | ✓ | parse+chunk+insert 합 |
| GET | `/chunks` | **query** | ✓ | chunk_count 만큼 순차 GET |
| DELETE | `/doc` | **query** | ✓ | 즉시 (204) |
| POST | `/communities/build` | **query** | ✓ | 즉시 202 (백그라운드) |
| POST | `/jobs/{parse\|chunk\|insert\|ingest}` | multipart / JSON | ✓ | **즉시 202** `{job_id}` |
| GET | `/jobs/workers` | — | ✓ | 즉시 |
| GET | `/jobs` | **query** | ✓ | 즉시 |
| GET | `/jobs/{job_id}` | path | ✓ | 즉시 |
| GET | `/jobs/{job_id}/result` | path | ✓ | 즉시 (409 미완료 / 422 실패) |
| DELETE | `/jobs/{job_id}` | path | ✓ | 즉시 |

> ⚠️ `/insert/status`·`/chunks`·`/doc`·`/communities/build` 는 **query string** 이다(JSON body 아님).
> FastAPI 가 `def f(workspace_id: str, ...)` 시그니처(Body 미표기)를 query 로 바인딩하기 때문 —
> `service/app.py:259, 333, 339, 356`.

---

### GET `/healthz`

프로세스 부팅 여부만 증명한다. 다운스트림 상태는 보지 않는다.

```bash
curl -s http://localhost:3000/healthz
# {"status":"ok"}
```

---

### POST `/parse` — 파일 1건 파싱 (parse-svc 위임)

`service/app.py:112`. 전부 parse-svc 로 위임하고 응답을 **거의 그대로 통과**시킨다.

**Request** (`multipart/form-data`)

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `file` | file | ✓ | 업로드 원본. 파일명은 `_safe_basename` 으로 정규화(경로 탈출·제어문자 차단, **한글/공백/괄호는 보존**) |
| `content_type` | str | ✗ | 미지정 시 업로드 파트의 content-type 사용 |
| `docs_id` | str | ✗ | 오케스트레이터의 `sha256(bytes)[:16]`. 주면 parse-svc 의 MinIO 페이지 이미지 키가 소비자와 일치한다. 미지정 시 parse-svc 가 동일 식으로 자체 계산 |

**Response 200** — 경로에 따라 두 형태.

*(a) 일반 문서 (`chunk_needed: true`)* — PDF/DOCX/PPTX/이미지/기타

```jsonc
{
  "enriched_content": "…마크다운 + inline <table> HTML + 〈MODAL…〉 마커…",
  "n_blocks": 42,
  "modal_spans":  [{"id":"T1","type":"table","char_range":[1200,2400]}],
  "table_blocks": [{"category":"table","content":"<table>…</table>","page_number":3}],
  "chunk_needed": true,
  "docs_id": "9f2c…",           // 16 hex
  "page_count": 12,
  "pages":       [{"page_number":1,"page_uuid":"9f2c…_1","minio_object":"9f2c…/9f2c…_1.jpeg"}],
  "page_spans":  [{"page_number":1,"char_start":0,"char_end":980}],
  "timing_metrics": {
    "parse_ms": 8120.4, "modal_enrich_ms": 0.0, "render_upload_ms": 640.2,
    "counters": {"page_count":12,"n_blocks":42},
    "modal_llm": {"wall_ms":null,"calls":null,"by_type":null,"per_call_ms":null,"max_workers":null}
  }
}
```

*(b) Excel (xlsx/xlsm/xls) — 자체청킹 (`chunk_needed: false`)*

parse-svc 가 LLM 없이 parse+chunk 를 함께 끝내 native 청크를 돌려준다. `/chunk` 를 **호출하면 안 된다**.

```jsonc
{
  "enriched_content": "…청크 텍스트를 \n\n 로 이은 것…",
  "n_blocks": 17,               // = 청크 수
  "modal_spans": [],
  "chunks": [{"chunk_index":0,"text":"…","titles_context":["시트1","표A"],"pages":[]}],
  "gate_summary": {"ok": true, "sheets": [...]},   // 엑셀 게이트 판정 재료
  "chunk_needed": false,
  "chunk_strategy": "excel_rag_parser",            // ← facade 가 소비자 호환용으로 주입(app.py:130)
  "docs_id": "…", "page_count": 0, "pages": [], "page_spans": [],
  "timing_metrics": { … }
}
```

**Response 200 (실패)** — parse-svc 는 파싱 실패를 HTTP 5xx 가 아니라 **200 + 실패 바디**로 준다.

```json
{"status": "failed", "detail": "parse_failed: …"}
```

→ 소비자는 반드시 `status == "failed"` 를 확인해야 한다. HTTP 200 만 보고 성공으로 처리하지 말 것.

```bash
curl -s -m 1800 \
  -F "file=@sample.pdf;type=application/pdf" \
  -F "docs_id=$(shasum -a 256 sample.pdf | cut -c1-16)" \
  http://localhost:3000/parse | jq '{status, n_blocks, len: (.enriched_content|length), pages: .page_count}'
```

---

### POST `/chunk` — enriched content 청킹 (adaptive_chunk 위임)

`service/app.py:134`. 허브(:18060)의 **비동기 잡**(`POST /chunk/jobs` → `GET /chunk/jobs/{id}` 폴링)을 호출하고
동기 응답처럼 돌려준다. 마커(`atomic_markers`)는 facade 가 **항상** 자동 첨부한다 — 소비자가 넘길 필요 없다.

**Request** (`application/json`)

| 필드 | 타입 | 기본 | 설명 |
|---|---|---|---|
| `enriched_content` | str | **필수** | `/parse` 의 `enriched_content` 그대로(마커 포함 — 원자화에 필요) |
| `doc_name` | str | `""` | 청커에 넘길 문서명 |
| `page_spans` | list | `null` | `/parse` 의 `page_spans`. 주면 청크마다 `pages` 귀속이 붙는다 |
| `pages` | list | `null` | `[{page_number, markdown}]` — page 청킹 방법용 |
| `table_blocks` | list | `null` | `/parse` 의 `table_blocks`. 허브에는 `blocks` 이름으로 전달됨(⚠️ e2e 로 `table_blocks` 이름을 쓰지 말 것 — `app.py:166`) |
| `methods` | list | `null` | 방법 제한. `null` = auto(전 방법 경쟁 후 스코어 선택). 키: `recursive_1100`/`recursive_600`/`page`/`llm_regex`/`semantic` |
| `skip_scoring` | bool | `false` | true 면 경쟁 생략(단일 method 필수) |
| `llm_regex_pattern` | str | `null` | 사용자 지정 정규식(→ `methods==["llm_regex"]` 필요) |

`methods=null, skip_scoring=false, llm_regex_pattern=null` 이면 레거시 요청과 **byte-identical** 로 전송된다(회귀 보장).

**Response 200**

```jsonc
{
  "chunks": [
    {"chunk_index":0, "text":"…마커 스트립된 표시용 텍스트…",
     "titles_context":["1장","1.2절"], "pages":[1,2]}
  ],
  "method_selected": "recursive_1100",
  "scores": {"recursive_1100": {"total": 0.82, "sc": …}, "semantic": {…}},
  "methods_compared": ["recursive_1100","recursive_600","semantic","llm_regex"],
  "timing_details": { … }        // 허브가 AC_TIMING 시에만; 아니면 null
}
```

> **중요**: 응답의 `text` 는 `_strip_modal()` 로 마커가 제거된 **표시사본**이다(kb-backend 가 `chunks_meta` 로 저장).
> 적재용 텍스트도 같은 값을 그대로 `/insert` 에 넘기면 된다 — `/insert` 가 한 번 더 스트립하므로 멱등이다.

---

### POST `/insert` — 사전 청킹된 텍스트 적재

`service/app.py:221`. 청크를 U+001E 로 join → edgequake 에 passthrough 문서 1건으로 제출 → **터미널까지 폴링**.

**Request** (JSON)

| 필드 | 타입 | 기본 | 설명 |
|---|---|---|---|
| `workspace_id` | str | **필수** | 소비자의 kb id(문자열). facade 가 edgequake workspace UUID 로 해석 |
| `doc_id` | str | **필수** | 문서 식별자. `title` 미지정 시 제목으로도 쓰임 |
| `title` | str | `""` | 문서 제목 |
| `chunks` | list[str] | **필수** | 청크 텍스트 배열(순서 유지 — 저장 후 1:1 대응) |
| `extract_graph` | bool | `true` | `false` → `metadata.skip_graph_extraction=true`. 엔티티/관계 추출만 건너뛴다(임베딩·벡터검색 무영향). **엑셀은 관례상 false** |

**Response 200**

```jsonc
{
  "document_id": "3f0e…",
  "chunk_count": 37,
  "status": "indexed",           // 성공. 실패 시 "failed"
  "edgequake_workspace_id": "b71d…",   // ← KB.edgequake_workspace_id 로 영속할 것(그래프 보기 팝업의 X-Workspace-ID)
  "entity_count": 128,           // skip_graph / 구버전 edgequake / 회수실패 시 null
  "relationship_count": 91,
  "phases": [{"name":"chunking","ms":3010.0},{"name":"extracting","ms":184220.5},
             {"name":"embedding","ms":21400.0},{"name":"storing","ms":900.0}]
}
```

`phases` 는 **폴링 기반 근사**다(edgequake 가 per-phase 타임스탬프를 주지 않음 — 3초 간격 관측을 구간에 귀속).
"적재 중 어디가 느린가"(대개 `extracting` = 엔티티 LLM)를 드러내는 용도.

**타임아웃**: `poll_timeout=1200s`. 초과 시 `status:"failed"` + `detail:"insert poll timeout after 1200s"`.

---

### GET `/insert/status` — 적재 진행 상황

`service/app.py:258`. **query string**.

| 파라미터 | 설명 |
|---|---|
| `workspace_id` | kb id |
| `doc_id` | ⚠️ **`/insert` 가 반환한 edgequake `document_id`** (업로드 시 쓴 doc_id 아님) |

```jsonc
{"phase": "processing", "chunk_count": 0, "terminal": false, "succeeded": false}
```

`terminal`/`succeeded` 판정은 edgequake 문서 **`status`** 기준이며, `succeeded` 는 `chunk_count > 0` 까지 요구한다
(`service/edgequake.py:290`).

---

### POST `/ingest` — one-shot (parse→chunk→insert)

`service/app.py:276`. 단계별 호출과 **동일한 결과**를 한 번에. 요청은 `multipart/form-data`.

| 필드 | 필수 | 설명 |
|---|---|---|
| `file` | ✓ | 업로드 원본 |
| `workspace_id` | ✓ | kb id |
| `doc_id` | ✓ | 문서 식별자(제목·청커 doc_name 으로도 사용) |
| `content_type` | ✗ | |

**Response 200**

```jsonc
{
  "document_id": "3f0e…", "chunk_count": 37, "status": "indexed",
  "chunking_selection": {"method_selected":"recursive_1100","scores":{…},"methods_compared":[…]},
  "edgequake_workspace_id": "b71d…"
}
```

- 파싱 실패 시 **parse-svc 실패 바디를 그대로 반환**한다(`{"status":"failed","detail":…}`) — 빈 컨텐츠로 청킹을 태우지 않는다(`app.py:296`).
- Excel(`chunk_needed=false`)이면 parse-svc 청크를 그대로 적재하고 `method_selected: "excel_rag_parser"`.
- ⚠️ `/ingest` 는 `docs_id` 를 parse-svc 로 전달하지 **않는다**(`app.py:292`) → 페이지 이미지 MinIO 키는 parse-svc 자체 계산값이 된다.
  키를 소비자와 맞춰야 하면 단계별 경로(`/parse` 에 `docs_id` 명시)를 쓸 것.

---

### GET `/chunks` — 적재된 청크 조회

`service/app.py:332`. query `workspace_id`, `doc_id`(= edgequake document_id).

edgequake 에 chunk-list 라우트가 없어, 문서 상세로 `chunk_count` 를 얻고 `{doc_id}-chunk-{i}` 를 **i 개 순차 GET** 한다
(`service/edgequake.py:476`). 청크가 많으면 그만큼 느리다. 404 인 청크는 조용히 건너뛴다.

```jsonc
[{"chunk_id":"3f0e…-chunk-0","text":"…","hierarchy_path":"문서명","page_number":1}]
```

---

### DELETE `/doc` — 문서 삭제

query `workspace_id`, `doc_id`. 성공 시 **204 No Content**(본문 없음).

---

### POST `/communities/build` — 커뮤니티 리포트 생성

`service/app.py:354`. query `workspace_id`. **즉시 202** 를 주고 실제 작업은 FastAPI `BackgroundTasks` 로 돈다.

```json
{"status": "started", "workspace_id": "b71d…"}   // ← edgequake workspace UUID
```

- 백그라운드 잡은 **예외를 삼킨다**(`logger.exception` 만 — `app.py:350`). 202 는 "접수됨"일 뿐 성공 보장이 아니다.
  결과 확인은 `public.community_reports` 행 또는 facade 로그로 한다.
- 이 경로만 `KBP_PG_DSN` 으로 Postgres 에 직접 접속한다(networkx + python-louvain, `random_state=42`).
- 백그라운드 태스크는 **facade 프로세스 안**에서 돈다 → 재기동하면 진행 중 잡은 유실된다.

---

## 4. 배치 처리

**facade 에는 배치 엔드포인트가 없다.** 모든 엔드포인트가 문서 1건 단위다(`service/app.py` 전체 라우트 = 위 9개).

배치는 계층별로 이렇게 나뉜다:

| 계층 | 배치 주체 | 실체 |
|---|---|---|
| **다건 업로드** | **kb-backend(:8088)의 durable batch worker** — facade 밖 | 별도 프로세스 `app.workers.batch_worker`. DB 큐 기반, 기본 capacity **2**. 로그 `/tmp/kb_batch_worker.log`, PID `/tmp/kb_batch_worker.pid`. 워커가 죽으면 업로드가 `queued` 에 머문다 |
| **문서 1건 내부** | parse-svc 페이지 병렬 | VL/paddle_gw 페이지 호출 동시 **3**(`KBP_VL_MAX_CONCURRENT`), 모달 LLM 동시 **3**(`KBP_MODAL_MAX_WORKERS`) |
| **청킹** | adaptive_chunk 비동기 잡 | facade 가 잡 제출 후 3초 간격 폴링 (동기 호출처럼 보이게 은폐) |
| **적재** | edgequake 비동기 task | `async_processing:true` 제출 후 3초 간격 폴링 |
| **커뮤니티** | 오프라인 배치 | `/communities/build` 온디맨드 202, 또는 global 검색 시 build-if-missing |

즉 **facade 에 파일 N개를 병렬로 밀어넣는 것은 소비자 책임**이며, 아래 §5 의 동시성 한계를 반드시 감안해야 한다.

---

## 5. 동시 처리량 — 실측 가능한 상한

### 5.1 facade 프로세스

`scripts/run-facade.sh:44` 의 기동 명령에 `--workers` 가 **없다** → uvicorn **단일 프로세스 / 단일 이벤트루프**.

여기서 나오는 두 가지 서로 다른 동시성:

| 엔드포인트 | 정의 | 실행 위치 | 실질 동시성 |
|---|---|---|---|
| `/parse`, `/ingest` | `async def` | **이벤트루프 위** | **1** — 내부에서 동기(blocking) `httpx.Client` 를 호출하므로 파싱이 끝날 때까지 **이벤트루프 전체가 멈춘다** |
| 나머지 전부 | 일반 `def` | anyio 스레드풀 | 최대 **40** (anyio 기본) — 단, 각 요청이 다운스트림 대기 내내 스레드 1개를 점유 |

> **이것이 실무에서 가장 크게 물리는 지점**이다. `/parse` 나 `/ingest` 가 한 건 돌고 있으면
> 같은 facade 로 들어온 `/healthz` 조차 응답하지 않는다(이벤트루프 블로킹). "facade 가 죽은 것 같다"의
> 대부분은 실제로는 진행 중인 무거운 파싱이다. 로그(`/tmp/facade-kbp.log`)로 확인할 것.
>
> 근거: `service/app.py:113`(`async def parse`)·`:277`(`async def ingest`) → `pc.parse(...)`
> (`service/parse_client.py:33`, 동기 `httpx.Client.post`).

### 5.2 다운스트림 상한 (진짜 병목)

| 구간 | 상한 | 출처 |
|---|---|---|
| **parse-svc** | 사실상 **1건** — `scripts/run-parse-svc.sh` 도 단일 워커이고 `/parse` 가 동일한 async-blocking 구조 | `parse_service/app.py:366` |
| VL OCR 페이지 호출 | 3 (`KBP_VL_MAX_CONCURRENT`) | `parse_service/parsers/ocr/__init__.py:34`, `parsers/pdf/paddle_gw.py:178` |
| 모달 LLM | 3 (`KBP_MODAL_MAX_WORKERS`) | `parse_service/app.py:212` |
| adaptive_chunk | 허브 자체 큐 | — |
| edgequake | 자체 태스크 큐 | — |

**결론: 실질 처리량은 "동시 파싱 1건"에 수렴한다.** 대량 업로드는 반드시 kb-backend 배치 워커(capacity 2)를 통해
직렬화해서 넣어야 하고, facade 에 파일을 병렬로 난사하면 서로 큐에서 대기하다 타임아웃만 유발한다.

### 5.3 타임아웃 매트릭스

| 구간 | 값 | 튜닝 | 출처 |
|---|---|---|---|
| facade → parse-svc (read) | **1800s** | `KBP_PARSE_SVC_TIMEOUT` | `service/app.py:103` |
| facade → adaptive_chunk (HTTP) | 600s | 코드 기본 | `service/adaptive_chunk.py:42-43` |
| adaptive 잡 폴링 총 대기 | **3600s** (간격 3s) | 코드 기본 | `service/adaptive_chunk.py:42-43` |
| facade → edgequake (HTTP) | 600s | 코드 기본 | `service/edgequake.py:50` |
| edgequake insert 폴링 총 대기 | **1200s** (간격 3s) | `insert_chunks(poll_timeout=)` | `service/edgequake.py:365` |
| kb-backend → facade | 1800s | `kb_pipeline_timeout_seconds` (kb-backend 설정) | — |

`httpx.ReadTimeout` 이 보이면 대개 **표 여러 개짜리 PDF 의 순차 모달 LLM** 또는 **두 파싱이 단일 워커 parse-svc 에서 충돌**한 경우다.

### 5.4 알아둘 자원 특성

`get_edgequake()`/`get_adaptive_chunk()`/`get_parse_client()` 는 **요청마다 새 클라이언트**를 만들고
(`service/app.py:89-104`) `httpx.Client` 를 명시적으로 닫지 않는다. 커넥션 재사용이 없고 GC 시점까지 소켓이 남는다.
현재 처리량(동시 1건 수준)에서는 문제되지 않지만, facade 를 다중 워커로 스케일아웃할 때 먼저 손봐야 할 지점이다.

---

## 6. 환경변수

`service/app.py` 는 **dotenv 를 읽지 않는다** — `os.environ` 직독이다. 반드시 런처(`scripts/run-facade.sh`)로 띄워야
`scripts/facade.env`(gitignored)가 주입된다.

| 변수 | 기본값 | 용도 |
|---|---|---|
| `KBP_PARSE_SVC_URL` | `http://localhost:19001` | parse-svc |
| `KBP_ADAPTIVE_CHUNK_URL` | `http://localhost:18060` | 청킹 허브 |
| `KBP_EDGEQUAKE_URL` | `http://localhost:3001` | edgequake |
| `KBP_PG_DSN` | **필수(런처 검증)** | `/communities/build` 전용 DB 접속 |
| `KBP_OPENAI_API_KEY` | **필수(런처 검증)** | 커뮤니티 리포트 LLM |
| `KBP_OPENAI_BASE_URL` | — | 현재 OpenRouter |
| `KBP_LLM_MODEL` | — | 현재 `qwen/qwen3.5-122b-a10b` |
| `KBP_PARSE_SVC_TIMEOUT` | `1800` | parse-svc read timeout(초) |
| `KBP_FACADE_KEY` | 미설정 | **설정 시에만** X-Facade-Key 게이트 활성 |

> ⚠️ `export FOO=... ` 와 `uvicorn ... &` 를 **별개 명령**으로 실행하면 export 가 새 프로세스에 닿지 않는다. 항상 런처 스크립트를 쓸 것.

---

## 7. 기동 · 중지 · 헬스체크

### 7.1 dev(현행 표준) — facade/parse-svc 는 호스트 프로세스, 나머지는 docker

```bash
cd /Users/xxx/workspace/8.kb-pipeline

# 1) 백킹 서비스 (postgres/minio/edgequake/adaptive_chunk/doc_guard)
docker compose up -d --no-build postgres minio edgequake adaptive_chunk
docker compose up -d --no-build edgequake_webui        # 그래프 보기 UI (호스트 :3002)

# 2) 애플리케이션 (호스트 — 라이브 소스 반영)
bash scripts/run-parse-svc.sh    # :19001  — 반드시 facade 보다 먼저
bash scripts/run-facade.sh       # :3000
```

**의존 순서**: postgres → edgequake / adaptive_chunk / minio → parse-svc → facade.
facade 는 기동 시 다운스트림을 확인하지 않으므로 순서를 어겨도 뜨긴 뜨지만, 첫 요청에서 연결 거부가 난다.

두 런처가 공통으로 하는 일: **docker-shadow 가드**(같은 이름의 compose 컨테이너 stop) → env 로드 →
**포트가 비워질 때까지 대기하며 기존 프로세스 종료** → nohup 기동 → healthz 검증(10초).

> **docker-shadow 함정**: dev 에서 `docker compose up -d` 를 통째로 실행하면 facade/parse-svc **컨테이너**(옛 이미지 코드)가
> 뜨면서 호스트 프로세스를 가린다. facade 는 compose DNS `parse-svc:19001` 로 부르므로 소스 수정이 무시되고 "옛날 파싱"이 나온다.
> dev 에서는 이 두 서비스를 **docker 로 올리지 말 것**. 런처가 자동으로 shadow 컨테이너를 stop 하지만, 순서상 나중에 compose up 하면 다시 가려진다.

### 7.2 중지

전용 stop 스크립트는 없다. 재기동은 런처가 알아서 죽이고 다시 띄우므로, **완전 중지**만 아래처럼 한다.

```bash
# facade
pkill -f "uvicorn service.app:app"
# parse-svc
pkill -f "parse_service.app:app"

# 확실하게 (포트 기준 — 권장. 모듈 패턴 kill 은 다른 서비스를 오폭할 수 있다)
kill "$(lsof -nP -iTCP:3000 -sTCP:LISTEN -t)"
kill "$(lsof -nP -iTCP:19001 -sTCP:LISTEN -t)"

# 백킹 서비스 (볼륨 보존 — postgres/minio 데이터 유지)
docker compose stop
# 컨테이너까지 제거하되 볼륨은 유지
docker compose down
```

> ⚠️ `docker compose down -v` 는 `eq_pg_data`·`minio_data` 볼륨을 지운다 = **적재 데이터 전소**. 쓰지 말 것.
> 마찬가지로 `service/scripts/start_dedicated_edgequake.sh` 는 postgres 를 재생성한다(데이터 소거) — 핫 재기동엔 부적합.

### 7.3 헬스체크

```bash
# facade
curl -fsS http://localhost:3000/healthz          # {"status":"ok"}
# parse-svc  (deps.vl_ocr = 실 OCR origin)
curl -fsS http://localhost:19001/healthz          # {"status":"ok","deps":{"vl_ocr":"https://openrouter.ai/..."}}
# 다운스트림
curl -fsS http://localhost:3001/health            # edgequake
curl -fsS http://localhost:18060/healthz          # adaptive_chunk
curl -fsS http://localhost:3002/popup/graph      # edgequake webui (그래프 보기)
docker compose ps                                  # 컨테이너 healthy 여부
```

**healthz 는 "프로세스가 떴다"만 증명한다.** 실제 파이프라인이 동작하는지는 아래 스모크로 확인한다.

```bash
# ① parse 왕복 — java(OpenDataLoader) + 모달 경로까지 증명
curl -s -m 1800 -F "file=@sample.pdf;type=application/pdf" http://localhost:3000/parse \
  | jq '{status, n_blocks, enriched_len: (.enriched_content|length), page_count}'
# 기대: status 키 없음(=성공) + n_blocks > 0 + enriched_len > 0
# enriched_len == 0 이면 → java 미탑재 의심 ("Unable to locate a Java Runtime")

# ② e2e 적재 — chunk_count > 0 & status "indexed"
curl -s -m 3600 -F "file=@sample.pdf" -F "workspace_id=smoke-kb" -F "doc_id=smoke-1" \
  http://localhost:3000/ingest | jq '{status, chunk_count, method: .chunking_selection.method_selected}'

# ③ 검색 왕복
curl -s -X POST http://localhost:3000/search -H 'Content-Type: application/json' \
  -d '{"workspace_id":"smoke-kb","query":"핵심 내용 요약","top_k":5}' | jq '{answer, n: (.results|length)}'
```

> 스모크는 사용자 업로드가 진행 중일 때 돌리지 말 것 — parse-svc 는 단일 워커라 두 파싱이 직렬화되며
> facade 타임아웃을 유발한다(§5.2).

### 7.4 로그

| 서비스 | 위치 |
|---|---|
| facade | `/tmp/facade-kbp.log` (`FACADE_LOG` 로 변경 가능) |
| parse-svc | `/tmp/parse_svc.log` (`PARSE_SVC_LOG`) |
| kb-backend batch worker | `/tmp/kb_batch_worker.log`, PID `/tmp/kb_batch_worker.pid` |
| 컨테이너 | `docker compose logs -f edgequake` / `adaptive_chunk` / … |

### 7.5 컨테이너 운영 모드 (dev 아님)

전 서비스를 compose 로 띄우는 구성. `facade` 컨테이너는 compose DNS(`parse-svc:19001`, `adaptive_chunk:18060`,
`edgequake:8081`)로 배선되고 `depends_on: service_healthy` 로 순서가 보장된다.

```bash
docker compose up -d --build             # 전체
docker compose up -d --build facade      # 단일 서비스 재빌드
```

전제: `edgequake/` 서브모듈 체크아웃(`git submodule update --init --recursive edgequake`) + `.env` 실값.

---

## 8. 트러블슈팅 — 증상 → 원인

| 증상 | 원인 | 조치 |
|---|---|---|
| `/parse` 가 `enriched_content: ""` | OpenDataLoader 의 `java` 부재(macOS `/usr/bin/java` 는 스텁) | `brew install openjdk@17` 후 `run-parse-svc.sh` (PATH 를 자동 고정) |
| 소스를 고쳤는데 **옛 동작** | docker-shadow — compose 컨테이너가 호스트 프로세스를 가림 | `docker compose stop facade parse-svc` 후 런처 재실행 |
| `httpx.ReadTimeout` | 표 다수 PDF 의 순차 모달 LLM, 또는 파싱 2건 충돌 | `KBP_PARSE_SVC_TIMEOUT` 상향 / 업로드 직렬화 |
| facade 가 `/healthz` 조차 무응답 | `/parse`·`/ingest` 가 이벤트루프 블로킹 중(§5.1) | 로그에서 진행 중 파싱 확인 — 대개 정상, 기다릴 것 |
| `KeyError: KBP_OPENAI_API_KEY` | 런처 없이 uvicorn 직접 기동 | 반드시 `scripts/run-facade.sh` 사용 |
| `/insert` 가 `status:"failed"` + poll timeout | edgequake 엔티티 추출(LLM) 지연 | `phases` 로 병목 확인. 급하면 `extract_graph:false` |
| 적재는 됐는데 그래프가 빔 | `extract_graph:false` 또는 edgequake LLM 실패 | `entity_count`/`relationship_count` 확인 |
| **HTTP 422 적재 실패** | edgequake 가 `EDGEQUAKE_CHUNKER=adaptive` 로 떠서 이중청킹 | **반드시 `passthrough`** (불변식) |
| 배치가 `queued` 에 머묾 | kb-backend batch worker 미기동 | `bash scripts/run-kb-backend.sh`, `kill -0 $(cat /tmp/kb_batch_worker.pid)` |
| "그래프 보기" 실패 | `edgequake_webui` 미기동 | `docker compose up -d --no-build edgequake_webui` → `:3002` 검증 |
| 검색 결과 0건인데 에러 없음 | RLS 세션 미설정(`app.current_tenant_id`) 또는 workspace 불일치 | `edgequake_workspace_id` 가 KB 에 제대로 영속됐는지 확인 |

---

## 9. 관련 문서

- `_workspace/README.md` — 인덱스 · ADR · 포트/불변식
- `_workspace/01-architecture.md` — 전체 파이프라인 · 컴포넌트 설계 · 데이터 계약
- `_workspace/03-dev-progress.md` — 작업항목 상태 · 타이밍 모니터링
- `docs/kb-pipeline-process-definition.md` — 프로세스 정의서 v1.0
- `.claude/skills/restart-kbp-stack/SKILL.md` — 스택 재기동 절차(배치 워커 · 그래프 UI 포함)
