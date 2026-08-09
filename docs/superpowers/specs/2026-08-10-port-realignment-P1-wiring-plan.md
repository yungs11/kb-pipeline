<!-- plan-version: v3 -->
<!-- ultracode-validation: READY v3 at 2026-08-10T04:35:00Z — 문서 7건 반영, 런타임 5건은 §8 구현 후 검증 이관(글로벌 룰 비용관리 5번) -->

# [P1] 포트 재정리 — **실행 배선만**

> 원안 `2026-08-10-port-realignment-plan.md`(v3)는 3라운드 검증에서 **수렴하지 않았다**
> (15 → 10 → 14). 지적이 전부 문서·스킬 산문 스윕에 몰렸고, 스윕마다 호스트/컨테이너/런처
> 포트 오분류라는 새 위험이 생겼다. 그래서 **사용자 결정(2026-08-10)으로 둘로 쪼갰다.**
>
> - **P1(이 문서)** — 포트 숫자가 **실행되는** 곳. 돌아가게 만들고 실측한다.
> - **P2(별도)** — 포트 숫자가 **읽히는** 곳(설명·예시·런북 산문). 판별 규칙을 세운 뒤 일괄.
>
> 원안 v3 는 **SUPERSEDED**. 거기 모아둔 실측 사실(s1~s25)은 P2 의 소재로 보존한다.

## 0. 경계 규칙 (이 문서의 범위를 정하는 유일한 기준)

**P1 = 그 숫자가 실행되는 것**: compose `ports:`/env 기본값, 셸 스크립트가 바인드·curl 하는
포트, 파이썬/TS 코드의 기본값, **에이전트가 그대로 실행하는 스킬 절차**, **방화벽 개방 목록**.

**P2 = 그 숫자가 읽히는 것**: 아키텍처 설명, API 문서의 예시 URL, 런북 산문, 변경이력.

방화벽 목록을 P1 에 넣는 이유: 그건 산문이 아니라 **배포 시 실행되는 명령**이다
(`docs/architecture-ports.md:105-106` 의 `firewall-cmd --add-port`). 틀리면 폐쇄망에서 포트가
안 열려 접속이 안 된다.

## 0.1 확정된 결정

| | 결정 |
|---|---|
| `D-a` | dev edgequake 정본 = **compose**. dedicated 런처는 보조 + "매 기동 소거" 경고 |
| `D-b` | kb 프론트 포트 진실 출처 = **`frontend/package.json`**. `stack.sh` 의 `-p 4000` 제거 |
| `D-c` | 목표 배치는 **tracked `docker-compose.yml`** 에. override 는 충돌 회피용으로 축소 |
| `D-d` | kb 폐쇄망 프론트도 **18080**(이미 배포 템플릿·권위 포트맵의 값 — 고객 URL 불변) |
| `D-e` | **parse-svc 내부=외부 19001**. host 18081 이중주장(OCR 게이트웨이) 해소 |
| `D-f` | webui URL 은 **`window.location` 파생**(`NEXT_PUBLIC_*` 는 빌드시각 인라인이라 런타임 env 무효) |
| `D-g` | **벤치마크 스택 프론트를 3000 밖으로**(3010) 옮긴다. ⚠️ '단독 소유'는 아니다 — §0.2 참조 |

## 0.2 목표 배치

| 서비스 | 현행(dev) | 목표 | 비고 |
|---|---|---|---|
| facade | 19000 | **3000** | 컨테이너 내부 19000 불변 |
| edgequake API | 8081 | **3001** | 컨테이너 내부 8081 불변 |
| edgequake_webui | 13000 | **3002** | |
| minio 콘솔 | 9001 | **3003** | **API 9000 유지**(옮기면 챗 이미지 깨짐) |
| parse-svc | 19001 | **19001** | 폐쇄망 published 18081 → 19001 |
| kb 프론트 | 4000/3000 | **18080** | |
| 벤치마크 프론트 | 3000 | **3010** | D-g. `edgequake.quickstart.yml` |
| 벤치마크 API | 8080 | **8080 불변** | §1 — 별도 엔진 |

★ **"facade 가 3000 을 단독 소유" 는 성립하지 않는다.** vendored 서브모듈 compose 3건이
host 3000 을 발행한다 — `edgequake/docker-compose.quickstart.yml:139`,
`edgequake/edgequake/docker/docker-compose.yml:112`,
`edgequake/edgequake/docker/docker-compose.prebuilt.yml:83`(전부 `${FRONTEND_PORT:-3000}:3000`).
**의도적 제외**한다: (a) 이 프로젝트의 어떤 워크플로도 그 compose 를 띄우지 않는다(kbp 는
자기 `docker-compose.yml` 을 쓴다), (b) 서브모듈을 편집하면 롤백 경로가 서브모듈 커밋까지
번진다(`git status` 가 이미 `m edgequake`). 누군가 그걸 띄우면 충돌하고, 그때 답은
`KBP_FACADE_PORT`(§2.3)다 — 그래서 그 탈출구가 **선택이 아니라 필수**다.

## 1. ★ 철회 — v3 에 넣었던 위험한 변경

**`kb backend/app/config.py:101 edgequake_base_url: "http://localhost:8080"` 은 바꾸지 않는다.**

v3 는 이걸 "두 번째 edgequake 소비자" 로 보고 3001 로 바꾸려 했다. **틀렸다.** 실측:

- `rag-edgequake-benchmark/docker/edgequake.quickstart.yml:54` `${EDGEQUAKE_PORT:-8080}:8080`,
  `:13-16` Web UI 3000 / API 8080 / Swagger / Health
- kb `.claude/skills/kb-services/SKILL.md:52` "`stack.sh use edgequake` — edgequake(:8080)+shim(:18071)",
  `:61` "자체 pgvector+AGE, dify 불요"
- kb `.env:40` "edgequake provider 도 KURE 임베딩으로 통일 … base_url 은 config 기본 :8080"

즉 **별도 벤치마크 스택**(자체 pgvector+AGE+KURE)이고 `provider=edgequake` 비교 코호트다.
3001 로 재지정하면 그 코호트의 적재/검색이 **kbp 라이브 엔진으로 조용히 흘러** 리포트
650건·워크스페이스 24개에 낯선 워크스페이스·그래프를 만든다. **에러가 없어 어떤 검증도
못 잡는다.**

→ 값은 그대로 두고 **주석만** 붙인다: "이 8080 은 벤치마크 스택(rag-edgequake-benchmark)의
edgequake-api 다. kbp 전용 edgequake 는 `KBP_EDGEQUAKE_URL`(호스트 3001)이며 서로 다른 엔진·
다른 DB 다. 폐쇄망에서는 배선이 없으면 kb-api 자기 자신(8080)을 부르므로 compose 가
`http://edgequake:8081` 을 준다." (그 경고를 담은 kb compose `:67`·`:107`,
`.env.airgap.example:57` 은 **값을 안 바꾸므로 여전히 참** — 손대지 않는다.)

**교훈 기록**: v3 는 2라운드 검증관의 "선언처 누락" 지적을 **줄의 존재만 확인하고 의미를
확인하지 않고** 반영했다. 대리인 보고는 위치까지만 신뢰하고 **의미는 직접 확인**한다.

## 2. 변경 목록

### 2.1 kbp — tracked `docker-compose.yml`

| 줄 | 현행 | 변경 |
|---|---|---|
| `:329` facade | `["19000:19000"]` | `["3000:19000"]` |
| `:177` edgequake | `["8081:8081"]` | `["3001:8081"]` |
| `:383` webui | `["3000:3000"]` | `["3002:3000"]` |
| `:120` minio | `["19010:9000","19011:9001"]` | `["9000:9000","3003:9001"]` |
| `:380` webui env | `…:-http://localhost:8081` | `…:-http://localhost:3001` |

healthcheck(`:331`, 컨테이너 내부 19000) **불변**.

★ `:383` 인라인 주석 — **v2 의 지시는 틀렸다.** v2 는 이걸 "충돌 시 `KBP_FACADE_PORT`/
`FRONTEND_PORT` 로 회피" 로 바꾸라 했으나 **둘 다 이 경로에서 작동하지 않는다**: compose
facade 는 `19000:19000`(`:329`)로 3000 을 **발행조차 하지 않고**, `FRONTEND_PORT` 는 kbp
compose/`.env.example` 에 **존재하지 않는다**(실측 0건). `KBP_FACADE_PORT` 는 **호스트 런처
전용**이다.
→ 주석은 "이 호스트 포트가 충돌하면 **이 줄을 직접 바꾸거나** `docker-compose.override.yml`
로 리맵한다(§2.2 로 비워뒀지만 파일은 남아 있다)" 로 정정한다.

★ **불변 라인 목록**(파일 단위로 훑을 때 함께 바뀌면 조용히 깨지는 것):
`:385` webui healthcheck `http://localhost:3000`(컨테이너 내부),
`:179` edgequake healthcheck `localhost:8081`(내부),
`:115` minio `--console-address ":9001"`(내부),
`:331` facade healthcheck `localhost:19000`(내부).
`:385` 를 3002 로 바꾸면 webui 가 **영구 unhealthy** 가 되는데 V6(`curl :3002/` 200)·
V10(published 매핑만)은 **발화하지 않는다** → V6 에 `docker compose ps` healthy 확인을 넣는다.

### 2.2 kbp — `docker-compose.override.yml`

★ 서비스가 `minio`·`edgequake_webui` **둘뿐**(`:17`,`:23`)이므로 둘과 **`services:` 키까지**
지우고 주석만 남긴다. `services:` 를 남기면 `docker compose config` 가
`services must be a mapping` 으로 실패해 **모든 compose 호출이 죽는다.**
★ **삭제 전 원본을 백업**한다(gitignored — git 으로 복구 불가, §5 전제).

### 2.3 kbp — `scripts/run-facade.sh`

- `19000` **8곳 전부**: `:2`·`:21`·`:37` 주석, `:40`·`:43` lsof, `:47` `--port`, `:48` 로그, `:50` health.
- ★ health 판정을 **본문 검증**으로: 지금은 `[ -n "$r" ]` 라 3000 을 남이 잡아 HTML(실측
  **7533B**)이 와도 성공으로 본다 → 응답에 `"status"` 와 `"ok"` 가 있어야 성공.
- ★ uvicorn **PID 생존 확인** 후 폴링 — 바인드 실패를 실패로 보고.
- ★ `KBP_FACADE_PORT`(기본 3000) 탈출구. vendored compose 3건이 여전히 3000 을 발행하므로
  (§0.2) **필수**다.
- ★ **짝 env 를 함께 배선한다**: kb 는 `kb_pipeline_base_url` 기본값 하나로 kb_pipeline
  클라이언트와 `DocGuardClient` 를 **둘 다** 만들고(`dependencies.py:52`) kb `.env` 에
  `KB_PIPELINE_BASE_URL` 선언이 없다. 그래서 `KBP_FACADE_PORT` 로 facade 를 옮기면
  **kb→facade 적재·게이트가 끊긴다.** kb `.env.example` 에 `KB_PIPELINE_BASE_URL` 을 선언하고
  런처 안내에 "facade 포트를 바꾸면 이 값도 바꿔라" 를 넣는다. 검증은 V16.
- ★ edgequake 도달성 **경고**(§2.4).

`scripts/run-facade-worker.sh` — 헤더 주석 `facade(:19000)` → `(:3000)`.
`scripts/facade.env`(gitignored, 사람이 직접) — `KBP_EDGEQUAKE_URL` → `http://localhost:3001`.

### 2.4 kbp — edgequake 도달성 경고 (하드 중단 금지)

facade 는 edgequake 에 **기동 의존이 없다**(`get_edgequake` 는 요청별 `Depends`,
`app.py:137`) — `/parse`·`/chunk`·`/healthz`·`/gate/*`·`/objects/*` 는 edgequake 없이 동작한다.
그래서 v3 의 "비200 이면 중단" 은 **정상 작업(parse/gate 전용, eq 재빌드 중)을 막는다**
(CLAUDE.md '탈출구 유지' 위반).

→ `/health` 비200 이면 **크게 경고하고 계속 진행**(exit 0). `KBP_REQUIRE_EDGEQUAKE=1` 일 때만
중단. 경고문: "compose 면 3001, dedicated 런처면 8081(`KBP_EDGEQUAKE_URL` 로 지정)".

★ **신설 env 이므로 선언처를 함께 갱신한다**(CLAUDE.md 규칙 1 — 실측 현재 0건):
`scripts/run-facade.sh` 헤더 usage, `.env.example`, `.env.airgap.example` 에 주석 선언.
폐쇄망 운영자가 이 하드스톱 스위치의 존재를 알 방법이 없으면 안 된다.
(`verify-bundle.sh` 는 이 env·포트를 검사하지 않는다 — 기본값이 안전측(경고만)이라 가드
조건이 조용히 거짓이 되는 항목은 없다.)

### 2.5 kbp — 코드 기본값

`service/app.py:137` `os.environ.get("KBP_EDGEQUAKE_URL", "http://localhost:8081")` → `:3001`.
주석: "호스트 포트다(컨테이너끼리면 `edgequake:8081`)".

### 2.6 kbp — 폐쇄망 (D-e 한정)

`docker-compose.airgap.yml:334` `["18081:19001"]` → `["19001:19001"]`.
**그 외 published 포트는 불변**(V9 로 증명).

★ **두 리포에 같은 경로 파일이 있다** — v1 은 kb 쪽 줄을 kbp 절에 적었다. 리포별로 쪼갠다.

**kbp `scripts/airgap/load-and-up.sh`** (실측: `FRONTEND_PORT`/`3100` **0건**)
- `:103` 주석의 실측 포트 목록(edgequake 8081→3001 …, parse-svc 19001→18081)
- `:236` 접속 안내 출력 `parse-svc http://<서버IP>:18081` → `:19001`
- ⚠️ `:159` 는 **`SERVICES` 배열**이다. kb 파일의 `:159`(FRONT_PORT)와 혼동해 편집하면
  폐쇄망 health 폴링이 깨진다. **건드리지 않는다.**

**kb `scripts/airgap/load-and-up.sh`** (§2.11 표에서 누락됐던 것)
- `:99` "kbp 가 3000·3001·3002·3003·**18081**·5433 을 이미 쓴다" → `19001`
- `:103` `FRONT_PORT_CHECK="${FRONTEND_PORT:-3100}"`(선점 시 die) → `:-18080`
- `:159` `FRONT_PORT="${FRONTEND_PORT:-3100}"`(health URL) → `:-18080`

**kbp `scripts/airgap/deploy-both.sh`** — ★ v1 이 P2 로 미뤘으나 **배포 최상위 진입점**이라
§0 기준 P1 이다. `:85` `FRONT_PORT="${FRONT_PORT:-3100}"` → `:-18080`,
`:88` `parse-svc: curl … :18081/healthz` → `:19001`. 안 고치면 운영자에게 3100 을 열라 하고
**OCR 게이트웨이(18081)로 parse-svc 헬스체크**를 지시해 "떠 있는데 실패/죽었는데 성공" 오판.

`docs/architecture-ports.md` — **표(`:82`)와 방화벽 목록(`:105-106`)만**(P1 경계: 실행되는 것).
`:29`·`:53` 다이어그램은 P2.

★ **방화벽은 치환이 아니라 추가다.** `:105-106` 의 `for p in 3000 3001 3002 3003 18081 18080` 에서
18081 을 19001 로 **바꾸면 폐쇄망 OCR 이 죽는다** — `.env.airgap.example:254`
`KBP_PADDLE_OCR_GATEWAY_URL=http://host.containers.internal:18081/ocr/paddleocr_vl` 이 그 포트를
계속 쓴다. **최종 목록**: `3000 3001 3002 3003 18081 19001 18080`
(18081=OCR 게이트웨이, 19001=parse-svc).
★ 같은 편집에서 **표(`:82`)에 18081 행을 추가**한다 — "18081 | (없음) | **외부 OCR Gateway
(kbp 미소유)** | PaddleOCR-VL 프론트 | 열기". 권위 포트맵이 설명하지 않는 포트를 열게 두면
다음 스윕이 그걸 "정리" 해 폐쇄망 OCR 을 죽인다(이유가 `.env.airgap.example:254` 에만 남는다).

★ `§6 "포트 변경은 compose 호스트 매핑만 → 이미지 재빌드 불필요"` 는 **거짓**이 되므로
(§2.5 코드 기본값 + D-f 프론트 번들) 그 단언만 정정한다.

### 2.7 kbp — 실행 스킬

`.claude/skills/restart-kbp-stack/SKILL.md`:
- `19000`→`3000`(3곳), `13000`→`3002`(7곳), `8081`→`3001`(3곳)
- ★ `4000`→`18080` — **4곳만**(`:3`, `:10`, `:33` health curl, `:105`).
  `4000` 문자열 6건 중 **2건은 `:14000` 의 부분문자열**(`:22`, `:94`)이다. 그걸 치환하면
  `:118080` 이 되어 "14000 은 사용하지 않는다" 는 오해 방지 문장까지 망친다.
  **치환은 `\b4000\b` 경계로**, 각 줄을 눈으로 확인한다.
- ★ `:21` 은 "**compose override 에 의해** :13000" 이라는 **이유절**을 함께 주장한다. §2.2 가
  그 override 를 삭제하므로 숫자만 바꾸면 다음 세션이 override 를 재생성해 §2.2·§5 전제가
  무너진다. → "base `docker-compose.yml` 이 3002 로 발행" 으로 **문구를 재작성**한다.

### 2.8 kbp — dedicated 런처 경고 (D-a)

★ **v1 의 경고 문안은 틀렸다** — 실측: `:8-9` 가 `docker rm -f eq-pg-kbp` + 볼륨 없는
`docker run` 으로 **자기 컨테이너만** 재생성한다. compose 의 명명 볼륨
`eq_pg_data`(`docker-compose.yml:97`)는 **건드리지 못한다.** "라이브 650건을 소거한다" 는
과장이었고, 그런 거짓 경고를 헤더에 박으면 다음 사람이 이 런처를 "데이터 파괴 도구" 로
봉인해 D-a 의 탈출구가 사라진다.

**실제 고장 모드 둘만 적는다**:
(i) compose postgres 가 5433 을 점유 중이면 **바인드 충돌**로 런처가 실패한다.
(ii) compose postgres 를 내린 뒤 이 런처를 쓰면 **빈 PG 가 라이브 DB(볼륨)를 가려** facade 가
     빈 것을 본다 — 데이터는 볼륨에 남아 있지만 그 세션에서는 보이지 않는다.
그리고 이 런처의 **자기 PG 데이터는** 볼륨이 없어 매 기동 소거된다.

정본은 compose(`docker compose up -d postgres edgequake`). 이 런처를 쓰면 edgequake 는
8081 이므로 `KBP_EDGEQUAKE_URL=http://localhost:8081` 을 줄 것.

### 2.9 kb — 프론트 포트 (D-b)

| 파일 | 변경 |
|---|---|
| `frontend/package.json` | `"dev": "next dev"` → `"next dev -p 18080"` |
| `.claude/skills/kb-services/stack.sh:93,160` | `npm run dev -- -p 4000` → `npm run dev` |
| 같은 파일 `:92`, ★`:159`, `:116`, `:133` | `start_one`/`stop_one`/`status_one` 포트 `4000` → `18080` |
| 같은 파일 `:14`, ★`:39` | 주석 — `:14` 는 포트 선언, `:39` 는 "facade :19000/parse 500" 단정 |

★ 주석 경계 규칙: §0 은 산문을 P2 라 했지만 **같은 실행 스크립트 안에서 포트를 선언하거나
동작을 단정하는 주석은 P1** 이다(그걸 읽고 다음 사람이 포트를 되돌린다). 한 파일 안에서
규칙이 갈리지 않게 이 문장을 근거로 삼는다.
| `.claude/skills/kb-services/SKILL.md` | `4000` **발생 5건**(`:33`×2, `:64`, `:76`×2) — ★ 숫자 치환이 아니라 **문구 재작성** |

★ SKILL.md 처방이 숫자 치환이면 **방금 없앤 호출형을 계속 주장**한다 —
`:76` "frontend 는 :4000 (next dev 기본 :3000 아님 → `-p 4000`)", `:33` 실행열
`npm run dev -- -p 4000`. D-b 가 `-p` 를 package.json 으로 옮기고 stack.sh 에서 삭제하므로
`npm run dev` + "포트는 package.json 소유(18080)" 로 다시 쓴다.

★ `:159` 는 `ensure_baseline`(`stack.sh use <provider>`) 경로의 `start_one frontend 4000` 이다.
`:160` 만 고치면 프론트는 18080 에 뜨는데 **헬스 대기는 4000** 을 봐서 코호트 기동이 실패로
오판되거나 무한 대기한다. 카운트는 **줄 수가 아니라 발생 수** 기준으로 센다.

### 2.10 kb — webui URL (D-f)

`frontend/app/kb/[kbId]/documents/[docId]/page.tsx:562-563` 을 **`window.location` 파생**으로:
env 가 있으면 그것, 없으면 `${location.protocol}//${location.hostname}:3002`.
`"use client"` 라 브라우저에서 평가된다 → dev `localhost:3002`, 폐쇄망 `서버IP:3002` 가
**한 코드로 맞는다.**

**런타임 표면(airgap compose env·`.env.airgap.example`·`frontend/.env.example`)에는 넣지 않는다** —
`NEXT_PUBLIC_*` 는 `next build` 시 번들에 인라인되고 폐쇄망 프론트는 사전 빌드 이미지라
효과가 0 이고 거짓 안심을 준다(`frontend/Dockerfile:5-7` 이 같은 함정을 이미 문서화).

⚠️ **폐쇄망은 프론트 이미지 재빌드 후에만 반영**된다 → §7 에 open 으로 기록.

### 2.11 kb — facade 주소 · CORS · 폐쇄망 프론트 포트

| 파일 | 변경 |
|---|---|
| `backend/app/config.py:151` | `kb_pipeline_base_url` `:19000` → `:3000` |
| `backend/app/config.py:34` | `cors_origins` `3100`×2 → `18080` |
| `backend/app/config.py:101` | ★ **값 불변**, 주석만(§1) |
| `docker-compose.airgap.yml:81` | `CORS_ORIGINS:-…3100` → `…18080` |
| `docker-compose.airgap.yml:137` | `${FRONTEND_PORT:-3100}` → `:-18080` |
| `backend/tests/test_kb_provider_accept.py:65` | `19000` → `3000` |
| 같은 파일 `:66` | `1800.0` → `3600.0`(기존 실패 정리 — 코드 기본값이 권위) |

kb 폐쇄망 `KB_PIPELINE_BASE_URL=http://facade:19000` **불변**(컨테이너 DNS + 내부 포트).

### 2.12 벤치마크 스택 프론트 이동 (D-g)

`rag-edgequake-benchmark/docker/edgequake.quickstart.yml` — **라인별 대상/비대상**:
- `:139` `${FRONTEND_PORT:-3000}:3000` → `:-3010` ✅ 변경(호스트)
- `:13` 헤더 주석 `Web UI → http://localhost:3000` → `:3010` ✅ 변경
- ★ `:161` healthcheck 의 `3000` → **불변**(컨테이너 내부). 일괄치환하면 프론트가 영구
  unhealthy 가 되어 §1 이 지키려던 "벤치마크 무손상" 을 스스로 깬다.
- `:54` API `${EDGEQUAKE_PORT:-8080}:8080` → **불변**(§1).

★ v2 는 "kb `SKILL.md`·`stack.sh` 의 edgequake 코호트 안내에 3010 반영" 을 적었으나
**대상 문자열이 없다** — 두 파일에는 `-p 4000` 두 줄뿐이고 코호트의 Web-UI 포트를 언급하는
문장이 애초에 없다(그 포트는 `quickstart.yml:13` 에만 있고 이미 위에서 다룬다).
→ **이 단계를 삭제한다**(구현자가 문안을 지어내게 하지 않는다).

### 2.12b kb — eval 하네스 코드 기본값

`kb _workspace/run_kb_pipeline_eval.py:62` `KBP_EVAL_EDGEQUAKE_URL` 기본값
`http://localhost:8081` → `:3001`. §2.5 와 같은 부류(파이썬 코드의 호스트 포트 기본값)라
§0 기준 **P1** 이다. 안 고치면 8081 이 비어 eval 하네스가 connection refused 로 죽는다.

### 2.13 kbp — `.env.example` / `.env.airgap.example`

dev/폐쇄망 template 에 `EDGEQUAKE_WEBUI_API_URL`(호스트 3001) 을 **선언**한다. 지금 dev 쪽에
선언이 없어(폐쇄망 `.env.airgap.example:174` 에만 있음) 원격 PC 로 webui 를 볼 때
`서버IP:3001` 이 필요하다는 사실이 발견되지 않는다. `KBP_FACADE_PORT` 도 함께 문서화.

## 3. 실행 순서

0. **baseline (재현 가능하게 기록)** — 세션 scratchpad 는 휘발이므로 명령과 결과를 여기 박는다.
   ```
   # kbp (실 PG 필요)
   docker run -d --rm --name base-pg -e POSTGRES_PASSWORD=t -e POSTGRES_DB=t -p 55450:5432 postgres:16
   KBP_PG_DSN=postgres://postgres:t@127.0.0.1:55450/t \
     .venv-kb/bin/python -m pytest service/tests/ tests/ parse_service/tests/ -q
   # → 741 passed, 실패 0건
   # kb
   cd <kb>/backend && ../.venv/bin/python -m pytest tests/ -q
   # → 647 passed, 20 failed
   ```
   **kb baseline 실패 20건**(V13 비교 기준):
   `test_chat_edgequake.py::test_edgequake_chat_uses_search_and_normalizes_sources` ·
   `test_job_status.py::{test_gate_failed_job_persists_gate_popup, test_gate_failed_status,
   test_get_job_advisory_for_warning_only_success, test_get_job_exposes_gate_popup_and_file_name,
   test_warning_only_job_persists_advisory}` ·
   `test_kb_provider_accept.py::test_settings_have_kb_pipeline_defaults` ·
   `test_main.py::{test_alembic_ini_here_template_resolves_migrations_from_any_cwd,
   test_readyz_head_revision_returns_200, test_readyz_no_alembic_version_table_returns_503,
   test_readyz_stale_revision_returns_503_with_both_revisions}` ·
   `test_pipeline.py::{test_gate_error_rejects_with_popup, test_pipeline_forwards_gate_options_to_docguard,
   test_pipeline_gate_options_default_disables_nothing, test_warning_only_passes_advisory}` ·
   `test_pipeline_raganything.py::{test_raganything_insert_failed_marks_document_failed,
   test_raganything_stage_sequence, test_raganything_staged_happy_path,
   test_raganything_swap_deletes_old_doc}` ·
   `test_pipeline_ragflow.py::test_ragflow_gate_block_still_rejected`
1. kb 프론트 종료 → `:3000` 해제.
   ★ **옛 매핑의 compose 컨테이너도 내린다**: `docker compose stop facade parse-svc`
   (안 내리면 19000 에 옛 이미지가 계속 응답해 V1 의 "19000 비어야 함" 이 구조적으로 달성
   불가이고 docker-shadow 가 재현된다).
   벤치마크 스택이 떠 있으면 내리되 **V17 에서 다시 올려 D-g 를 실제로 행사한다.**
2. ★ `docker-compose.override.yml` **백업** 후 §2.2 삭제
3. kbp: compose · 런처 · `app.py` · 스킬 · airgap parse-svc · env template
4. kb: `package.json` · `stack.sh` · SKILL.md · webui 파생 · `config.py` · airgap · 테스트
5. 벤치마크 quickstart 프론트 3010
6. `scripts/facade.env` 의 `KBP_EDGEQUAKE_URL` → 3001 (사람이 직접)
7. ★ **서비스 명시 + `--no-deps`** 재생성:
   `docker compose up -d --force-recreate --no-deps edgequake edgequake_webui minio`
   - 무인자 `up -d` 는 facade/parse-svc 컨테이너까지 띄워 host 3000 을 뺏고 docker-shadow.
   - `--no-deps` 가 없으면 의존 서비스 **postgres 까지 재생성**되어 라이브 DB(650건, 명명
     볼륨이라 손실은 없음)가 예고 없이 바운스된다. 라이브 보호를 근거로 V8 케이스를 잘라낸
     이 plan 의 자기 기준과 어긋난다.
8. `run-facade.sh` → `run-facade-worker.sh` → kb 백엔드 → kb 프론트(18080)

## 4. 검증

- **V1** LISTEN: `3000` facade · `3001` eq · `3002` webui · `3003` minio 콘솔 · `18080` kb front ·
  `9000` minio API · `19001` parse-svc · `8088` kb api · **`5433` postgres**(V4 전제).
  **비어야 함**: `19000` `8081` `13000` `9001` `4000` `19010` `19011`.
- **V2** `curl -s localhost:3000/healthz` 본문이 `{"status":"ok"}` — **HTML 이면 실패**.
- **V3** `curl -s localhost:3000/jobs/workers` → `"online":true`.
- **V4** ★ facade→edgequake 실왕복 — **v1 은 이 항목이 라이브를 오염시켰다.**
  실측 체인: `app.py:473 ensure_workspace(workspace_id, name=workspace_id)` →
  `edgequake.py:70 ensure_workspace(kb_id, name, …)` → `:65 _slug_for(kb_id) = "kb-"+kb_id(하이픈 제거)`
  → **slug 로 find-or-create**. 즉 body 의 `workspace_id` 는 **kb_id** 이고, eq 의
  `workspaces.workspace_id` 를 넣으면 **그 UUID 를 이름으로 하는 새 워크스페이스가 생기고
  빈 것을 검색**한다.
  → 넣어야 하는 값은 `workspaces.`**`name`**. 실측 대조:
  `name=1d9c9928-31c9-472b-84ba-8bf913cd15ab` · `slug=kb-1d9c992831c9472b84ba8bf913cd15ab` ·
  `workspace_id=e512080e-28f6-4ca4-b5be-4c05280d467b` · **리포트 231건**.
  ```
  curl -s -X POST localhost:3000/search -H 'content-type: application/json' \
    -d '{"workspace_id":"1d9c9928-31c9-472b-84ba-8bf913cd15ab","query":"전체 내용","top_k":3}'
  ```
  **통과 기준**: 200 **그리고 `results` 가 비어있지 않음**(200 만 보면 `results:[]` 로도
  통과해 유일한 기능 증명이 공허해진다). dev 는 `KBP_FACADE_KEY` 미설정이라 헤더 불필요.
  **포트만 열린 것으로는 알 수 없는 것을 이 항목만 증명한다.**
  실행 후 `SELECT count(*) FROM public.workspaces` 가 **24 에서 늘지 않았는지** 확인한다.
- **V5** ★ kb→facade 실왕복 — **증거를 교체했다.** v1 은 "kb 백엔드 로그의 outbound URL" 을
  1차 증거로 삼았으나 획득 불가다: `docguard_client.py` 에 logger 가 없고 kb `backend/` 에
  `basicConfig`/`dictConfig` 가 없어 httpx INFO 가 핸들러를 못 만난다(grep 0건).
  → **facade 쪽 access log** 를 본다(`Dockerfile`/런처가 `--access-logfile -` 로 켜 둠):
  `/tmp/facade-kbp.log` 에 `GET /gate/rules 200` 이 찍히는지. 트리거는 **업로드 화면 열기**
  (`/docguard/rules` 패스스루 — `frontend/lib/api.ts:481`); 문서 목록 화면은 이 경로를 타지
  않는다. 보조 증거로 업로드 다이얼로그의 **룰 목록이 비어있지 않음**.
  `Settings().kb_pipeline_base_url` 출력은 설정만 증명하므로 **단독 통과 근거로 쓰지 않는다.**
- **V6** webui `curl -s localhost:3002/` 200 **그리고** dev 브라우저 문서상세 "그래프 보기" 가
  3002 를 여는지. **폐쇄망은 이미지 재빌드 후에만** 반영되므로 이 항목으로 폐쇄망을 통과
  처리하지 않는다(§7).
- **V7** minio 콘솔 `3003` 200 **그리고** `/obj/` 프록시 동작(9000 유지).
- **V8** ★ 가드 실증: (a) 죽은 주소 → **경고 + 기동 성공**, (b) 미설정 + eq down → 경고 + 성공,
  (c) `KBP_REQUIRE_EDGEQUAKE=1` + 죽은 주소 → **중단**.
  ★ **dedicated 런처로 8081 을 띄우는 케이스는 검증하지 않는다** — 근거는 §2.8 의 **실제
  고장 모드**다: (i) compose postgres 가 5433 을 점유 중이라 런처가 **바인드 충돌**로 실패하고,
  (ii) compose postgres 를 내려서 우회하면 런처의 **빈 PG 가 라이브 볼륨을 가려** V4 의 전제
  (리포트 231건 워크스페이스)가 무효가 된다.
  ⚠️ v2 는 여기에 "라이브 650건을 **소거**" 라고 적었는데 그건 §2.8 이 이미 과장으로 정정한
  주장이다(런처는 자기 `eq-pg-kbp` 만 건드리고 명명 볼륨 `eq_pg_data` 는 못 지운다).
  한 문서가 사실과 그 반박을 동시에 주장하면 안 되므로 문안을 통일했다.
- **V9** 폐쇄망 불변: `git diff docker-compose.airgap.yml`(kbp) 이 **parse-svc 한 줄만**,
  kb 쪽은 **CORS_ORIGINS·FRONTEND_PORT 두 줄만** 건드림.
- **V10** `docker compose config` — minio `9000:9000`+`3003:9001` **두 개만**,
  facade `3000:19000`, webui `3002:3000`, host 이중 발행 없음.
- **V11** ★ override 삭제 후 `docker compose config` **성공**(`services must be a mapping` 회피) +
  `docker compose -f docker-compose.yml config` 단독 통과.
- **V12** ★ 스킬 실행: `stack.sh status` 가 프론트를 **18080** 에서 찾고,
  `stack.sh use <provider>` 의 `ensure_baseline`(`:159`)도 18080 을 본다.
- **V13** 회귀: baseline ID 집합과 비교 → **신규 0건**. kbp `741 passed` 유지.
  kb 는 `:66` 수정으로 **20 → 19**(줄지 않으면 근거가 틀린 것).
- **V14** ★ 폐쇄망 가드 실행: `bash scripts/airgap/verify-bundle.sh --env <합성 .env>` 통과.
  (실측상 이 가드는 `--env/--images/--imports` 3종이고 **포트 검사는 없다** — 그래서 조건이
  조용히 거짓이 되는 항목은 없지만, 만들어두고 안 돌리는 전례가 있어 1회 실행을 기록한다.)
- **V15** `\b4000\b` 치환이 `:14000` 을 건드리지 않았는지 — `grep -n 14000` 로 2곳 원형 확인.
- **V16** ★ `KBP_FACADE_PORT` 탈출구가 **양쪽** 배선됐는지: facade 를 3100 으로 띄우고
  kb `.env` 에 `KB_PIPELINE_BASE_URL=http://localhost:3100` 을 준 상태에서 V5 가 통과하는지.
  (짝 env 없이 탈출구만 있으면 kb→facade 가 끊긴다 — §2.3)
  ★ **원복까지가 이 항목이다**: kb `.env` 의 그 줄을 지우고 → facade 를 3000 으로 재기동 →
  **V1·V2·V5 재통과**. kb `.env` 는 gitignored 라 `git revert` 로 복구되지 않으므로 남기면
  kb 가 죽은 3100 을 계속 부른다.
- **V17** ★ **D-g 성립 + §1 무회귀**: 벤치마크 스택을 **다시 올린 상태**에서
  (a) `3010` LISTEN(벤치마크 프론트), (b) `8080` `/health` 200(벤치마크 API — §1 대로 불변),
  (c) `3000` 이 여전히 facade(V2 본문 검증 재실행), (d) 벤치마크 컨테이너 healthcheck 가
  **healthy**(quickstart `:161` 을 안 건드렸다는 증거).
  step 1 에서 내린 채로 끝내면 **D-g 가 제거하려던 경합을 한 번도 행사하지 않는다.**

## 5. 롤백

1. `git revert`(kbp · kb · 벤치마크 레포)
2. `scripts/facade.env` 의 `KBP_EDGEQUAKE_URL` → `:8081`
   ★ kb `.env` 도 확인한다 — V16 에서 `KB_PIPELINE_BASE_URL` 을 넣었다면 지운다(gitignored).
3. ★ §3 step 2 백업으로 `docker-compose.override.yml` 복원(**`!override` 태그 포함** — 없으면
   append-merge 로 19010/19011 이 남아 minio 콘솔이 19011 이 되고 kb·OCR 이 기대하는 `:9000` 이
   깨진다. override 가 애초에 막으려던 고장이다)
4. `docker compose up -d --force-recreate edgequake edgequake_webui minio`
5. kb 프론트 원복 후 재기동, 런처 재기동 → V1·V2
6. 폐쇄망은 이전 프론트 이미지를 재배포(§2.10 을 되돌리려면 재빌드 필요)

§2.4 가 **경고 기준**이라 롤백 중간 상태도 facade 기동을 막지 않는다.

## 6. P2 로 넘기는 것 (읽히는 곳 — 별도 plan)

판별 규칙(§0)을 세운 뒤 일괄 처리한다. **호스트/컨테이너 내부/dedicated 런처 세 의미가
같은 숫자로 쓰이므로 라인별 대상/비대상 표를 먼저 만든다** — 일괄 치환하면 §0.2 의
"컨테이너 내부 불변" 과 D-a 를 스스로 깬다.

- `docs/facade-api.md`(본문 ~16곳) + `docs/facade-api.html`(~13곳) — env 기본값 표(`:499`/`:1280`)
  **포함**
- `docs/kb-pipeline-process-definition.md` — **CLAUDE.md 가 권위 출처로 지정**한 문서
  (`:21,:38,:41,:46,:62,:63,:141`, `:261` "8080 vs 8081 일원화 필요" 옛 진술)
- `docs/kbp-docker-startup.md` — 5절 재매핑 표·"override 사용(이미 적용됨)" 등 **삭제된 절차를
  안내하는 서술**(`:164-180`,`:208`,`:216`), `:217`·`:241` `EDGEQUAKE_API_URL` 기본 8081
- `docs/compose-smoke.md:61` — override 로 minio 재매핑 안내
- `docs/architecture-ports.md` `:29`·`:53` 다이어그램
- `docs/airgap-deploy.md`(`:55`,`:158`,`:204`,`:272`) · `docs/parse-only-guide.md:82` ·
  `docs/runbook-v2-smoke.md` · `HANDOVER-*.md` · `service/tests/test_e2e_smoke.md`
  (★ `scripts/airgap/deploy-both.sh:85`·`:88` 은 **§2.6 에서 P1 으로 승격**했으므로 여기서 제외 —
  v2 는 두 phase 에 이중 배정했다)
- ★ `scripts/ocr-test/` **전부 NO-CHANGE** — v1 의 근거는 **인과가 거꾸로였다.**
  이 도구들은 원래 **OCR 게이트웨이**를 겨냥한다(`README.md:3` "대상: **OCR Gateway
  (`:18081`)**", 호출 경로 `{base}/ocr/{engine}/tasks`). D-e 로 parse-svc 가 19001 로 비켜주면
  18081 은 게이트웨이 단독 소유가 되어 이 기본값이 **오히려 정답**이 된다.
  v1 문안대로 "18081 스윕" 을 하면 정상 도구를 19001(parse-svc, `/ocr/*` 없음)로 돌려
  **plan 이 막으려던 사고를 plan 이 지시**하게 된다.
  대상 파일 전수(변경 없음 확인용): `ocr_batch_stress.py:115` · `ocr_loadtest.py:147` ·
  `ocr_single.sh:18` · `ocr_batch_stress.sh:18`.
- kb `docs/airgap-known-issues.md` `:149-152`(낡은 kbp 포트 목록) · `:244` · `:251`(18080 통일로
  닫힘) · `:252`(`CORS_ORIGINS 3100` 거짓 — P1 이 고치므로 닫힘 표시) ·
  `docs/airgap-deploy-kb.md`(`:44`,`:47`) · kb `_workspace/96_frontend_docs_theme.md:6`(dev 프론트
  `:3100` 주장 — 다섯 번째 포트 주장)
- `_workspace/README.md`·`01-architecture.md`·`02-changes.md`

## 7. 이 작업으로 닫히지 않는 것

- ★ **폐쇄망 webui 링크** — §2.10 은 코드 수정이라 **프론트 이미지 재빌드 후에만** 반영된다.
  번들 재포장이 보류 중이므로 `docs/airgap-known-issues.md` 에 **open** 으로 기록:
  "현장 문서상세 '그래프 보기' 는 프론트 이미지를 다시 빌드할 때까지 13000 을 부른다."
- **P2 문서 스윕** — 그때까지 문서가 옛 포트를 말한다. P1 커밋 메시지와 known-issues 에
  "문서는 P2 에서 정합화" 를 명시해 다음 사람이 되돌리지 않게 한다.

## 8. 구현 후 검증 (착수 전 검증에서 이관)

> 2라운드 검증관이 각 지적에 `runtime_discoverable` 을 판정했다. `true` 인 5건은 글로벌 룰
> "검증 비용 관리" 5번대로 **구현 중 실측으로 닫는다** — 계획서에서 더 다투는 것보다 한 번
> 돌리는 게 확실한 종류다. 각 항목은 **증거(명령 출력)를 남겨야** 완료로 친다.
> 라운드 추이 20 → 12 로 수렴했고 남은 것이 문서 내부 불일치 계열이라, 방향 재확인은 불필요.

| # | 항목 | 어떻게 닫나 |
|---|---|---|
| 1 | **V4 실패 원인 판별 규칙** — `POST /search` mode=local 은 eq `POST /api/v1/query` 를 거쳐 **LLM 백엔드**를 탄다(dev: OpenRouter — 이 프로젝트에서 반복적으로 죽는다). 백엔드 장애면 포트와 무관하게 V4 가 붉어지는데 두 원인을 구분할 규칙이 없었다 | 실행해서 실제로 어떻게 실패하는지 본다. **eq `/query` 5xx + eq 로그에 LLM 에러** = LLM 장애(포트 회귀 아님) → `sources`/`results` 존재만으로 수용. 그 판정을 기록한다 |
| 2 | **V5 증거 문안** — v2 가 "`Dockerfile`/런처가 `--access-logfile -` 로 켜 둠" 이라 적었으나 그 플래그는 **gunicorn 전용**(`Dockerfile.facade:36-37`)이고 호스트 런처는 **plain uvicorn**(`run-facade.sh:46-47`) | `/tmp/facade-kbp.log` 에 `GET /gate/rules 200` 이 실제로 찍히는지 확인(uvicorn 기본 access log 는 ON — `GET /jobs/workers 200` 로 이미 실측됨). 문안을 "uvicorn 기본 access log" 로 정정 |
| 3 | **compose 내부 healthcheck 불변 실증** — `:385` webui healthcheck 를 3002 로 잘못 바꿔도 V6·V10 은 발화하지 않는다 | 편집 후 `docker compose ps` 로 **webui/edgequake/minio 가 healthy** 임을 확인하고 출력을 남긴다. `grep -n 'localhost:3000' docker-compose.yml` 로 `:385` 원형 확인 |
| 4 | **`load-and-up.sh:159` 오인용** — v2 가 "`:159` 는 `SERVICES` 배열" 이라 했으나 실측은 `SERVICES=(…)` 가 `:109`(참조 `:143`)이고 `:159` 는 health 보고 루프의 `case` 줄이다 | 편집 전 `sed -n '105,165p'` 로 두 줄을 눈으로 확인하고 인용을 정정. **결론(건드리지 않는다)은 유효** |
| 5 | **`cors_origins` 기본값을 단정하는 kb 테스트** — §2.11 이 `3100`→`18080` 으로 바꾸므로 그런 테스트가 있으면 V13 의 "20→19 · 신규 0건" 두 기준이 동시에 깨져 회귀로 오판된다 | `grep -rn cors_origins <kb>/backend/tests/` 로 확인. 있으면 **함께 갱신**하고 V13 의 기대 수치를 조정한다 |

## 9. deferred (범위 밖 — 조용히 넘기지 않는다)

- `scripts/ocr-test/README.md:68-72` 의 `## ⚠️ 포트 주의 — parse-svc 와 18081 충돌` 절.
  D-e 로 그 충돌이 **해소**되므로 이 경고는 없는 충돌을 경고하는 산문이 된다.
  §6 이 ocr-test 를 "전부 NO-CHANGE" 로 못 박으면 P2 에서도 대상이 아니게 되므로 여기 남긴다 —
  **기본값은 NO-CHANGE(정답), 산문 §포트주의 는 P2**.

### 8.1 닫힘 기록 (2026-08-10 구현 중 실측)

| # | 결과 | 증거 |
|---|---|---|
| 1 | ✅ **모호함 없이 통과** — LLM 백엔드가 살아 있어 판별 규칙이 필요하지 않았다 | `POST :3000/search` → 200, **`results` 204건 + LLM 답변**. 워크스페이스 수 **24 → 24**, 리포트 **650 → 650**(오염 없음). kb_id=`1d9c9928-…`(= `workspaces.name`)가 slug `kb-1d9c9928…` 로 기존 워크스페이스를 찾았다 |
| 2 | ✅ 닫힘 — **v2 의 근거가 틀렸다** | `--access-logfile` 은 gunicorn 전용이고 호스트 런처는 plain uvicorn 이다. 그러나 **uvicorn 기본 access log 가 ON** 이라 경로 자체는 유효: `/tmp/facade-kbp.log` 에 `"POST /search HTTP/1.1" 200 OK` 실측 |
| 3 | ✅ 닫힘 | 재생성 후 `docker compose ps` — edgequake/postgres/adaptive/doc_guard **healthy**, webui·minio `starting`→정상. `:389` webui healthcheck(`localhost:3000`)·`:179`·`:331`·`:115` 전부 원형(grep 확인) |
| 4 | ✅ 인용 정정 | 실측: `SERVICES=(` 는 **`:109`**(참조 `:143`), `:159` 는 health 루프의 **`esac`**. v2 의 "`:159` 는 SERVICES 배열" 은 오인용이었다. **결론(건드리지 않는다)은 유효** — 둘 다 변경 대상이 아니다 |
| 5 | ✅ 닫힘 | `grep -rn "cors_origins\|3100" backend/tests/` → **0건**. `cors_origins` 기본값을 단언하는 kb 테스트가 없어 V13 기대값(20→19)이 유효하다 |

### 8.2 구현 중 새로 발견해 고친 것

★ **`run-facade.sh` 가 호출자 env 를 무시했다.** `set -a; . scripts/facade.env` 가 CLI 로 준
값을 **덮어써서** `KBP_EDGEQUAKE_URL=… bash scripts/run-facade.sh` 가 조용히 무시됐다.
그래서 V8 ②(`KBP_REQUIRE_EDGEQUAKE=1` + 죽은 주소 → 중단)가 **통과해버렸다** —
가드가 사라진 게 아니라 통과한 것이라 더 위험한 형태다(이 프로젝트 전례와 동형).
→ 덮어쓰기 허용 키(`KBP_EDGEQUAKE_URL`·`KBP_FACADE_PORT`)를 source 전에 스냅샷해
source 후 재적용한다(dotenv 관례: **CLI 가 파일보다 우선**).
`export -p` 재적용은 `PWD` 같은 것까지 건드리므로 쓰지 않았다.
**가드를 실제로 돌렸기 때문에** 드러났다.

★ **health 폴링 10초가 콜드 스타트에 부족했다.** 첫 기동에서 정상 서버에 경고가 떴다
(재기동은 5초). 정상에 오경보하는 가드는 곧 무시되므로 **30초**로 늘렸다.

### 8.3 실행하지 않은 검증 (정직하게 남긴다)

- **V17 전체 기동** — 벤치마크 스택을 실제로 띄우지 않았다. `docker compose config` 로
  **published 가 `8080`·`3010` 둘뿐**임을 확인해 D-g(3000 청구 제거)를 입증했고, facade 가
  3000 을 계속 소유함은 V1·V2 로 확인했다. 자체 edgequake+postgres 를 올리는 비용이 한
  단언에 비해 과하고 잔여 컨테이너 위험이 있어 멈췄다. **전체 기동은 미실행이다.**
- **V5 브라우저 왕복** — `/docguard/rules` 는 JWT 가 필요해 헤더 없는 curl 로는 401 이다.
  설정값(`kb_pipeline_base_url = http://localhost:3000`)과 uvicorn access log 경로는
  확인했으나 **업로드 화면을 열어 룰 목록을 보는 것은 사람이 해야 한다.**
- **V16 탈출구 왕복** — `KBP_FACADE_PORT` 는 §8.2 수정으로 CLI 우선이 됐음을 V8 로
  간접 확인했으나, kb `.env` 에 `KB_PIPELINE_BASE_URL` 을 넣고 되돌리는 왕복은 미실행이다.

## 10. 최종 검증 결과 (2026-08-10)

| # | 항목 | 결과 |
|---|---|---|
| V1 | LISTEN 9종 / 해제 7종 | ✅ `3000`·`3001`·`3002`·`3003`·`18080`·`9000`·`19001`·`8088`·`5433` 전부 LISTEN. `19000`·`8081`·`13000`·`9001`·`4000`·`19010`·`19011` 전부 해제 |
| V2 | healthz **본문** | ✅ `{"status":"ok"}` |
| V3 | worker online | ✅ `"online":true, capacity 4` |
| V4 | facade→edgequake 실왕복 | ✅ **200 · `results` 204건 · LLM 답변**. 워크스페이스 24→24, 리포트 650→650(**오염 없음**) |
| V5 | kb→facade | ◐ 설정값 `http://localhost:3000` 확인 + uvicorn access log 경로 확인. **브라우저 왕복은 사람 몫**(§8.3) |
| V6 | webui | ✅ `:3002` 200 |
| V7 | minio 콘솔 + `/obj/` | ✅ `:3003` 200, `/obj/` 프록시 404(존재하지 않는 키 — 연결 자체는 성립, 9000 유지 확인) |
| V8 | 가드 3경로 | ✅ 죽은주소→경고+성공 / `REQUIRE=1`→중단 / 정상→통과. **이 검증이 §8.2 버그를 잡았다** |
| V9 | 폐쇄망 published 불변 | ✅ kbp **parse-svc 한 줄만**, kb **CORS·FRONTEND_PORT 두 줄만** |
| V10·V11 | compose 합성 | ✅ facade `3000:19000` · eq `3001:8081` · webui `3002:3000` · minio `9000:9000`+`3003:9001`. 이중 발행 없음 |
| V12 | override 삭제 후 config | ✅ `docker compose config` 및 `-f docker-compose.yml` 단독 통과(`services must be a mapping` 회피) |
| V13 | 회귀 | ✅ kbp **741 passed 유지(0 failed)**. kb **20 → 19**, **새 실패 0건**, 고쳐진 것이 정확히 `test_settings_have_kb_pipeline_defaults` — **plan 의 예측과 일치** |
| V14 | 폐쇄망 가드 실행 | ✅ `verify-bundle.sh --env` 통과 |
| V15 | `:14000` 원형 | ✅ 2건 보존, `118080`/`13002` 오염 0 |
| V16 | 탈출구 왕복 | ◐ CLI 우선 배선은 V8 로 확인. kb `.env` 왕복은 미실행(§8.3) |
| V17 | D-g | ◐ config 로 published `8080`·`3010` 확인. **전체 기동 미실행**(§8.3) |
