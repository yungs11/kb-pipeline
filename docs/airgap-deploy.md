# kb-pipeline 폐쇄망(air-gap) 배포 매뉴얼 — RHEL + Podman

인터넷이 안 되는 폐쇄망 RHEL 서버에 kb-pipeline 스택을 **빌드/pull 없이** 구동하는 절차.
온라인 개발기에서 amd64 이미지를 만들어 단일 번들로 옮긴다.

- 단일 진실 출처: `docker-compose.airgap.yml` + `.env`(← `.env.airgap.example`) + `images/*.tar.gz`
- 스크립트: `scripts/airgap/{build-bundle,load-and-up,verify-bundle,deploy-both,parse-only-up}.sh`
- 일반(온라인) docker 구동은 `docs/kbp-docker-startup.md`. 본 문서는 **폐쇄망 전용**.

## 어떤 배포를 할 것인가 — 먼저 고르세요

| 목적 | 쓸 스크립트 | 서비스 수 | 절차 |
|---|---|---|---|
| **kbp만** (적재·검색 엔진) | `load-and-up.sh` | 9 | §1→§5 |
| **kbp + kb 웹앱** (전체) | `deploy-both.sh` | 9 + 4 | §1→§6 |
| **파싱 배치만** (청킹·적재·검색 불필요) | `parse-only-up.sh` | 5 | §7 |

---

## ✅ 검증 상태 (2026-08-07)

무엇이 **실제로 검증됐고** 무엇이 안 됐는지 명시한다. 안 된 것을 된 것처럼 쓰지 않는다.

**검증 환경**: RHEL 계열(Fedora) + **podman 4.9.4** + **podman-compose 1.6.0** + 인터넷 완전 차단.
LLM·임베딩·리랭커·VL은 스키마 호환 목업으로 대체(응답 품질은 검증 대상 아님).

| 항목 | 결과 |
|---|---|
| 번들 전송 → `sha256sum -c` 무결성 → 재결합 → 압축해제 | ✅ |
| `deploy-both.sh` → kbp 9개 전부 healthy | ✅ |
| MinIO 버킷 자동 생성 / facade-worker 등록 | ✅ |
| kb `/readyz`(스키마 마이그레이션 완료 판정) | ✅ |
| **관리자 시드**(`admin@kb.local`, role=developer) | ✅ |
| **적재 한 바퀴**(gate→parse→chunk→insert→persist_meta) | ✅ `succeeded` |
| **검색**(facade `/search`) | ✅ HTTP 200, 원문 청크 + 그래프 엔티티 반환 |

**검증하지 못한 것 (그대로 두면 위험한 가정)**
- **실제 온프렘 LLM/임베딩/리랭커/VL/파일변환 서버와의 연동** — 전부 목업이었다.
  주소·인증·응답 스키마가 사내 서버와 맞는지는 **현장에서 반드시 확인**해야 한다.
- **네트워크 백엔드가 `cni`인 환경** — 검증 환경은 `netavark`였다. RHEL 8 계열은 `cni`일
  수 있고, 그 경우 컨테이너 이름 DNS에 별도 패키지가 필요하다(§3).
- 대용량·다건 동시 적재의 성능/안정성.

---

## 0. 구성 (번들에 포함되는 9개 이미지)

| 티어 | 서비스 | 이미지 | 호스트 포트 | 출처 |
|------|--------|--------|-------------|------|
| 인프라 | postgres | `kbp-postgres:airgap` | 5433 | pull(digest 고정) → 로컬 태그 |
| 인프라 | minio | `kbp-minio:airgap` | 3003(콘솔) | pull → 로컬 태그 |
| 엔진 | edgequake | `kbp-edgequake:airgap` | 3001 | 빌드 |
| 문서 | doc_guard | `kbp-doc_guard:airgap` | (내부) | 빌드 |
| 문서 | adaptive_chunk | `kbp-adaptive_chunk:airgap` | 18060 | 빌드 |
| 앱 | parse-svc | `kbp-parse-svc:airgap` | 19001 | 빌드 |
| 앱 | facade | `kbp-facade:airgap` | **3000** | 빌드 |
| 앱 | facade-worker | `kbp-facade:airgap`(명령만 다름) | (내부) | 재사용 |
| 확인용 | edgequake_webui | `kbp-edgequake_webui:airgap` | **3002** | 빌드 |

포트는 **호스트 발행 포트**다. 권위 출처는 `docker-compose.airgap.yml`,
전체 포트 맵은 `docs/architecture-ports.md`.

기동 순서: postgres → edgequake / minio / doc_guard / adaptive_chunk → parse-svc →
facade / edgequake_webui.

> **인프라 이미지도 로컬 태그(`kbp-*:airgap`)로 참조한다.** build-bundle.sh 가 재현성을
> 위해 digest 로 pull 한 뒤 로컬 태그를 붙여 save 하기 때문이다.
> compose 가 업스트림 digest(`...@sha256:...`)를 직접 참조하면 **podman 배포가 깨진다**:
> `docker save` 된 digest-only 이미지가 `podman load` 시 `<none>:<none>` 로 들어와
> `image not known` 이 되고 그 서비스가 아예 안 뜬다(실측 2026-08-07).
> **docker 는 관대해서 넘어가므로 docker 로만 테스트하면 절대 안 잡힌다.**
> 업스트림 버전을 올릴 땐 `scripts/airgap/build-bundle.sh` 의 `PULLS` digest 를 갱신한다.

### 런타임 외부 의존 (번들에 없다 — 사내에 있어야 한다)

스택은 실행 중 아래 5가지를 HTTP로 호출한다. `.env`에 **사내 주소**로 설정해야 한다(§4).

LLM · 임베딩 · 리랭커 · VL-OCR · **파일변환(한컴)**

파일변환은 2026-08-06부터 **docx/hwp/ppt/html 파싱의 유일한 경로**다(구 kordoc 폴백 제거).
미설정이면 그 확장자 전부 파싱 실패한다.

---

## 1. [Phase A] 온라인 준비 — 번들 생성

인터넷 되는 머신(Docker Desktop)에서 1회.

```bash
cd /path/to/8.kb-pipeline
./scripts/airgap/build-bundle.sh                 # 전체(9개 이미지)
./scripts/airgap/build-bundle.sh --parse-only    # 파싱 배치용 축소 번들(§7)
./scripts/airgap/build-bundle.sh --no-build      # 이미지 재사용, 번들만 다시 묶기
```

- ARM 서버면 `PLATFORM=linux/arm64`.
- edgequake(Rust) 최초 빌드가 오래 걸린다(캐시 후엔 빠름).
- 산출물: `dist/kbp-airgap-bundle-<arch>.tar.gz` (+ `.sha256`, 2GB 초과 시 분할).

검증(선택): `./scripts/airgap/verify-bundle.sh --images`

> **소스를 고쳤으면 이미지도 다시 빌드해야 한다.** 스크립트·`.env`만 바뀐 경우엔
> `--no-build` 로 충분하다. 헷갈리면 이미지 안 파일과 소스를 직접 비교하라:
> `docker run --rm --entrypoint sh kbp-facade:airgap -c 'md5sum /app/service/app.py'`

---

## 2. 전송 (2GB 분할)

2GB를 넘으면 자동으로 분할된다(조각 `.part-aa`, `.part-ab`… + `.parts.sha256`).

```bash
# 폐쇄망 서버에서
sha256sum -c kbp-airgap-bundle-amd64.tar.gz.parts.sha256        # 조각 무결성
cat kbp-airgap-bundle-amd64.tar.gz.part-* > kbp-airgap-bundle-amd64.tar.gz
sha256sum -c kbp-airgap-bundle-amd64.tar.gz.sha256              # 재결합 무결성
```

`SPLIT_SIZE=1g` 로 조각 크기 변경, `KEEP_WHOLE=1` 로 원본 보존.

---

## 3. [Phase B] 서버 전제조건 — 컨테이너 이름 DNS ⚠️ RHEL 8 주의

이 스택은 **전부 컨테이너 이름으로** 통신한다(`facade:19000`, `parse-svc:19001`,
`postgres:5432`, kb→`facade`). 이름 해석이 안 되면 컨테이너는 다 뜨는데 통신만 안 되고,
증상이 서비스마다 제각각(타임아웃/커넥션거부)이라 진단이 오래 걸린다.

```bash
podman info --format '{{.Host.NetworkBackend}}'   # cni / netavark
dnf install -y podman-plugins    # cni 인 경우 (dnsname 플러그인)
dnf install -y aardvark-dns      # netavark 인 경우
```

> RHEL 8.x는 백엔드가 `cni`일 수 있다. 업스트림 podman 컨테이너 이미지는 같은 4.9라도
> netavark가 기본이라 **개발기 테스트로는 이 차이가 드러나지 않는다** — 배포 대상에서
> 직접 확인할 것. `load-and-up.sh` 는 기동 **전에** 임시 네트워크로 실제 이름 해석을
> 시도하고, 실패하면 백엔드에 맞는 설치 명령을 안내하며 즉시 중단한다.

### ⚠️ 같은 서버에서 dev 스택과 함께 쓸 때 — 프로젝트명 충돌

`docker-compose.yml`(dev)과 `docker-compose.airgap.yml` 은 **둘 다 `name: kbp`** 다.
compose 는 프로젝트명으로 컨테이너·볼륨을 식별하므로, 같은 머신에서 두 파일을 그냥 쓰면
**나중에 띄운 쪽이 먼저 뜬 쪽의 컨테이너와 볼륨을 인수한다.**

실측(2026-08-07): 개발기에서 airgap compose 를 그대로 올렸더니 dev 스택의 postgres 가
airgap 이미지(pg18)로 교체됐고, 기존 볼륨(pg16 데이터)과 레이아웃이 안 맞아 기동 불가가 됐다.
**데이터는 남아 있었지만 dev 스택이 통째로 내려갔다.**

폐쇄망 운영 서버에는 dev 스택이 없으므로 문제되지 않는다. 다만 **검증 서버·개발기에서
두 스택을 함께 돌린다면 반드시 프로젝트명을 분리**한다:

```bash
podman-compose -p kbp-airgap -f docker-compose.airgap.yml --env-file .env up -d
# 또는 compose 파일 최상단의 `name: kbp` 를 다른 값으로 바꾼다
```

호스트 포트도 함께 겹치므로(5433·3000·3003·19001 …) `ports:` **좌측 숫자**도 바꿔야 한다.

---

## 4. [Phase B] `.env` 채우기 (가장 중요)

```bash
mkdir kbp && tar xzf kbp-airgap-bundle-amd64.tar.gz -C kbp && cd kbp
cp .env.airgap.example .env
vi .env      # 【A. 온프렘 재설정 필수】 블록
```

| 그룹 | 키 | 사내 예시 | 안 바꾸면 |
|------|----|-----------|-----------|
| LLM | `KBP_OPENAI_BASE_URL`, `ADAPTIVE_CHUNK_OPENROUTER_BASE_URL`, `*_LLM_MODEL`, `*_API_KEY` | `http://llm.corp:8000/v1` | 그래프추출·합성·청킹 실패 |
| **edgequake LLM provider** | `EDGEQUAKE_LLM_PROVIDER` — **기본값 `openai-compatible` 유지** | — | 아래 ⚠️ 참고 |
| VL-OCR | `MODEL_API_URL`, `MODEL_API_KEY` | `http://vl.corp:8000/v1/chat/completions` | 이미지/PPTX 파싱 빈 결과 |
| 임베딩 | `LITELLM_EMBEDDING_BASE_URL`, `ADAPTIVE_CHUNK_SCORING_EMBEDDING_BASE_URL`, `*_API_KEY`, `*_MODEL` | `http://embed.corp:8000/v1` | 적재/검색 임베딩 실패 |
| 리랭커 | `EDGEQUAKE_RERANK_BASE_URL`(→`/v1/rerank`), `ADAPTIVE_CHUNK_RERANK_BASE_URL`(→`/v1`) | `http://rerank.corp:8000/...` | BM25 폴백(품질 저하, 비치명) |
| 파일변환(한컴) | `KBP_FILECONVERT_URL`, `KBP_FILECONVERT_TOKEN` | `http://fileconvert.corp/api/fileconvert/agent/tool` | **docx/hwp/ppt/html 전면 실패** |
| facade 게이트 | `KBP_FACADE_KEY` | `openssl rand -hex 32` | 무인증으로 적재·삭제가 열린다 |

> ⚠️ **`EDGEQUAKE_LLM_PROVIDER` 는 건드리지 말 것.** 실측(2026-08-07):
> - `openrouter` → base_url 오버라이드가 없어 `https://openrouter.ai` 하드코딩. 폐쇄망 100% 실패.
> - `openai` → COMPAT-GUARD가 비-`gpt-*` 모델명(예: `qwen/...`)을 감지해 `gpt-4.1-nano` +
>   `api.openai.com` 으로 **조용히 되돌린다**. 역시 실패.
> - `openai-compatible`(기본값) → 이 가드 대상이 아니라 커스텀 모델명·base_url이 그대로 간다.

【B. 내부 전용】 블록(MinIO·Postgres·인트라스택 DNS)은 손대지 않는다.
검증: `./scripts/airgap/verify-bundle.sh --env .env`

---

## 5. [Phase B] 로드 + 기동 (kbp 단독)

```bash
./scripts/airgap/load-and-up.sh
```

순서: `podman load` → `.env` 검증 → **컨테이너 DNS 사전점검**(§3) →
`podman-compose up -d` → **health 폴링** → **MinIO 버킷 생성**(멱등) → 스모크 요약.

스모크:
```bash
curl -fsS http://localhost:3000/healthz      # facade
curl -fsS http://localhost:3001/health       # edgequake
curl -fsS http://localhost:19001/healthz     # parse-svc

# ★ facade-worker 등록 — 0 이면 healthz 는 다 통과해도 /parse·/ingest 가 503
curl -fsS -H "X-Facade-Key: $KBP_FACADE_KEY" http://localhost:3000/jobs/workers
#   → {"online":true, "capacity":N, ...}
```

그래프 UI: `http://<서버IP>:3002` (`EDGEQUAKE_WEBUI_API_URL` 을 서버 IP:3001 로).

---

## 6. kbp + kb 한 번에 — `deploy-both.sh`

두 번들을 각각 압축 해제해 같은 서버에 `kbp/`, `kb/` 로 두고 각자 `.env` 를 채운 뒤:

```bash
cd kbp
./scripts/airgap/deploy-both.sh ../kb
```

① kbp `load-and-up.sh` → ② kbp가 만든 **실제** 네트워크 이름 조회(`podman network ls`,
추측하지 않음) → ③ `kb/.env` 의 `KBP_NETWORK`/`KBP_FACADE_KEY` 자동 동기화 →
④ kb `load-and-up.sh` → ⑤ 헬스체크 요약.

③이 핵심이다 — 두 값이 어긋나면 kb가 네트워크를 못 찾거나 적재·검색이 401/403으로 막히는데,
수동 배포에서 가장 자주 놓치는 지점이다.

kb 쪽 나머지 값은 `knowledge_base/docs/airgap-deploy.md` 참고
(`JWT_SECRET`/`CREDENTIAL_ENCRYPTION_KEY` 는 비워두면 **자동 생성**된다).

---

## 7. 파싱 배치 전용 — `parse-only-up.sh`

> 설치부터 API 사용법·배치 튜닝·트러블슈팅까지 전용 문서가 있다 →
> **[`parse-only-guide.md`](parse-only-guide.md)**. 아래는 요약이다.

청킹·적재·검색 없이 **대량 파싱만** 할 때. 9개 대신 **5개**만 띄운다.

```bash
./scripts/airgap/parse-only-up.sh
```

| 서비스 | 왜 필요한가 |
|---|---|
| parse-svc | 파싱 엔진 |
| facade | 잡 접수 API |
| **facade-worker** | **잡 실행.** 없으면 healthz 다 통과해도 `/parse` 가 503 → 한 건도 처리 안 됨 |
| postgres | 잡 큐(`kbp.jobs`). 기동 시 스키마 자동 생성 — 빈 DB로 충분 |
| **minio** | **잡 staging.** 파서 단독이면 없어도 되지만 facade 잡 큐엔 **필수** — 없으면 접수가 `NoSuchBucket` 500 |

빠지는 것: edgequake / adaptive_chunk / doc_guard / edgequake_webui
→ `/chunk`·`/insert`·`/search`·`/gate` 는 이 구성에서 **동작하지 않는다**.

**엔진 무관**: docker·podman 둘 다 지원한다(자동 탐지, `KBP_ENGINE=docker` 로 강제 가능).
Windows/Linux Docker 환경에도 그대로 쓸 수 있고, 인터넷이 되는 환경이면 번들 tar 없이
이미 빌드/pull 된 이미지로도 기동한다.

문서 던지기:
```bash
curl -sS -H "X-Facade-Key: $KBP_FACADE_KEY" -F "file=@문서.pdf" http://localhost:3000/parse
# → 파싱 결과 JSON (잡 큐가 뒤에서 처리하고 완료까지 대기 후 반환)
```

배치 튜닝: `KBP_JOB_LIMIT_PARSE`(기본 4, 동시 처리), `KBP_JOB_MAX_WAITERS`(기본 4,
동시 대기자 — 초과 요청은 거절), `KBP_JOB_MAX_UPLOAD_BYTES`(기본 50MB),
`KBP_JOB_LEGACY_WAIT_SECONDS`(기본 3300s).

잡 큐 없이 더 가볍게: `parse-svc` 직접 호출(`:19001/parse`) — 컨테이너 1개면 되지만
동시성 제어·재시도는 직접 해야 한다.

---

## 8. 지원 확장자 (`parse_service/router.py`, 2026-08-06 기준)

| 확장자 | 경로 | 파일변환(한컴) 필요 |
|---|---|---|
| pdf | ODL 직행 | 불필요 |
| xlsx/xlsm/xls | 자체 청킹(`chunk_needed=False`) | 불필요 |
| png/jpg/jpeg 등 이미지 | VL-OCR in-process | 불필요 |
| txt/md/markdown/csv/json/log | 그대로 블록화 | 불필요 |
| **그 외**(docx·hwp·hwpx·ppt·pptx·html·htm) | **파일변환 API → PDF → ODL** | **필요** |

구 kordoc docx 폴백은 제거됐다(장·조 계층 인식 실패). kordoc은 **엑셀 파서의 백엔드**로만 남아 있다.

엑셀 게이트: 파싱과 `gate_summary` 계산은 **parse-svc 안에서** 끝난다. `doc_guard` 는
그 `gate_summary` 를 받아 **판정**만 한다 — 파서 전용 구성(§7)엔 없으므로 자동 반려 판정은 안 된다.

---

## 9. 트러블슈팅

| 증상 | 원인 | 조치 |
|------|------|------|
| 컨테이너는 다 떴는데 서로 통신 불가 | 컨테이너 이름 DNS 미동작 | §3. `load-and-up.sh` 가 기동 전에 잡아준다 |
| `podman-compose: command not found` | compose 프론트엔드 미설치 | `dnf install podman-compose` (스크립트가 `podman compose` 도 자동탐지) |
| 서비스가 뜨자마자 재시작 반복 | podman-compose 가 `depends_on: service_healthy` 를 안 기다림 | 정상 — `restart: unless-stopped` 자가치유 + 스크립트 health 폴링 |
| health 가 계속 `starting` 이고 로그가 비어 있음 | podman healthcheck 는 **systemd 타이머**로 돈다. systemd 없는 환경(컨테이너 안 등)에선 아예 실행되지 않음 | 실서버(systemd 있음)에선 정상. 컨테이너 안에서 테스트 중이라면 `podman healthcheck run <ctr>` 를 주기 실행해야 한다 |
| `up` 후 health TIMEOUT | 상위 서비스 unhealthy 또는 온프렘 엔드포인트 불통 | `podman-compose logs edgequake` 부터. `.env` 주소 도달성 확인 |
| edgequake `OPENROUTER_API_KEY is empty` (exit 101) | `.env` 빈 값 | §4 |
| 적재가 insert 에서 매번 실패 + edgequake 로그에 `openrouter.ai` / `api.openai.com` | `EDGEQUAKE_LLM_PROVIDER` 오설정 | §4 ⚠️ — `openai-compatible` 로 되돌린다 |
| 적재가 **간헐적**으로 실패(`RemoteProtocolError` / `ReadError: Connection reset`) | 폴링 주기(3s) > 대상의 gunicorn keep-alive(2s) 경합. 잡은 succeeded 인데 폴링 연결이 끊겨 실패 처리 | 수정본은 폴링에 keep-alive 를 안 쓰고 전송계층 재시도도 한다(`service/http_retry.py`). 그래도 나면 대상 서비스 로그 확인 |
| 청킹이 `embedding call failed: Name or service not known` 으로 실패 | 임베딩 주소 DNS 해석 실패(오타 또는 미기동) | `.env` 의 `*_EMBEDDING_BASE_URL` 확인. 컨테이너 안에서 `curl` 로 도달성 확인 |
| 이미지/PPTX 파싱 빈 결과 | `MODEL_API_URL/KEY` 미설정 | §4 |
| docx/hwp/ppt/html 파싱 실패 | `KBP_FILECONVERT_URL` 미설정/불통 | `podman exec <parse-svc> curl -fsS "$KBP_FILECONVERT_URL"`. facade 응답 `detail` 에 실제 원인이 실려 온다 |
| 검색은 되나 순위 이상 | 리랭커 불통 → BM25 폴백 | `*_RERANK_BASE_URL` 확인 |
| `Bind for 0.0.0.0:PORT failed` | 호스트 포트 점유 | compose 의 해당 `ports:` **좌측(호스트)** 숫자만 변경 |
| `NoSuchBucket` | 버킷 미생성 | `load-and-up.sh` 가 `mc stat` 로 검증하며 자동 생성. 수동은 부록 B |
| `sha256sum -c` 가 `No such file or directory` | 체크섬 파일에 빌드머신 절대경로 | 2026-08-07 수정됨(basename 기록). 옛 번들이면 `sed 's#/.*/##'` 로 정규화 |
| load 한 이미지를 compose 가 못 찾고 pull 시도 | 태그 불일치 | `podman images \| grep kbp-` 로 `:airgap` 확인. digest 참조는 §0 ⚠️ |
| SELinux 볼륨 거부 | bind mount 라벨 | 본 스택은 named volume 만 써서 불필요. bind mount 추가 시 `:Z` |

---

## 10. 종료 / 재기동 / 업데이트

```bash
podman-compose -f docker-compose.airgap.yml down       # 컨테이너 제거(볼륨 유지)
podman-compose -f docker-compose.airgap.yml down -v    # 볼륨까지 삭제(데이터 소거 주의)
./scripts/airgap/load-and-up.sh                        # 재기동(멱등)
```

업데이트: 온라인에서 새 번들 빌드 → 전송 → `load-and-up.sh`.

---

## 부록 A. 외부 MinIO 재사용

1. compose 의 `minio:` 서비스와 `volumes: minio_data` 삭제, `parse-svc.depends_on` 에서 `minio` 제거.
2. `.env` 의 `MINIO_ENDPOINT` 를 외부 주소로, HTTPS면 `MINIO_SECURE=true`,
   `MINIO_ACCESS_KEY/SECRET_KEY/BUCKET` 를 외부 값으로.
3. 외부 MinIO 에 버킷 미리 생성.
4. **⚠️ 버킷을 kbp 스택이 둘 이상 공유하면 프리픽스를 배포마다 다르게 잡는다.**

   ```
   KBP_JOB_MINIO_PREFIX=kbp-jobs-prod        # 기본 kbp-jobs
   KBP_STAGING_PREFIX=parse-staging-prod     # 기본 parse-staging
   ```

   **왜**: facade 고아 스윕은 *"객체는 있는데 내 `kbp.jobs` 에 행이 없다 → 고아 → 삭제"* 로
   판정한다. 스택마다 Postgres 가 다르므로 프리픽스가 같으면 **A의 스윕이 B가 방금 올린
   살아있는 staging 을 지운다.** B는 `staging object not found` 로 실패하는데 원인은
   **다른 서버에** 있어 진단이 극도로 어렵다. 둘 다 나눠야 한다.

## 부록 B. MinIO 버킷 수동 생성

```bash
CTR=$(podman ps -a --filter "label=com.docker.compose.service=minio" --format '{{.Names}}' | head -1)
podman exec "$CTR" sh -c \
  'mc alias set local http://localhost:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" \
   && mc mb -p local/document-parser && mc stat local/document-parser'
```
> 호스트에서 바로 `mc mb` 하면 alias 자격증명이 비어 `Access Denied`. 반드시 컨테이너 내부
> 환경변수로 alias 를 잡는다. 컨테이너 탐색은 compose 라벨 기반(`grep -i minio` 는 다른
> 스택의 minio 를 집을 수 있다). 성공 확인은 `mc stat`(`mc ls local/` 는 특정 버킷 존재를
> 보장하지 않는다).
