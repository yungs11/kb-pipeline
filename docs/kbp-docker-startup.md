# kb-pipeline Docker 기동 매뉴얼

Docker Compose로 kb-pipeline 엔진 스택 전체를 빌드·기동하는 절차. 단일 진실 출처는
`docker-compose.yml` + `docker-compose.override.yml` + `.env` 세 파일이다.

---

## 1. 구성 개요

`name: kbp` 프로젝트. 3개 티어로 나뉜다.

| 티어 | 서비스 | 이미지 | 컨테이너 포트 | 호스트 포트(기본) |
|------|--------|--------|----------------|-------------------|
| 인프라 | postgres | ghcr.io/raphaelmansuy/edgequake-postgres | 5432 | **5433** |
| 인프라 | minio | minio/minio | 9000/9001 | 19010/19011* |
| 인프라 | gotenberg | gotenberg/gotenberg:8 | 3000 | (내부) |
| 엔진 | **edgequake** | **kbp-edgequake (docker/edgequake.Dockerfile)** | 8081 | **8081** |
| 문서처리 | document-parser | kbp-document-parser (sibling repo) | 8000 | 18050* |
| 문서처리 | excel-parser | kbp-excel-parser (sibling repo) | 18055 | 18055 |
| 문서처리 | doc_guard | kbp-doc_guard (sibling repo) | 8000 | 8001 |
| 문서처리 | adaptive_chunk | kbp-adaptive_chunk (sibling repo) | 18060 | (내부) |
| 앱 | parse-svc | kbp-parse-svc (Dockerfile.parse-svc) | 19001 | 19001 |
| 앱 | facade | kbp-facade (Dockerfile.facade) | 19000 | **19000** |

\* `docker-compose.override.yml`가 다른 무관한 스택과의 호스트 포트 충돌을 피하려고
minio를 **19020/19021**, document-parser를 **18051**로 재매핑한다(아래 5절 참고).

기동 순서(의존성): postgres → edgequake / (gotenberg,minio → document-parser →
adaptive_chunk) / excel-parser → parse-svc → facade.

---

## 2. edgequake 이미지 (Task 7 산출물)

`docker/edgequake.Dockerfile` — Rust 멀티스테이지 빌드.

- **빌드/런타임 모두 Debian trixie 로 고정.** 빌드 스테이지(`rust:1-slim-trixie`, glibc
  2.39)가 만든 바이너리는 bookworm(glibc 2.36)에서 `GLIBC_2.39 not found`로 죽는다.
  런타임 베이스를 `debian:trixie-slim`으로 맞추고 OpenSSL 런타임 라이브러리는 trixie
  네이밍인 `libssl3t64`를 쓴다.
- **빌드 스테이지에 `curl` 필수.** `pdfium-auto` 크레이트(→ edgequake-pdf2md)가 컴파일
  타임에 curl로 prebuilt pdfium 바이너리를 내려받는다. curl 없으면 빌드가
  `pdfium-auto[bundled]: curl unavailable`로 실패.
- **HOST/PORT 를 읽는다** (EDGEQUAKE_HOST/EDGEQUAKE_PORT 아님). `ENV HOST=0.0.0.0
  PORT=8081`. 잘못 주면 8080에 바인드→healthcheck(8081) 실패→depends_on 체인 데드락.
- **`EDGEQUAKE_CHUNKER=passthrough` 불변식.** 전용 edgequake는 재청킹하지 않는다(adaptive로
  띄우면 HTTP 422 적재 실패).
- 마이그레이션은 `sqlx::migrate!()`로 바이너리에 임베드 → 런타임 복사 불필요.
- 컨텍스트 정리: `docker/edgequake.Dockerfile.dockerignore`(per-Dockerfile ignore)가
  루트 `.dockerignore`의 `edgequake` 제외 라인을 이 빌드에 한해 무효화해 서브모듈 소스를
  컨텍스트에 포함시킨다.

검증(단독):
```bash
docker run -d --name eq-verify --network kbp_kbp \
  -e DATABASE_URL="postgres://edgequake:edgequake_secret@postgres:5432/edgequake" \
  -e EDGEQUAKE_CHUNKER=passthrough -e EDGEQUAKE_LLM_PROVIDER=openrouter \
  -e OPENROUTER_API_KEY=<유효키> -e HOST=0.0.0.0 -e PORT=8081 \
  kbp-edgequake:latest
docker exec eq-verify curl -fsS http://localhost:8081/health   # -> {"status":"healthy",...}
docker rm -f eq-verify
```
정상 응답: `storage_mode:"postgresql"`, `components` kv/vector/graph/llm_provider 모두
true, `llm_provider_name:"openrouter"`, 스키마 v38.

---

## 3. 사전 준비 — .env 채우기 (가장 중요)

`.env`는 현재 **`.env.example`을 그대로 복사한 빈 템플릿**이라 비밀값이 모두 비어 있다.
비어 있으면 기동이 실패한다. 최소 아래 키를 채워야 스택이 온전히 뜬다.

| 키 | 채우는 서비스 | 비면 생기는 증상 |
|----|---------------|-------------------|
| `OPENROUTER_API_KEY` | edgequake | 부팅 시 panic `OPENROUTER_API_KEY is empty` (exit 101) |
| `LITELLM_EMBEDDING_API_KEY` | edgequake 임베딩(bge-m3) | 적재/검색 임베딩 실패 |
| `MODEL_API_URL`, `MODEL_API_KEY` | document-parser(비전 OCR) | `/health` degraded (`vl_api unhealthy`) |
| `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY` | minio, parse-svc, document-parser | MinIO 인증/객체 저장 실패 |
| `ADAPTIVE_CHUNK_OPENROUTER_API_KEY` 등 | adaptive_chunk | 청킹 LLM 실패 |
| `ADAPTIVE_CHUNK_QDRANT_URL` | adaptive_chunk | 벡터 저장 실패 |
| `ADAPTIVE_CHUNK_RERANK_API_KEY`, `ADAPTIVE_CHUNK_SCORING_EMBEDDING_API_KEY` | adaptive_chunk | rerank/scoring 실패 |

`POSTGRES_PASSWORD`, `KBP_OPENAI_*`, `*_BASE_URL`, `*_MODEL` 등은 템플릿에 이미 값이 있다.

> 값이 빈 상태에서 `up`하면 인프라 티어(postgres/minio/gotenberg/excel-parser/doc_guard)는
> healthy가 되지만 **edgequake는 OPENROUTER_API_KEY 없으면 panic**하고, 그 뒤 앱 티어
> (parse-svc/facade)는 depends_on 때문에 못 뜬다.

---

## 4. 기동 절차

### 4-1. 기존 것 내리기 (필수 선행)
이전에 런처 스크립트로 띄운 로컬 프로세스/컨테이너가 포트를 잡고 있으면 compose가 못 뜬다.
```bash
cd /Users/xxx/workspace/8.kb-pipeline

# (a) 런처로 띄운 로컬 개발 프로세스 종료 (facade/parse-svc/edgequake/excel-parser)
for p in 19000 19001 8081 18055; do
  pid=$(lsof -tiTCP:$p -sTCP:LISTEN -P -n 2>/dev/null | head -1)
  [ -n "$pid" ] && kill "$pid"
done

# (b) 런처가 띄운 전용 edgequake postgres 컨테이너 제거 (:5433 해제)
docker rm -f eq-pg-kbp 2>/dev/null || true

# (c) 이전 compose 잔여물 정리 (볼륨 유지)
docker compose down
```

### 4-2. 빌드
```bash
docker compose build          # 7개 이미지 (edgequake는 Rust라 최초 ~10분)
```
edgequake만 다시 빌드: `docker compose build edgequake`.

### 4-3. 기동
```bash
docker compose up -d --wait   # 모든 healthcheck 통과까지 블록
```
`--wait`는 하나라도 unhealthy면 실패로 끝난다. 비밀값이 다 채워졌으면 전 서비스 healthy가
되고, 아니면 아래 6절로 원인을 좁힌다.

### 4-4. 상태 확인
```bash
docker compose ps
docker compose logs -f edgequake        # 개별 서비스 로그
```

### 4-5. 스모크 테스트
```bash
docker exec kbp-edgequake-1  curl -fsS http://localhost:8081/health
curl -fsS http://localhost:19000/healthz     # facade
curl -fsS http://localhost:19001/healthz     # parse-svc
```

---

## 5. 호스트 포트 재매핑 (docker-compose.override.yml)

이 머신엔 무관한 다른 docker 스택(dify, trust-backend, docker-* 등)이 이미 떠서 기본 포트를
점유 중이라, override가 **호스트 쪽 포트만** 바꿔 공존시킨다:

| 서비스 | 기본 | 재매핑 | 점유 중인 무관 컨테이너 |
|--------|------|--------|--------------------------|
| minio | 19010/19011 | **19020/19021** | docker-minio-1 |
| document-parser | 18050 | **18051** | trust-backend-document-parser-1 |

컨테이너 내부 포트와 서비스 DNS(예: `http://minio:9000`, `http://document-parser:8000`)는
그대로라 스택 내부 통신엔 영향 없다. YAML `!override` 태그로 base의 ports 리스트를
**치환**한다(compose 기본 merge는 append라 그냥 두면 옛 포트가 남아 충돌).

기본 포트가 비어 있는 깨끗한 머신이라면 `docker-compose.override.yml`을 지워도 된다.

---

## 6. 트러블슈팅

| 증상 | 원인 | 조치 |
|------|------|------|
| `Bind for 0.0.0.0:19010 failed: port is already allocated` | 무관 스택이 포트 점유 | 5절 override 사용(이미 적용됨) 또는 점유 컨테이너 확인 `docker ps` |
| edgequake `OPENROUTER_API_KEY is empty` exit 101 | `.env` 빈 값 | 3절대로 키 채우기 |
| edgequake `GLIBC_2.39 not found` | 런타임 베이스가 빌드와 glibc 불일치 | Dockerfile trixie 고정(이미 수정됨). 옛 이미지면 `docker compose build edgequake` |
| edgequake 빌드가 `pdfium-auto[bundled]: curl unavailable` | 빌드 스테이지에 curl 없음 | Dockerfile 빌드 스테이지 curl 포함(이미 반영). 옛 캐시면 `--no-cache` |
| document-parser `degraded` / unhealthy | redis(:6379) 부재 + `MODEL_API_URL` 미설정 | compose엔 redis 없음. `MODEL_API_URL/KEY` 채우고, 필요 시 redis 서비스 추가 |
| depends_on 체인이 안 뜸 | 상위 서비스가 unhealthy | `docker compose ps`로 최초 unhealthy 서비스부터 로그 확인 |
| `:5433` 충돌 | 런처가 띄운 `eq-pg-kbp` 잔존 | `docker rm -f eq-pg-kbp` |

---

## 7. 종료

```bash
docker compose down            # 컨테이너/네트워크 제거 (볼륨 eq_pg_data, minio_data 유지)
docker compose down -v         # 볼륨까지 삭제 (postgres/minio 데이터 소거 — 주의)
```

---

## 부록: 현재 확인된 상태 (2026-07-01, 빈 .env 기준)

- **edgequake 이미지(Task 7): 정상.** 유효 키를 주면 compose postgres에 붙어 마이그레이션
  후 `/health` healthy 확인 완료. GLIBC/pdfium/HOST-PORT/passthrough 모두 반영.
- **인프라 티어 healthy:** postgres, minio, gotenberg, excel-parser, doc_guard.
- **막힘:** `.env`가 빈 템플릿이라 edgequake(OPENROUTER_API_KEY), 그 하류 앱 티어가 못 뜨고,
  document-parser는 redis+MODEL_API_URL 부재로 degraded. → 3절대로 키를 채우면 해소.
