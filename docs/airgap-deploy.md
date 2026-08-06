# kb-pipeline 폐쇄망(air-gap) 배포 매뉴얼 — RHEL + Podman

인터넷이 안 되는 폐쇄망 RHEL 서버에 kb-pipeline 엔진 스택 8종을 **빌드/pull 없이**
바로 구동하는 절차. 온라인 개발기에서 amd64 이미지를 만들어 단일 번들로 옮긴다.

- 단일 진실 출처: `docker-compose.airgap.yml` + `.env`(← `.env.airgap.example`) + `images/*.tar.gz`
- 스크립트: `scripts/airgap/{build-bundle,load-and-up,verify-bundle,deploy-both}.sh`
- 일반(온라인) docker 구동은 `docs/kbp-docker-startup.md` 참고. 본 문서는 **폐쇄망 전용**.

> **kb(웹앱) 스택도 같이 올릴 거면 `scripts/airgap/deploy-both.sh` 를 쓴다** — kbp→kb
> 순서·두 `.env` 사이 필수 일치값(`KBP_NETWORK`/`KBP_FACADE_KEY`)을 자동으로 맞춘다.
> 이 문서는 kbp 단독 기준이고, kb 쪽 세부사항은 `knowledge_base/docs/airgap-deploy.md`.
> 사용법은 §7 참고.

---

## 0. 구성 개요 (번들에 포함되는 9개 이미지)

| 티어 | 서비스 | 이미지 태그 | 호스트 포트 | 출처 |
|------|--------|-------------|-------------|------|
| 인프라 | postgres | ghcr.io/raphaelmansuy/edgequake-postgres:latest | 5433 | pull |
| 인프라 | minio | minio/minio | 3003(콘솔) | pull |
| 엔진 | edgequake | kbp-edgequake:airgap | 3001 | 빌드 |
| 문서 | doc_guard | kbp-doc_guard:airgap | (내부) | 빌드 |
| 문서 | adaptive_chunk | kbp-adaptive_chunk:airgap | 18060 | 빌드 |
| 앱 | parse-svc | kbp-parse-svc:airgap | 18081 | 빌드 |
| 앱 | facade | kbp-facade:airgap | **3000** | 빌드 |
| 앱 | facade-worker | kbp-facade:airgap (명령만 다름) | (내부) | 재사용 |
| 확인용 | edgequake_webui | kbp-edgequake_webui:airgap | **3002** | 빌드 |

> 포트는 **호스트 발행 포트**다(컨테이너 내부 포트가 아니다). 권위 출처는
> `docker-compose.airgap.yml` 이고, `docs/architecture-ports.md` 에 전체 표가 있다.

기동 순서(의존성): postgres → edgequake / (minio) / doc_guard / adaptive_chunk
→ parse-svc → facade / edgequake_webui.

> **런타임 외부 의존(중요)**: 스택은 실행 중 LLM·임베딩·리랭커·VL-OCR·**파일변환(한컴)** 을
> HTTP 로 호출한다. 폐쇄망에서는 이 5가지가 **사내 온프렘 엔드포인트**로 `.env`에 설정돼
> 있어야 한다(§3). 특히 파일변환은 2026-08-06 부로 **docx/hwp/ppt/html 파싱의 유일한
> 경로**가 됐다(구 kordoc 폴백 레인은 제거됨, router.py 참고) — 미설정이면 그 확장자
> 전부가 파싱 실패한다. 모델
> 서버 자체는 번들에 포함되지 않는다(이미 사내에 존재한다는 전제).

---

## 1. [Phase A] 온라인 준비 — 개발기에서 번들 생성

인터넷이 되는 머신(Docker Desktop 포함)에서 1회 수행.

```bash
cd /path/to/8.kb-pipeline
./scripts/airgap/build-bundle.sh          # amd64 크로스빌드 + save + 번들
```

- 대상이 x86 서버면 기본값 `linux/amd64` 그대로. ARM 서버면 `PLATFORM=linux/arm64 ./scripts/airgap/build-bundle.sh`.
- QEMU 에뮬 크로스빌드라 **edgequake(Rust) 최초 빌드가 ~10분+**, 전체 30분~1시간 가능.
- 산출물: `dist/kbp-airgap-bundle-amd64.tar.gz` (+ sha256 출력).

검증(선택): `ARCH_EXPECT=amd64 ./scripts/airgap/verify-bundle.sh --images`

---

## 2. 전송 (2GB 분할)

번들이 2GB 를 넘으면 build-bundle.sh 가 **2GB 단위로 분할**한다(전송매체 한도 대응).
분할 시 원본 `.tar.gz` 대신 조각 `.part-aa .part-ab ...` + `.parts.sha256` 가 생긴다.
2GB 이하면 분할 없이 단일 tar.gz 그대로다. (`SPLIT_SIZE=1g` 로 조각 크기 변경, `KEEP_WHOLE=1` 로 원본도 보존.)

분할본 전송·재결합(폐쇄망 서버에서):
```bash
# 1) 모든 조각(.part-*) 을 서버로 옮긴 뒤 무결성 확인
sha256sum -c kbp-airgap-bundle-amd64.tar.gz.parts.sha256

# 2) 순서대로 이어붙여 원본 복원(part-aa, ab, ... 는 사전순이라 * 로 충분)
cat kbp-airgap-bundle-amd64.tar.gz.part-* > kbp-airgap-bundle-amd64.tar.gz

# 3) (선택) 원본 해시 확인
sha256sum -c kbp-airgap-bundle-amd64.tar.gz.sha256
```
이후 §3 부터 동일하게 진행한다. (단일 tar 이면 이 재결합 단계는 건너뛴다.)

---

## 3. [Phase B] 폐쇄망 서버 — .env 채우기 (가장 중요)

```bash
mkdir kbp && tar xzf kbp-airgap-bundle-amd64.tar.gz -C kbp && cd kbp
cp .env.airgap.example .env
vi .env      # 【A. 온프렘 재설정 필수】 블록만 사내 주소/키로 채운다
```

`.env` 최상단 【A】 블록의 4개 그룹을 사내 엔드포인트로 바꾼다(각 키에 주석으로 위치·증상 명시):

| 그룹 | 바꿀 키 | 사내 예시 | 안 바꾸면 |
|------|---------|-----------|-----------|
| LLM | `KBP_OPENAI_BASE_URL`, `ADAPTIVE_CHUNK_OPENROUTER_BASE_URL`, `*_LLM_MODEL`, `*_API_KEY` | `http://llm.corp:8000/v1` | 그래프추출·합성·청킹 실패 |
| LLM(edgequake provider) | `EDGEQUAKE_LLM_PROVIDER` — 기본값 `openai-compatible`(건드릴 필요 없음) | — | `openrouter`로 바꾸면 인터넷 필요(하드코딩 openrouter.ai). `openai`로 바꾸면 COMPAT-GUARD가 비-`gpt-*` 모델명(예: `qwen/...`)을 감지해 `gpt-4.1-nano`+`api.openai.com`으로 조용히 되돌린다(실측, §5). 반드시 `openai-compatible` 유지 |
| VL-OCR | `MODEL_API_URL`, `MODEL_API_KEY` | `http://vl.corp:8000/v1/chat/completions` | 이미지/PPTX 파싱 빈 결과 |
| 임베딩 | `LITELLM_EMBEDDING_BASE_URL`, `ADAPTIVE_CHUNK_SCORING_EMBEDDING_BASE_URL`, `*_API_KEY`, `*_MODEL` | `http://embed.corp:8000/v1` | 적재/검색 임베딩 실패 |
| 리랭커 | `EDGEQUAKE_RERANK_BASE_URL`(→ `/v1/rerank`), `ADAPTIVE_CHUNK_RERANK_BASE_URL`(→ `/v1`), `*_API_KEY`, `*_MODEL` | `http://rerank.corp:8000/...` | BM25 폴백(품질 저하, 비치명) |
| 파일변환(한컴) | `KBP_FILECONVERT_URL`, `KBP_FILECONVERT_TOKEN` | `http://fileconvert.corp:8080` | **docx/hwp/ppt/html 파싱 전면 실패**(A6) — pdf·xlsx·이미지·txt 는 무관 |

【B. 내부 전용】 블록(MinIO·Postgres·인트라스택 DNS)은 원칙적으로 손대지 않는다.
검증: `./scripts/airgap/verify-bundle.sh --env .env` (빈 필수키 / 잔존 인터넷주소 경고).

---

## 4. [Phase B] 로드 + 기동

```bash
./scripts/airgap/load-and-up.sh
```

스크립트가 순서대로: `podman load`(9종) → `.env` 검증 → `podman-compose up -d` →
**health 폴링**(podman-compose 엔 `--wait` 가 없음) → **MinIO 버킷 생성**(멱등) → 스모크 요약.

수동으로 하려면:
```bash
podman load -i images/kbp-images-amd64.tar.gz
podman-compose -f docker-compose.airgap.yml --env-file .env up -d
podman-compose -f docker-compose.airgap.yml ps
```

스모크(호스트 발행 포트 기준):
```bash
curl -fsS http://localhost:3000/healthz      # facade
curl -fsS http://localhost:3001/health       # edgequake
curl -fsS http://localhost:18081/healthz     # parse-svc

# ★ facade-worker 등록 확인 — 이게 0 이면 healthz 는 다 통과해도
#   /parse·/ingest 접수가 503("no live facade-worker") 이라 적재가 통째로 안 된다.
curl -fsS -H "X-Facade-Key: $KBP_FACADE_KEY" http://localhost:3000/jobs/workers
#   → {"online":true, "capacity":N, ...}

# 그래프 확인 UI: 브라우저로 http://<서버IP>:3002  (EDGEQUAKE_WEBUI_API_URL 을 서버 IP:3001 로)
```

`load-and-up.sh` 를 쓰면 위 검사를 자동으로 수행한다(healthcheck 상태 + worker 등록).

---

## 5. 트러블슈팅 (폐쇄망/Podman 특화)

| 증상 | 원인 | 조치 |
|------|------|------|
| `podman-compose: command not found` | compose 프론트엔드 미설치 | `dnf install podman-compose` 또는 podman 4.x `podman compose` 사용(스크립트가 자동탐지) |
| 서비스가 뜨자마자 재시작 반복 | podman-compose 가 `depends_on: service_healthy` 를 안 기다려 상위 미준비 상태에서 기동 | 정상 — `restart: unless-stopped` 로 자가치유. load-and-up.sh 의 health 폴링이 최종 확인. 계속 실패면 `podman-compose logs <svc>` |
| `up` 후 health TIMEOUT | 상위 서비스 unhealthy 또는 온프렘 엔드포인트 불통 | `podman-compose -f docker-compose.airgap.yml logs edgequake` 부터. 임베딩/LLM 주소 도달성(`.env`) 확인 |
| edgequake `OPENROUTER_API_KEY is empty` (exit 101) | `.env` 빈 값 | §3 대로 키 채우기 |
| 문서 적재는 진행되는데 매번 insert 단계에서 실패, edgequake 로그에 `error sending request for url (https://openrouter.ai/...)` 또는 `(https://api.openai.com/...)` | `EDGEQUAKE_LLM_PROVIDER` 가 `openrouter`거나(base_url 오버라이드 불가, 하드코딩) `openai`인데 모델명이 `gpt-*`가 아님(COMPAT-GUARD가 `api.openai.com`으로 되돌림) — 실측(2026-08-07) | `.env`에서 `EDGEQUAKE_LLM_PROVIDER=openai-compatible`(기본값)로 되돌리고 `OPENAI_COMPATIBLE_BASE_URL/API_KEY/MODEL`(=`KBP_OPENAI_*`)가 채워졌는지 확인 |
| 이미지/PPTX 파싱 빈 결과 | `MODEL_API_URL/KEY` 미설정 | §3 VL-OCR 채우기 |
| docx/hwp/ppt/html 파싱 실패(`enriched_content` 비어있음) | `KBP_FILECONVERT_URL` 미설정 또는 온프렘 변환 서버 불통(A6) — pdf·xlsx·이미지·txt 는 이 경로를 안 탄다 | `podman exec <parse-svc> curl -fsS "$KBP_FILECONVERT_URL"` 로 도달성 확인. facade 응답의 `detail` 필드에 실제 원인이 실려 온다(2026-08-06 이전엔 카테고리명만 있었다) |

**지원 확장자**(2026-08-06 라우팅 기준, `parse_service/router.py`):

| 확장자 | 경로 | 파일변환(한컴) 필요 |
|---|---|---|
| pdf | ODL 직행 | 불필요 |
| xlsx/xlsm/xls | 자체 청킹(`chunk_needed=False`) | 불필요 |
| png/jpg/jpeg 등 이미지 | VL-OCR in-process | 불필요 |
| txt/md/markdown/csv/json/log | 그대로 블록화 | 불필요 |
| **그 외 전부**(docx·hwp·hwpx·ppt·pptx·html·htm 등) | **파일변환 API → PDF 변환 → ODL** | **필요** — 미설정/불통이면 전면 실패 |

구 kordoc docx 폴백 레인은 제거됐다(장·조 계층 인식 실패, `parse_service/router.py` 주석
참고). kordoc 은 **엑셀 파서의 별도 백엔드**로만 남아 있다(다른 관심사).
| 검색은 되나 순위 이상 | 리랭커 주소 불통 → BM25 폴백 | `EDGEQUAKE_RERANK_BASE_URL`/`ADAPTIVE_CHUNK_RERANK_BASE_URL` 확인 |
| `Bind for 0.0.0.0:9000 failed` | 호스트 포트 점유 | `docker-compose.airgap.yml` 의 해당 `ports:` 좌측(호스트) 숫자만 변경 |
| MinIO 버킷 미생성(`NoSuchBucket`) | 최초 1회 생성 필요 | load-and-up.sh 가 `mc stat` 로 존재를 실제 검증하며 자동 생성. 실패 시 `FAIL` 로 표시되고 6번 요약에 원인이 남는다. §부록 B 로 수동 생성(재실행은 스크립트를 다시 돌리는 게 우선) |
| SELinux 로 컨테이너가 볼륨 접근 거부 | bind mount 라벨 | 본 스택은 **named volume**(eq_pg_data/minio_data)만 써서 relabel 불필요. bind mount 를 추가한다면 `:Z` 를 붙일 것 |
| load 한 이미지를 compose 가 못 찾고 pull 시도 | 태그 불일치 | `podman images | grep kbp-` 로 `:airgap` 태그 확인. compose `image:` 와 일치해야 함 |

---

## 6. 종료 / 재기동

```bash
podman-compose -f docker-compose.airgap.yml down       # 컨테이너 제거(볼륨 유지)
podman-compose -f docker-compose.airgap.yml down -v     # 볼륨까지 삭제(데이터 소거 주의)
./scripts/airgap/load-and-up.sh                         # 재기동(로드는 멱등)
```

---

## 부록 A. 외부 MinIO 재사용 (사내에 이미 MinIO 가 있을 때)

1. `docker-compose.airgap.yml` 의 `minio:` 서비스 블록과 최상단 `volumes: minio_data` 삭제,
   `parse-svc.depends_on` 의 `minio` 줄 제거.
2. `.env` 의 `MINIO_ENDPOINT` 를 외부 주소(예: `minio.corp:9000`), HTTPS 면 `MINIO_SECURE=true`,
   `MINIO_ACCESS_KEY/SECRET_KEY/BUCKET` 를 외부 값으로.
3. 외부 MinIO 에 `MINIO_BUCKET` 버킷 미리 생성.
4. **⚠️ 이 버킷을 kbp 스택이 둘 이상 공유한다면(운영+검증 등) 프리픽스를 배포마다 다르게
   잡는다. 안 하면 서로의 작업 파일을 지운다.**

   ```
   KBP_JOB_MINIO_PREFIX=kbp-jobs-prod        # 기본 kbp-jobs
   KBP_STAGING_PREFIX=parse-staging-prod     # 기본 parse-staging
   ```

   **왜**: facade 의 고아 스윕은 *"객체는 있는데 내 `kbp.jobs` 에 그 행이 없다 → 고아 →
   삭제"* 로 판정한다. 스택마다 Postgres 가 다르므로, 프리픽스가 같으면 **A 의 스윕이
   B 가 방금 올린 살아있는 staging 을 지운다.** B 의 잡은 `staging object not found` 로
   실패하고, 원인은 **다른 서버에** 있어 진단이 극도로 어렵다.

   프리픽스를 나누면 서로를 나열조차 하지 않는다. 값은 배포 시작 시 로그에 찍힌다.
   (`kbp-jobs` 는 잡 큐 staging, `parse-staging` 은 kb 의 미리보기·배치 원본이다 —
   **둘 다** 나눠야 한다.)

## 부록 B. MinIO 버킷 수동 생성

```bash
CTR=$(podman ps -a --filter "label=com.docker.compose.service=minio" \
                   --format '{{.Names}}' | head -1)
podman exec "$CTR" sh -c \
  'mc alias set local http://localhost:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" \
   && mc mb -p local/document-parser && mc stat local/document-parser'
```
> 호스트에서 바로 `mc mb` 하면 alias 자격증명이 비어 `Access Denied`. 반드시 컨테이너 내부
> `$MINIO_ROOT_USER/$MINIO_ROOT_PASSWORD` 로 alias 를 잡는다. 컨테이너 탐색은 본문
> `load-and-up.sh` 와 같은 compose 라벨 기반이다(`grep -i minio` 는 다른 스택의 minio 를
> 집을 수 있어 쓰지 않는다). 성공 여부는 `mc stat` 로 확인한다(`mc ls local/` 는 버킷 전체
> 목록이라 특정 버킷의 존재를 보장하지 않는다).

---

## 7. 두 스택(kbp + kb) 한 번에 배포 — `deploy-both.sh`

kb(웹앱)까지 같이 올리는 게 목적이면 각자 `load-and-up.sh`를 순서 맞춰 두 번 돌리는
대신 이 스크립트 하나로 끝낸다. 두 번들을 각각 압축 해제(§2~3)해서 같은 서버에
디렉터리 두 개(`kbp/`, `kb/`)로 둔 다음, 각 디렉터리에서 `.env`(§3 표)를 채운 뒤:

```bash
cd kbp
./scripts/airgap/deploy-both.sh ../kb
```

내부 동작: ① kbp 자체 `load-and-up.sh` 실행 → ② kbp 가 만든 실제 podman 네트워크
이름 조회(`*_kbp` 패턴, 추측하지 않고 `podman network ls` 로 확인) → ③ `kb/.env` 의
`KBP_NETWORK`/`KBP_FACADE_KEY` 를 그 값·kbp 와 동일 값으로 자동 동기화(수동 배포에서
가장 자주 놓치는 지점 — 두 값이 안 맞으면 kb 가 네트워크를 못 찾거나 적재/검색이
401/403 으로 막힌다) → ④ kb 자체 `load-and-up.sh` 실행 → ⑤ 두 스택 헬스체크 요약 출력.

kb 쪽 나머지 필수값(`JWT_SECRET`/`CREDENTIAL_ENCRYPTION_KEY`/`SEED_ADMIN_PASSWORD`
등, `KBP_NETWORK`/`KBP_FACADE_KEY` 제외)은 이 스크립트가 대신 채워주지 않는다 —
`kb/.env` 를 미리 §3(이 리포 표) + `knowledge_base/docs/airgap-deploy.md` §3 대로
채워둬야 한다. 실패 시 어느 단계인지 로그에 그대로 나오고, 각 리포 `load-and-up.sh`
는 멱등이라 원인만 고치고 `deploy-both.sh` 를 다시 돌리면 된다.
