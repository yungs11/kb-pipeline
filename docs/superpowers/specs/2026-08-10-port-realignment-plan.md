<!-- plan-version: v3 -->
<!-- ultracode-validation: SUPERSEDED — P1/P2 로 분할 (2026-08-10) -->

> ## ⛔ 이 문서로 구현하지 말 것 — P1/P2 로 분할됨
>
> v1→v2→v3 세 라운드에서 blocking 이 **15 → 10 → 14 로 수렴하지 않았다.** 지적이 전부
> 문서·스킬 산문 스윕에 몰렸고, 스윕마다 **호스트/컨테이너 내부/dedicated 런처** 포트
> 오분류라는 새 위험이 생겼다. 사용자 결정으로 둘로 쪼갰다.
>
> | | 내용 | 문서 |
> |---|---|---|
> | **P1** | 포트 숫자가 **실행되는** 곳(compose·스크립트·코드 기본값·스킬 절차·방화벽 목록) | `2026-08-10-port-realignment-P1-wiring-plan.md` |
> | P2 | 포트 숫자가 **읽히는** 곳(설명·예시·런북 산문) | 미작성 |
>
> ⚠️ **이 문서 §2.11 의 `kb config.py:101 edgequake_base_url 8080→3001` 은 위험한 오류다.**
> 그 8080 은 kbp edgequake 가 아니라 별도 벤치마크 스택(rag-edgequake-benchmark)이며,
> 재지정하면 provider=edgequake 코호트가 **kbp 라이브 데이터를 조용히 오염**시킨다.
> P1 §1 에서 철회했다.
>
> 아래 본문의 실측 사실(s1~s25)은 **P2 의 소재로 보존**한다.

# 포트 재정리 — 사람이 접속하는 포트를 3000번대로 통일

## 0. 목적 · 확정된 결정 · 비범위

**목적**: 사람이 브라우저로 접속하는 포트를 3000번대로 통일하고 **dev 와 폐쇄망이 같은
번호**를 쓰게 한다.

| 서비스 | dev 현행 | 폐쇄망 published | 목표 (dev=폐쇄망) |
|---|---|---|---|
| facade | 19000 | `3000:19000` | **3000** |
| edgequake API | 8081 | `3001:8081` | **3001** |
| edgequake_webui | 13000(override) | `3002:3000` | **3002** |
| minio 콘솔 | 9001(override) | `3003:9001` | **3003** |
| **parse-svc** | `19001:19001` | `18081:19001` | **19001 (내부=외부)** |
| kb 프론트 | 4000(런처)/3000(직접) | `${FRONTEND_PORT:-3100}` | **18080** |

### 0.1 사용자 확정 결정

`D-a` **dev edgequake 정본 = compose 서비스.** 볼륨(`kbp_eq_pg_data`)이 있어 데이터가 남는다
(오늘 이 경로로 리포트 650건·워크스페이스 24개 보존). `start_dedicated_edgequake.sh` 는
**보조**로 남기고 "매 기동 시 postgres 재생성 = 전부 소거" 경고를 헤더에 박는다.

`D-b` **kb 프론트 포트 진실 출처 = `frontend/package.json`**(`next dev -p 18080`).
`kb-services` 스킬의 `-p 4000` 을 제거해 package.json 을 따르게 한다.

`D-c` **목표 배치는 tracked `docker-compose.yml` 에 직접 적는다.**
`docker-compose.override.yml` 은 gitignored(`.gitignore:35`)라 거기에만 두면 정답 포트가
추적되지 않고 base 단독 기동·신규 클론·CI 가 틀린 포트로 뜬다.

`D-d` **kb 폐쇄망 프론트도 18080.** v1 §6 질문은 **틀린 전제**였다 —
`.env.airgap.example:96` 이 이미 `FRONTEND_PORT=18080`, `:101` 이 `CORS_ORIGINS=…:18080`,
권위 포트맵도 18080 으로 문서화·방화벽 개방한다. compose 의 `:-3100` 은 **뒤처진 폴백**이라
고객 접속 URL 은 **변하지 않고 일치하게** 된다. `docs/airgap-known-issues.md:251` 을 닫는다.

`D-e` **parse-svc 는 내부=외부 19001** (사용자 지시 2026-08-10). 폐쇄망 published
`18081:19001` → `19001:19001`. **이것이 host 18081 이중 주장을 해소한다** —
`.env.airgap.example:249-254` 가 Paddle OCR 게이트웨이를 host 18081 로 지정하는데
parse-svc 도 같은 포트를 발행해, 나중에 바인드하는 쪽이 실패하고 OCR 경로가 parse-svc 로
갔다(세션 초반 현장 오류 `18081 ALREADY PORT` 의 정체). v2 에서 deferred 였던 항목이
**범위 안으로 승격**됐다.
⚠️ 고객 안내 필요: parse-svc 직접 호출 주소와 **방화벽 개방 포트**가 바뀐다.

`D-f` **webui URL 은 `window.location` 파생으로 바꾼다**(v2 must_fix #1 해소).
근거는 §1 `s21` — `NEXT_PUBLIC_*` 는 `next build` 시 번들에 **인라인**되고 폐쇄망 프론트는
사전 빌드 이미지라, v2 가 고른 선언처(airgap compose env·`.env.airgap.example`·
`frontend/.env.example`)는 **전부 런타임 표면이라 효과가 0** 이다. Dockerfile 이 이미 같은
함정을 문서화해 뒀다(`:5-7`). 게다가 폐쇄망 브라우저는 원격 PC 라 `http://<서버IP>:3002` 가
필요한데 그건 사이트별 값이어서 빌드시각에 구울 수 없다.
→ **kb 프론트를 보고 있는 그 호스트의 :3002** 를 쓰면 dev(`localhost:3002`)와
폐쇄망(`서버IP:3002`)이 **한 코드로 둘 다 맞는다.** env 오버라이드는 이상한 토폴로지용으로 남긴다.

### 0.2 비범위

- parse-svc **컨테이너 포트**(19001) · kb-backend(8088) · adaptive_chunk(18060) ·
  doc_guard(8001) · postgres(5433) · minio **API**(9000) — 현행 유지.
- **facade 컨테이너 내부 포트 19000 불변.** 폐쇄망 `KB_PIPELINE_BASE_URL=http://facade:19000`
  도 그대로(서비스 DNS + 내부 포트).
- doc_guard 추가 노출(`POST /v1/check`) — D19 '안 함' 별건. §6 에 확인 결과만.
- `NEXT_PUBLIC_*` 런타임 주입 일반화(BACKEND_ORIGIN 등) — 이번엔 webui 한 건만 방식 결정.
- 폐쇄망 프론트 **이미지 재빌드 자체** — §7 참조(번들 작업에 종속).

## 1. 실측 사실 (2026-08-10, 전부 직접 확인)

`s1` dev 실효 포트 = `docker-compose.yml` + `override` 합성. override 가 `!override` 로 base
목록을 **교체**한다(기본 merge 는 append). minio base `["19010:9000","19011:9001"]` →
override `["9000:9000","9001:9001"]`; webui base `["3000:3000"]` → override `["13000:3000"]`.
실측 `:9000`·`:9001` LISTEN, `:19010`·`:19011` 비어 있음.

`s2` `docker-compose.override.yml` 은 **gitignored**(`.gitignore:35`).
★ **서비스가 `minio`·`edgequake_webui` 둘뿐**(`:17`,`:23`)이라 둘을 지우고 `services:` 키만
남기면 `docker compose config` 가 `services must be a mapping` 으로 실패해 **모든 compose
호출이 죽는다.** → `services:` 키까지 지워야 한다(§2.2).

`s3` dev facade/parse-svc 는 **호스트 프로세스**. compose 컨테이너를 띄우면 옛 이미지가
호스트를 가려 "옛날 파싱" 이 되고 `run-facade.sh` 가 그 컨테이너를 죽인다.

`s4` `run-facade.sh` 에 `19000` **8회**: `:2`·`:21`·`:37` 주석, `:40`·`:43` lsof,
`:47` `--port`, `:48` 로그, `:50` **health 폴링**.

`s5` ★ `run-facade.sh:49-53` 준비 판정이 `r=$(curl … /healthz); [ -n "$r" ] && exit 0` —
**본문이 비어있지 않으면 성공**. 3000 을 남이 잡으면 HTML 이 오고(실측 **7533B**) uvicorn 이
`Address already in use` 로 죽어도 **exit 0**. 19000 에서 드러나지 않던 결함을 이 작업이 활성화한다.

`s6` ★ `service/app.py:137` `os.environ.get("KBP_EDGEQUAKE_URL", "http://localhost:8081")` —
코드 기본값이 또 하나의 선언처.

`s7` ★ `service/app.py:450` `POST /search` 는 `dependencies=[Depends(require_facade_key)]` 이고
본문 키가 **`workspace_id`**(`Body(..., embed=True)`)다 — `kb_id` 가 아니다. dev `facade.env` 에
`KBP_FACADE_KEY` 가 없어 게이트는 dormant. `eq.ensure_workspace` 가 없는 workspace 를
**새로 만드는 부작용**이 있어 라이브 값을 써야 한다.

`s8` `scripts/facade.env`(gitignored) `KBP_EDGEQUAKE_URL=http://localhost:8081`.

`s9` `docker-compose.yml:380` `EDGEQUAKE_API_URL: ${EDGEQUAKE_WEBUI_API_URL:-http://localhost:8081}`
— webui **브라우저**가 API 에 닿는 URL. 폐쇄망은 이미 `:-http://localhost:3001`(`:391`).

`s10` ★ kb `frontend/…/documents/[docId]/page.tsx:1` 이 `"use client"`, `:562-563` 이
`process.env.NEXT_PUBLIC_EDGEQUAKE_WEBUI_URL || "http://localhost:13000"`. 그 키 선언처 **0건**.

`s11` ★ kb `frontend/Dockerfile` 은 `deps→build→run` 멀티스테이지로 `:29 RUN npm run build`.
`:5-7` 이 **이미 같은 함정을 문서화**한다 — "`.next` 에 굽힌다. **런타임 compose env 로는
못 바꾼다.** build 스테이지의 ENV 로 고정한다. 바꾸려면 이미지를 다시 빌드해야 한다."

`s12` ★ kb 프론트 공식 런처 `.claude/skills/kb-services/stack.sh` — `npm run dev -- -p 4000`
(`:93`,`:160`), start/stop/status 4000 기준(`:92`,`:116`,`:133`), `:14` 주석.
`npm run dev -- -p 4000` 은 인자를 **덧붙이므로** package.json 만 바꾸면
`next dev -p 18080 -p 4000` → **4000 이 이긴다.**
같은 스킬 `SKILL.md` 에도 `4000` **3회**.

`s13` ★ `.claude/skills/restart-kbp-stack/SKILL.md` — `19000` 3회, `13000` 7회, `8081` 3회,
**`4000` 6회**(kb 프론트 health curl 포함). **문서가 아니라 실행 절차**다.

`s14` ★ 권위 포트맵 `docs/architecture-ports.md` 를 kb `.env.airgap.example:95` 가
**"이 리포에도 없는 유일한 권위 출처"** 로 참조 → 크로스리포 오염.
`:29`·`:53` 다이어그램, `:82` 표, `:105-106` **방화벽 개방 목록**에 18081.

`s15` `docs/facade-api.md:499` 가 `| KBP_EDGEQUAKE_URL | http://localhost:8081 |` 로
**기본값을 표로 선언**한다(+ `facade-api.html:1280`, `kbp-docker-startup.md:217`·`:241`).

`s16` ★ kb 에 **두 번째 edgequake 소비자**: `backend/app/config.py:101`
`edgequake_base_url: str = "http://localhost:8080"`(provider=edgequake 경로).
kb `.env` 선언 없음. 방치하면 다음 사람이 8080 을 이제 없는 8081 로 "고친다".

`s17` kb `backend/app/config.py:151` `kb_pipeline_base_url = "http://localhost:19000"`,
`:34` `cors_origins = "…3100,…3100"`.

`s18` kb 프론트는 `/api/backend/*` → `BACKEND_ORIGIN`(`.env.local`=`:8088`),
`/obj/*` → `MINIO_ORIGIN || :9000` **same-origin 프록시**(`next.config.mjs`) →
CORS 실사용 없음(값이 거짓말인 것이 문제). **minio API 9000 유지 필수** — 옮기면 챗 이미지 깨짐.

`s19` kb 테스트 `test_kb_provider_accept.py:65`(`localhost:19000`) 고쳐야 함.
`:66`(`timeout==1800.0`, 코드 3600.0) 은 **기존 실패** — baseline 실패 20건에 포함(실측).
`test_minio_facade_client.py:18`·`test_docguard_check_excel.py:41` 의 `http://facade:19000` 은
**컨테이너 DNS + 내부 포트라 불변**. kbp 의 `http://eq:8081` 은 가짜 호스트명(불변).

`s20` `load-and-up.sh:180` 의 `curl localhost:19000/jobs/workers` 는 `podman exec` 로
**컨테이너 안**이라 불변. `:103`·`:159`의 `${FRONTEND_PORT:-3100}` 은 호스트 폴백이라 바꿔야 함.
`:236` 은 접속 안내 출력(parse-svc 18081).

`s21` ★ **`NEXT_PUBLIC_*` 는 `next build` 시 클라이언트 번들에 인라인된다.** s10+s11 조합으로
v2 §2.9 가 무효임이 확정 — 런타임 표면(compose env·`.env.*`)에 넣어도 효과 0.
`.dockerignore` 가 `.env.*` 를 배제하고 Next 는 `.env.example` 을 읽지 않는다.

`s22` **baseline 실측(§3 step 0 선행 완료)**: kbp `741 passed / 실패 0건`(실 PG 포함).
kb `647 passed / 20 failed` — 실패 ID 20건 저장. `test_settings_have_kb_pipeline_defaults` 는
**`:66` 때문에** 이미 빨강이므로, `:65`+`:66` 을 함께 고치면 **20 → 19** 가 되어야 한다.

`s23` 목표 포트 가용: `3001`·`3002`·`3003`·`18080` 비어 있음. `3000` 은 kb 프론트 점유중.
`9000`·`9001` LISTEN. **순서 의존성**: 프론트를 먼저 비켜야 facade 가 3000 을 잡는다.

`s24` `docker-compose.yml:383` webui base `["3000:3000"]` — facade 를 base 에서 `3000:19000` 으로
바꾸면 **base 단독 기동 시 host 3000 이중 발행**(자기모순) → D-c 로 해소.

`s25` facade 는 edgequake 에 **기동 의존이 없다**(`get_edgequake` 는 요청별 `Depends`,
`app.py:137`). `/parse`·`/chunk`·`/healthz`·`/gate/*`·`/objects/*` 는 edgequake 없이 동작한다
→ §2.5 가드를 하드 중단으로 두면 **탈출구를 없애는 것**(CLAUDE.md 규칙 3 위반).

## 2. 변경 목록

### 2.1 kbp — tracked `docker-compose.yml` (D-c)

| 줄 | 현행 | 변경 |
|---|---|---|
| `:329` facade | `["19000:19000"]` | `["3000:19000"]` |
| `:177` edgequake | `["8081:8081"]` | `["3001:8081"]` |
| `:383` webui | `["3000:3000"]` | `["3002:3000"]` ← s24 해소 |
| `:120` minio | `["19010:9000","19011:9001"]` | `["9000:9000","3003:9001"]` |
| `:380` webui env | `…:-http://localhost:8081` | `…:-http://localhost:3001` |

healthcheck(`:331` 컨테이너 내부 19000)는 **불변**.

### 2.2 kbp — `docker-compose.override.yml` (s2)

minio·webui 블록과 **`services:` 키까지 삭제**하고 주석만 남긴다. `services:` 를 남기면
`services must be a mapping` 으로 **모든 compose 호출이 죽는다.**
★ **삭제 전 원본을 백업**한다(gitignored라 git 으로 복구 불가 — §5 롤백 전제).
헤더 주석: "정답 포트는 base 에 있다. 이 파일은 남의 스택과 충돌할 때만 쓰는 임시 탈출구."

### 2.3 kbp — 호스트 런처

**`scripts/run-facade.sh`** — `19000` **8곳 전부**(s4). 추가:
- ★ health 판정을 **본문 검증**으로(s5): 응답에 `"status"` 와 `"ok"` 가 있어야 성공.
- ★ uvicorn **PID 생존 확인** 후 폴링 — 바인드 실패를 실패로 보고.
- ★ `KBP_FACADE_PORT`(기본 3000) 탈출구 — 3000 은 경합이 가장 심한 번호다.

`scripts/run-facade-worker.sh` 주석. `scripts/facade.env`(gitignored, 사람이) `KBP_EDGEQUAKE_URL` → `:3001`.

### 2.4 kbp — 코드 기본값

`service/app.py:137` 기본값 `:8081` → `:3001`(s6). 주석에 "호스트 포트다(컨테이너끼리면
`edgequake:8081`)".

### 2.5 kbp — edgequake 도달성 **경고**(하드 중단 아님, s25)

v1/v2 의 "패턴 거부" 와 "하드 중단" 모두 틀렸다 — 전자는 키 부재를 통과시키고 후자는
**정상 작업(parse/gate 전용, edgequake 재빌드 중)을 막는다.**

`run-facade.sh` 가 `KBP_EDGEQUAKE_URL`(미설정 시 코드 기본값)의 `/health` 를 찔러
비200 이면 **크게 경고하고 계속 진행**(exit 0)한다. `KBP_REQUIRE_EDGEQUAKE=1` 을 주면
그때만 중단한다. 경고문에 "compose 면 3001, dedicated 런처면 8081" 을 적는다.

### 2.6 kbp — 실행 스킬 (s13)

`.claude/skills/restart-kbp-stack/SKILL.md` — `19000`→`3000`(3), `13000`→`3002`(7),
`8081`→`3001`(3), **`4000`→`18080`(6, kb 프론트 health curl 포함)**.

### 2.7 kbp — 문서

- `docs/architecture-ports.md` — **권위 포트맵**. dev/폐쇄망 두 열 + parse-svc 19001 +
  `:105-106` **방화벽 목록**(18081 → 19001).
- `docs/kbp-docker-startup.md` — minio 19010/19011, facade 19000, webui 13000,
  `EDGEQUAKE_API_URL` 기본 8081(`:217`,`:241`), 스모크 `for p in 19000 19001 8081`.
- ★ **`KBP_EDGEQUAKE_URL` 기본값 `8081` 표기 전수**(s15): `facade-api.md:499`,
  `facade-api.html:1280`.
- `docs/airgap-deploy.md` — 서비스 표(`:55`), 호스트 포트 목록(`:158`), healthz(`:204`),
  직접 호출(`:272`) — parse-svc 18081 → 19001.
- `docs/parse-only-guide.md:82` · `scripts/airgap/deploy-both.sh:88` ·
  `scripts/ocr-test/ocr_batch_stress.py:115`(기본 HOST) — 18081 → 19001.
- `_workspace/README.md` 포트 표 · `_workspace/01-architecture.md` · `docs/runbook-v2-smoke.md` ·
  `docs/HANDOVER-kb-pipeline-provider.md` · `service/tests/test_e2e_smoke.md`.

### 2.8 kbp — 폐쇄망 (D-e 한정)

`docker-compose.airgap.yml:334` `["18081:19001"]` → `["19001:19001"]`.
`scripts/airgap/load-and-up.sh:103`(주석)·`:236`(안내 출력).
**그 외 폐쇄망 published 포트는 불변**(V10 으로 증명).

### 2.9 kb — 프론트 포트 (D-b, s12)

| 파일 | 변경 |
|---|---|
| `frontend/package.json` | `"dev": "next dev"` → `"next dev -p 18080"` |
| `.claude/skills/kb-services/stack.sh:93,160` | `npm run dev -- -p 4000` → `npm run dev` |
| 같은 파일 `:14,:92,:116,:133` | 주석·start/stop/status 포트 `4000` → `18080` |
| `.claude/skills/kb-services/SKILL.md` | `4000` **3회 전수** → `18080`("package.json 이 정본") |

### 2.10 kb — webui URL (D-f, s21)

`frontend/…/documents/[docId]/page.tsx:562-563` 을 **`window.location` 파생**으로:
`NEXT_PUBLIC_EDGEQUAKE_WEBUI_URL` 이 있으면 그것, 없으면
`${location.protocol}//${location.hostname}:3002`. `"use client"` 이므로 브라우저에서 평가된다.
→ dev `localhost:3002`, 폐쇄망 `서버IP:3002` 가 **한 코드로 맞는다.**
주석에 "왜 env 폴백을 하드코딩하지 않는가(번들 인라인 + 사이트별 서버IP)" 를 남긴다.

**런타임 표면(airgap compose env·`.env.airgap.example`·`frontend/.env.example`)에는 넣지 않는다** —
효과가 0 이고 거짓 안심을 준다(v2 의 오류).

⚠️ **폐쇄망은 프론트 이미지를 다시 빌드해야 반영된다**(s11). §7 에 open 으로 기록.

### 2.11 kb — facade/edgequake 주소 · CORS · 폐쇄망 프론트 포트

| 파일 | 변경 |
|---|---|
| `backend/app/config.py:151` | `kb_pipeline_base_url` `:19000` → `:3000` |
| `backend/app/config.py:101` | ★ `edgequake_base_url` `:8080` → `:3001`(s16) + 주석 |
| `backend/app/config.py:34` | `cors_origins` `3100`×2 → `18080` |
| `docker-compose.airgap.yml:81` | `CORS_ORIGINS:-…3100` → `…18080` |
| `docker-compose.airgap.yml:137` | `${FRONTEND_PORT:-3100}` → `:-18080` |
| `scripts/airgap/load-and-up.sh:103,159` | `${FRONTEND_PORT:-3100}` → `:-18080` |
| `backend/tests/test_kb_provider_accept.py:65` | `19000` → `3000` |
| 같은 파일 `:66` | `1800.0` → `3600.0` — **기존 실패 정리**(근거: 코드 기본값이 권위) |
| `.env.airgap.example:93` · `docs/airgap-deploy-kb.md:47` · `scripts/airgap/load-and-up.sh:99` | "kbp 가 …18081… 을 이미 쓴다" → 19001 |
| `docs/airgap-known-issues.md:251` | 닫힘(18080 통일) |
| 같은 파일 `:149-152` | ★ 낡은 kbp 포트 목록(facade 19000·webui 13000) 갱신 |
| 같은 파일 `:252` | ★ `CORS_ORIGINS 3100` 거짓 항목 **닫힘 표시**(이 작업이 고친다) |
| `docs/airgap-deploy-kb.md:44` | "compose 기본값은 3100" 정정 |
| `_workspace/96_frontend_docs_theme.md:6` | ★ dev 프론트 `:3100` 주장(다섯 번째 포트 주장) → 18080 |

kb 폐쇄망 `KB_PIPELINE_BASE_URL=http://facade:19000` 은 **불변**.

### 2.12 kbp — dedicated 런처 경고 (D-a)

`service/scripts/start_dedicated_edgequake.sh` 헤더에: **보조 경로**이며 볼륨 없는
`docker run` 이라 **매 기동 시 리포트·문서·그래프 전부 소거**. 정본은 compose. 이 런처로
띄우면 edgequake 는 8081 이므로 `KBP_EDGEQUAKE_URL=http://localhost:8081` 을 주라고 안내.

## 3. 실행 순서

0. ~~baseline 수집~~ **완료**(s22) — kbp 741/0, kb 647/20(ID 저장).
1. kb 프론트 종료 → `:3000` 해제(s23)
2. ★ `docker-compose.override.yml` **백업** 후 §2.2 대로 삭제
3. kbp: compose · 런처 · `service/app.py` · 스킬 · 문서 · airgap parse-svc
4. kb: 프론트 포트/webui 파생 · `config.py` ×3 · airgap 기본값 · 스킬 · 테스트 · 문서
5. `scripts/facade.env` 의 `KBP_EDGEQUAKE_URL` → 3001 (사람이 직접)
6. ★ **서비스를 명시해** 재생성: `docker compose up -d --force-recreate edgequake edgequake_webui minio`
   (무인자 `up -d` 는 facade/parse-svc 컨테이너까지 띄워 host 3000 을 뺏고 docker-shadow)
7. `run-facade.sh`(3000) → `run-facade-worker.sh`
8. kb 백엔드 재기동, 프론트 기동(18080)

## 4. 검증

- **V1** LISTEN: `3000` facade · `3001` eq · `3002` webui · `3003` minio 콘솔 · `18080` kb front ·
  `9000` minio API · `19001` parse-svc · `8088` kb api.
  **비어야 함**: `19000` `8081` `13000` `9001` `4000` `19010` `19011`.
- **V2** `curl -s localhost:3000/healthz` 본문이 `{"status":"ok"}` — **HTML 이면 실패**(s5 오탐을 잡는다).
- **V3** `curl -s localhost:3000/jobs/workers` → `"online":true`.
- **V4** ★ facade→edgequake 실왕복(s7 반영):
  `curl -s -X POST localhost:3000/search -H 'content-type: application/json'
   -d '{"workspace_id":"<라이브 eq ws UUID>","query":"전체 내용","top_k":3}'` → 200.
  **`workspace_id`** 다(`kb_id` 아님). 라이브 목록에서 고른 값을 써야 한다
  (`ensure_workspace` 가 없는 값을 새로 만든다). dev 는 `KBP_FACADE_KEY` 미설정이라 헤더 불필요.
  **포트만 열린 것으로는 알 수 없는 것을 이 항목만이 증명한다.**
- **V5** ★ kb→facade 실증(s-검증 반영): `/api` 접두어는 없고(`prefix=/docguard`) JWT 필수라
  헤더 없는 curl 은 401 이다. 그래서 **두 증거로 대체**한다 —
  (a) `python -c "from app.config import Settings; print(Settings().kb_pipeline_base_url)"` 가 `:3000`,
  (b) kb 백엔드 로그에 outbound `http://localhost:3000/gate/rules` 가 찍히는지(로그인 후 문서 목록 1회).
- **V6** ★ webui: `curl -s localhost:3002/` 200 **그리고** dev 브라우저에서 문서상세
  "그래프 보기" 가 `localhost:3002` 를 열는지. **폐쇄망은 이미지 재빌드 후에만 반영**되므로
  이 항목으로 폐쇄망을 통과 처리하지 않는다(§7 open).
- **V7** minio 콘솔 `3003` 200 **그리고** `/obj/` 프록시 동작(9000 유지 확인).
- **V8** ★ 가드 실증(§2.5): (a) 죽은 주소 → **경고 + 기동 성공**, (b) 미설정 + eq down → 경고 +
  성공, (c) `KBP_REQUIRE_EDGEQUAKE=1` + 죽은 주소 → **중단**, (d) 8081(런처 정본) → 통과.
- **V9** 회귀: s22 의 실패 ID 집합과 비교 → **신규 0건**. kbp 는 `741 passed` 유지.
  kb 는 `:66` 수정으로 **20 → 19** 가 되어야 한다(줄지 않으면 근거가 틀린 것).
- **V10** 폐쇄망 불변 증명: `git diff docker-compose.airgap.yml` 이 **parse-svc(D-e) ·
  CORS_ORIGINS · FRONTEND_PORT 세 줄만** 건드림. 다른 published 포트는 무변경.
- **V11** `docker compose config` — minio `9000:9000`+`3003:9001` **두 개만**,
  facade `3000:19000`, webui `3002:3000`, 이중 발행 없음.
- **V12** ★ override 삭제 후에도 `docker compose config` **성공**(s2 의 `services must be a
  mapping` 을 안 밟았는지) + `docker compose -f docker-compose.yml config` 단독 통과.

## 5. 롤백

untracked 표면 **둘** 다 절차에 넣는다:
1. `git revert`(kbp·kb)
2. `scripts/facade.env` 의 `KBP_EDGEQUAKE_URL` 을 `:8081` 로 되돌린다
3. ★ **§3 step 2 의 백업으로 `docker-compose.override.yml` 복원**(`!override` 태그 포함 —
   없으면 append-merge 로 19010/19011 이 남아 minio 콘솔이 19011, kb/OCR 이 기대하는 `:9000`
   이 깨진다. override 가 애초에 막으려던 고장이다)
4. `docker compose up -d --force-recreate edgequake edgequake_webui minio`
5. kb 프론트 원복(`package.json` + 3000/4000 재기동), 런처 재기동 → V1·V2 확인
6. 폐쇄망은 이전 프론트 이미지를 다시 배포(§2.10 을 되돌리려면 재빌드 필요)

§2.5 가드가 **경고 기준**이라 롤백 중간 상태도 facade 기동을 막지 않는다.

## 6. doc_guard 노출 — 재확인 결과

| doc_guard | facade |
|---|---|
| `POST /v1/check-excel` | ✅ `POST /gate/check-excel` |
| `GET /v1/rules` | ✅ `GET /gate/rules` |
| `POST /v1/check` | ❌ 없음 — D19 '안 함'(2026-08-05) |

**"열려 있다" 는 절반만 맞다.**

## 7. 이 작업으로 닫히지 않는 것 (조용히 넘기지 않는다)

- ★ **폐쇄망 webui 링크** — §2.10 은 코드 수정이라 **프론트 이미지 재빌드 후에만** 반영된다.
  번들 재포장이 보류 중이므로 `docs/airgap-known-issues.md` 에 **open** 으로 기록한다:
  "현장 문서상세의 '그래프 보기' 는 프론트 이미지를 다시 빌드할 때까지 13000 을 부른다."
- **kbp `.env.example` 에 `EDGEQUAKE_WEBUI_API_URL` 미선언** — 원격 PC 로 webui 를 볼 때
  `서버IP:3001` 이 필요하다는 사실이 발견되지 않는다. 배포 차단 아님 → 후속.
- **`NEXT_PUBLIC_*` 런타임 주입 일반화** — 이번엔 webui 한 건만 방식 결정(§0.2).
