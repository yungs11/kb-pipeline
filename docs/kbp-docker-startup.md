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
| 문서처리 | doc_guard | kbp-doc_guard (sibling repo) | 8000 | 8001 |
| 문서처리 | adaptive_chunk | kbp-adaptive_chunk (sibling repo) | 18060 | (내부) |
| 앱 | parse-svc | kbp-parse-svc (Dockerfile.parse-svc) | 19001 | 19001 |
| 앱 | facade | kbp-facade (Dockerfile.facade) | 19000 | **19000** |
| 확인용 | **edgequake_webui** | kbp-edgequake_webui (edgequake/edgequake_webui/Dockerfile) | 3000 | 3000\*\* |

> **Phase 2e**: 외부 파서 서비스 `document-parser(:18050)`·`excel-parser(:18055)`·`redis` 는 제거됐다. 모든 문서 파싱(PDF/Excel/DOCX/PPTX/이미지/스캔)은 parse-svc(:19001)가 in-process 로 수행한다(이미지에 java21 + node/kordoc + PyMuPDF 내장). office(pptx/docx)→PDF 변환용 gotenberg 만 잔존.

> **edgequake_webui(그래프 적재 확인용, 선택 서비스)**: edgequake 에 적재된 지식그래프·워크스페이스·문서를 브라우저로 조회·시각화·질의하는 확인용 UI(`http://localhost:3000`, 이 머신은 리맵 후 **13000**). **운영 적재 경로가 아니다** — 문서 적재는 facade `/ingest`(parse-svc 파싱 + adaptive 청킹 + 모달원자성)로 하고, 이 UI 로 직접 업로드하면 kb-pipeline 경로를 우회하므로 "적재 결과(그래프) 확인/디버깅" 용도로만 쓴다. `EDGEQUAKE_API_URL` 은 **브라우저가** API 에 닿는 호스트 URL(기본 `http://localhost:8081`)이며 컨테이너 DNS 가 아니다.

\* `docker-compose.override.yml`가 다른 무관한 스택과의 호스트 포트 충돌을 피하려고
minio를 **19020/19021**로 재매핑한다(아래 5절 참고). (document-parser 재매핑은 서비스 제거로 삭제됨.)
\*\* 이 머신은 호스트 3000 이 점유되어 override 가 webui 를 **13000**으로 리맵한다(5절).

기동 순서(의존성): postgres → edgequake / (gotenberg,minio) / doc_guard /
adaptive_chunk → parse-svc(depends_on gotenberg+minio) → facade / edgequake_webui(depends_on edgequake).

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
| `MODEL_API_URL`, `MODEL_API_KEY` | parse-svc(in-process VL OCR) | 이미지/PPTX/스캔 파싱 시 VL 호출 실패(빈 enriched_content) |
| `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY` | minio, parse-svc | MinIO 인증/객체 저장 실패 |
| `ADAPTIVE_CHUNK_OPENROUTER_API_KEY` 등 | adaptive_chunk | 청킹 LLM 실패 |
| `ADAPTIVE_CHUNK_RERANK_API_KEY`, `ADAPTIVE_CHUNK_SCORING_EMBEDDING_API_KEY` | adaptive_chunk | rerank/scoring 실패 |

`POSTGRES_PASSWORD`, `KBP_OPENAI_*`, `*_BASE_URL`, `*_MODEL` 등은 템플릿에 이미 값이 있다.

> `ADAPTIVE_CHUNK_QDRANT_URL` 은 제거됨(2026-07-01): 벡터 적재는 edgequake(pgvector)가 소유하고 adaptive_chunk 는 Qdrant 를 소비하지 않는다(compose 에도 없음).

> 값이 빈 상태에서 `up`하면 인프라 티어(postgres/minio/gotenberg/doc_guard)는
> healthy가 되지만 **edgequake는 OPENROUTER_API_KEY 없으면 panic**하고, 그 뒤 앱 티어
> (parse-svc/facade)는 depends_on 때문에 못 뜬다.

---

## 4. 기동 절차

### 4-1. 기존 것 내리기 (필수 선행)
이전에 런처 스크립트로 띄운 로컬 프로세스/컨테이너가 포트를 잡고 있으면 compose가 못 뜬다.
```bash
cd /Users/xxx/workspace/8.kb-pipeline

# (a) 런처로 띄운 로컬 개발 프로세스 종료 (facade/parse-svc/edgequake)
for p in 19000 19001 8081; do
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
docker compose build          # edgequake(Rust, 최초 ~10분) + parse-svc(node/kordoc) + edgequake_webui(Next.js) 등
```
개별 빌드: `docker compose build edgequake` / `... parse-svc` / `... edgequake_webui`.
> edgequake_webui(그래프 적재 확인용)는 선택 서비스다. Next.js 빌드가 무거우니, 필요 없으면
> `docker compose up -d --wait facade`(또는 서비스 나열)로 webui 를 빼고 띄워도 된다.

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

### 4-5. MinIO 버킷 사전 생성 (페이지 이미지용, 최초 1회)
parse-svc 는 페이지 이미지(썸네일)를 `MINIO_BUCKET`(기본 `document-parser`) 버킷에 올린다.
버킷은 **자동 생성되지 않으므로**(인프라가 미리 만드는 정책) 최초 기동 후 한 번 만들어야 한다.
없으면 파싱·검색은 정상이나 페이지 이미지 업로드만 `NoSuchBucket` 으로 skip 된다(비치명).

```bash
# 컨테이너 내부 root 크레덴셜로 alias 를 잡고 버킷 생성 (호스트 셸의 $MINIO_* 는 비어있을 수 있음)
docker exec kbp-minio-1 sh -c \
  'mc alias set local http://localhost:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" \
   && mc mb -p local/document-parser && mc ls local/'
```
> ⚠️ 호스트에서 `mc mb local/document-parser` 를 바로 치면 alias 크레덴셜이 비어 있어
> `Access Denied` 가 난다. 반드시 위처럼 **컨테이너 내부의 `$MINIO_ROOT_USER/$MINIO_ROOT_PASSWORD`**
> 로 alias 를 설정한다. `MINIO_BUCKET` 을 바꿨다면 `document-parser` 대신 그 값으로 만든다.

### 4-6. 스모크 테스트
```bash
docker exec kbp-edgequake-1  curl -fsS http://localhost:8081/health
curl -fsS http://localhost:19000/healthz     # facade
curl -fsS http://localhost:19001/healthz     # parse-svc
curl -fsS http://localhost:13000             # edgequake_webui (그래프 적재 확인용, override 리맵 13000)
```
그래프 적재 확인: 브라우저로 **http://localhost:13000** 접속 → 워크스페이스 선택 →
문서/그래프(엔티티·관계) 조회. (적재 자체는 facade `/ingest` 로 하고, 이 UI 는 결과 확인용.)

---

## 5. 호스트 포트 재매핑 (docker-compose.override.yml)

이 머신엔 무관한 다른 docker 스택(dify, trust-backend, docker-* 등)이 이미 떠서 기본 포트를
점유 중이라, override가 **호스트 쪽 포트만** 바꿔 공존시킨다:

| 서비스 | 기본 | 재매핑 | 점유 중인 무관 컨테이너 |
|--------|------|--------|--------------------------|
| minio | 19010/19011 | **19020/19021** | docker-minio-1 |
| edgequake_webui | 3000 | **13000** | 다른 스택이 3000 점유 |

> (Phase 2e: document-parser 서비스 제거로 그 18050→18051 재매핑도 삭제됨.)
> webui 리맵은 호스트 포트만 바꾼다 — `EDGEQUAKE_API_URL` 은 여전히 API 호스트포트 `:8081` 을 가리키므로 webui 호스트포트(13000)와 무관하다.

컨테이너 내부 포트와 서비스 DNS(예: `http://minio:9000`)는
그대로라 스택 내부 통신엔 영향 없다. YAML `!override` 태그로 base의 ports 리스트를
**치환**한다(compose 기본 merge는 append라 그냥 두면 옛 포트가 남아 충돌).

기본 포트가 비어 있는 깨끗한 머신이라면 `docker-compose.override.yml`을 지워도 된다.

---

## 5.5 외부 MinIO 로 전환 (다른 곳에 떠 있는 MinIO 재사용)

parse-svc 는 페이지 이미지를 MinIO 에 올린다. compose 내장 `minio` 대신 **이미 다른 곳에 떠 있는 MinIO** 에 붙이려면 (`docker-compose.yml` 의 해당 위치에도 같은 주석을 달아두었다):

1. **삭제**
   - `docker-compose.yml` 의 `minio:` 서비스 블록 전체.
   - 최상단 `volumes:` 의 `minio_data` 항목.
   - `docker-compose.override.yml` 의 `minio` 호스트포트 리맵.
2. **수정** — `docker-compose.yml` 의 `parse-svc.environment`:
   - `MINIO_ENDPOINT` → 외부 호스트:포트 (예: `minio.example.com:9000`)
   - `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` → 외부 크레덴셜(`.env`)
   - `MINIO_BUCKET` → 외부 버킷명(기본 `document-parser`)
   - `MINIO_SECURE` → HTTPS 면 `"true"`
   - `parse-svc.depends_on` 에서 `minio` 줄 제거.
3. **버킷** — 외부 MinIO 에 그 버킷을 미리 생성(§4-5 와 동일, 단 대상은 외부 엔드포인트).

> 내부 통신은 서비스 DNS `http://minio:9000` 을 쓰므로, 외부 전환 시 `MINIO_ENDPOINT` 만 실제 도달 가능한 주소로 바꾸면 된다. facade 는 MinIO 를 직접 쓰지 않으므로 손댈 것이 없다.

---

## 6. 트러블슈팅

| 증상 | 원인 | 조치 |
|------|------|------|
| `Bind for 0.0.0.0:19010 failed: port is already allocated` | 무관 스택이 포트 점유 | 5절 override 사용(이미 적용됨) 또는 점유 컨테이너 확인 `docker ps` |
| edgequake `OPENROUTER_API_KEY is empty` exit 101 | `.env` 빈 값 | 3절대로 키 채우기 |
| edgequake `GLIBC_2.39 not found` | 런타임 베이스가 빌드와 glibc 불일치 | Dockerfile trixie 고정(이미 수정됨). 옛 이미지면 `docker compose build edgequake` |
| edgequake 빌드가 `pdfium-auto[bundled]: curl unavailable` | 빌드 스테이지에 curl 없음 | Dockerfile 빌드 스테이지 curl 포함(이미 반영). 옛 캐시면 `--no-cache` |
| 이미지/PPTX 파싱 시 빈 enriched_content | parse-svc `MODEL_API_URL/KEY` 미설정 (in-process VL OCR 미호출) | 3절대로 `MODEL_API_URL/KEY` 채우기 |
| parse-svc `kordoc: not found` / docx·폴백 파싱 실패 | 이미지에 node/kordoc 미빌드 | `docker compose build parse-svc`(Dockerfile.parse-svc 가 `npm install -g kordoc`) |
| depends_on 체인이 안 뜸 | 상위 서비스가 unhealthy | `docker compose ps`로 최초 unhealthy 서비스부터 로그 확인 |
| `:5433` 충돌 | 런처가 띄운 `eq-pg-kbp` 잔존 | `docker rm -f eq-pg-kbp` |
| edgequake_webui `Bind for 0.0.0.0:3000 failed` | 호스트 3000 점유 | 5절 override 리맵(이미 13000 적용) 또는 base `ports` 조정 |
| webui 는 뜨는데 API 호출 실패(빈 그래프/네트워크 오류) | `EDGEQUAKE_API_URL` 이 브라우저가 못 닿는 값(`http://edgequake:8081` 같은 컨테이너 DNS) | 브라우저 도달 가능한 호스트 URL(`http://localhost:8081`)로. 원격 접속은 `EDGEQUAKE_WEBUI_API_URL=http://<호스트IP>:8081` |

---

## 7. 종료

```bash
docker compose down            # 컨테이너/네트워크 제거 (볼륨 eq_pg_data, minio_data 유지)
docker compose down -v         # 볼륨까지 삭제 (postgres/minio 데이터 소거 — 주의)
```

---

## 부록: 현재 확인된 상태 (2026-07-03, 채워진 .env 기준)

- **전 서비스 healthy(8개):** postgres, minio, gotenberg, edgequake, doc_guard,
  adaptive_chunk, parse-svc, facade. `docker compose ps` 로 확인.
- **Phase 2 파서 일원화 완료·실증:** 외부 파서 서비스(document-parser :18050 /
  excel-parser :18055 / redis) 제거됨. parse-svc(:19001) in-process 로 xlsx(→
  `chunk_strategy=excel_rag_parser`) / docx(kordoc, `<table>` 보존) / png·pptx(VL OCR)
  파싱 정상 확인. 이미지에 kordoc 3.8.3 + java21 + PyMuPDF(fitz) 내장 확인.
- **MinIO 버킷:** `document-parser` 생성 완료(4-5절). 페이지 이미지 업로드 경로 정상.
- **edgequake_webui(그래프 적재 확인용):** compose 서비스로 배선됨(build context
  `edgequake/`, dockerfile `edgequake_webui/Dockerfile`, host 3000→override 13000,
  `EDGEQUAKE_API_URL=http://localhost:8081`). 선택 서비스.
- **참고(baseline):** `.venv-kb` 전체 테스트 208 passed / 5 failed 는 기존 baseline
  (minio bucket auto-create 정책변경 1건 + 모달 4건은 `KBP_MODAL_ENRICH=1` 필요, 기본 off).
  Phase 2 로 인한 신규 실패 0.
