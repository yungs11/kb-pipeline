# 파서 전용(파싱 배치) 설치·사용 가이드

문서를 **파싱만** 하고 결과(마크다운 + 표 HTML + 페이지 이미지)를 받아 가는 배포.
청킹·적재·검색은 하지 않는다. 전체 스택 9개 대신 **5개**만 띄운다.

- 전체 스택이 필요하면 → [`airgap-deploy.md`](airgap-deploy.md)
- API 상세 규격은 → [`facade-api.md`](facade-api.md) (본 문서는 파싱 경로만 발췌)

---

## 1. 구성 — 왜 5개인가

| 서비스 | 역할 | 빼면 |
|---|---|---|
| `parse-svc` | 실제 파싱 엔진 | — |
| `facade` | 잡 접수 API (`POST /parse`) | 동시성 제어·재시도 없음 |
| **`facade-worker`** | **잡 실행**(facade 와 같은 이미지, 명령만 다름) | ⚠️ healthz 는 전부 통과하는데 `/parse` 가 **503**("no live facade-worker") — **한 건도 처리 안 됨** |
| `postgres` | 잡 큐(`kbp.jobs`). 기동 시 스키마 자동 생성 — **빈 DB로 충분** | 잡 큐 불가 |
| **`minio`** | 잡 staging(업로드 바이트 보관) | ⚠️ 잡 접수가 **`NoSuchBucket` 500** — 파서 단독이면 없어도 되지만 **facade 잡 큐엔 필수** |

**빠지는 것**: `edgequake`(적재·검색) / `adaptive_chunk`(청킹) / `doc_guard`(엑셀 게이트 판정) / `edgequake_webui`
→ `/chunk`·`/insert`·`/search`·`/gate` 는 이 구성에서 **동작하지 않는다**.

> **엑셀은?** 파싱과 게이트 판정 재료(`gate_summary`) 생성까지는 **parse-svc 안에서** 끝난다.
> `doc_guard` 는 그 재료로 **판정**만 하므로, 이 구성에서도 xlsx 파싱과 `gate_summary` 수신은 된다.
> 자동 반려 판정만 없다.

### 더 가볍게 — parse-svc 단독

잡 큐가 필요 없으면 `parse-svc` 하나만 띄우고 `:19001/parse` 를 직접 호출해도 된다.
MinIO 없이도 동작한다(페이지 이미지만 `minio_object: null` 로 degrade).
대신 **동시성 제어·재시도·유량제어는 직접** 해야 한다.

---

## 2. 설치

### 2-1. 번들 준비 (온라인 머신)

```bash
cd /path/to/8.kb-pipeline
./scripts/airgap/build-bundle.sh --parse-only     # 축소 번들(edgequake 제외 → 훨씬 작고 빠름)
# 또는 전체 번들을 그대로 써도 된다(parse-only-up.sh 가 함께 들어있다)
```

산출물: `dist/kbp-parse-bundle-<arch>.tar.gz`

### 2-2. 대상 서버에서

```bash
mkdir kbp && tar xzf kbp-parse-bundle-amd64.tar.gz -C kbp && cd kbp
cp .env.airgap.example .env
vi .env
./scripts/airgap/parse-only-up.sh
```

**docker·podman 둘 다 지원**한다(자동 탐지). 강제 지정은 `KBP_ENGINE=docker`.
인터넷이 되는 환경이면 번들 tar 없이 이미 빌드/pull 된 이미지로도 기동한다.

### 2-3. `.env` — 파싱에 필요한 값만

| 키 | 필요 시점 | 안 채우면 |
|---|---|---|
| `KBP_FACADE_KEY` | 항상(권장) | 게이트가 꺼져 **무인증**으로 열린다 |
| `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` | 항상 | 잡 staging 불가 |
| `KBP_FILECONVERT_URL` / `_TOKEN` | **docx·hwp·hwpx·ppt·pptx·html** 파싱 시 | 그 확장자 **전부 실패** |
| `KBP_DRM_URL` / `_TOKEN` | **DRM(Fasoo) 문서**가 있을 때 | DRM 문서 **전부 실패**<br>(`detail: "parse_failed: KBP_DRM_URL 미설정 — DRM 해제 불가"`) |
| `MODEL_API_URL` / `MODEL_API_KEY` | **스캔 PDF·이미지·PPTX** 파싱 시(VL-OCR) | 그 문서들 `enriched_content` 가 빈다 |
| `KBP_OPENAI_*` | 모달(표/그림) 보강 LLM | 보강 없이 진행 |

임베딩·리랭커·edgequake 관련 키는 **이 구성에서 안 쓴다**(비워둬도 무방).

> **DRM 판정은 매직바이트로 한다** — 파일 앞부분에 `DRMONE` 이 있으면 DRM 문서로 보고
> 해제 API를 호출한다. 비-DRM 파일은 이 경로를 **아예 타지 않으므로** DRM 문서를 다루지
> 않는다면 `KBP_DRM_URL` 을 비워둬도 된다.

### 2-4. 기동 확인

스크립트가 자동으로 확인하지만 수동으로는:

```bash
curl -fsS http://localhost:19001/healthz                      # parse-svc
curl -fsS http://localhost:3000/healthz                       # facade
curl -fsS -H "X-Facade-Key: $KBP_FACADE_KEY" \
     http://localhost:3000/jobs/workers                       # ★ 가장 중요
#   → {"online":true,"capacity":4,"active":0,"available":4, ...}
```

`online:false` 면 **적재가 한 건도 안 된다** — `facade-worker` 컨테이너 로그를 본다.

---

## 3. 사용법

### 3-1. 문서 1건 파싱

```bash
curl -sS -H "X-Facade-Key: $KBP_FACADE_KEY" \
     -F "file=@문서.pdf" \
     http://localhost:3000/parse
```

**동기 API처럼 쓰면 된다.** 내부적으로 잡을 만들어 큐에 넣고 **완료까지 기다린 뒤 결과 본문을 반환**한다.
폴링 코드를 짤 필요가 없다.

선택 필드:
- `-F "filename=원본이름.pdf"` — 미지정 시 업로드 파트의 파일명 사용
- `-F "docs_id=$(shasum -a 256 문서.pdf | cut -c1-16)"` — 페이지 이미지 MinIO 키를 소비자와 일치시키고 싶을 때

### 3-2. ⚠️ 실패 판정 — HTTP 200 만 보면 안 된다

파싱 실패는 **5xx 가 아니라 `200 + 실패 바디`** 로 온다.

```json
{"status": "failed", "detail": "parse_failed: KBP_DRM_URL 미설정 — DRM 해제 불가"}
```

반드시 `status == "failed"` 를 확인할 것. `detail` 에 실제 원인이 실려 온다.

```bash
curl -sS ... http://localhost:3000/parse | jq 'if .status=="failed" then .detail else {n_blocks, page_count} end'
```

### 3-3. 성공 응답

```jsonc
{
  "enriched_content": "# 제목\n\n본문…  <table>…</table>  〈MODAL id=\"I1\" type=\"image\"〉…〈/MODAL〉",
  "n_blocks": 42,
  "page_count": 12,
  "chunk_needed": true,
  "docs_id": "9f2c1a…",                                    // 16 hex
  "pages":      [{"page_number":1,"page_uuid":"9f2c…_1","minio_object":"9f2c…/9f2c…_1.jpeg"}],
  "page_spans": [{"page_number":1,"char_start":0,"char_end":980}],
  "modal_spans":  [{"id":"T1","type":"table","char_range":[1200,2400]}],
  "table_blocks": [{"category":"table","content":"<table>…</table>","page_number":3}],
  "timing_metrics": {"parse_ms":8120.4, "drm_ms":391.4, "convert_ms":0.0, "render_upload_ms":640.2}
}
```

주요 필드:

| 필드 | 의미 |
|---|---|
| `enriched_content` | 파싱 본문. 마크다운 + **표는 `<table>` HTML 그대로**(pipe 평탄화하지 않는다) + 모달 마커 |
| `page_spans` | `enriched_content` 안에서 각 페이지가 차지하는 문자 범위 → **청크별 페이지 귀속**에 쓴다 |
| `pages[].minio_object` | 페이지 이미지 객체 키. **실제로 올라간 것만** 값이 있다(미업로드면 `null`) |
| `modal_spans` | 표·이미지 등 원자 영역. 청킹 시 이 구간을 쪼개면 안 된다 |
| `table_blocks` | 표 블록만 별도로. 표 품질 점수 계산 등에 쓴다 |
| `chunk_needed` | `false` 면 엑셀 — `chunks` 가 이미 들어 있으니 추가 청킹 금지 |

### 3-4. 엑셀은 응답이 다르다

xlsx/xlsm/xls 는 parse-svc 가 파싱+청킹을 함께 끝낸다.

```jsonc
{
  "chunk_needed": false,
  "chunks": [{"chunk_index":0,"text":"…","titles_context":["시트1","표A"],"pages":[]}],
  "chunk_strategy": "excel_rag_parser",
  "gate_summary": {"ok": true, "sheets": [...]}     // 엑셀 게이트 판정 재료
}
```

### 3-5. 배치 — 병렬로 던진다

facade `/parse` 는 요청마다 블로킹이므로, **배치 = 클라이언트에서 파일들을 병렬 호출**하는 것이다.
동시성·재시도·정리는 잡 큐가 한다.

```bash
find ./docs -name '*.pdf' -print0 | xargs -0 -P 4 -I{} \
  curl -sS -H "X-Facade-Key: $KBP_FACADE_KEY" -F "file=@{}" \
       http://localhost:3000/parse -o "결과/$(basename {}).json"
```

| 튜닝 키 | 기본 | 의미 |
|---|---|---|
| `KBP_JOB_LIMIT_PARSE` | 4 | **동시 파싱 수**. 대량이면 여기부터 올린다 |
| `KBP_JOB_MAX_WAITERS` | 4 | **동시 대기자 수**. 초과 요청은 거절되니 클라이언트 병렬도(`-P`)를 이 값 이하로 |
| `KBP_JOB_MAX_UPLOAD_BYTES` | 52428800 (50MB) | 업로드 상한 |
| `KBP_JOB_LEGACY_WAIT_SECONDS` | 3300 | 한 건당 대기 상한(초) |

> `-P` 를 `KBP_JOB_MAX_WAITERS` 보다 크게 잡으면 초과분이 **거절**된다. 둘을 같이 올려야 한다.

---

## 4. 지원 확장자

| 확장자 | 경로 | 추가 필요 |
|---|---|---|
| pdf | ODL 직행(스캔 페이지는 VL 보충) | 스캔이면 `MODEL_API_URL` |
| xlsx / xlsm / xls | 자체 청킹(`chunk_needed:false`) | — |
| png / jpg / jpeg / gif / bmp / tif / webp | VL-OCR | `MODEL_API_URL` |
| txt / md / markdown / csv / json / log | 그대로 블록화 | — |
| **docx · hwp · hwpx · ppt · pptx · html · htm** | **파일변환 API → PDF → ODL** | **`KBP_FILECONVERT_URL`** |

DRM(Fasoo) 래핑 파일은 **확장자와 무관하게** 먼저 `KBP_DRM_URL` 로 해제한 뒤 위 경로를 탄다.

---

## 5. 트러블슈팅

| 증상 | 원인 | 조치 |
|---|---|---|
| `/parse` 가 **503** | `facade-worker` 미기동 | `/jobs/workers` 로 `online` 확인 → worker 컨테이너 로그 |
| `/parse` 가 **500 `NoSuchBucket`** | MinIO 버킷 미생성 | `parse-only-up.sh` 가 자동 생성한다. 수동은 [`airgap-deploy.md` 부록 B](airgap-deploy.md) |
| `status:"failed"`, `detail: "…KBP_DRM_URL 미설정…"` | DRM 문서인데 해제 API 미설정 | `.env` 의 `KBP_DRM_URL`/`_TOKEN` |
| `detail` 에 파일변환 관련 오류 | docx/hwp/ppt/html 인데 변환 API 불통 | `KBP_FILECONVERT_URL` 도달성 확인 |
| `enriched_content` 가 비었는데 실패는 아님 | 스캔 문서인데 VL 미설정/불통 | `MODEL_API_URL` 확인. parse-svc 로그에 `VL API CONNECT_ERROR` |
| 401 / 403 | `X-Facade-Key` 불일치 | `.env` 의 `KBP_FACADE_KEY` 와 헤더 값 일치 |
| 요청이 거절됨(대기자 초과) | 클라이언트 병렬도 > `KBP_JOB_MAX_WAITERS` | 병렬도를 낮추거나 두 값을 함께 올린다 |

---

## 6. 검증 상태 (2026-08-07)

docker 환경에서 실측 확인한 범위:

| 항목 | 결과 |
|---|---|
| 5개 구성 기동(엔진 자동탐지·버킷 생성·worker 등록) | ✅ |
| 일반 PDF 파싱(헤딩·본문·`page_spans`·페이지이미지 업로드) | ✅ |
| **DRM(Fasoo) PDF** — 실제 해제 API 호출 | ✅ `drm_ms` 기록, 해제 성공 |
| 호스트 포트 변경 시에도 정상 동작 | ✅ |

**미검증**: 실제 온프렘 VL/파일변환 서버 연동(목업으로 대체), 대량 동시 배치의 성능·안정성.
