# kb-pipeline 폐쇄망(air-gap) 배포 매뉴얼 — RHEL + Podman

인터넷이 안 되는 폐쇄망 RHEL 서버에 kb-pipeline 엔진 스택 8종을 **빌드/pull 없이**
바로 구동하는 절차. 온라인 개발기에서 amd64 이미지를 만들어 단일 번들로 옮긴다.

- 단일 진실 출처: `docker-compose.airgap.yml` + `.env`(← `.env.airgap.example`) + `images/*.tar.gz`
- 스크립트: `scripts/airgap/{build-bundle,load-and-up,verify-bundle}.sh`
- 일반(온라인) docker 구동은 `docs/kbp-docker-startup.md` 참고. 본 문서는 **폐쇄망 전용**.

---

## 0. 구성 개요 (번들에 포함되는 9개 이미지)

| 티어 | 서비스 | 이미지 태그 | 호스트 포트 | 출처 |
|------|--------|-------------|-------------|------|
| 인프라 | postgres | ghcr.io/raphaelmansuy/edgequake-postgres:latest | 5433 | pull |
| 인프라 | minio | minio/minio | 9000/9001 | pull |
| 인프라 | gotenberg | gotenberg/gotenberg:8 | (내부) | pull |
| 엔진 | edgequake | kbp-edgequake:airgap | 8081 | 빌드 |
| 문서 | doc_guard | kbp-doc_guard:airgap | (내부) | 빌드 |
| 문서 | adaptive_chunk | kbp-adaptive_chunk:airgap | 18060 | 빌드 |
| 앱 | parse-svc | kbp-parse-svc:airgap | 19001 | 빌드 |
| 앱 | facade | kbp-facade:airgap | **19000** | 빌드 |
| 확인용 | edgequake_webui | kbp-edgequake_webui:airgap | **13000** | 빌드 |

기동 순서(의존성): postgres → edgequake / (gotenberg·minio) / doc_guard / adaptive_chunk
→ parse-svc → facade / edgequake_webui.

> **런타임 외부 의존(중요)**: 스택은 실행 중 LLM·임베딩·리랭커·VL-OCR 를 HTTP 로 호출한다.
> 폐쇄망에서는 이 4가지가 **사내 온프렘 엔드포인트**로 `.env`에 설정돼 있어야 한다(§3). 모델
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
| VL-OCR | `MODEL_API_URL`, `MODEL_API_KEY` | `http://vl.corp:8000/v1/chat/completions` | 이미지/PPTX 파싱 빈 결과 |
| 임베딩 | `LITELLM_EMBEDDING_BASE_URL`, `ADAPTIVE_CHUNK_SCORING_EMBEDDING_BASE_URL`, `*_API_KEY`, `*_MODEL` | `http://embed.corp:8000/v1` | 적재/검색 임베딩 실패 |
| 리랭커 | `EDGEQUAKE_RERANK_BASE_URL`(→ `/v1/rerank`), `ADAPTIVE_CHUNK_RERANK_BASE_URL`(→ `/v1`), `*_API_KEY`, `*_MODEL` | `http://rerank.corp:8000/...` | BM25 폴백(품질 저하, 비치명) |

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

스모크:
```bash
curl -fsS http://localhost:19000/healthz     # facade
curl -fsS http://localhost:8081/health       # edgequake
curl -fsS http://localhost:19001/healthz     # parse-svc
# 그래프 확인 UI: 브라우저로 http://<서버IP>:13000  (EDGEQUAKE_WEBUI_API_URL 을 서버 IP:8081 로)
```

---

## 5. 트러블슈팅 (폐쇄망/Podman 특화)

| 증상 | 원인 | 조치 |
|------|------|------|
| `podman-compose: command not found` | compose 프론트엔드 미설치 | `dnf install podman-compose` 또는 podman 4.x `podman compose` 사용(스크립트가 자동탐지) |
| 서비스가 뜨자마자 재시작 반복 | podman-compose 가 `depends_on: service_healthy` 를 안 기다려 상위 미준비 상태에서 기동 | 정상 — `restart: unless-stopped` 로 자가치유. load-and-up.sh 의 health 폴링이 최종 확인. 계속 실패면 `podman-compose logs <svc>` |
| `up` 후 health TIMEOUT | 상위 서비스 unhealthy 또는 온프렘 엔드포인트 불통 | `podman-compose -f docker-compose.airgap.yml logs edgequake` 부터. 임베딩/LLM 주소 도달성(`.env`) 확인 |
| edgequake `OPENROUTER_API_KEY is empty` (exit 101) | `.env` 빈 값 | §3 대로 키 채우기 |
| 이미지/PPTX 파싱 빈 결과 | `MODEL_API_URL/KEY` 미설정 | §3 VL-OCR 채우기 |
| 검색은 되나 순위 이상 | 리랭커 주소 불통 → BM25 폴백 | `EDGEQUAKE_RERANK_BASE_URL`/`ADAPTIVE_CHUNK_RERANK_BASE_URL` 확인 |
| `Bind for 0.0.0.0:9000 failed` | 호스트 포트 점유 | `docker-compose.airgap.yml` 의 해당 `ports:` 좌측(호스트) 숫자만 변경 |
| MinIO 버킷 미생성(`NoSuchBucket`) | 최초 1회 생성 필요 | load-and-up.sh 가 자동 생성. 실패 시 §부록 수동 생성 |
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

## 부록 B. MinIO 버킷 수동 생성

```bash
CTR=$(podman ps --format '{{.Names}}' | grep -i minio | head -1)
podman exec "$CTR" sh -c \
  'mc alias set local http://localhost:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" \
   && mc mb -p local/document-parser && mc ls local/'
```
> 호스트에서 바로 `mc mb` 하면 alias 자격증명이 비어 `Access Denied`. 반드시 컨테이너 내부
> `$MINIO_ROOT_USER/$MINIO_ROOT_PASSWORD` 로 alias 를 잡는다.
