<!-- plan-version: v6 -->
<!-- codex-validation: READY v6 at 2026-07-01T08:45:00Z -->

# kb_pipeline 엔진 스택 도커화 + compose (Phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** kb_pipeline provider 엔진측 서비스 전부를 도커 이미지 + 단일 `docker-compose.yml` 로 통일하고, 파이썬 서비스는 gunicorn 다중워커로 띄운다. 코드 병합 없음.

**Architecture:** 각 서비스에 Dockerfile 추가(없는 것 신규, 있는 것 재사용) → `8.kb-pipeline/docker-compose.yml` 에서 build.context 로 각 레포를 빌드 → 서비스명 DNS 네트워킹 → healthcheck+depends_on 순서 → postgres named volume 영속. 파이썬 FastAPI 는 gunicorn(uvicorn worker).

**Tech Stack:** Docker, docker-compose(v2), gunicorn+uvicorn.workers.UvicornWorker, Python 3.10-3.12, JRE17(facade+parse-svc), Node/kordoc(excel-parser), Rust(edgequake), postgres(pgvector+AGE), gotenberg, minio.

## Global Constraints
- compose 파일 위치: `8.kb-pipeline/docker-compose.yml`. Dockerfile 은 각 레포 루트(그 서비스 소스와 함께).
- 파이썬 서비스 실행 = `gunicorn -k uvicorn.workers.UvicornWorker -w <N> -b 0.0.0.0:<port> --timeout <T> <module:app>`. 워커수(in-process job store 위험 포함):
  - parse-svc=4, facade=2, document-parser=3, doc_guard=2 — 상태없는 per-request FastAPI
  - **adaptive_chunk=1, excel-parser=1** — in-process job store(`_shared_job_store_for`, `_job_store`) 가 multi-worker 에서 cross-worker 404 를 유발. Phase 1 에서 단일 워커. (multi-worker = Phase 2: 외부 공유 job store 필요)
- 서비스명/포트(컨테이너 내부): facade `service.app:app`@19000, parse-svc `parse_service.app:app`@19001, excel-parser `service.main:app`@18055, doc_guard `app.main:app`@8000, adaptive_chunk `service.main:app`@18060, document-parser@8000(내부), edgequake@8081, postgres@5432, gotenberg@3000, minio@9000.
- 서비스명 DNS URL(§4): facade→`http://parse-svc:19001`,`http://adaptive_chunk:18060`,`http://edgequake:8081`,`postgres:5432`,`http://excel-parser:18055`,`http://document-parser:8000`; parse-svc→`http://excel-parser:18055`,`http://document-parser:8000`; document-parser→`http://gotenberg:3000`,`minio:9000`; edgequake→`postgres:5432`.
- 외부 URL 유지(교체 금지): 원격 litellm(bge-m3 임베딩), OpenRouter(qwen LLM), 원격 VL(`MODEL_API_URL`).
- 불변식: edgequake `EDGEQUAKE_CHUNKER=passthrough`; 모달마커 U+3008/U+3009; 표 `<table>` 보존; BGE-M3 1024d; 청킹=facade `/chunk` 소유.
- postgres 데이터 = **named volume `eq_pg_data`** 영속. edgequake 기동은 바이너리-온리(DB 초기화 금지, 마이그레이션 idempotent).
- 시크릿은 compose 루트 `.env`(gitignore) + `.env.example`(커밋). 절대 이미지에 굽지 않음.
- build context 경로: compose 파일(`8.kb-pipeline/docker-compose.yml`) 기준 **상대경로**. `8.kb-pipeline/` 과 형제 레포(`7.excel-parser/`, `99.projects/...`)는 동일 부모(`workspace/`)에 있으므로 **`../` 한 단계**(예: `../7.excel-parser`). `../../` 두 단계 금지(한 단계 초과 → context-not-found). 절대경로 금지.
- 비범위: 파서 코드 병합(Phase 2), kb-backend/frontend.

---

## File Structure (생성/수정)
- `7.excel-parser/requirements.txt` — 신규(docker 빌드용; 기존 venv 에서 추출)
- `7.excel-parser/Dockerfile` — 신규(python+node/kordoc+gunicorn, -w 1)
- `99.projects/adaptive_chunk/Dockerfile` — 신규(python+gunicorn, **-w 1**)
- `8.kb-pipeline/Dockerfile.facade` — 신규(python+**JRE17**+gunicorn)
- `8.kb-pipeline/Dockerfile.parse-svc` — 신규(python+JRE17+gunicorn, `COPY service`)
- `8.kb-pipeline/docker/edgequake.Dockerfile` — 신규(멀티스테이지 Rust)
- `99.projects/shinhan_trust/doc_guard/Dockerfile` — 수정(gunicorn + **curl** 설치)
- `99.projects/jiju_chaekmu/.../document-parser-backend-src/Dockerfile.aws` — 재사용(gunicorn 확인)
- `8.kb-pipeline/docker-compose.yml` — 신규(전 서비스+인프라)
- `8.kb-pipeline/.env.example` — 신규(compose 키 카탈로그)
- `8.kb-pipeline/docs/HANDOVER-kb-pipeline-provider.md` — 수정(compose 기동으로 갱신)

---

## Task 0: requirements 감사 (의존성 파일 정비)

**Files:**
- Modify: `8.kb-pipeline/requirements.txt`
- Create: `7.excel-parser/requirements.txt`

**Why first:** Dockerfile 들은 requirements.txt 또는 pyproject.toml 로 의존성을 설치한다. 활성 venv 에 있는 패키지가 이 파일에 누락되면 이미지 런타임에 ImportError. 이 Task 를 먼저 끝낸 뒤 각 Dockerfile 을 작성한다.

**Interfaces:**
- Produces: 두 레포의 requirements.txt 가 실제 실행 의존성을 포함.

- [ ] **Step 1: 8.kb-pipeline venv 감사**

Run:
```bash
# .venv-kb 존재 시 freeze 비교; 없으면 Step 2 의 기본 목록으로 진행
if [ -f /Users/xxx/workspace/8.kb-pipeline/.venv-kb/bin/pip ]; then
  /Users/xxx/workspace/8.kb-pipeline/.venv-kb/bin/pip freeze | sort > /tmp/venv-kb-freeze.txt
  sort /Users/xxx/workspace/8.kb-pipeline/requirements.txt > /tmp/req-kb.txt
  echo "=== venv 에 있지만 requirements.txt 에 없는 패키지 ==="
  comm -23 /tmp/venv-kb-freeze.txt /tmp/req-kb.txt | head -40
else
  echo ".venv-kb 없음 → Step 2 의 기본 목록으로 진행(pip freeze 스킵)"
fi
```
Expected(venv 존재 시): 아래 패키지들이 누락 목록에 포함됨:
- `fastapi`, `python-multipart`, `opendataloader-pdf`, `markitdown`, `minio`, `pillow`, `uvicorn`, `httpx`, `gunicorn`
(venv 없으면 Step 2 기본 목록을 그대로 사용 — 버전은 `>=` 범위 허용)

- [ ] **Step 2: requirements.txt 에 누락 패키지 추가**

아래 패키지를 `8.kb-pipeline/requirements.txt` 에 추가(버전은 Step 1 freeze 결과 기준; 없으면 `>=` 범위로):
```
fastapi>=0.100.0
python-multipart>=0.0.9
uvicorn>=0.20.0
gunicorn>=21.0.0
opendataloader-pdf>=2.4.0
markitdown>=0.0.2
minio>=7.2.0
pillow>=10.0.0
httpx>=0.24.0
```
Step 1 freeze 에서 exact 버전을 가져다 쓰는 것이 우선.

- [ ] **Step 3: excel-parser requirements.txt 생성**

```bash
ls /Users/xxx/workspace/7.excel-parser/.venv/lib/python*/site-packages/ 2>/dev/null | head -5 || \
  /Users/xxx/workspace/7.excel-parser/.venv/bin/pip freeze 2>/dev/null || \
  find /Users/xxx/workspace/7.excel-parser -name "*.egg-info" -o -name "pyproject.toml" | head
```

최소 필수 패키지를 `7.excel-parser/requirements.txt` 로 생성:
```
fastapi>=0.100.0
python-multipart>=0.0.9
uvicorn>=0.20.0
gunicorn>=21.0.0
openpyxl>=3.1.0
pydantic>=2.0.0
httpx>=0.24.0
pillow>=10.0.0
```
venv freeze 결과가 있으면 그 버전을 우선 사용.

- [ ] **Step 4: Commit**

```bash
cd /Users/xxx/workspace/8.kb-pipeline
git add requirements.txt
git commit -m "build: add missing packages to requirements.txt (fastapi, multipart, minio, pillow, opendataloader-pdf)"

cd /Users/xxx/workspace/7.excel-parser
git add requirements.txt
git commit -m "build(excel-parser): add requirements.txt for Docker build"
```

---

## Task 1: facade 이미지 (python + JRE17 + gunicorn)

**Files:**
- Create: `8.kb-pipeline/Dockerfile.facade`
- Create: `8.kb-pipeline/.dockerignore` (없으면)

**Interfaces:**
- Produces: 이미지가 `gunicorn ... service.app:app` 를 :19000 으로 서비스. `/healthz` 200.

**Note:** `pyproject.toml` 의 `packages.find.include=['kb_pipeline*']` 는 `kb_pipeline` 패키지만 설치한다. `service/` 와 `parse_service/` 는 **COPY 로 /app 에 배치**해서 importable 로 만든다(editable install 이 등록하는 것이 아님). facade `/ingest/submit` 는 `service/parsing.py` → `opendataloader_pdf` → `java` 를 호출하므로 **JRE17 필수**.

- [ ] **Step 1: 의존성 설치 방식 확인**

Run: `ls /Users/xxx/workspace/8.kb-pipeline/{pyproject.toml,setup.py,requirements.txt} 2>/dev/null; head -5 /Users/xxx/workspace/8.kb-pipeline/requirements.txt`
목적: requirements.txt 존재·내용 확인(Task 0 후 패키지 추가됨).

- [ ] **Step 2: Dockerfile.facade 작성 (JRE17 포함)**

```dockerfile
# 8.kb-pipeline/Dockerfile.facade
FROM python:3.12-slim AS base
WORKDIR /app
# JRE17: service/parsing.py → opendataloader_pdf → java subprocess (facade /ingest/submit)
RUN apt-get update && apt-get install -y --no-install-recommends \
      curl build-essential openjdk-17-jre-headless && rm -rf /var/lib/apt/lists/*
ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
COPY pyproject.toml setup.py* requirements*.txt ./
# service/ 와 kb_pipeline/ 은 COPY 로 배치(pyproject.toml 은 kb_pipeline* 만 등록함).
COPY kb_pipeline ./kb_pipeline
COPY service ./service
RUN pip install --no-cache-dir uv && \
    (uv pip install --system --no-cache -r requirements.txt 2>/dev/null || uv pip install --system --no-cache -e .) && \
    uv pip install --system --no-cache gunicorn uvicorn
EXPOSE 19000
CMD ["gunicorn","-k","uvicorn.workers.UvicornWorker","-w","2","-b","0.0.0.0:19000","--timeout","1800","service.app:app"]
```

`.dockerignore`(없으면):
```
.venv
.venv-kb
__pycache__
*.pyc
.git
edgequake
```

- [ ] **Step 3: 빌드**

Run: `cd /Users/xxx/workspace/8.kb-pipeline && docker build -f Dockerfile.facade -t kbp-facade:local .`
Expected: 빌드 성공.

- [ ] **Step 4: 헬스 + java 확인**

Run:
```bash
docker run --rm -d --name t-facade -p 19000:19000 kbp-facade:local
sleep 5
curl -s -m3 localhost:19000/healthz
docker exec t-facade java -version 2>&1 | head -1
docker rm -f t-facade
```
Expected: `{"status":"ok"}` + `openjdk version "17...`.

- [ ] **Step 5: Commit**

```bash
cd /Users/xxx/workspace/8.kb-pipeline
git add Dockerfile.facade .dockerignore
git commit -m "build(facade): Dockerfile (python+JRE17+gunicorn, :19000)"
```

---

## Task 2: doc_guard 이미지 gunicorn 화

**Files:**
- Modify: `99.projects/shinhan_trust/doc_guard/Dockerfile`

**Interfaces:** Produces: `app.main:app` @:8000 gunicorn 2워커, `/healthz` 200 + `/v1/check-excel` 존재.

**Note:** compose healthcheck 가 `curl` 을 사용하므로 **curl 을 apt 에서 설치해야** healthcheck 가 작동한다. curl 없으면 service_healthy 조건이 영원히 실패 → facade 기동 불가.

- [ ] **Step 1: 기존 Dockerfile 확인**

Run: `cat /Users/xxx/workspace/99.projects/shinhan_trust/doc_guard/Dockerfile`
목적: base/설치/CMD 파악.

- [ ] **Step 2: curl 추가 + CMD 를 gunicorn 으로**

기존 Dockerfile 에서:
1. `apt-get install` 라인에 `curl` 추가(없으면 새 RUN 라인 추가: `RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*`)
2. 설치 단계에 `gunicorn uvicorn` 추가: `RUN pip install --no-cache-dir gunicorn uvicorn`
3. 기존 CMD(uvicorn)를 아래로 교체:

```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends gcc curl && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -e . && pip install --no-cache-dir gunicorn uvicorn
EXPOSE 8000
CMD ["gunicorn","-k","uvicorn.workers.UvicornWorker","-w","2","-b","0.0.0.0:8000","--timeout","120","app.main:app"]
```

(기존 gcc-only apt 라인과 pip 라인을 위 형태로 병합. Step 1 Dockerfile 내용 기준으로 정확한 위치에 삽입.)

- [ ] **Step 3: 빌드 + 헬스 + 새 엔드포인트**

Run:
```bash
cd /Users/xxx/workspace/99.projects/shinhan_trust/doc_guard && docker build -t kbp-doc-guard:local .
docker run --rm -d --name t-dg -p 8001:8000 kbp-doc-guard:local; sleep 4
curl -s -m3 localhost:8001/healthz
curl -s -m5 -X POST localhost:8001/v1/check-excel \
  -H 'Content-Type: application/json' \
  -d '{"filename":"t.xlsx","gate_summary":{"ok":true,"sheets":[]}}'
docker exec t-dg curl -fsS localhost:8000/healthz   # curl in-container 확인
docker rm -f t-dg
```
Expected: `{"status":"ok"}` + `{"result":"pass",...}` + 컨테이너 내 curl 성공.

- [ ] **Step 4: Commit**

```bash
cd /Users/xxx/workspace/99.projects/shinhan_trust/doc_guard
git add Dockerfile
git commit -m "build(doc_guard): gunicorn CMD + curl install (:8000, healthcheck-safe)"
```

---

## Task 3: excel-parser 이미지 (python + node/kordoc, **-w 1**)

**Files:**
- Create: `7.excel-parser/Dockerfile`

**Interfaces:** Produces: `service.main:app`@:18055, `/parse` 가 `stats.gate_summary` 반환(kordoc 동작).

**Note:** excel-parser 의 `_job_store` 는 per-process 전역 변수. 멀티워커 환경에서 POST→Worker A, GET→Worker B → 404. **-w 1** 고정.

- [ ] **Step 1: kordoc npm 패키지명 확인**

Run: `npm info kordoc version 2>/dev/null; ls /Users/xxx/workspace/7.excel-parser/{package.json,requirements.txt} 2>/dev/null`
Expected: `npm info kordoc` 가 버전 반환하면 패키지명 `kordoc` 확인. 버전(예: 3.5.4) 기록.

- [ ] **Step 2: Dockerfile 작성 (node+python, -w 1)**

```dockerfile
# 7.excel-parser/Dockerfile
FROM python:3.12-slim AS base
WORKDIR /app
# Node(kordoc CLI용)
RUN apt-get update && apt-get install -y --no-install-recommends \
      curl build-essential nodejs npm && rm -rf /var/lib/apt/lists/*
# kordoc: public npm package (npm info kordoc 확인됨, MIT)
RUN npm install -g kordoc
COPY requirements*.txt pyproject.toml* setup.py* ./
COPY excel_parser_rag ./excel_parser_rag
COPY service ./service
RUN pip install --no-cache-dir uv && \
    (uv pip install --system --no-cache -r requirements.txt 2>/dev/null || uv pip install --system --no-cache -e .) && \
    uv pip install --system --no-cache gunicorn uvicorn
ENV EXCEL_PARSER_BACKEND=auto KORDOC_BIN=kordoc KORDOC_MD_OUT=/tmp/kordoc_md_out
RUN mkdir -p /tmp/kordoc_md_out
EXPOSE 18055
# -w 1: _job_store 는 in-process 전역 변수 → multi-worker 에서 cross-worker 404 발생
CMD ["gunicorn","-k","uvicorn.workers.UvicornWorker","-w","1","-b","0.0.0.0:18055","--timeout","600","service.main:app"]
```

- [ ] **Step 3: 빌드 + gate_summary 검증**

Run:
```bash
cd /Users/xxx/workspace/7.excel-parser && docker build -t kbp-excel-parser:local .
docker run --rm -d --name t-xp -p 18056:18055 kbp-excel-parser:local; sleep 5
curl -s -m120 -F "file=@test_doc_excel/신한자산신탁_외부테이터_필요사이트 정리.xlsx" localhost:18056/parse \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('gate ok=',d['stats']['gate_summary']['ok'])"
docker rm -f t-xp
```
Expected: `gate ok= False`.

- [ ] **Step 4: Commit**

```bash
cd /Users/xxx/workspace/7.excel-parser
git add Dockerfile
git commit -m "build(excel-parser): Dockerfile (python+node/kordoc, -w 1, gate_summary verified)"
```

---

## Task 4: adaptive_chunk 이미지 (python, **-w 1**)

**Files:**
- Create: `99.projects/adaptive_chunk/Dockerfile`

**Interfaces:** Produces: `service.main:app`@:18060, `/healthz` 200.

**Note:** `adaptive_chunk/service/jobs.py` 의 `_shared_job_store_for` 는 `@lru_cache` per-process. 멀티워커 → cross-worker 404. **-w 1** 고정.

- [ ] **Step 1: 의존성 확인**

Run: `ls /Users/xxx/workspace/99.projects/adaptive_chunk/{requirements.txt,pyproject.toml,setup.py} 2>/dev/null`

- [ ] **Step 1b: ADAPTIVE_CHUNK_* env var 감사 (Task 8 compose 작성 전 필수)**

```bash
grep -nE "ADAPTIVE_CHUNK_|Field\(|env_prefix" \
  /Users/xxx/workspace/99.projects/adaptive_chunk/config.py | head -40
```
목적: pydantic-settings `env_prefix="ADAPTIVE_CHUNK_"` 하에 서비스가 읽는 **모든** 환경변수 파악.
예상 필드(실제 config.py 내용으로 확인 후 Task 8 compose adaptive_chunk environment 에 추가):
- `ADAPTIVE_CHUNK_OPENROUTER_API_KEY` — OpenRouter LLM 키
- `ADAPTIVE_CHUNK_OCR_BASE_URL` — OCR 서비스 URL
- `ADAPTIVE_CHUNK_REGEX_LLM_MODEL`, `ADAPTIVE_CHUNK_SCORING_EMBEDDING_MODEL`, `ADAPTIVE_CHUNK_RERANK_API_KEY` 등

이 목록을 Task 8 Step 2 adaptive_chunk environment 블록에 반영한다(현재 블록에 없는 필드 추가).

- [ ] **Step 2: Dockerfile 작성 (-w 1)**

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
# -w 1: _shared_job_store_for(@lru_cache) 는 per-process 전역 → multi-worker 에서 job 404
CMD ["gunicorn","-k","uvicorn.workers.UvicornWorker","-w","1","-b","0.0.0.0:18060","--timeout","900","service.main:app"]
```

- [ ] **Step 3: 빌드 + 헬스**

Run: `cd /Users/xxx/workspace/99.projects/adaptive_chunk && docker build -t kbp-adaptive-chunk:local . && docker run --rm -d --name t-ac -p 18061:18060 kbp-adaptive-chunk:local; sleep 4; curl -s -m3 localhost:18061/healthz; docker rm -f t-ac`
Expected: `{"status":"ok"}`.

- [ ] **Step 4: Commit**

```bash
cd /Users/xxx/workspace/99.projects/adaptive_chunk
git add Dockerfile
git commit -m "build(adaptive_chunk): Dockerfile (python+gunicorn, -w 1)"
```

---

## Task 5: parse-svc 이미지 (python + JRE17)

**Files:**
- Create: `8.kb-pipeline/Dockerfile.parse-svc`

**Interfaces:** Produces: `parse_service.app:app`@:19001, `/healthz` 200. OpenDataLoader java 동작.

**Note:** `parse_service/app.py` 는 `from service.llm import get_text_llm` 를 lazy import 한다. `service/` 가 이미지에 없으면 KBP_MODAL_ENRICH=1 시 런타임 ImportError. **`COPY service ./service` 필수**.

- [ ] **Step 1: java 의존/OpenDataLoader 확인**

Run: `grep -rniE "java|OpenDataLoader|\.jar|subprocess" /Users/xxx/workspace/8.kb-pipeline/parse_service/*.py /Users/xxx/workspace/8.kb-pipeline/kb_pipeline/*.py 2>/dev/null | head`

- [ ] **Step 2: Dockerfile 작성 (JRE17 + service 디렉터리)**

```dockerfile
# 8.kb-pipeline/Dockerfile.parse-svc
FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
      curl build-essential openjdk-17-jre-headless && rm -rf /var/lib/apt/lists/*
ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
COPY pyproject.toml setup.py* requirements*.txt ./
COPY kb_pipeline ./kb_pipeline
COPY parse_service ./parse_service
# parse_service/app.py 가 runtime에 `from service.llm import get_text_llm` 을 lazy import함
COPY service ./service
RUN pip install --no-cache-dir uv && \
    (uv pip install --system --no-cache -r requirements.txt 2>/dev/null || uv pip install --system --no-cache -e .) && \
    uv pip install --system --no-cache gunicorn uvicorn
EXPOSE 19001
CMD ["gunicorn","-k","uvicorn.workers.UvicornWorker","-w","4","-b","0.0.0.0:19001","--timeout","1800","parse_service.app:app"]
```

- [ ] **Step 3: 빌드 + 헬스(+java)**

Run:
```bash
cd /Users/xxx/workspace/8.kb-pipeline
docker build -f Dockerfile.parse-svc -t kbp-parse-svc:local .
docker run --rm -d --name t-ps -p 19002:19001 kbp-parse-svc:local
sleep 5
curl -s -m3 localhost:19002/healthz
docker exec t-ps java -version 2>&1 | head -1
docker rm -f t-ps
```
Expected: `{"status":"ok",...}` + `openjdk version "17...`.

- [ ] **Step 4: Commit**

```bash
cd /Users/xxx/workspace/8.kb-pipeline
git add Dockerfile.parse-svc
git commit -m "build(parse-svc): Dockerfile (python+JRE17+COPY service+gunicorn, :19001)"
```

---

## Task 6: document-parser 이미지 확인/보정

**Files:**
- Use: `99.projects/jiju_chaekmu/sourceCode/document-parser-backend-src/Dockerfile.aws` (**기본 `Dockerfile` 아님**)

**Interfaces:** Produces: document-parser app @:8000(내부), 헬스경로 `/health` 200 (readiness, gotenberg 연결 검증). gotenberg/minio/원격VL 를 env 로 받음.

**Note:** 기존 `Dockerfile`(기본) 는 `COPY backend/requirements.txt` 등 존재하지 않는 경로를 참조해 빌드 실패. **`Dockerfile.aws`** 가 올바른 파일(`COPY requirements.txt .`, `COPY . /app`). compose 에서 `dockerfile: Dockerfile.aws` 명시 필수.

- [ ] **Step 1: Dockerfile.aws + 헬스경로 확인**

Run:
```bash
ls /Users/xxx/workspace/99.projects/jiju_chaekmu/sourceCode/document-parser-backend-src/Dockerfile*
grep -nE "CMD|EXPOSE|HEALTHCHECK|uvicorn|gunicorn|/health|/healthz|8000" \
  /Users/xxx/workspace/99.projects/jiju_chaekmu/sourceCode/document-parser-backend-src/Dockerfile.aws | head
```
목적: 헬스 경로(`/` vs `/health` vs `/healthz`), 내부 포트(8000), CMD 확인.

- [ ] **Step 2: gunicorn 설치 + CMD 보정(3워커)**

document-parser app module = **`main:app`** (Dockerfile.aws CMD 확인: `python -m uvicorn main:app`).
Dockerfile.aws 에 아래 두 변경을 순서대로 적용:
1. 기존 `RUN pip install ...` 라인 뒤에 gunicorn 추가(없으면 새 RUN 추가):
   ```dockerfile
   RUN pip install --no-cache-dir gunicorn uvicorn
   ```
2. 기존 CMD(uvicorn) 를 아래로 교체 — ENTRYPOINT `/entrypoint.sh` 는 그대로 유지:
   ```dockerfile
   CMD ["gunicorn","-k","uvicorn.workers.UvicornWorker","-w","3","-b","0.0.0.0:8000","--timeout","1800","main:app"]
   ```
이미 gunicorn 이면 CMD 워커 수(-w 3)·timeout 만 확인 후 스킵.

- [ ] **Step 3: 빌드 + 헬스**

Run:
```bash
cd "/Users/xxx/workspace/99.projects/jiju_chaekmu/sourceCode/document-parser-backend-src"
docker build -f Dockerfile.aws -t kbp-document-parser:local .
docker run --rm -d --name t-dp -p 18051:8000 kbp-document-parser:local; sleep 5
curl -s -m3 -o /dev/null -w '%{http_code}\n' localhost:18051/health   # /health = readiness
docker rm -f t-dp
```
Expected: 200 또는 503(/health 는 gotenberg 연결 확인 — 단독 실행 시 503 가능; 부팅 증거로 허용).

- [ ] **Step 4: Commit**(수정했을 때만)

```bash
cd "/Users/xxx/workspace/99.projects/jiju_chaekmu/sourceCode/document-parser-backend-src"
git add Dockerfile.aws 2>/dev/null && git commit -m "build(document-parser): gunicorn CMD" 2>/dev/null || echo "변경 없음"
```

---

## Task 7: edgequake 이미지 (멀티스테이지 Rust)

**Files:**
- Create: `8.kb-pipeline/docker/edgequake.Dockerfile`

**Interfaces:** Produces: edgequake 바이너리 @:**8081**(HOST+PORT env로 바인드), `EDGEQUAKE_CHUNKER=passthrough`, `DATABASE_URL=...@postgres:5432/edgequake`. `/health` 200.

**CRITICAL 주의사항:**
1. **edgequake 바이너리는 `HOST`/`PORT` 를 읽는다** (EDGEQUAKE_HOST/EDGEQUAKE_PORT 아님). 잘못된 env 명 → 기본 8080 바인드 → healthcheck 8081 probe 실패 → 전체 depends_on 체인 데드락.
2. **`COPY ... 2>/dev/null || true` 는 유효한 Dockerfile 문법이 아님** (shell redirect 은 RUN 안에서만 가능). migrations 은 sqlx::migrate!() 로 컴파일 타임 임베드됨 → 런타임 이미지에 불필요.
3. **edgequake 는 git submodule** — 빌드 전 `git submodule update --init --recursive edgequake` 필수.

- [ ] **Step 0: git submodule 초기화 확인**

Run:
```bash
ls /Users/xxx/workspace/8.kb-pipeline/edgequake/edgequake/Cargo.toml 2>/dev/null || \
  (cd /Users/xxx/workspace/8.kb-pipeline && git submodule update --init --recursive edgequake && echo "submodule initialized")
```
Expected: Cargo.toml 이 존재하거나 init 성공.

- [ ] **Step 1: cargo 빌드 대상/env 확인**

Run:
```bash
grep -nE "\[\[bin\]\]|^name|workspace" /Users/xxx/workspace/8.kb-pipeline/edgequake/edgequake/Cargo.toml | head -10
grep -nE "HOST|PORT|DATABASE_URL|EDGEQUAKE_" /Users/xxx/workspace/8.kb-pipeline/service/scripts/start_dedicated_edgequake.sh | head -20
```
목적: 바이너리명 확인, 실제 env 변수명(HOST/PORT) 확인.

- [ ] **Step 2: 멀티스테이지 Dockerfile 작성**

```dockerfile
# 8.kb-pipeline/docker/edgequake.Dockerfile
FROM rust:1-slim AS build
WORKDIR /src
RUN apt-get update && apt-get install -y --no-install-recommends \
      pkg-config libssl-dev libpq-dev && rm -rf /var/lib/apt/lists/*
COPY edgequake/edgequake /src
RUN cargo build --release --locked --bin edgequake

FROM debian:bookworm-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl libssl3 libpq5 && rm -rf /var/lib/apt/lists/*
COPY --from=build /src/target/release/edgequake /usr/local/bin/edgequake
# migrations 은 sqlx::migrate!()로 바이너리에 임베드됨 → 런타임 복사 불필요
# edgequake 는 HOST/PORT 를 읽는다 (EDGEQUAKE_HOST/EDGEQUAKE_PORT 아님)
ENV HOST=0.0.0.0 PORT=8081 EDGEQUAKE_CHUNKER=passthrough PDFIUM_AUTO_CACHE_DIR=/tmp/eqkbp-pdfium
RUN mkdir -p /tmp/eqkbp-pdfium
EXPOSE 8081
CMD ["edgequake"]
```

- [ ] **Step 3: 빌드(최초 오래 — 수분~수십분)**

Run: `cd /Users/xxx/workspace/8.kb-pipeline && docker build -f docker/edgequake.Dockerfile -t kbp-edgequake:local . 2>&1 | tail -5`
Expected: 빌드 성공.

- [ ] **Step 4: Commit**

```bash
cd /Users/xxx/workspace/8.kb-pipeline
mkdir -p docker
git add docker/edgequake.Dockerfile
git commit -m "build(edgequake): multistage Rust Dockerfile (HOST/PORT, passthrough, :8081)"
```

---

## Task 8: docker-compose.yml (전 서비스 통합)

**Files:**
- Create: `8.kb-pipeline/docker-compose.yml`
- Create: `8.kb-pipeline/.env.example`

**Interfaces:** Consumes: Task0-7 이미지/Dockerfile. Produces: `docker compose up -d` 로 전 스택 기동.

- [ ] **Step 1: .env.example 작성 (키 카탈로그)**

```dotenv
# 8.kb-pipeline/.env.example — 실값은 .env(gitignore)에.
# OpenRouter (edgequake LLM + facade/parse-svc LLM)
OPENROUTER_API_KEY=
KBP_OPENAI_API_KEY=          # parse-svc/facade modal LLM key (OpenRouter or litellm)
KBP_OPENAI_BASE_URL=https://openrouter.ai/api/v1
KBP_LLM_MODEL=qwen/qwen3.5-122b-a10b
# litellm (bge-m3 임베딩)
LITELLM_EMBEDDING_API_KEY=
LITELLM_EMBEDDING_BASE_URL=https://litellm.ax-demo.com/v1
# VL (document-parser)
MODEL_API_URL=
MODEL_API_KEY=
# MinIO
MINIO_ACCESS_KEY=
MINIO_SECRET_KEY=
MINIO_BUCKET=document-parser
# Postgres
POSTGRES_PASSWORD=edgequake_secret
# adaptive_chunk (env_prefix="ADAPTIVE_CHUNK_" — 반드시 접두사 포함)
ADAPTIVE_CHUNK_OPENROUTER_API_KEY=  # compose 에서 OPENROUTER_API_KEY 값을 매핑
```

- [ ] **Step 2: docker-compose.yml 작성**

build context 는 compose 파일(`8.kb-pipeline/`) 기준 **상대경로**. 절대경로 금지.

```yaml
# 8.kb-pipeline/docker-compose.yml
name: kbp
networks: { kbp: {} }
volumes: { eq_pg_data: {}, minio_data: {} }
services:
  postgres:
    image: ghcr.io/raphaelmansuy/edgequake-postgres:latest
    environment:
      POSTGRES_USER: edgequake
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-edgequake_secret}
      POSTGRES_DB: edgequake
    volumes: ["eq_pg_data:/var/lib/postgresql/data"]
    ports: ["5433:5432"]
    healthcheck:
      test: ["CMD-SHELL","pg_isready -U edgequake"]
      interval: 5s
      timeout: 3s
      retries: 20
    networks: [kbp]

  edgequake:
    build: { context: ., dockerfile: docker/edgequake.Dockerfile }
    environment:
      DATABASE_URL: postgres://edgequake:${POSTGRES_PASSWORD:-edgequake_secret}@postgres:5432/edgequake
      EDGEQUAKE_CHUNKER: passthrough
      EDGEQUAKE_LLM_PROVIDER: openrouter
      OPENROUTER_API_KEY: ${OPENROUTER_API_KEY}
      # HOST/PORT (not EDGEQUAKE_HOST/EDGEQUAKE_PORT) — edgequake main.rs reads these
      HOST: 0.0.0.0
      PORT: "8081"
      EDGEQUAKE_DEFAULT_LLM_MODEL: qwen/qwen3.5-122b-a10b
      EDGEQUAKE_LLM_MODEL: qwen/qwen3.5-122b-a10b   # prevents COMPAT-GUARD downgrade bug
      EDGEQUAKE_EMBEDDING_PROVIDER: openai
      EDGEQUAKE_EMBEDDING_BASE_URL: ${LITELLM_EMBEDDING_BASE_URL}
      EDGEQUAKE_EMBEDDING_API_KEY: ${LITELLM_EMBEDDING_API_KEY}
      EDGEQUAKE_EMBEDDING_MODEL: bge-m3
      EDGEQUAKE_EMBEDDING_DIMENSION: "1024"
      PDFIUM_AUTO_CACHE_DIR: /tmp/eqkbp-pdfium
    depends_on: { postgres: { condition: service_healthy } }
    ports: ["8081:8081"]
    healthcheck:
      test: ["CMD","curl","-fsS","http://localhost:8081/health"]
      interval: 10s
      timeout: 5s
      retries: 20
    networks: [kbp]

  gotenberg:
    image: gotenberg/gotenberg:8
    networks: [kbp]
    healthcheck:
      test: ["CMD","curl","-fsS","http://localhost:3000/health"]
      interval: 10s
      timeout: 5s
      retries: 12

  minio:
    image: minio/minio
    command: server /data --console-address ":9001"
    environment:
      MINIO_ROOT_USER: ${MINIO_ACCESS_KEY}
      MINIO_ROOT_PASSWORD: ${MINIO_SECRET_KEY}
    volumes: ["minio_data:/data"]
    ports: ["19010:9000","19011:9001"]
    healthcheck:
      test: ["CMD","curl","-fsS","http://localhost:9000/minio/health/live"]
      interval: 10s
      timeout: 5s
      retries: 12
    networks: [kbp]

  document-parser:
    # Dockerfile.aws: 올바른 파일. 기본 Dockerfile 은 존재하지 않는 경로 참조로 빌드 실패.
    # build context: 8.kb-pipeline/ 기준 ../로 한 단계 (형제 레포)
    build:
      context: ../99.projects/jiju_chaekmu/sourceCode/document-parser-backend-src
      dockerfile: Dockerfile.aws
    environment:
      GOTENBERG_URL: http://gotenberg:3000
      MINIO_ENDPOINT: minio:9000
      MINIO_ACCESS_KEY: ${MINIO_ACCESS_KEY}
      MINIO_SECRET_KEY: ${MINIO_SECRET_KEY}
      MINIO_ENABLED: "true"   # loader.py reads MINIO_ENABLED (default False) — must be true
      MINIO_BUCKET: ${MINIO_BUCKET:-document-parser}   # core/config/loader.py default='document-parser'
      MODEL_API_URL: ${MODEL_API_URL}
      MODEL_API_KEY: ${MODEL_API_KEY}
      # MAX_CONCURRENT_REQUESTS=1: gunicorn worker-count controls parallelism (not semaphore)
      MAX_CONCURRENT_REQUESTS: "1"
    depends_on:
      gotenberg: { condition: service_healthy }
      minio: { condition: service_healthy }
    ports: ["18050:8000"]
    healthcheck:
      # /health = readiness (verifies gotenberg connectivity). / = liveness only. Use readiness.
      test: ["CMD","curl","-fsS","http://localhost:8000/health"]
      interval: 10s
      timeout: 5s
      start_period: 30s
      retries: 20
    networks: [kbp]

  excel-parser:
    # build context: 8.kb-pipeline/ 기준 ../로 한 단계 (형제 레포)
    build: { context: ../7.excel-parser }
    environment:
      EXCEL_PARSER_BACKEND: auto
      KORDOC_BIN: kordoc
    ports: ["18055:18055"]   # Task 9 회귀 테스트에서 host curl 사용
    networks: [kbp]   # must be on kbp network — parse-svc + facade route to http://excel-parser:18055
    healthcheck:
      test: ["CMD","curl","-fsS","http://localhost:18055/healthz"]
      interval: 10s
      timeout: 5s
      retries: 12

  doc_guard:
    # build context: 8.kb-pipeline/ 기준 ../로 한 단계 (형제 레포)
    build: { context: ../99.projects/shinhan_trust/doc_guard }
    networks: [kbp]
    ports: ["8001:8000"]   # host:8001 → 컨테이너:8000 (검증 curl 은 localhost:8001)
    healthcheck:
      test: ["CMD","curl","-fsS","http://localhost:8000/healthz"]
      interval: 10s
      timeout: 5s
      retries: 12

  adaptive_chunk:
    # build context: 8.kb-pipeline/ 기준 ../로 한 단계 (형제 레포)
    build: { context: ../99.projects/adaptive_chunk }
    environment:
      # pydantic-settings env_prefix="ADAPTIVE_CHUNK_" — env_file 제거: 전체 .env 주입으로 불필요 시크릿 노출 방지
      # Task 4 Step 1b 에서 99.projects/adaptive_chunk/config.py 감사 후 필요한 ADAPTIVE_CHUNK_* 키를 여기에 추가
      ADAPTIVE_CHUNK_OPENROUTER_API_KEY: ${OPENROUTER_API_KEY}
      ADAPTIVE_CHUNK_OCR_BASE_URL: http://document-parser:8000   # default=localhost:18050 → unreachable inside container
    depends_on:
      document-parser: { condition: service_healthy }   # ADAPTIVE_CHUNK_OCR_BASE_URL 가 document-parser 를 가리킴
    networks: [kbp]
    healthcheck:
      test: ["CMD","curl","-fsS","http://localhost:18060/healthz"]
      interval: 10s
      timeout: 5s
      retries: 12

  parse-svc:
    build: { context: ., dockerfile: Dockerfile.parse-svc }
    environment:
      KBP_OPENAI_API_KEY: ${KBP_OPENAI_API_KEY}
      KBP_OPENAI_BASE_URL: ${KBP_OPENAI_BASE_URL}
      KBP_LLM_MODEL: ${KBP_LLM_MODEL}
      KBP_OCR_URL: http://document-parser:8000
      KBP_EXCEL_URL: http://excel-parser:18055
      MINIO_ENDPOINT: minio:9000
      MINIO_ACCESS_KEY: ${MINIO_ACCESS_KEY}
      MINIO_SECRET_KEY: ${MINIO_SECRET_KEY}
      MINIO_BUCKET: ${MINIO_BUCKET:-document-parser}
      MINIO_SECURE: "false"
    depends_on:
      excel-parser: { condition: service_healthy }
      document-parser: { condition: service_healthy }   # was service_started, now healthy
    ports: ["19001:19001"]
    healthcheck:
      test: ["CMD","curl","-fsS","http://localhost:19001/healthz"]
      interval: 10s
      timeout: 5s
      retries: 20
    networks: [kbp]

  facade:
    build: { context: ., dockerfile: Dockerfile.facade }
    environment:
      KBP_PARSE_SVC_URL: http://parse-svc:19001
      KBP_ADAPTIVE_CHUNK_URL: http://adaptive_chunk:18060
      KBP_EDGEQUAKE_URL: http://edgequake:8081
      KBP_PG_DSN: postgres://edgequake:${POSTGRES_PASSWORD:-edgequake_secret}@postgres:5432/edgequake
      KBP_OPENAI_API_KEY: ${KBP_OPENAI_API_KEY}
      KBP_OPENAI_BASE_URL: ${KBP_OPENAI_BASE_URL}
      KBP_LLM_MODEL: ${KBP_LLM_MODEL}
      # facade /ingest/submit: service/app.py reads KBP_EXCEL_URL+KBP_OCR_URL (default localhost → unreachable)
      KBP_EXCEL_URL: http://excel-parser:18055
      KBP_OCR_URL: http://document-parser:8000
    depends_on:
      parse-svc: { condition: service_healthy }
      # doc_guard removed: facade never calls doc_guard (kb-backend calls it, out of scope)
      adaptive_chunk: { condition: service_healthy }
      edgequake: { condition: service_healthy }
    ports: ["19000:19000"]
    healthcheck:
      test: ["CMD","curl","-fsS","http://localhost:19000/healthz"]
      interval: 10s
      timeout: 5s
      retries: 20
    networks: [kbp]
```

- [ ] **Step 3: 검증 — 전 스택 기동**

```bash
cd /Users/xxx/workspace/8.kb-pipeline
# edgequake submodule 초기화 확인
git submodule update --init --recursive edgequake
# .env 준비(실값 채운 뒤)
cp -n .env.example .env
# 전 스택 빌드+기동
docker compose up -d --build 2>&1 | tail -30
sleep 30
docker compose ps
```
Expected: 전 서비스 `running`/`healthy`.

빌드 순서 최적화(edgequake 가 오래 걸리므로 먼저):
```bash
docker compose build edgequake &   # 백그라운드 Rust 빌드
docker compose build facade parse-svc document-parser excel-parser adaptive_chunk doc_guard
wait
docker compose up -d
```

- [ ] **Step 4: Commit**

```bash
cd /Users/xxx/workspace/8.kb-pipeline
git add docker-compose.yml .env.example
grep -q '^\.env$' .gitignore || echo ".env" >> .gitignore
git add .gitignore
git commit -m "build(compose): unified engine stack v2 — all healthchecks, HOST/PORT fix, service-name DNS, relative build contexts"
```

---

## Task 9: 검증 — 게이트 회귀 + E2E 스모크 (compose)

**Files:** (신규 파일 없음 — 실행 검증 + 결과 기록)
- Create: `8.kb-pipeline/docs/compose-smoke.md` (결과 기록)

- [ ] **Step 1: 게이트 회귀(compose 내 excel-parser+doc_guard)**

Run:
```bash
# excel-parser gate_summary (18055 노출됨)
curl -s -m120 -F "file=@/Users/xxx/workspace/7.excel-parser/test_doc_excel/신한자산신탁_외부테이터_필요사이트 정리.xlsx" \
  http://localhost:18055/parse \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('법령리스트 ok=',d['stats']['gate_summary']['ok'])"
# doc_guard check-excel pass (ports: 8001:8000 → host:8001)
curl -s -m5 -X POST http://localhost:8001/v1/check-excel \
  -H 'Content-Type: application/json' \
  -d '{"filename":"normal.xlsx","gate_summary":{"ok":true,"sheets":[]}}'
```
Expected: `법령리스트 ok= False` + `{"result":"pass",...}`.

- [ ] **Step 2: E2E — facade ingest→search 스모크**

Run:
```bash
curl -s -m3 localhost:19000/healthz
# 소형 문서 적재(정확한 multipart 필드는 service/app.py /ingest 계약 참조)
```
Expected: facade 헬스 200 + 적재 성공(status completed, chunk_count>0).

- [ ] **Step 3: 워커 수 확인**

Run:
```bash
docker compose exec -T parse-svc sh -c "ps -ef | grep -c '[g]unicorn'"   # 4+마스터
docker compose exec -T adaptive_chunk sh -c "ps -ef | grep -c '[g]unicorn'"  # 1+마스터
docker compose exec -T excel-parser sh -c "ps -ef | grep -c '[g]unicorn'"    # 1+마스터
```
Expected: parse-svc=5(마스터+4워커), adaptive_chunk=2, excel-parser=2.

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

§2 기동순서를 `docker compose up -d` 1순위 + run-*.sh 는 보조(단일 서비스 재기동)로 갱신. BACKEND_ORIGIN/frontend 는 compose facade(:19000) 가리키게 명시. submodule 초기화 전제조건 추가.

- [ ] **Step 2: restart-kbp-stack 스킬에 compose 경로 추가**

`docker compose up -d [--build <svc>]` 를 1순위로, 개별 run-*.sh 는 fallback 으로 문서화.

- [ ] **Step 3: Commit**

```bash
cd /Users/xxx/workspace/8.kb-pipeline
git add docs/HANDOVER-kb-pipeline-provider.md .claude/skills/restart-kbp-stack/SKILL.md
git commit -m "docs: compose as primary startup (handover + restart skill)"
```

---

## Self-Review (v2 — verified issues applied)

**v5→v6 변경 요약:**
- **CRITICAL-1** Task 6 Step 2 플레이스홀더 `<app_module>:app` → `main:app` 명시
- **CRITICAL-2** Task 6 Step 2에 gunicorn RUN 라인 추가 위치 명시 + ENTRYPOINT 유지 지시
- **IMPORTANT-1** document-parser compose에 `MINIO_BUCKET: ${MINIO_BUCKET:-document-parser}` 추가
- **IMPORTANT-2** Task 0 Step 1에 .venv-kb 부재 시 fallback 처리 추가

**v4→v5 변경 요약:**
- **IMPORTANT-1** Task 8 compose 주석 경로 수정: `service/config.py` → `config.py`
- **IMPORTANT-2** Task 4 Step 1b 신설: ADAPTIVE_CHUNK_* env var 감사 단계 (config.py grep, 필드 목록 확인 후 Task 8 반영 지시)

**v3→v4 변경 요약:**
- **IMPORTANT-1** Task 6 Interfaces + Step 3 curl: `/` → `/health` (readiness)
- **IMPORTANT-2** excel-parser compose에 `ports: ["18055:18055"]` 추가 (Task 9 host curl)
- **IMPORTANT-3** adaptive_chunk `env_file: [.env]` 제거 (전체 시크릿 노출 방지) + explicit env만 사용 + `depends_on: document-parser: service_healthy` 추가
- **MINOR-1** gotenberg healthcheck 추가 + document-parser `depends_on gotenberg: service_healthy`

**v2→v3 변경 요약:**
- **새 CRITICAL-1** 상대경로 수정: `../../` → `../` (8.kb-pipeline/ 기준 한 단계)
- **새 CRITICAL-2** adaptive_chunk env prefix 수정: `ADAPTIVE_CHUNK_OPENROUTER_API_KEY`, `ADAPTIVE_CHUNK_OCR_BASE_URL` 추가
- **새 CRITICAL-3** cargo build 에 `--locked` 추가 (재현성)
- **새 IMPORTANT-1** document-parser healthcheck: `/` (liveness) → `/health` (readiness, gotenberg 연결 검증)
- **새 IMPORTANT-2** minio healthcheck + document-parser depends_on minio: service_healthy 추가
- **새 IMPORTANT-3** doc_guard ports: 8001:8000 추가 + Task 9 curl URL localhost:8001 수정
- .env.example 에 ADAPTIVE_CHUNK_OPENROUTER_API_KEY 추가

**v1→v2 변경 요약:**
- **C1** edgequake env 명 수정: `EDGEQUAKE_HOST/PORT` → `HOST/PORT` (compose + Dockerfile)
- **C2** 유효하지 않은 Dockerfile COPY shell 문법 제거 (migrations 불필요)
- **C3** git submodule init 단계 추가 (Task 7 Step 0)
- **C4** parse-svc `COPY service ./service` 추가 (lazy import 보호)
- **C5** facade Dockerfile 에 JRE17 추가 (/ingest/submit → opendataloader_pdf → java)
- **C6/C7** Task 0 신설: requirements.txt 감사 + excel-parser requirements.txt 생성
- **C8** adaptive_chunk + excel-parser `-w 1` 고정 (in-process job store 불변식)
- **C9** doc_guard Dockerfile 에 curl 설치 (compose healthcheck 작동 필수)
- **C10** facade compose 에 `KBP_EXCEL_URL` + `KBP_OCR_URL` 추가
- **C11** excel-parser compose 에 `networks: [kbp]` 명시
- **C12** document-parser 빌드에 `dockerfile: Dockerfile.aws` 명시
- **C13** edgequake compose 에 `EDGEQUAKE_LLM_MODEL` 추가 (COMPAT-GUARD 버그 방지)
- **I1** facade depends_on 에서 `doc_guard` 제거 (facade 는 doc_guard 를 호출하지 않음)
- **I2** parse-svc compose 에 `KBP_OPENAI_BASE_URL`, `KBP_LLM_MODEL`, `MINIO_BUCKET`, `MINIO_SECURE` 추가
- **I3** facade compose 에 `KBP_OPENAI_BASE_URL`, `KBP_LLM_MODEL` 추가
- **I4** document-parser `MAX_CONCURRENT_REQUESTS: "1"` (semaphore × worker-count 누적 방지)
- **I5** document-parser healthcheck + ports 추가, parse-svc depends_on → `service_healthy`
- **I6** document-parser `MINIO_ENABLED: "true"` 추가
- **I7** 절대경로 → 상대경로 (`../../7.excel-parser` 등)
- **I8/I9/I10** 주석 정정, doc_guard 패치 명세 완전화, edgequake `PDFIUM_AUTO_CACHE_DIR`

**Spec coverage:** §2 서비스→Task0-8, §3 gunicorn→각 Task CMD+워커수(job store 제약 반영), §4 네트워킹→Task8 env, §5 순서→Task8 depends_on/healthcheck, §6 edgequake→Task7 + pg named volume, §7 마이그레이션→Task10, §9 검증→Task9. 전부 매핑.
