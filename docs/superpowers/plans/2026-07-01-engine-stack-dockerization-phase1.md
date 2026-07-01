<!-- plan-version: v1 -->
<!-- codex-validation: PENDING -->

# kb_pipeline 엔진 스택 도커화 + compose (Phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** kb_pipeline provider 엔진측 서비스 전부를 도커 이미지 + 단일 `docker-compose.yml` 로 통일하고, 파이썬 서비스는 gunicorn 다중워커로 띄운다. 코드 병합 없음.

**Architecture:** 각 서비스에 Dockerfile 추가(없는 것 신규, 있는 것 재사용) → `8.kb-pipeline/docker-compose.yml` 에서 build.context 로 각 레포를 빌드 → 서비스명 DNS 네트워킹 → healthcheck+depends_on 순서 → postgres named volume 영속. 파이썬 FastAPI 는 gunicorn(uvicorn worker).

**Tech Stack:** Docker, docker-compose(v2), gunicorn+uvicorn.workers.UvicornWorker, Python 3.10-3.12, JRE17(parse-svc), Node/kordoc(excel-parser), Rust(edgequake), postgres(pgvector+AGE), gotenberg, minio.

## Global Constraints
- compose 파일 위치: `8.kb-pipeline/docker-compose.yml`. Dockerfile 은 각 레포 루트(그 서비스 소스와 함께).
- 파이썬 서비스 실행 = `gunicorn -k uvicorn.workers.UvicornWorker -w <N> -b 0.0.0.0:<port> --timeout 1800 <module:app>`. 워커수: parse-svc=4, adaptive_chunk=3, document-parser=3, facade=2, excel-parser=2, doc_guard=2.
- 서비스명/포트(컨테이너 내부): facade `service.app:app`@19000, parse-svc `parse_service.app:app`@19001, excel-parser `service.main:app`@18055, doc_guard `app.main:app`@8000, adaptive_chunk `service.main:app`@18060, document-parser(기존 Dockerfile app)@8000, edgequake@8081, postgres@5432, gotenberg@3000, minio@9000.
- 서비스명 DNS URL(§4): facade→`http://parse-svc:19001`,`http://adaptive_chunk:18060`,`http://edgequake:8081`,`postgres:5432`; parse-svc→`http://excel-parser:18055`,`http://document-parser:8000`; document-parser→`http://gotenberg:3000`,`minio:9000`; edgequake→`postgres:5432`.
- 외부 URL 유지(교체 금지): 원격 litellm(bge-m3 임베딩), OpenRouter(qwen LLM), 원격 VL(`MODEL_API_URL`).
- 불변식: edgequake `EDGEQUAKE_CHUNKER=passthrough`; 모달마커 U+3008/U+3009; 표 `<table>` 보존; BGE-M3 1024d; 청킹=facade `/chunk` 소유.
- postgres 데이터 = **named volume `eq_pg_data`** 영속. edgequake 기동은 바이너리-온리(DB 초기화 금지, 마이그레이션 idempotent).
- 시크릿은 compose 루트 `.env`(gitignore) + `.env.example`(커밋). 절대 이미지에 굽지 않음.
- 비범위: 파서 코드 병합(Phase 2), kb-backend/frontend.

---

## File Structure (생성/수정)
- `7.excel-parser/Dockerfile` — 신규(python+node/kordoc+gunicorn)
- `99.projects/adaptive_chunk/Dockerfile` — 신규(python+gunicorn)
- `8.kb-pipeline/Dockerfile.facade` — 신규(python+gunicorn)
- `8.kb-pipeline/Dockerfile.parse-svc` — 신규(python+JRE17+gunicorn)
- `8.kb-pipeline/docker/edgequake.Dockerfile` — 신규(멀티스테이지 Rust)
- `99.projects/shinhan_trust/doc_guard/Dockerfile` — 수정(gunicorn 화)
- `99.projects/jiju_chaekmu/.../document-parser-backend-src/Dockerfile` — 재사용(필요시 gunicorn 확인)
- `8.kb-pipeline/docker-compose.yml` — 신규(전 서비스+인프라)
- `8.kb-pipeline/.env.example` — 신규(compose 키 카탈로그)
- `8.kb-pipeline/docs/HANDOVER-kb-pipeline-provider.md` — 수정(compose 기동으로 갱신)

> 각 파이썬 서비스는 이미 `.venv` 로 로컬 실행됨 → 의존성은 해당 레포의 `requirements.txt`/`pyproject.toml`/`setup.py` 에 있다. Dockerfile 은 그 파일로 설치한다(구현자가 각 레포에서 확인).

---

## Task 1: facade 이미지 (python + gunicorn)

**Files:**
- Create: `8.kb-pipeline/Dockerfile.facade`
- Create: `8.kb-pipeline/.dockerignore` (없으면)

**Interfaces:**
- Produces: 이미지가 `gunicorn ... service.app:app` 를 :19000 으로 서비스. `/healthz` 200.

- [ ] **Step 1: 의존성 설치 방식 확인**

Run: `ls /Users/xxx/workspace/8.kb-pipeline/{pyproject.toml,setup.py,requirements.txt} 2>/dev/null; sed -n '1,20p' /Users/xxx/workspace/8.kb-pipeline/pyproject.toml 2>/dev/null`
목적: facade(`service/`)+`kb_pipeline/` 패키지 설치 방법 파악(대개 `pip install -e .`).

- [ ] **Step 2: Dockerfile.facade 작성**

```dockerfile
# 8.kb-pipeline/Dockerfile.facade
FROM python:3.12-slim AS base
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl build-essential && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml setup.py* requirements*.txt ./
# 소스 복사 후 editable 설치(패키지: kb_pipeline + service). requirements 있으면 우선.
COPY kb_pipeline ./kb_pipeline
COPY service ./service
RUN pip install --no-cache-dir uv && \
    (uv pip install --system --no-cache -r requirements.txt 2>/dev/null || uv pip install --system --no-cache -e .) && \
    uv pip install --system --no-cache gunicorn uvicorn
EXPOSE 19000
CMD ["gunicorn","-k","uvicorn.workers.UvicornWorker","-w","2","-b","0.0.0.0:19000","--timeout","1800","service.app:app"]
```

`.dockerignore`(없으면): `\n.venv\n__pycache__\n*.pyc\n.git\nedgequake\n`

- [ ] **Step 3: 빌드**

Run: `cd /Users/xxx/workspace/8.kb-pipeline && docker build -f Dockerfile.facade -t kbp-facade:local .`
Expected: 빌드 성공(의존성 설치 방식이 다르면 Step 1 근거로 COPY/설치 라인 조정).

- [ ] **Step 4: 단독 실행 헬스(임시, env 없이 import 되는지)**

Run: `docker run --rm -d --name t-facade -p 19000:19000 kbp-facade:local; sleep 4; curl -s -m3 localhost:19000/healthz; docker rm -f t-facade`
Expected: `{"status":"ok"}`(또는 의존 미배선 경고여도 프로세스 부팅+헬스 200). 실패 시 로그로 원인.

- [ ] **Step 5: Commit**

```bash
cd /Users/xxx/workspace/8.kb-pipeline
git add Dockerfile.facade .dockerignore
git commit -m "build(facade): Dockerfile (python+gunicorn, :19000)"
```

---

## Task 2: doc_guard 이미지 gunicorn 화

**Files:**
- Modify: `99.projects/shinhan_trust/doc_guard/Dockerfile`

**Interfaces:** Produces: `app.main:app` @:8000 gunicorn 2워커, `/healthz` 200 + `/v1/check-excel` 존재.

- [ ] **Step 1: 기존 Dockerfile 확인**

Run: `sed -n '1,40p' /Users/xxx/workspace/99.projects/shinhan_trust/doc_guard/Dockerfile`
목적: base/설치/CMD 파악.

- [ ] **Step 2: CMD 를 gunicorn 으로 (+gunicorn 설치)**

기존 CMD(uvicorn app.main:app ...)를 아래로 교체하고, 설치 단계에 `gunicorn` 추가:

```dockerfile
RUN pip install --no-cache-dir gunicorn uvicorn
EXPOSE 8000
CMD ["gunicorn","-k","uvicorn.workers.UvicornWorker","-w","2","-b","0.0.0.0:8000","--timeout","120","app.main:app"]
```

- [ ] **Step 3: 빌드 + 헬스 + 새 엔드포인트**

Run:
```bash
cd /Users/xxx/workspace/99.projects/shinhan_trust/doc_guard && docker build -t kbp-doc-guard:local .
docker run --rm -d --name t-dg -p 8001:8000 kbp-doc-guard:local; sleep 4
curl -s -m3 localhost:8001/healthz
curl -s -m5 -X POST localhost:8001/v1/check-excel -H 'Content-Type: application/json' -d '{"filename":"t.xlsx","gate_summary":{"ok":true,"sheets":[]}}'
docker rm -f t-dg
```
Expected: `{"status":"ok"}` + `{"result":"pass",...}`.

- [ ] **Step 4: Commit**

```bash
cd /Users/xxx/workspace/99.projects/shinhan_trust/doc_guard
git add Dockerfile
git commit -m "build(doc_guard): gunicorn CMD (:8000, /v1/check-excel verified)"
```

---

## Task 3: excel-parser 이미지 (python + node/kordoc)

**Files:**
- Create: `7.excel-parser/Dockerfile`

**Interfaces:** Produces: `service.main:app`@:18055, `/parse` 가 `stats.gate_summary` 반환(kordoc 동작).

- [ ] **Step 1: 의존성/실행 확인**

Run: `ls /Users/xxx/workspace/7.excel-parser/{pyproject.toml,requirements.txt,setup.py} 2>/dev/null; which kordoc; node -v`
목적: python 설치법 + kordoc CLI(node) 필요 확인(auto backend). kordoc 은 npm 패키지.

- [ ] **Step 2: Dockerfile 작성 (node+python)**

```dockerfile
# 7.excel-parser/Dockerfile
FROM python:3.12-slim AS base
WORKDIR /app
# Node(kordoc CLI용) + soffice(.xls 변환) 설치
RUN apt-get update && apt-get install -y --no-install-recommends \
      curl build-essential nodejs npm libreoffice-calc && rm -rf /var/lib/apt/lists/*
# kordoc CLI 설치(글로벌). 실제 패키지명은 Step1 로 확인(예: -g kordoc).
RUN npm install -g kordoc || echo "WARN: kordoc npm 설치 실패 — 패키지명 확인 필요"
COPY requirements*.txt pyproject.toml* setup.py* ./
COPY excel_parser_rag ./excel_parser_rag
COPY service ./service
RUN pip install --no-cache-dir uv && \
    (uv pip install --system --no-cache -r requirements.txt 2>/dev/null || uv pip install --system --no-cache -e .) && \
    uv pip install --system --no-cache gunicorn uvicorn
ENV EXCEL_PARSER_BACKEND=auto KORDOC_BIN=kordoc KORDOC_MD_OUT=/tmp/kordoc_md_out
RUN mkdir -p /tmp/kordoc_md_out
EXPOSE 18055
CMD ["gunicorn","-k","uvicorn.workers.UvicornWorker","-w","2","-b","0.0.0.0:18055","--timeout","600","service.main:app"]
```

> ⚠️ kordoc npm 패키지명이 다르면 Step1 결과로 교체. kordoc 이 사설/로컬 빌드면 소스 COPY+빌드로 대체.

- [ ] **Step 3: 빌드 + gate_summary 검증**

Run:
```bash
cd /Users/xxx/workspace/7.excel-parser && docker build -t kbp-excel-parser:local .
docker run --rm -d --name t-xp -p 18056:18055 kbp-excel-parser:local; sleep 5
curl -s -m120 -F "file=@test_doc_excel/신한자산신탁_외부테이터_필요사이트 정리.xlsx" localhost:18056/parse \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('gate ok=',d['stats']['gate_summary']['ok'])"
docker rm -f t-xp
```
Expected: `gate ok= False`. `detail: *.md 를 찾을 수 없습니다` 면 kordoc 미설치 → Step2 수정.

- [ ] **Step 4: Commit**

```bash
cd /Users/xxx/workspace/7.excel-parser
git add Dockerfile
git commit -m "build(excel-parser): Dockerfile (python+node/kordoc, gate_summary verified)"
```

---

## Task 4: adaptive_chunk 이미지 (python)

**Files:**
- Create: `99.projects/adaptive_chunk/Dockerfile`

**Interfaces:** Produces: `service.main:app`@:18060, `/healthz` 200.

- [ ] **Step 1: 의존성 확인**

Run: `ls /Users/xxx/workspace/99.projects/adaptive_chunk/{requirements.txt,pyproject.toml,setup.py} 2>/dev/null`

- [ ] **Step 2: Dockerfile 작성**

```dockerfile
# 99.projects/adaptive_chunk/Dockerfile
FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl build-essential && rm -rf /var/lib/apt/lists/*
COPY requirements*.txt pyproject.toml* setup.py* ./
COPY . .
RUN pip install --no-cache-dir uv && \
    (uv pip install --system --no-cache -r requirements.txt 2>/dev/null || uv pip install --system --no-cache -e .) && \
    uv pip install --system --no-cache gunicorn uvicorn
EXPOSE 18060
CMD ["gunicorn","-k","uvicorn.workers.UvicornWorker","-w","3","-b","0.0.0.0:18060","--timeout","900","service.main:app"]
```

- [ ] **Step 3: 빌드 + 헬스**

Run: `cd /Users/xxx/workspace/99.projects/adaptive_chunk && docker build -t kbp-adaptive-chunk:local . && docker run --rm -d --name t-ac -p 18061:18060 kbp-adaptive-chunk:local; sleep 4; curl -s -m3 localhost:18061/healthz; docker rm -f t-ac`
Expected: `{"status":"ok"}`.

- [ ] **Step 4: Commit**

```bash
cd /Users/xxx/workspace/99.projects/adaptive_chunk
git add Dockerfile
git commit -m "build(adaptive_chunk): Dockerfile (python+gunicorn, :18060)"
```

---

## Task 5: parse-svc 이미지 (python + JRE17)

**Files:**
- Create: `8.kb-pipeline/Dockerfile.parse-svc`

**Interfaces:** Produces: `parse_service.app:app`@:19001, `/healthz` 200. OpenDataLoader java 동작.

- [ ] **Step 1: java 의존/OpenDataLoader 확인**

Run: `grep -rniE "java|OpenDataLoader|\.jar|KBP_JAVA|subprocess" /Users/xxx/workspace/8.kb-pipeline/parse_service/*.py /Users/xxx/workspace/8.kb-pipeline/kb_pipeline/*.py 2>/dev/null | head`
목적: java 실행 경로·jar 위치 파악(런처 run-parse-svc.sh 가 openjdk@17 PATH 핀).

- [ ] **Step 2: Dockerfile 작성 (JRE17 포함)**

```dockerfile
# 8.kb-pipeline/Dockerfile.parse-svc
FROM python:3.12-slim
WORKDIR /app
# OpenDataLoader = JRE17 필요. libreoffice/soffice 도 문서변환에 쓰이면 추가.
RUN apt-get update && apt-get install -y --no-install-recommends \
      curl build-essential openjdk-17-jre-headless && rm -rf /var/lib/apt/lists/*
ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
COPY requirements*.txt pyproject.toml* setup.py* ./
COPY kb_pipeline ./kb_pipeline
COPY parse_service ./parse_service
RUN pip install --no-cache-dir uv && \
    (uv pip install --system --no-cache -r requirements.txt 2>/dev/null || uv pip install --system --no-cache -e .) && \
    uv pip install --system --no-cache gunicorn uvicorn
EXPOSE 19001
CMD ["gunicorn","-k","uvicorn.workers.UvicornWorker","-w","4","-b","0.0.0.0:19001","--timeout","1800","parse_service.app:app"]
```

> OpenDataLoader jar 이 소스에 포함/다운로드되는지 Step1 로 확인 후 COPY/설치 추가. java 스텁 아닌 실 JRE 확인(`java -version` in build).

- [ ] **Step 3: 빌드 + 헬스(+java)**

Run: `cd /Users/xxx/workspace/8.kb-pipeline && docker build -f Dockerfile.parse-svc -t kbp-parse-svc:local . && docker run --rm -d --name t-ps -p 19002:19001 kbp-parse-svc:local; sleep 5; curl -s -m3 localhost:19002/healthz; docker exec t-ps java -version 2>&1 | head -1; docker rm -f t-ps`
Expected: `{"status":"ok",...}` + `openjdk version "17...`.

- [ ] **Step 4: Commit**

```bash
cd /Users/xxx/workspace/8.kb-pipeline
git add Dockerfile.parse-svc
git commit -m "build(parse-svc): Dockerfile (python+JRE17+gunicorn, :19001)"
```

---

## Task 6: document-parser 이미지 확인/보정

**Files:**
- Modify(필요시): `99.projects/jiju_chaekmu/sourceCode/document-parser-backend-src/Dockerfile`

**Interfaces:** Produces: document-parser app @:8000(내부), `/health`(또는 헬스경로) 200. gotenberg/minio/원격VL 를 env 로 받음.

- [ ] **Step 1: 기존 Dockerfile + 실행/헬스 경로 확인**

Run: `sed -n '1,50p' "/Users/xxx/workspace/99.projects/jiju_chaekmu/sourceCode/document-parser-backend-src/Dockerfile"; grep -rnE "uvicorn|gunicorn|CMD|EXPOSE|:8000|healthz|/health" "/Users/xxx/workspace/99.projects/jiju_chaekmu/sourceCode/document-parser-backend-src/Dockerfile" "/Users/xxx/workspace/99.projects/jiju_chaekmu/sourceCode/document-parser-backend-src/main.py" | head`

- [ ] **Step 2: gunicorn 아니면 CMD 보정(3워커)**

기존이 uvicorn 단일이면 `gunicorn -k uvicorn.workers.UvicornWorker -w 3 --timeout 1800` 로 교체(설치 라인에 gunicorn 추가). 이미 적절하면 스킵.

- [ ] **Step 3: 빌드 + 헬스**

Run: `cd "/Users/xxx/workspace/99.projects/jiju_chaekmu/sourceCode/document-parser-backend-src" && docker build -t kbp-document-parser:local . && docker run --rm -d --name t-dp -p 18051:8000 kbp-document-parser:local; sleep 5; curl -s -m3 -o /dev/null -w '%{http_code}\n' localhost:18051/health 2>/dev/null || curl -s -m3 -o /dev/null -w '%{http_code}\n' localhost:18051/healthz; docker rm -f t-dp`
Expected: 200.

- [ ] **Step 4: Commit**(수정했을 때만)

```bash
cd "/Users/xxx/workspace/99.projects/jiju_chaekmu/sourceCode/document-parser-backend-src"
git add Dockerfile 2>/dev/null && git commit -m "build(document-parser): gunicorn CMD" 2>/dev/null || echo "변경 없음(기존 Dockerfile 사용)"
```

---

## Task 7: edgequake 이미지 (멀티스테이지 Rust)

**Files:**
- Create: `8.kb-pipeline/docker/edgequake.Dockerfile`

**Interfaces:** Produces: edgequake 바이너리 @:8081, `EDGEQUAKE_CHUNKER=passthrough`, `DATABASE_URL=...@postgres:5432/edgequake`. `/health` 200.

- [ ] **Step 1: cargo 빌드 대상/마이그레이션 위치 확인**

Run: `ls /Users/xxx/workspace/8.kb-pipeline/edgequake/edgequake/Cargo.toml 2>/dev/null; grep -nE "\[\[bin\]\]|name *=|migrations" /Users/xxx/workspace/8.kb-pipeline/edgequake/edgequake/Cargo.toml 2>/dev/null | head; sed -n '1,60p' /Users/xxx/workspace/8.kb-pipeline/service/scripts/start_dedicated_edgequake.sh | grep -nE "cargo|target|migrat|EDGEQUAKE_|DATABASE_URL|PORT"`
목적: 바이너리명(`edgequake`), 마이그레이션 경로, 필수 env.

- [ ] **Step 2: 멀티스테이지 Dockerfile 작성**

```dockerfile
# 8.kb-pipeline/docker/edgequake.Dockerfile
FROM rust:1-slim AS build
WORKDIR /src
RUN apt-get update && apt-get install -y --no-install-recommends pkg-config libssl-dev && rm -rf /var/lib/apt/lists/*
COPY edgequake/edgequake /src
RUN cargo build --release --bin edgequake

FROM debian:bookworm-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl libssl3 && rm -rf /var/lib/apt/lists/*
COPY --from=build /src/target/release/edgequake /usr/local/bin/edgequake
# 마이그레이션 디렉토리 있으면 복사(Step1 경로)
COPY edgequake/edgequake/migrations ./migrations 2>/dev/null || true
ENV EDGEQUAKE_HOST=0.0.0.0 EDGEQUAKE_PORT=8081 EDGEQUAKE_CHUNKER=passthrough
EXPOSE 8081
CMD ["edgequake"]
```

> ⚠️ build.context 는 compose 에서 `8.kb-pipeline`(submodule 포함) 로 지정. Cargo 워크스페이스면 `--bin edgequake` 대상 경로 확인.

- [ ] **Step 3: 빌드(최초 오래)**

Run: `cd /Users/xxx/workspace/8.kb-pipeline && docker build -f docker/edgequake.Dockerfile -t kbp-edgequake:local . 2>&1 | tail -5`
Expected: 빌드 성공(캐시 없으면 수분~수십분). 실패 시 Cargo 대상/의존 조정.

- [ ] **Step 4: Commit**

```bash
cd /Users/xxx/workspace/8.kb-pipeline
git add docker/edgequake.Dockerfile
git commit -m "build(edgequake): multistage Rust Dockerfile (:8081, passthrough)"
```

---

## Task 8: docker-compose.yml (전 서비스 통합)

**Files:**
- Create: `8.kb-pipeline/docker-compose.yml`
- Create: `8.kb-pipeline/.env.example`

**Interfaces:** Consumes: Task1-7 이미지/Dockerfile. Produces: `docker compose up -d` 로 전 스택 기동.

- [ ] **Step 1: .env.example 작성 (키 카탈로그)**

```dotenv
# 8.kb-pipeline/.env.example — 실값은 .env(gitignore)에.
OPENROUTER_API_KEY=
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
LITELLM_EMBEDDING_API_KEY=
LITELLM_EMBEDDING_BASE_URL=https://litellm.ax-demo.com/v1
KBP_OPENAI_API_KEY=
MODEL_API_URL=
MODEL_API_KEY=
MINIO_ACCESS_KEY=
MINIO_SECRET_KEY=
POSTGRES_PASSWORD=edgequake_secret
```

- [ ] **Step 2: docker-compose.yml 작성**

```yaml
# 8.kb-pipeline/docker-compose.yml
name: kbp
networks: { kbp: {} }
volumes: { eq_pg_data: {}, minio_data: {} }
services:
  postgres:
    image: ghcr.io/raphaelmansuy/edgequake-postgres:latest
    environment: { POSTGRES_USER: edgequake, POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-edgequake_secret}, POSTGRES_DB: edgequake }
    volumes: [ "eq_pg_data:/var/lib/postgresql/data" ]
    ports: [ "5433:5432" ]
    healthcheck: { test: ["CMD-SHELL","pg_isready -U edgequake"], interval: 5s, timeout: 3s, retries: 20 }
    networks: [kbp]
  edgequake:
    build: { context: ., dockerfile: docker/edgequake.Dockerfile }
    environment:
      DATABASE_URL: postgres://edgequake:${POSTGRES_PASSWORD:-edgequake_secret}@postgres:5432/edgequake
      EDGEQUAKE_CHUNKER: passthrough
      EDGEQUAKE_LLM_PROVIDER: openrouter
      OPENROUTER_API_KEY: ${OPENROUTER_API_KEY}
      EDGEQUAKE_DEFAULT_LLM_MODEL: qwen/qwen3.5-122b-a10b
      EDGEQUAKE_EMBEDDING_PROVIDER: openai
      EDGEQUAKE_EMBEDDING_BASE_URL: ${LITELLM_EMBEDDING_BASE_URL}
      EDGEQUAKE_EMBEDDING_API_KEY: ${LITELLM_EMBEDDING_API_KEY}
      EDGEQUAKE_EMBEDDING_MODEL: bge-m3
      EDGEQUAKE_EMBEDDING_DIMENSION: "1024"
    depends_on: { postgres: { condition: service_healthy } }
    ports: [ "8081:8081" ]
    healthcheck: { test: ["CMD","curl","-fsS","http://localhost:8081/health"], interval: 10s, timeout: 5s, retries: 20 }
    networks: [kbp]
  gotenberg:
    image: gotenberg/gotenberg:8
    networks: [kbp]
  minio:
    image: minio/minio
    command: server /data --console-address ":9001"
    environment: { MINIO_ROOT_USER: ${MINIO_ACCESS_KEY}, MINIO_ROOT_PASSWORD: ${MINIO_SECRET_KEY} }
    volumes: [ "minio_data:/data" ]
    ports: [ "19010:9000", "19011:9001" ]
    networks: [kbp]
  document-parser:
    build: { context: /Users/xxx/workspace/99.projects/jiju_chaekmu/sourceCode/document-parser-backend-src }
    environment:
      GOTENBERG_URL: http://gotenberg:3000
      MINIO_ENDPOINT: minio:9000
      MODEL_API_URL: ${MODEL_API_URL}
      MODEL_API_KEY: ${MODEL_API_KEY}
      MINIO_ACCESS_KEY: ${MINIO_ACCESS_KEY}
      MINIO_SECRET_KEY: ${MINIO_SECRET_KEY}
    depends_on: [gotenberg, minio]
    networks: [kbp]
  excel-parser:
    build: { context: /Users/xxx/workspace/7.excel-parser }
    environment: { EXCEL_PARSER_BACKEND: auto, KORDOC_BIN: kordoc }
    networks: [kbp]
    healthcheck: { test: ["CMD","curl","-fsS","http://localhost:18055/healthz"], interval: 10s, timeout: 5s, retries: 12 }
  doc_guard:
    build: { context: /Users/xxx/workspace/99.projects/shinhan_trust/doc_guard }
    networks: [kbp]
    healthcheck: { test: ["CMD","curl","-fsS","http://localhost:8000/healthz"], interval: 10s, timeout: 5s, retries: 12 }
  adaptive_chunk:
    build: { context: /Users/xxx/workspace/99.projects/adaptive_chunk }
    env_file: [ .env ]   # ADAPTIVE_CHUNK_* 키
    networks: [kbp]
    healthcheck: { test: ["CMD","curl","-fsS","http://localhost:18060/healthz"], interval: 10s, timeout: 5s, retries: 12 }
  parse-svc:
    build: { context: ., dockerfile: Dockerfile.parse-svc }
    environment:
      KBP_OPENAI_API_KEY: ${KBP_OPENAI_API_KEY}
      KBP_OCR_URL: http://document-parser:8000
      KBP_EXCEL_URL: http://excel-parser:18055
      MINIO_ENDPOINT: minio:9000
      MINIO_ACCESS_KEY: ${MINIO_ACCESS_KEY}
      MINIO_SECRET_KEY: ${MINIO_SECRET_KEY}
    depends_on:
      excel-parser: { condition: service_healthy }
      document-parser: { condition: service_started }
    ports: [ "19001:19001" ]
    healthcheck: { test: ["CMD","curl","-fsS","http://localhost:19001/healthz"], interval: 10s, timeout: 5s, retries: 20 }
    networks: [kbp]
  facade:
    build: { context: ., dockerfile: Dockerfile.facade }
    environment:
      KBP_PARSE_SVC_URL: http://parse-svc:19001
      KBP_ADAPTIVE_CHUNK_URL: http://adaptive_chunk:18060
      KBP_EDGEQUAKE_URL: http://edgequake:8081
      KBP_PG_DSN: postgres://edgequake:${POSTGRES_PASSWORD:-edgequake_secret}@postgres:5432/edgequake
      KBP_OPENAI_API_KEY: ${KBP_OPENAI_API_KEY}
    depends_on:
      parse-svc: { condition: service_healthy }
      doc_guard: { condition: service_healthy }
      adaptive_chunk: { condition: service_healthy }
      edgequake: { condition: service_healthy }
    ports: [ "19000:19000" ]
    healthcheck: { test: ["CMD","curl","-fsS","http://localhost:19000/healthz"], interval: 10s, timeout: 5s, retries: 20 }
    networks: [kbp]
```

> compose 내부 이미지에 `curl` 이 없으면 healthcheck 를 `python -c "import urllib.request,sys;urllib.request.urlopen(...)"` 로 대체(각 Dockerfile 에 curl 설치했는지 확인 — Task1-5 는 설치함).

- [ ] **Step 3: 검증 — 전 스택 기동**

Run: `cd /Users/xxx/workspace/8.kb-pipeline && cp -n .env.example .env 2>/dev/null; (실값 채운 뒤) docker compose up -d --build 2>&1 | tail -20 && sleep 20 && docker compose ps`
Expected: 전 서비스 `running`/`healthy`. (최초 edgequake 빌드 오래 — 별도 사전 빌드 가능.)

- [ ] **Step 4: Commit**

```bash
cd /Users/xxx/workspace/8.kb-pipeline
git add docker-compose.yml .env.example
# .env 는 gitignore 확인
grep -q '^\.env$' .gitignore || echo ".env" >> .gitignore
git add .gitignore
git commit -m "build(compose): unified engine stack (postgres/edgequake/parsers/gate/chunk/facade + gotenberg/minio, gunicorn, healthchecks)"
```

---

## Task 9: 검증 — 게이트 회귀 + E2E 스모크 (compose)

**Files:** (신규 파일 없음 — 실행 검증 + 결과 기록)
- Create: `8.kb-pipeline/docs/compose-smoke.md` (결과 기록)

- [ ] **Step 1: 게이트 회귀(compose 내 excel-parser+doc_guard)**

Run:
```bash
# excel-parser gate_summary
docker compose exec -T excel-parser python -c "print('gate service up')"
curl -s -m120 -F "file=@/Users/xxx/workspace/7.excel-parser/test_doc_excel/신한자산신탁_외부테이터_필요사이트 정리.xlsx" http://localhost:18055/parse 2>/dev/null | python3 -c "import sys,json;d=json.load(sys.stdin);print('법령리스트 ok=',d['stats']['gate_summary']['ok'])" || echo "(:18055 미노출이면 compose ports 추가 or exec 로)"
```
Expected: `법령리스트 ok= False`. (excel-parser 포트 미노출이면 compose 에 임시 `ports: 18055:18055` 추가.)

- [ ] **Step 2: E2E — facade ingest→search 스모크**

Run:
```bash
# facade /healthz 및 소형 문서 적재(계약은 facade /ingest — 실제 필드는 service/app.py 확인)
curl -s -m3 localhost:19000/healthz
# 예: 소형 텍스트/엑셀 업로드 → status, chunk_count 확인. (정확한 multipart 필드는 facade 계약 참조.)
```
Expected: facade 헬스 200 + 적재 성공(status completed, chunk_count>0). edgequake 조회로 확인.

- [ ] **Step 3: 동시성 — parse-svc 다중워커 확인**

Run: `docker compose exec -T parse-svc sh -c "ps -ef | grep -c '[g]unicorn'"` → 워커 4(+마스터) 확인. 동시 2건 업로드가 직렬화 안 되는지(대략적 wall-clock) 관찰.

- [ ] **Step 4: 결과 기록 + Commit**

`docs/compose-smoke.md` 에 각 서비스 healthy + 게이트 회귀 + E2E 결과 기록.
```bash
cd /Users/xxx/workspace/8.kb-pipeline
git add docs/compose-smoke.md
git commit -m "test(compose): stack smoke — health, gate regression, e2e ingest"
```

---

## Task 10: 문서/스킬 갱신 (compose 1순위)

**Files:**
- Modify: `8.kb-pipeline/docs/HANDOVER-kb-pipeline-provider.md`
- Modify: `8.kb-pipeline/.claude/skills/restart-kbp-stack/SKILL.md`

- [ ] **Step 1: HANDOVER 기동 섹션을 compose 우선으로**

§2 기동순서를 `docker compose up -d` 1순위 + run-*.sh 는 보조(단일 서비스 재기동)로 갱신. BACKEND_ORIGIN/frontend 는 compose facade(:19000) 가리키게 명시.

- [ ] **Step 2: restart-kbp-stack 스킬에 compose 경로 추가**

`docker compose up -d [--build <svc>]` 를 1순위로, 개별 run-*.sh 는 fallback 으로 문서화.

- [ ] **Step 3: Commit**

```bash
cd /Users/xxx/workspace/8.kb-pipeline
git add docs/HANDOVER-kb-pipeline-provider.md .claude/skills/restart-kbp-stack/SKILL.md
git commit -m "docs: compose as primary startup (handover + restart skill)"
```

---

## Self-Review (작성자 체크)
- **Spec coverage**: §2 서비스→Task1-8, §3 gunicorn→각 Task CMD+워커수, §4 네트워킹→Task8 env, §5 순서→Task8 depends_on/healthcheck, §6 edgequake→Task7 + pg named volume(Task8 volumes), §7 마이그레이션→Task10, §9 검증→Task9. 전부 매핑.
- **열린 항목(구현자 확인)**: 각 레포 의존성 설치법(Step1들), kordoc npm 패키지명, OpenDataLoader jar 위치, document-parser 헬스 경로, edgequake Cargo 대상. Dockerfile 은 이를 근거로 조정.
- **Type/이름 일관성**: 서비스명(compose)·포트·env 키가 Global Constraints 와 Task8 에서 동일.
