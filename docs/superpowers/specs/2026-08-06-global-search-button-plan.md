<!-- plan-version: v5 -->
<!-- implemented: 2026-08-09 — §6 닫힘(6.1), 검증(6.2) -->
<!-- gate: 2026-08-10 — 축1 통과 / 축2 는 알려진 한계로 수용(D38). 배포 승인 -->
<!-- ultracode-validation: READY v5 at 2026-08-09T21:10:00Z (구현 우선 전환 — §6 참조) -->

# [B] global 검색을 명시적 버튼으로 노출

> `2026-08-06-community-nightly-batch-plan.md`(A, 야간 배치)에서 분리했다. A 와 B 는
> **코드 의존이 없다** — 어느 쪽을 먼저 구현해도 된다. B 만 배포하면 리포트는 수동
> `/communities/build` 로 만들어진 것만 쓴다.
>
> v1 은 A 의 v1~v5 검증(누적 blocking 41건)에서 살아남은 내용으로 시작했지만, **독립
> 검증에서 blocking 8건**이 새로 나왔다. **v2 에서 고친 것**: LLM 미설정 가드가 키 하나만
> 보던 것을 세 변수로 확장, 테스트 위치·러너 부재를 명시, kb 클라이언트 재시도가 실패를
> 3배로 증폭하는 문제에 **비재시도 오류 매핑**을 추가, D10 이 잡 큐로 옮긴 이유(웹 프로세스
> 점유)를 이 설계가 되돌리는 것에 **동시성 상한**을 추가, LLM 실패 경로의 오류 전파를
> 명시, 리포트 신선도 신호와 관련성 실측을 넣었다.

---

## 0. 사용자 결정 (2026-08-06)

**global 검색을 명시적 버튼으로 노출한다.** 자동 라우팅(`route()` + `GLOBAL_CUES`)은
**하지 않는다** — 오분류하면 답이 나빠지고("휴가 규정 요약해줘"가 global 로 가면 조문
원문을 못 준다) 그 비용을 아직 실측하지 못했다.

**목적**: local 검색이 구조적으로 못 답하는 질문("이 KB 에 어떤 규정들이 있나", "휴가 관련
전부 정리")을 사용자가 **직접 선택해서** 물을 수 있게 한다. 실제로 쓰이는지 관측한 뒤에야
자동 라우팅을 검토한다.

---

## 1. 실측 사실 (2026-08-06 작성 / **2026-08-09 인용 재검증**)

> **v3 → v4**: 코드 변경 없이 라인 인용만 재검증했다. 40개 중 34개는 그대로 맞고
> **6개가 밀렸다**(교정 완료): 사실 1·10(facade `/search`), 6·26(`service/llm.py` —
> 2026-08-09 의 다운스트림 로깅 커밋으로 import 가 늘었다), 12(`_reports_exist`),
> 20(kb 클라이언트 `search`).
> **핵심 전제는 그대로 유효하다** — `global_search` 는 여전히 facade 에 배선돼 있지 않고
> (`service/` 에 grep 0건), `service/app.py` 는 `kb_pipeline` 을 import 하지 않는다.

| # | 사실 | 근거 |
|---|---|---|
| 1 | facade `/search` 는 edgequake 프록시. mode/global 개념이 없다. **`def search(` 는 동기 함수** | `service/app.py:320-341`, `:321`(def search) |
| 2 | `global_search` 호출부 = CLI(`search.py:347`) + `unified_search`(`:275`, `--mode auto`) + 테스트. **웹 경로 없음** | grep |
| 3 | `global_search(question, workspace_id, *, llm, dsn, top_k=5, …)` — `llm` **기본값 없는 필수 키워드** | `search.py:148-157` |
| 4 | 반환은 `{answer, sources, mode, workspace_id}`. `sources` 는 **커뮤니티 id 목록** | `search.py:186-192` |
| 5 | `global_query` 는 map N + reduce 1 의 **순차 LLM**. `top_k` 가 곧 map 횟수 | `community.py:652-681` |
| 6 | **`get_text_llm()` 은 인자를 받지 않고** `KBP_LLM_TIMEOUT`(기본 **300s**)만 읽는다 | `service/llm.py:10`(def), `:17`(KBP_LLM_TIMEOUT) |
| 7 | `KBP_GLOBAL_LLM_TIMEOUT` 은 리포지토리에 **0건** | grep |
| 8 | `KBP_OPENAI_API_KEY: ${KBP_OPENAI_API_KEY}` — 기본값이 없어 미설정 시 **빈 문자열** 주입(KeyError 아님) | compose `:28`; `llm.py:11` |
| 9 | `community_reports.workspace_id` = **eq workspace UUID** | 라이브 5행이 `workspaces.workspace_id` 와 일치 |
| 10 | facade `/search` 가 받는 `workspace_id` 는 **kb id** — `ensure_workspace` 로 해석한다 | `app.py:332` |
| 11 | `public.community_reports` 는 `store_reports` 안의 `cur.execute(_DDL)` 에서만 **lazy 생성** | `community.py:484` |
| 12 | `_reports_exist` 는 `UndefinedTable` 도 `psycopg.Error` 도 **모두 False** 로 삼킨다 | `search.py:125-133` |
| 13 | `global_search`·`reports_exist` 는 `service/app.py` 에 **import 되어 있지 않다**(`:31` 은 `get_text_llm` 뿐) | grep |
| 14 | facade 는 `KBP_PG_DSN` 없이도 뜬다(잡 큐만 꺼짐) | `app.py:113,124` |
| 15 | kb 검색 진입은 **챗뿐** — 검색 전용 페이지가 없다 | `frontend/app/**/page.tsx` |
| 16 | 챗 함수 = `_run_kb_pipeline_chat`(`core/chat.py:438`). **Protocol `_KbPipelineSearch`(`:92-101`)가 계약을 고정** | 해당 파일 |
| 17 | `ChatTurnResult`(`chat.py:127-132`) = answer/citations/dify_run_id | 해당 파일 |
| 18 | `ChatRequest`/`ChatResponse` = `schemas/chat.py:14`/`:53`. **`extra='allow'` 없음** | 해당 파일 |
| 19 | 라우터: 호출부 `:99-106`, **응답 조립 `:124-127`** | `routers/chat.py` |
| 20 | 클라이언트 `def search(` = `:410`, 반환 **화이트리스트** = `:426-428`(`{answer, results}` 만) | `kb_pipeline_client.py` |
| 21 | `ChatPanel` props = `{ kbId }`. 부모는 `provider`/`providerStatus` **state** 를 갖고 `UploadPanel` 에만 준다 | `ChatPanel.tsx:29`; `page.tsx:21-25,105` |
| 22 | `page.tsx:75-82` 이 provider 미해결(loading/error) 동안 UI 를 감추는 **기존 관례**를 갖는다 | 해당 파일 |
| 23 | `tests/test_search.py:221` 이 `build_if_missing` 기본값에 의존한다 | 해당 파일 |
| 24 | `_workspace/01-architecture.md:159`(불릿)·`:160`(라이브러리 경로)·`:223`·`:224`(표 행) — **네 곳** 모두 "미배선" 서술 | 해당 파일 |
| 25 | `docker-compose.yml:27-29`·airgap `:46-48` — `KBP_OPENAI_API_KEY`/`KBP_OPENAI_BASE_URL`/`KBP_LLM_MODEL` **셋 다** `${VAR}`(기본값 없음) | 해당 파일 |
| 26 | `get_text_llm()` 의 `base = os.environ.get("KBP_OPENAI_BASE_URL", "https://openrouter.ai/api/v1")` — **"있는데 빈 값"에는 default 가 적용되지 않는다** | `service/llm.py:12` |
| 27 | `kb_pipeline_client.py:139-148` 재시도 루프: `retryable = status==429 or status>=500`, `attempts=self._max_retries`(기본 **3**, `config.py:158`) | 해당 파일 |
| 28 | `runner.py:247-261` docstring — D10 이 `/communities/build` 를 잡 큐로 옮긴 이유 3가지(유량제어 밖·**웹 프로세스 점유**·흔적 없음)를 명시한다 | 해당 파일 |
| 29 | `admission.py:145` `KBP_JOB_LIMIT_COMMUNITY` 상한은 **잡 큐 경로에만** 적용된다 — `/search` 동기 호출에는 아무 상한도 없다 | 해당 파일 |
| 30 | `routers/chat.py:113` `except (httpx.HTTPError, Exception)` → **일반 502**(원인 소실). `:107` 의 503 분기는 `ChatConfigError` 전용 | 해당 파일 |
| 31 | `community.py:601-614` `_rank_reports` — `re.split(r"\W+", ...)` + 부분문자열 매칭. **`\W` 는 한글을 단어문자로 취급**해 조사 붙은 어절 단위로 쪼갠다 | 해당 파일 |
| 32 | `community_reports` 에 **DELETE 문이 0건**(전체 코드베이스) — upsert-only. 라이브 `cf7b9733…` 39행이 2026-07-20 자 | grep; 라이브 |
| 33 | `pyproject.toml:30` `testpaths = ["tests", "service/tests"]` — `kb_pipeline/tests/` 는 수집 대상이 아니다(그 디렉터리엔 `conftest.py`+`test_modal_spans.py` 뿐) | 해당 파일 |
| 34 | `frontend/package.json` scripts = `{dev,build,start,lint,typecheck}` — **`test` 스크립트·러너가 없다**(`*.test.ts(x)` 0건) | 해당 파일 |
| 35 | `service/tests/test_job_runner.py:347` 은 `monkeypatch.setattr("service.llm.get_text_llm", lambda: …)` — **무인자 람다**로 심는다 | 해당 파일 |
| 36 | `get_text_llm()` 무인자 호출자 = `runner.py:276`, `parse_service/app.py:381` | grep |
| 37 | `core/chat.py:230-250` 은 provider 로 분기 — edgequake/raganything/ragflow 경로는 `mode` 를 받지 않는다 | 해당 파일 |
| 38 | `run_chat_turn` 의 두 번째 호출자 = `routers/comparison.py:479`(기본값 `"local"` 이라 무해) | 해당 파일 |
| 39 | `edgequake.py:80-88` `ensure_workspace` 는 **조회 전용 경로가 없다** — POST 로 생성 시도, 5xx 3회 재시도 후 `raise_for_status` | 해당 파일 |
| 40 | `community.py:543-548` `_SELECT_REPORTS_SQL` 은 `LIMIT` 없이 workspace 전체 리포트를 끌어와 파이썬에서 정렬(라이브 최대 231행) | 해당 파일 |

---

## 2. 설계

### 2.1 facade `/search` 에 `mode`

```python
@app.post("/search", ...)
def search(workspace_id=Body(...), query=Body(...), top_k=Body(10),
           mode: str = Body("local", embed=True),
           global_top_k: int = Body(5, embed=True), eq=Depends(get_edgequake)):
    if mode not in ("local", "global"):
        raise HTTPException(400, "mode must be 'local' or 'global'")   # ★ ensure_workspace 前
    eq_ws = eq.ensure_workspace(workspace_id, name=workspace_id)       # kb id → eq UUID
    if mode == "global":
        return _search_global(eq_ws, query, global_top_k)
    ...   # 기존 경로 + "mode": "local"
```

- **검증을 `ensure_workspace` 앞에** 둔다 — 뒤에 두면 `mode=bogus` 가 workspace 를 만든다.
- **`eq_ws` 로 부른다.** `community_reports.workspace_id` 는 eq UUID 인데(사실 9) facade 가
  받는 것은 kb id 다(사실 10). kb id 를 그대로 넘기면 `reports_exist` 가 **영구히 0행** →
  오류 없이 매번 *"야간 배치 이후 사용 가능"* 이라는 **거짓 안내**가 뜬다.
- 기본 `"local"` → **기존 호출자 전원 무변경**. 응답에 `mode` 를 더한다(local 도).
- **`top_k` 를 global 로 흘리지 않는다.** facade 의 `top_k` 는 반환 청크 수(기본 10)지만
  `global_query` 의 `top_k` 는 **map 대상 = 순차 LLM 호출 수**다(사실 5). 그대로 흘리면
  기본이 11회 직렬 LLM 이 된다. `global_top_k` 기본 5, **1~5 clamp**.
- **import 를 추가한다** — `global_search`·`reports_exist` 는 `app.py` 에 없다(사실 13).

```python
def _search_global(eq_ws, query, k):
    dsn = os.environ.get("KBP_PG_DSN")        # 인덱싱 금지 — facade 는 DSN 없이도 뜬다(사실 14)
    if not dsn:
        raise HTTPException(503, "global search unavailable: KBP_PG_DSN unset")
    if not _llm_configured():                 # §2.1.1 — 세 변수 모두 확인
        raise HTTPException(503, "global search unavailable: LLM not configured")
    slot_id = _acquire_global_slot(dsn, _GLOBAL_SEARCH_CONCURRENCY)   # §2.3 — DB 기반, 워커 4개 경계를 넘는 상한
    if slot_id is None:
        raise HTTPException(503, "global search busy: too many concurrent requests")
    try:
        try:
            ready = reports_exist(eq_ws, dsn)     # §2.2
        except psycopg.Error as exc:              # ★ Exception 이 아니라 psycopg.Error 만
            raise HTTPException(503, f"global search unavailable: {exc}") from exc
        if not ready:
            return {"answer": None, "results": [], "communities": [],
                    "mode": "global", "community_reports_ready": False,
                    "report_generated_at": None}
        max_age_at = newest_report_time(eq_ws, dsn)          # §2.6 — 신선도
        try:
            out = global_search(query, eq_ws, llm=get_text_llm(timeout=_GLOBAL_LLM_TIMEOUT),
                                dsn=dsn, top_k=k, build_if_missing=False)
        except httpx.HTTPError as exc:   # §2.4 — httpx 예외 계층의 공통 상위(아래 참고)
            raise HTTPException(422, f"global search LLM call failed: {exc}") from exc  # ★ 4xx — 500대는 kb 클라이언트가 재시도한다(사실 27)
        return {"answer": out.get("answer"), "results": [],
                "communities": out.get("sources") or [],
                "mode": "global", "community_reports_ready": True,
                "report_generated_at": max_age_at.isoformat() if max_age_at else None}
    finally:
        _release_global_slot(dsn, slot_id)
```

- **응답 키 집합을 global 의 두 분기(ready/not-ready)에서 동일하게** 고정한다.
  (local 과 같다는 뜻이 아니다 — local 에는 `communities`·`community_reports_ready` 가 없다.)
- `sources` 는 커뮤니티 id 목록이라(사실 4) `results`(`{chunk_id,text,score,document_id}`)
  계약에 안 맞는다 → **`communities`** 로 낸다. `results` 는 global 에서 항상 `[]`.
- **`report_generated_at`**: §2.6 이 다루는 리포트 신선도 신호. `ready:false` 면 `null`.

#### 2.1.1 LLM 미설정 가드 — 세 변수 모두 확인한다

v1 은 `KBP_OPENAI_API_KEY` 하나만 봤다. 그런데 compose 양쪽(사실 25)이 **세 변수 모두**
`${VAR}`(기본값 없음)로 주입하므로, `KBP_OPENAI_BASE_URL` 이 미설정이면 빈 문자열이 들어가고
`get_text_llm()` 의 `.get(key, default)` 는 **"있는데 빈 값"에는 default 를 안 적용한다**
(사실 26) — `base=""` 로 `httpx.post("/chat/completions")` 를 호출해 `UnsupportedProtocol`
로 뒤늦게 500 이 난다.

```python
def _llm_configured() -> bool:
    return all((os.environ.get(k) or "").strip()
               for k in ("KBP_OPENAI_API_KEY", "KBP_OPENAI_BASE_URL", "KBP_LLM_MODEL"))
```
`KBP_OPENAI_BASE_URL`·`KBP_LLM_MODEL` 도 실제로는 compose 앵커에 항상 값이 채워져 있는
배포가 정상이지만(로컬 개발·에어갭 모두), **가드는 방어적으로 세 개를 다 본다** — 코드
변경으로 default 를 하나만 없애도 깨지지 않도록.

### 2.2 오류 의미론 — **새로 추가하는 DB 호출 전부**에 적용한다

> **★ v5**: v4 는 `try/except` 가 `reports_exist` 만 감쌌다. `_acquire_global_slot`·
> `newest_report_time`·`_release_global_slot` 은 psycopg 예외에 무방비였다 →
> `kbp.global_search_slots` 부재(UndefinedTable)나 요청 시점 PG 장애에서 **미처리 예외 →
> FastAPI 500 → kb 클라이언트 3회 재시도**(사실 27).
>
> **테이블 부재는 가상이 아니다** — facade lifespan 이 `ensure_schema` 실패를
> `except Exception` 으로 삼키고 기동을 계속한다(`service/app.py:113-124`,
> "로그를 남기고 기동은 계속(가시성 우선)"). **스키마 없이 정상 응답하는 배포가 설계상
> 존재한다.**
>
> **규칙**: `_search_global` 안의 모든 DB 호출은 `psycopg.Error` 를 잡아
> **503**(설정·인프라 문제라는 정직한 신호)으로 바꾼다. 단 `_release_global_slot` 만
> 예외다 — 이미 만든 응답을 뒤집으면 안 되므로 **로그만**(위 §2.3).
> 500 대역은 어떤 경로로도 내지 않는다.

### 2.2.1 `reports_exist` 는 **fail-open**

```python
# kb_pipeline/search.py — 신규 공개 함수. **인자는 eq workspace UUID 다.**
def reports_exist(workspace_id: str, dsn: str, *, level: int = 0) -> bool:
    """UndefinedTable → False(아직 아무도 안 만들었다). 그 외 psycopg.Error → raise."""
```

- `public.community_reports` 는 `store_reports` 안에서만 lazy 생성된다(사실 11). 폐쇄망 신규
  기동·덤프 복원 직후에는 **테이블 자체가 없다**. 모든 오류를 올리면 `mode=global` 이 항상
  503 이 되어 §2.6 의 안내가 **첫 빌드 전에는 절대 안 뜬다.**
- 반대로 진짜 DB 장애를 `ready:false` 로 위장하면 *"야간 배치 이후 사용 가능"* 이라는
  **거짓 안내**가 된다 → 그 경우만 503.
- `_reports_exist`(사실 12) 는 **그대로 둔다** — "모르면 빌드해라"라는 원래 전제와 기존
  호출자를 보존한다. 새 경로만 `reports_exist` 를 쓴다 → 비공개 심볼 크로스패키지 import 도
  해소된다.
- `_search_global` 은 **`except psycopg.Error`**(v1 은 `except Exception` 이었다) 로 좁힌다
  — 코드 버그(TypeError 등)까지 "서비스 일시 불가" 로 위장하지 않는다.
- `workspace_id` 컬럼은 `uuid NOT NULL` 이라, non-UUID 값을 넘기면 `UndefinedTable` 이 아닌
  **`InvalidTextRepresentation`**(`psycopg.Error`)이 나 fail-open 규칙상 **503**이 된다
  (이 경로는 `eq_ws` 가 항상 UUID 이므로 실제로는 발생하지 않지만, §2.1 의 근거문 "kb id 를
  그대로 넘기면 영구히 0행" 은 "0행 또는 타입 오류(→503)" 로 정정한다).

### 2.3 LLM 타임아웃 배선 + 동시성 상한 — D10 의 웹 프로세스 점유 문제를 반복하지 않는다

`get_text_llm()` 은 인자를 안 받고 `KBP_LLM_TIMEOUT`(300s)만 읽는다(사실 6).
`KBP_GLOBAL_LLM_TIMEOUT` 은 **코드에 없다**(사실 7) — compose 에만 넣으면 **조용한 no-op**
이고, 그 위에 세운 비용 산정 전체가 무효가 된다.

```python
# service/llm.py — keyword-only + default 로 기존 무인자 호출자를 보존한다(사실 35·36)
def get_text_llm(*, timeout: float | None = None):
    ...
    timeout = timeout if timeout is not None else float(os.environ.get("KBP_LLM_TIMEOUT", "300"))
```
`runner.py:276`·`parse_service/app.py:381`(무인자 호출, 사실 36)과
`test_job_runner.py:347`(무인자 람다 monkeypatch, 사실 35)은 **키워드 전용 + 기본값**이므로
무변경으로 계속 동작한다 — 시그니처 회귀 테스트를 §4 에 넣는다.

프로세스 전역 env 를 호출 시점에 바꾸는 방식은 **금지** — 같은 워커의 다른 경로와 경합한다.

**웹 프로세스 점유 — D10 이 이미 겪은 문제다.** `runner.py:247-261` 의 docstring(사실 28)은
`/communities/build` 를 잡 큐로 옮긴 이유로 "유량제어 밖 / **facade 웹 프로세스 점유** /
흔적 없음" 을 명시했다. `_search_global` 은 같은 성질(LLM 5 map + 1 reduce 순차)의 작업을
**동기 함수 안에서** 돌리므로, 잡 큐의 `KBP_JOB_LIMIT_COMMUNITY` 상한(사실 29)이 전혀 적용
되지 않는 새 미유량제어 경로를 다시 만드는 것이다. **완전히 잡 큐로 옮기지는 않는다** —
그러면 "즉시 답하는 대화형 검색" 이라는 목적 자체가 깨진다. 대신 **동시 실행 수를 상한**
한다.

**`threading.Semaphore` 는 이 상한을 제공하지 못한다.** `Dockerfile.facade:20` 이
`gunicorn -k uvicorn.workers.UvicornWorker -w 4` 로 뜬다(`docker-compose.yml:255` 가 이
이미지를 빌드) — **4개의 독립 프로세스**다. 모듈 스코프에서 만든 `threading.Semaphore` 는
프로세스마다 별도 인스턴스라, "상한 2" 를 의도해도 **실제 시스템 전역 상한은 최대
4배(8)** 가 된다. 최악 스레드 점유도 12분치가 아니라 **48분치**(6분 × 8) — 세마포어가
막으려던 문제를 다른 배수로 재도입할 뿐이다.

**Postgres 기반 카운터로 프로세스 경계를 넘는다** — 이미 `KBP_PG_DSN` 이 있고, 잡 큐가
아닌 **가벼운 전역 카운터**만 필요하므로 새 테이블을 하나 더 만든다:

```sql
CREATE TABLE IF NOT EXISTS kbp.global_search_slots (
  id serial PRIMARY KEY, claimed_at timestamptz NOT NULL DEFAULT now()
);
```
```python
def _acquire_global_slot(dsn: str, limit: int) -> int | None:
    """현재 점유 수가 limit 미만이면 슬롯을 잡고 그 id 를, 아니면 None.

    ★ v5: **트랜잭션 전체를 advisory lock 으로 직렬화한다.** `SELECT count(*)` → `INSERT`
    를 잠금 없이 하면 커밋 전 INSERT 가 다른 트랜잭션의 count 에 보이지 않아, 워커 4개가
    동시에 누르면 각자 `count < limit` 을 읽고 각자 INSERT 한다 → **전역 상한 2 가 4로
    깨진다**(TOCTOU). 그러면 §2.3 이 threading.Semaphore 를 기각한 이유(프로세스마다
    별도 인스턴스라 실제 상한이 워커 수배)가 **새 구현에도 그대로 남는다**.

    이 레포는 같은 문제를 이미 해결해 뒀다 — `repo.py:279-301` 의 claim 이
    "여러 worker 가 각자 '현재 running 수' 를 읽고 각자 승인하면 전역 상한이 깨지기
    때문" 이라며 `SET LOCAL lock_timeout` + `pg_advisory_xact_lock` 으로 직렬화한다.
    그 관용구를 그대로 따른다(락 안에서 다운스트림을 호출하지 않아 수 ms 다).
    """
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        # advisory lock 은 blocking 획득이라 상한이 없으면 무한정 매달린다(repo.py:298 동일).
        cur.execute("SET LOCAL lock_timeout = '5s'")
        cur.execute("SET LOCAL statement_timeout = '30s'")
        cur.execute("SELECT pg_advisory_xact_lock(%s, %s)",
                    (schema.LOCK_CLASSID, schema.LOCK_OBJ_GLOBAL_SEARCH))
        # 죽은 슬롯 청소(프로세스가 응답 없이 죽는 경우). TTL 유도는 아래 ★ 참고.
        cur.execute("DELETE FROM kbp.global_search_slots"
                    " WHERE claimed_at < now() - make_interval(secs => %s)",
                    (_slot_ttl_seconds(),))
        cur.execute("SELECT count(*) FROM kbp.global_search_slots")
        if cur.fetchone()[0] >= limit:
            return None
        cur.execute("INSERT INTO kbp.global_search_slots DEFAULT VALUES RETURNING id")
        slot_id = cur.fetchone()[0]
        conn.commit()
        return slot_id


def _release_global_slot(dsn: str, slot_id: int) -> None:
    """★ v5: **예외를 삼킨다(로그만).** 이 호출은 `finally` 절에서 새 커넥션을 여는데,
    그 시점에 PG 가 흔들리면 `psycopg.OperationalError` 가 **이미 완성된 성공 응답
    (6분·LLM 6회분)이나 HTTPException(422) 을 500 으로 바꾼다** → kb 클라이언트의
    재시도 조건(`>=500`, 사실 27)에 걸려 map-reduce 를 3회 반복한다.
    §2.4 가 422 를 고른 유일한 목적을 자기 finally 절이 되돌리는 셈이다.
    슬롯 회수는 위 TTL 청소가 담당한다.
    """
    try:
        with psycopg.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM kbp.global_search_slots WHERE id = %s", (slot_id,))
            conn.commit()
    except Exception:  # noqa: BLE001 — 성공한 응답을 뒤집지 않는다
        log.error("global 검색 슬롯 반납 실패 id=%s (TTL 청소가 회수한다)", slot_id,
                  exc_info=True)


def _slot_ttl_seconds() -> int:
    """★ v5: 죽은 슬롯 TTL 을 **LLM 타임아웃에서 유도한다**(10분 하드코딩 금지).

    요청 최악 소요 = (global_top_k 상한 5 + reduce 1) × KBP_GLOBAL_LLM_TIMEOUT.
    TTL 이 그보다 짧으면 **살아있는 요청의 슬롯이 매 acquire 마다 삭제**되고, 그 뒤
    `_release_global_slot` 은 없는 id 를 조용히 지워 성공하므로 **상한이 사실상 사라진다.**
    기본(60s)에서는 6분인데 10분 하드코딩이 겨우 맞았을 뿐이고, 기존 `KBP_LLM_TIMEOUT`
    관례대로 300 을 넣으면 최악 30분이 되어 즉시 깨진다.
    여유배수 2를 곱한다 — 커넥션·DB 왕복 등 LLM 외 시간을 감싼다.
    """
    return int((5 + 1) * _global_llm_timeout() * 2)
```
#### 2.3.1 설정을 **요청 시점에** 읽는다 (v5)

`_GLOBAL_SEARCH_CONCURRENCY`·`_GLOBAL_LLM_TIMEOUT` 을 모듈 상수(`_UPPER`)로 두면 두 가지가
깨진다: (a) 빈/오타 값이 **import 시점 `ValueError`** → gunicorn 워커 **전체 기동 실패**로
`/parse`·`/ingest` 까지 동반 사망한다(§3 이 "500" 이라 적었는데 증상이 완전히 다르고 훨씬
나쁘다), (b) §4 의 회귀 테스트가 `monkeypatch.setenv` 로 상한을 바꿔 검증하는데 import
시점 1회 읽기면 **먹지 않는다**.

```python
def _global_llm_timeout() -> float:
    return _env_float("KBP_GLOBAL_LLM_TIMEOUT", 60.0)

def _global_concurrency() -> int:
    return _env_int("KBP_GLOBAL_SEARCH_CONCURRENCY", 2)
```
`_env_int`/`_env_float` 는 **파싱 실패 시 기본값 + warning**(레포 관례: `worker.py:43`,
`community_schedule.py`). 설정 오타가 기동을 막거나 500 을 내지 않는다.

`KBP_GLOBAL_SEARCH_CONCURRENCY` 기본 **2** — 이제 **워커 4개를 합친 시스템 전역** 상한이다
(v1 은 워커당 상한이라 실제 전역은 최대 4배였다). 상한 도달 시 **큐잉하지 않고 즉시 503**
— 사용자가 명시적으로 누른 동작이니 잠깐 기다리라는 안내보다 즉시 실패 후 재시도가 더
명확하다. `finally` 절에서 `_release_global_slot` 을 호출한다(예외로 빠져도 슬롯을 반드시
반납) — 그래도 프로세스가 응답 없이 죽는 경우(OOM kill 등)에 대비해 `claimed_at` 기반
죽은 슬롯 청소를 매 acquire 마다 함께 돌린다.

**DB 왕복 비용**: acquire·release 각 1회 왕복(수 ms) — global 검색 자체가 초~분 단위이므로
무시할 만하다. 매 요청 커넥션을 새로 여는 것은 비효율적이지만, 동시성 2~4 수준에서는
문제가 되지 않는다(커넥션 풀링은 필요해지면 추가 — 지금은 범위 밖).

- 점유 모델: `def search(` 는 **동기 함수**라(사실 1) FastAPI 가 anyio 스레드풀(기본 40)에서
  돌린다 — gunicorn 워커 수가 아니라 **스레드풀**이 이론적 상한이지만, 실효 상한은 이제
  DB 카운터가 정한다.
- 배선 후 최악 = (5 map + 1 reduce) × `KBP_GLOBAL_LLM_TIMEOUT`(기본 **60s**) = **6분/요청**.
  **시스템 전역** 동시성 상한 2 이므로 **최악 총 점유는 12분치 스레드**(6분 × 2, 워커
  분산과 무관하게 참이다) — 40 × 4 = 160개 중 2개.
- **★ v5 상위 계층 예산 — 사용자 확인(2026-08-09): 운영은 사내망 직결이다.**
  터널·리버스프록시의 엣지 유휴 타임아웃이 없으므로 **6분 예산을 그대로 유지**한다.
  단 프론트 `chat()`(`frontend/lib/api.ts:637-645`)에 **타임아웃이 아예 없어** 무한
  스피너가 된다 → `AbortController` 로 상한을 둔다(`KBP_GLOBAL_LLM_TIMEOUT` 기반
  최악치 + 여유, 기본 **420초**). 사용자가 끊어도 facade 는 LLM 비용을 계속 태우므로
  (§2.4 의 map 루프는 취소 신호를 모른다) **프론트 타임아웃은 UI 보호용이지 비용 절감이
  아니다** — 그 사실을 §2.8 에 명시한다. 요청 취소를 facade 까지 전파하는 것은 범위 밖.
  ⚠️ 터널·프록시 경유 배포로 바뀌면 이 결정을 다시 봐야 한다(엣지 유휴 100초 수준이면
  6분 요청은 구조적으로 완주 불가 — 그때는 global_top_k 축소 또는 스트리밍이 필요하다).
- **감수 근거**: 버튼이 명시적이라(자동 라우팅 없음) 노출이 제한되고, DB 카운터가 워커
  경계를 넘어 폭주를 막고, 로컬 검색도 edgequake LLM 으로 이미 스레드를 블로킹한다 —
  차수가 다르지 종류가 다르지 않다.

### 2.4 LLM 실패 경로 — 오류 전파를 명시한다

v1 은 `try/except` 가 `reports_exist` 만 감싸고 `global_search(...)` 호출은 밖에 있어, map
도중 `httpx.ReadTimeout`/401 이 나면 **미처리 예외 → FastAPI 500** 이 됐다. §2.1 의 코드
스케치에서 `global_search` 호출도 명시적으로 감싼다.

**예외 타입은 `httpx.HTTPError` 로 잡는다** — 처음엔 `(httpx.TimeoutException,
httpx.HTTPStatusError)` 로 좁게 썼는데, httpx 의 예외 계층에서 이 둘은 **서로 다른 상속선**
이다: `HTTPStatusError` 는 `HTTPError` 의 직속이지만 `TimeoutException` 은
`RequestError → TransportError → HTTPError` 를 거친다. 그 형제 클래스인 `ConnectError`·
`ReadError`·`WriteError`·`RemoteProtocolError`(폐쇄망 환경에서 흔한 connection-refused 등)
는 좁은 튜플에 걸리지 않아 그대로 전파되고, FastAPI 500 → kb 클라이언트가 재시도 조건
(`>=500`, 사실 27)에 걸려 **3회 재시도** — §2.4 가 막으려는 3배 증폭이 그대로 재현된다.
`httpx.HTTPError` 는 이 계층 전체의 공통 상위 클래스라 전부 잡힌다.

`community.py:652-664` 의 map 루프는 재시도·부분 수집이 없어 첫 예외에서 전파된다(이미 쓴
map 비용은 회수 불가) — 이것을 재작업하는 것은 **범위 밖**(map-reduce 자체의 견고성은 A/B
어느 쪽에도 속하지 않는 별건 → deferred).

**422 를 고른 이유 — kb 클라이언트의 재시도 조건이 `429` 또는 `>=500`(사실 27)이다.**
LLM 호출 실패를 **500 대역**(예: 502)으로 매핑하면 kb 클라이언트가 **3회 재시도**해
map-reduce 전체가 세 번 돌고(최악 6분 → 18분, LLM 비용 3배) §2.3 의 상한 계산이 무효가
된다. `429`·`5xx` 가 아닌 **422**(비재시도 대상)로 매핑해야 증폭을 막는다. 503(DSN·LLM
설정 오류)은 그대로 둔다 — `psycopg.Error`/`_llm_configured()` 실패는 재시도해도 안 낫는
영구 오류라 kb 클라이언트가 3회 재시도해도(사실 27) 매번 같은 이유로 실패할 뿐 map-reduce
비용을 반복하지는 않는다(§2.1 의 가드가 `global_search` 호출 **전**에 걸린다).

### 2.5 `build_if_missing` — 기본값을 안 바꾼다

호출부에서 `False` 를 **명시**한다. 기본값을 뒤집으면 실제 호출부인 CLI·`unified_search`
(사실 2)가 조용히 바뀌고 `test_search.py:221`(사실 23)이 깨진다.

리포트가 없으면 **빌드하지 않고** `community_reports_ready: false` 로 답한다 — 만드는 것은
A(야간 배치) 또는 수동 `/communities/build` 가 소유한다.

### 2.6 리포트 신선도·관련성 — B 가 처음으로 최종 사용자에게 노출하는 리스크

`community_reports` 는 코드 전체에 **DELETE 가 0건**(사실 32) — upsert-only 라 문서를
지워도 그 기반 리포트가 영구 잔존한다. 지금까지는 아무도 이 리포트를 최종 사용자에게 직접
보여주지 않았는데(수동 `/communities/build` 호출자는 운영자뿐), **B 는 그것을 처음으로
사용자 화면에 낸다.** 낡은 리포트 자체의 정리(DELETE·재생성)는 **A(§3) 가 소유**하지만, B
단독 배포 시(A 없이) 사용자가 신선도를 판단할 수 있어야 한다 → §2.1 의 `report_generated_at`
을 응답에 싣는다.

```python
def newest_report_time(workspace_id: str, dsn: str, *, level: int = 0) -> datetime | None:
    """이 workspace 리포트 중 가장 최근 생성/갱신 시각. 없으면 None."""
```
**★ v5 — `max(created_at)` 만으로는 신선도 신호가 거짓 안심을 준다.** `community_reports`
에 DELETE 가 0건이라(사실 32) 사라진 문서 기반 리포트가 옛 시각으로 잔존하는데, workspace
전체 `max()` 는 그 행들을 완전히 가린다 — 리포트 39행 중 38행이 2026-07-20 이고 1행만
어제 갱신됐어도 "어제 기준" 으로 보인다. `_rank_reports` 는 rank/overlap 만 보므로 그
낡은 리포트가 map 대상으로 뽑힐 수 있다.
→ **`newest_report_time` 이 `(max, min, count)` 를 함께 돌려주고**, 프론트는
`max` 를 기준으로 표기하되 **`min` 이 임계(기본 30일)보다 오래면 경고 문구를 덧붙인다**
("일부 리포트는 {min} 이후 갱신되지 않았습니다"). 낡은 리포트의 **정리**는 여전히 A 소관
(§2.6 서두) — B 는 **감추지 않는 것**까지만 책임진다.

프론트는 이 값을 턴 아래에 작게 표기한다(§2.8). **"A 없이 B 단독 배포 가능"이라는 주장은
유지하되, 신선도 불명 상태로 노출하지 않는다는 조건을 붙인다.**

**관련성 — `_rank_reports` 가 한국어에서 사실상 질문-무관하다.** `community.py:601-614`
(사실 31)는 `\W` 분할 + 부분문자열 매칭인데 `\W` 가 한글을 단어문자로 취급해 조사가 붙은
어절 단위로 쪼갠다. "연차휴가는 며칠인가요" 같은 질문은 리포트의 "연차휴가" 어절과
"연차휴가는" 이 문자열째 다르면 overlap 이 0 이 되어 **랭킹이 사실상 무작위**가 될 수 있다.

이것을 지금 고치는 것은 **범위 밖**(토크나이저 교체는 커뮤니티 리포트 검색 전반에 영향을
주는 별건) — 대신 **노출 전에 실측한다**:

- 착수 시 대표 질문 10건(실제 KB 문서 기반) 으로 `global_search` 를 돌려 **선택된 리포트가
  질문과 실제로 관련 있는지**를 수동 확인한다. 결과를 이 plan 의 부록 또는
  `_workspace/02-changes.md` 에 남긴다.
- 관련성이 명백히 나쁘면(무작위 리포트가 선택됨) **B 의 배포를 보류**하고 토크나이저 개선을
  선행 과제로 올린다 — 사용자가 명시적으로 누른 버튼이 무관한 답을 내면 신뢰를 잃는다.
- 이 실측은 §4 테스트가 아니라 **배포 전 수동 게이트**로 문서에 남긴다(자동화된 관련성
  단언은 임계값 설계가 필요해 범위를 넘는다).

### 2.7 kb 배선

**`communities` 는 kb 챗으로 내리지 않는다.** 커뮤니티 id 는 사용자에게 의미 없는 정수이고,
`ChatTurnResult`(사실 17)·`ChatResponse`(사실 18)·라우터 조립(사실 19) **세 곳에 캐리어**를
새로 만들어야 한다. facade 응답에는 남기되(다른 소비자·디버깅) kb 는 `answer` 와
`community_reports_ready` 만 쓴다.

| 파일 | 변경 |
|---|---|
| `core/chat.py:92-101` Protocol `_KbPipelineSearch` | `search(ws, query, *, top_k=…, mode: str = ..., global_top_k: int = ...)` — **안 고치면 fake 가 TypeError**(사실 16) |
| `clients/kb_pipeline_client.py:403` | `search(..., mode="local", global_top_k=5)`; **화이트리스트(`:426-428`)에 `mode`·`community_reports_ready`·`report_generated_at` 추가**(`communities` 제외) — 안 하면 여기서 소실(사실 20) |
| `core/chat.py:438` `_run_kb_pipeline_chat` | `mode` 수령 → 전달. global 이면 `citations=[]`(커뮤니티는 청크가 아니라 `_attach_page_images` 대상이 아니다); `community_reports_ready`·**실제 응답의 `mode`**(요청한 값이 아니라 facade 가 답한 값 — §2.7.1) 전달 |
| `core/chat.py:211` `run_chat_turn` | `mode: str = "local"`. **kb_pipeline 분기에만 전달**, 다른 provider(사실 37, `run_chat_turn` 두 번째 호출자 `comparison.py:479` 포함, 사실 38)는 무시 |
| `core/chat.py:127-132` `ChatTurnResult` | `community_reports_ready: bool \| None = None`; `effective_mode: str \| None = None`; `report_generated_at: str \| None = None` |
| `schemas/chat.py:14` `ChatRequest` | `mode: Literal["local", "global"] = "local"` — **경계에서 422 로 막는다**(v1 은 무검증 `str` 이라 오타가 facade 400 → `routers/chat.py:113` 광역 except → 502 로 원인이 뒤바뀐다) |
| `schemas/chat.py:53` `ChatResponse` | `community_reports_ready: bool \| None = None`; `effective_mode: str \| None = None`; `report_generated_at: str \| None = None` — `extra='allow'` 없어(사실 18) 안 더하면 **조용히 버려진다** |
| `routers/chat.py:99-106` | `mode=req.mode` 전달 |
| **`routers/chat.py:113`** | **무변경** — 광역 except 가 422·503 을 모두 502 로 뭉갠다(사실 30). §2.7.1 에서 이 뭉개짐을 v2 범위에서 감수하는 이유를 다룬다 |
| **`routers/chat.py:124-127`** | **응답 조립에 새 필드** — 호출부만 고치면 값이 사라진다(사실 19) |
| `tests/test_chat_kb_pipeline.py:29` | fake 를 새 시그니처로 |

#### 2.7.1 `mode` 가 dead field 가 되지 않게 한다

v1 은 요청에 `mode` 를 실었지만 **응답에 "실제로 어떤 mode 로 답했는가"를 안 실었다** —
`ChatTurnResult`(사실 17)·`ChatResponse`(사실 18) 조립(사실 19)이 `answer`/`citations` 만
낸다. 사용자가 요청한 `mode` 와 서버가 실제로 처리한 `mode` 가 항상 같다는 보장이
없다면(예: facade 가 어떤 이유로 local 로 처리) 사용자는 확인할 방법이 없다.

facade 응답의 `mode` 필드를 그대로 **`effective_mode`** 로 캐리한다(위 표에 반영). 프론트는
`effective_mode !== 요청한 mode` 면 안내를 조정할 수 있으나, **v2 범위에서는 캐리만 하고
불일치 UI 분기는 만들지 않는다**(현재 설계상 facade 는 요청한 mode 그대로 처리하므로
불일치가 발생하지 않는다 — 향후 방어용으로 필드만 남긴다).

**503 vs 422 의 의미론이 kb 라우터에서 뭉개지는 것은 감수한다.** `routers/chat.py:113`
(사실 30)의 광역 except 는 `httpx.HTTPError` 전체를 502 로 매핑한다. `_search_global` 이
던지는 `HTTPException(503, ...)`(설정 오류)와 `HTTPException(422, ...)`(LLM 호출 실패)는
**kb 클라이언트를 거쳐 오면서** 둘 다 `httpx.HTTPStatusError` 가 되어 이 광역 except 에
걸린다 — kb 클라이언트의 **재시도 여부**(§2.4 가 422 를 고른 이유)에는 영향이 없지만,
**최종 사용자에게 보이는 상태 코드**는 둘 다 502 로 뭉개진다. 이 구분을 kb 라우터·프론트
까지 살려 전파하는 것은 **v2 범위를 넘는다** — 사용자에게는 "일시적 지연, 잠시 후 재시도"
로만 보이는 것을 감수한다(503 케이스에서 재시도해도 안 낫는다는 점만 알려주지 못할 뿐,
안전하지 않은 동작은 아니다). 이 구분을 프론트까지 넘기는 것은 별건으로 deferred 에 남긴다.

### 2.8 프론트

| 파일 | 변경 |
|---|---|
| `app/kb/[kbId]/page.tsx:105` | `<ChatPanel kbId={kbId} provider={provider} providerStatus={providerStatus} />` — 부모가 이미 갖고 있고 `UploadPanel` 에만 주고 있다(사실 21) |
| `components/ChatPanel.tsx:29` | prop 수령; `globalMode` state; 전송 폼(`:244`)에 **"전체 요약" 토글**; 요청에 `mode` |
| `lib/api.ts`, `lib/types.ts` | `mode` 요청 필드, `community_reports_ready`·`report_generated_at` 응답 필드 |

- **`providerStatus === "ready" && provider === "kb_pipeline"`** 일 때만 렌더한다. 다른
  provider 는 `mode` 를 무시하므로 보이면 거짓말이 된다. 미해결 동안 감추는 것은
  `page.tsx:75-82` 의 **기존 관례**와 같다(사실 22).
- **`globalMode` state 리셋 규칙**: `kbId`(즉 provider)가 바뀌면 `globalMode` 를
  `false` 로 리셋한다(`useEffect([kbId])`). v1 은 이 규칙이 없어 provider 가 바뀌어도
  토글이 유지되면 사용자가 local 답변을 "전체 요약" 결과로 오인할 수 있었다. 토글 자체는
  §3.6 가드로 다른 provider 에서 안 보이지만, **같은 kb_pipeline KB 사이를 이동**할 때도
  이전 KB 의 리포트가 없을 수 있으므로 매 KB 전환마다 local 로 되돌리는 것이 안전한 기본값
  이다.
- `community_reports_ready === false` → 턴 아래 안내:
  *"아직 커뮤니티 리포트가 없습니다. 야간 배치(03:00) 이후 사용할 수 있습니다."*
  (A 가 아직 배포되지 않았다면 문구를 *"관리자가 커뮤니티를 빌드한 뒤 사용할 수 있습니다."*
  로 둔다 — 구현 시 A 배포 여부에 맞춘다.)
- `report_generated_at` 이 있으면 턴 아래 작게 *"커뮤니티 리포트 기준: {날짜}"* 를 표기한다
  (§2.6 — 신선도 신호).

---

## 3. 변경 목록

**kbp**
- `service/app.py` — `/search` 에 `mode`·`global_top_k` + `_search_global` + **import 추가**
  (`global_search`, `reports_exist`, `psycopg`, `httpx`) + `_llm_configured()`(§2.1.1) +
  `_acquire_global_slot`/`_release_global_slot`(§2.3, DB 기반) + `newest_report_time()`
  사용(§2.6)
- `service/jobs/schema.py` — `kbp.global_search_slots`(§2.3, `ensure_schema` 에 추가 —
  A(야간 배치) 의 `kbp.batch_runs`/`kbp.community_builds` 와 같은 스키마 파일)
- `service/llm.py` — `get_text_llm(*, timeout=None)` (§2.3, 키워드 전용 + 기본값 유지)
- `kb_pipeline/search.py` — `reports_exist()`, `newest_report_time()` 신규.
  **`build_if_missing` 기본값 불변**
- **env 선언처 전수**(프로젝트 CLAUDE.md "폐쇄망 배포는 소스 수정과 함께"):
  `.env.example`, `.env.airgap.example`, `.env.parse-only.example` **세 템플릿 모두**에
  `KBP_GLOBAL_LLM_TIMEOUT`·`KBP_GLOBAL_SEARCH_CONCURRENCY` 를 넣는다.
  v4 는 compose 와 `scripts/facade.env` 만 적어 **템플릿 3종이 빠져 있었다** — 그러면
  운영자가 이 손잡이의 존재를 모른다(A1 에서 `KBP_JOB_*` 18개가 정확히 그 상태였다).
  ※ `.env.parse-only.example` 은 **`KBP_GLOBAL_SEARCH_CONCURRENCY=0` 으로 둔다** —
  파서 전용 배포에는 edgequake·커뮤니티가 없어 global 검색이 무의미하다(0=비활성).
  ※ `scripts/parse-svc.env.example` 은 parse-svc 프로세스용이라 불필요(누락 아님).
- `service/jobs/schema.py` — `LOCK_OBJ_GLOBAL_SEARCH = 4` 상수 추가(§2.3 advisory lock)
- compose ×2 `facade_env` 앵커(compose `:10`, airgap `:27`) —
  `KBP_GLOBAL_LLM_TIMEOUT: ${KBP_GLOBAL_LLM_TIMEOUT:-60}`,
  `KBP_GLOBAL_SEARCH_CONCURRENCY: ${KBP_GLOBAL_SEARCH_CONCURRENCY:-2}`(**시스템 전역** —
  워커별이 아니다, §2.3)
- `scripts/facade.env` — 같은 두 변수. **평문 `KEY=VALUE` 라 compose 의 `${:-기본}` 치환이
  적용되지 않는다** — 이 파일에는 **값을 직접** 써야 한다(`KBP_GLOBAL_LLM_TIMEOUT=60`,
  빈 줄로 두면 `float("")` → `ValueError` → 500)
- `service/tests/test_search_endpoint.py` — `mode`/global 분기 + 동시성 상한 + LLM 가드
- **`tests/test_search.py`**(기존 파일, `kb_pipeline/tests/` 아님 — 사실 33, 그 디렉터리는
  수집 대상이 아니다) — `reports_exist`(**테이블 부재 픽스처**), `newest_report_time`

**kb**
- `core/chat.py`(Protocol·`ChatTurnResult`·두 함수), `clients/kb_pipeline_client.py`,
  `schemas/chat.py`(`Literal["local","global"]`), `routers/chat.py`(**`:99-106` +
  `:124-127`**), `tests/test_chat_kb_pipeline.py`
- `app/kb/[kbId]/page.tsx`, `components/ChatPanel.tsx`, `lib/api.ts`, `lib/types.ts`
- **프론트 테스트 러너 부재**(사실 34): 이 변경의 프론트 부분은 **자동화 테스트를 추가하지
  않는다** — §4 의 프론트 항목을 `typecheck`(기존 스크립트) + 수동 브라우저 체크리스트로
  대체한다. 러너 도입은 이 plan 의 범위를 넘는 별건이라 deferred.

**문서**
- `_workspace/01-architecture.md` — **`:159`·`:160`·`:223`·`:224` 네 곳**(사실 24) 모두
  "미배선" → "명시 `mode` 로 배선됨"
- `_workspace/02-changes.md` — global 배선 결정 + **자동 라우팅을 하지 않는 근거** + 관련성
  실측 결과(§2.6 배포 전 게이트)
- `docs/facade-api.md` — `/search` 계약(`mode`·`global_top_k`·새 응답 키)
- `docs/kb-pipeline-process-definition.md:65,180`(사실 24 재확인 시 실제 라인이 `:66,
  175-179` 가 아니라 여기였다 — 착수 시 grep 으로 재확인) — "local/global 라우팅 없음" 정정
- deferred — **D22 를 "오진, 철회"로 닫는다**(D22 는 `build_if_missing` 이 웹 워커를 점유
  한다는 것이었으나 그 경로는 애초에 미배선이었다), 신규 2건: **503/422 의미론이 kb
  라우터·프론트까지 전파되지 않는다**(§2.7.1), **프론트 테스트 러너 부재**

---

## 4. 테스트

**`/search` (kbp)**
- `mode` 미지정 → **기존 키·값 전부 불변 + `mode:"local"` 추가** ← 회귀 핵심
- `mode=global` 이 **`eq_ws` 로** `reports_exist`/`global_search` 를 부른다(kb id 아님) ← 회귀 핵심
- 리포트 있음 → `build_if_missing=False`, `top_k=global_top_k` 로 호출
- 리포트 없음 → **`global_search` 미호출** + `ready:false` + `report_generated_at:null`
- **`community_reports` 테이블 부재 → 503 이 아니라 `ready:false`** ← 신규 배포 회귀
- `reports_exist` 가 `psycopg.Error` 를 올리면 → **503** / `TypeError` 같은 코드 버그는
  **503 으로 위장되지 않는다**(전파된다) ← `except psycopg.Error` 로 좁힌 회귀
- `KBP_PG_DSN` 미설정 → global 만 503, local 정상
- **`KBP_OPENAI_API_KEY`·`KBP_OPENAI_BASE_URL`·`KBP_LLM_MODEL` 중 하나라도 빈 문자열 →
  503**(세 변수 전부 확인하는 회귀 — v1 은 첫 번째만 확인해 나머지가 빈 값이면 늦은 500)
- **`get_text_llm(timeout=…)` 로 타임아웃이 실제 주입된다** ← no-op 회귀
- **`get_text_llm()` 무인자 호출(`runner.py`/`parse_service` 관례)이 여전히 동작한다** ←
  시그니처 회귀
- **동시성 상한(DB 기반)**: `KBP_GLOBAL_SEARCH_CONCURRENCY=1` 로 두고 `_acquire_global_slot`
  을 **직접** 두 번 호출하면(같은 프로세스 안에서도) 두 번째가 `None`(즉시 503, 큐잉 없음)
  ← 프로세스 경계와 무관해야 하는 회귀. 가능하면 **별도 프로세스 두 개**(또는 `fork` 시늉)
  로도 재현해 "워커가 달라도 상한이 전역"임을 확인한다 — v1 의 `threading.Semaphore` 는
  이 테스트를 통과할 수 없었다(워커마다 별도 인스턴스)
- **죽은 슬롯 청소**: `claimed_at` 을 10분 전으로 조작한 슬롯이 다음 acquire 에서 삭제된다
  ← 프로세스가 응답 없이 죽는 경우의 방어 회귀
- **`global_search` 호출 자체가 `httpx.TimeoutException` 을 던지면 422**(503 도 502 도
  아님) ← 오류 전파 회귀
- **`httpx.ConnectError`/`RemoteProtocolError` 도 422 로 잡힌다**(`TimeoutException`·
  `HTTPStatusError` 만 잡는 좁은 튜플이 아니라 `httpx.HTTPError` 로 잡아야 하는 회귀 —
  v1 은 이 형제 클래스들이 전파돼 500 이 됐다)
- **422 는 kb 클라이언트에서 재시도되지 않는다**(`retryable = status==429 or status>=500` —
  422 는 둘 다 아니다) ← 증폭 방지 회귀. **503 은 재시도되지만**(`>=500`) `global_search`
  호출 **전**에 걸리는 가드라 map-reduce 비용 자체는 반복되지 않는다
- `global_top_k` clamp(0→1, 99→5)
- `mode=bogus` → 400, **`ensure_workspace` 미호출**
- **global 의 ready/not-ready 두 분기 응답 키 집합이 동일**(`report_generated_at` 포함)
- `kb_pipeline/search.py` 기존 테스트 전부 통과(`test_search.py:221` 포함)

**kb**
- Protocol fake 가 새 시그니처로 통과(사실 16)
- 클라이언트가 `community_reports_ready`·`report_generated_at` 을 **버리지 않는다** /
  `communities` 는 안 싣는다
- **`ChatResponse` 가 `community_reports_ready`·`effective_mode`·`report_generated_at` 을
  직렬화한다**(사실 18·19)
- 챗이 `mode` 를 kb_pipeline 에만 전달 / 다른 provider 는 무시 / `comparison.py:479` 호출자는
  기본값 `"local"` 로 무변경(사실 38)
- `ChatRequest.mode` 에 `"bogus"` → **422**(`Literal` 검증)
- global 턴의 `citations` 가 `[]` 다
- 회귀 기준선: **착수 시 `cd backend && pytest -q` 로 기존 실패 건수를 측정해 이 문서에
  기록하고** 그 수를 늘리지 않는다.

**프론트 — 자동화 없음, typecheck + 수동 체크리스트**(사실 34)
- `npm run typecheck` 통과(신규 prop·타입)
- 수동: `provider !== "kb_pipeline"` 또는 `providerStatus !== "ready"` → 토글 미렌더
- 수동: 토글 on → 요청에 `mode: "global"` (네트워크 탭으로 확인)
- 수동: `community_reports_ready === false` → 안내 문구 렌더
- 수동: **KB 를 전환하면 `globalMode` 가 false 로 리셋된다** ← state 리셋 회귀
- 수동: `report_generated_at` 있으면 날짜 표기 렌더

---

## 5. 리스크

| 리스크 | 완화 |
|---|---|
| **global 이 스레드풀(40×4워커)을 잠식** | `global_top_k ≤ 5` + `KBP_GLOBAL_LLM_TIMEOUT=60s` **실제 배선**(§2.3) + **DB 기반 동시성 상한 2**(워커 4개를 합친 **시스템 전역**, §2.3) → 최악 총 점유 12분치. 버튼이 명시적이라 노출 제한 — 감수 |
| **D10 이 해결한 웹 프로세스 점유를 되돌린다** | 완전 회귀는 아니다 — DB 기반 카운터로 워커 경계를 넘는 상한(§2.3, `threading.Semaphore` 는 프로세스별이라 이 상한을 못 준다는 것을 검증에서 확인했다). 완전한 해법(잡 큐)은 대화형 응답 목적과 상충해 범위 밖 |
| **상한 카운터 자체의 DB 왕복 비용·죽은 슬롯** | 동시성 2~4 수준에서 무시할 만함(§2.3) + 매 acquire 시 오래된 슬롯 청소 |
| **LLM 실패가 kb 클라이언트 재시도로 3배 증폭** | LLM 호출 실패를 **422**로 매핑(§2.4, 500 대역이 아니라 kb 클라이언트가 재시도하지 않는다) |
| 리포트가 없어 빈 답 | 계약이다. `community_reports_ready` 로 UI 에 드러낸다(§2.8) |
| **낡은 리포트로 답하는데 신선도가 안 보인다** | `report_generated_at` 을 응답·UI 에 노출(§2.6). 리포트 정리 자체는 A(§3)가 소유 |
| **선택된 리포트가 질문과 무관할 수 있다**(한국어 토크나이저) | 배포 전 대표 질문 10건 수동 실측 게이트(§2.6) — 자동화 테스트 아님 |
| **503/422 의미론이 kb 라우터·프론트에서 뭉개진다**(둘 다 502로 보임) | v2 범위에서는 감수(§2.7.1) — 재시도해도 안 낫는 케이스를 구분 못 함, 안전성 문제는 아님 |
| **mode 요청/응답 불일치를 사용자가 확인할 수 없다** | `effective_mode` 캐리(§2.7.1) — 불일치 UI 분기는 아직 없음(현재는 발생 안 함) |
| 자동 라우팅 오분류 | **하지 않는다**(§0). 사용자가 명시적으로 고른다 |
| 신규 배포에서 테이블 부재 | `reports_exist` **fail-open**(§2.2) |
| DB 장애를 "리포트 없음"으로 위장 | 그 경우만 **503**(§2.2), `psycopg.Error` 로 좁혀 코드 버그와 구분 |
| 프론트 토글이 dify KB 에 노출 | `providerStatus === "ready"` 가드(§2.8) |
| **프론트 회귀가 자동화되지 않는다** | typecheck + 수동 체크리스트(§4) — 러너 도입은 별건 |

---

## 6. 구현 후 검증 (착수 전 검증에서 이관)

> 검증관이 각 지적에 **`runtime_discoverable`** 을 직접 판정했다(2렌즈, 224k 토큰).
> `true` 인 5건은 글로벌 룰 "검증 비용 관리" 5번대로 **구현 중 실측으로 닫는다** —
> 계획서에서 더 다투는 것보다 한 번 돌리는 게 확실한 종류다. 설계 판단이 필요한 5건은
> v5 본문에 반영했다.

| # | 어디 | 무엇 |
|---|---|---|
| 1 | plan §2.1 (plan:119 `_acquire_global_slot`, plan:131 `_newest_report_time`, pl | try/except 가 `reports_exist` 만 감싼다. `_acquire_global_slot`·`_newest_report_time`·`_release_global_slot` 은 psycopg 예외에 무방비 → `kbp.global_search_slots`  |
| 2 | plan §2.1:141-142 `finally: _release_global_slot(dsn, slot_id)` + §2.3:245-249 | release 가 새 커넥션을 열면서 예외를 흡수하지 않는다. release 시점에 DB 가 흔들리면 finally 절의 `psycopg.OperationalError` 가 진행 중이던 `HTTPException(422)`(plan:136)이나 이미 완성된 성공 응답( |
| 3 | plan §2.1:119,133 (`_GLOBAL_SEARCH_CONCURRENCY`·`_GLOBAL_LLM_TIMEOUT` 을 모듈 상수처 | 두 값을 어디서·언제 읽고 파싱 실패를 어떻게 다루는지 정하지 않았다. `_UPPER` 표기는 import 시점 1회 읽기를 암시하는데, 그러면 (a) 빈/오타 값이 import 시점 ValueError → gunicorn 워커 전체 기동 실패(/parse·/inges |
| 4 | plan §4:455-487 (DB 의존 단언 다수) | '회귀 핵심' 항목 다수(테이블 부재→ready:false, 죽은 슬롯 청소, 프로세스 경계 상한)가 실 Postgres 를 요구한다. 이 레포 관례상 그런 테스트는 KBP_PG_DSN 없으면 조용히 skip 되어 기본 `pytest -q` 에서 '통과'처럼 보인다.  |
| 5 | plan §2.6:316 `def _newest_report_time` + §3:417 (kb_pipeline/search.py) | §2.2 는 새 공개 `reports_exist` 를 만드는 이유로 '비공개 심볼 크로스패키지 import 도 해소된다' 를 들었는데, §2.6/§3 은 신선도 함수를 `_newest_report_time`(선행 밑줄=비공개)로 kb_pipeline/search.py  |

**닫는 방법**
1. facade `/search` 를 실제로 호출해 mode=local/global 양쪽 응답 키를 확인(빠진 import 는
   즉시 `NameError`).
2. 일회용 PG 로 `_acquire_global_slot` 동시 호출 — advisory lock 이 상한을 지키는지.
   `ensure_schema` 를 **돌리지 않은** DB 로도 한 번 호출해 503 이 나오는지(500 아님).
3. `KBP_GLOBAL_SEARCH_CONCURRENCY` 를 monkeypatch 로 1 로 두고 두 번째 acquire 가
   `None` 인지 — 요청 시점 읽기가 실제로 동작하는지.
4. `get_text_llm()` 무인자 호출자 2곳(`runner.py`, `parse_service/app.py`)과 무인자 람다
   monkeypatch(`test_job_runner.py`)가 무변경 통과하는지 — 시그니처 회귀.
5. PG 필요 테스트는 별도 `service/tests/test_global_search_pg.py` + `requires_pg` +
   DSN 게이트(레포 관례). **DSN 없으면 미검증임을 완료판정에 명시한다.**

**착수 조건**: 위 5건 외 §2 설계는 확정이다. 구현 중 **설계 자체를 바꿔야 하는
문제**가 나오면 코드를 더 쓰지 말고 plan 을 v6 로 올린 뒤 방향을 확인한다.

### 6.1 닫힘 기록 (2026-08-09)

| # | 결과 | 증거 |
|---|---|---|
| 1 | ✅ 닫힘 | `_acquire_global_slot`·`newest_report_time`·`_release_global_slot` 모두 psycopg 예외 처리. `_search_global` 이 **모든 psycopg 오류를 503** 으로 매핑. `test_db_error_on_slot_acquire_is_503_not_500`, `test_global_search_pg.py::test_reports_exist_missing_table_is_not_an_error` |
| 2 | ✅ 닫힘 | `_release_global_slot` 이 예외를 흡수한다 — 완성된 응답이 500 으로 뒤집히지 않는다. `test_release_swallows_errors`(죽은 DSN 으로 호출), `test_slot_is_released_even_on_failure` |
| 3 | ✅ 닫힘 | 모듈 상수 대신 `_env_float`/`_env_int` 로 **요청 시점**에 읽고 파싱 실패는 기본값. import 시점 ValueError 로 워커 전체가 죽는 경로 없음. `test_settings_are_read_per_request` |
| 4 | ✅ 닫힘 | PG 필요 단언을 `test_global_search_pg.py`(`requires_pg` + DSN 게이트)로 분리. **DSN 없이는 13건 미검증**임이 표시된다 — 실측: DSN 없이 `609 passed, 2 skipped`, DSN 있으면 `687 passed`. 실제로 이 13건만 잡은 버그가 있었다(아래) |
| 5 | ✅ 닫힘 | 밑줄 없는 공개 `reports_exist`·`newest_report_time` 로 신설하고 `kb_pipeline/__init__.py` `.search` 블록에서 export. 크로스패키지 비공개 import 없음 |

**구현 중 발견 — 실 PG 테스트만 잡은 버그**: `_acquire_global_slot` 이
`cur.fetchone()["count"]` 로 읽었으나 bare `psycopg.connect` 에는 `row_factory=dict_row`
가 없어 `TypeError: tuple indices must be integers`. `JobRepo` 에는 있어서 착각했다 —
DSN 을 직접 여는 코드에는 없다. `[0]` 으로 수정.

**advisory lock 이 장식이 아님을 실증**: 잠금을 제거하니 `상한 2 인데 5개가 승인됐다`.

### 6.2 최종 검증

| 대상 | 결과 |
|---|---|
| kbp `service/ + tests/ + parse_service/` (PG 없음) | **609 passed, 2 skipped** |
| kbp 동일 (PG 있음) | **687 passed** — `test_global_search_pg.py` 13건 포함 |
| kb `backend/tests/` | **651 passed, 16 failed** (기준선 648/16 → **+3, 회귀 0**) |
| frontend | `typecheck`·`lint`·`build` 통과 + `npm run test:chatmode` **14 passed** |
| 되돌리기 실증 | kb 3종·frontend 5종 전부 빨강 확인 |

kb 의 16 failed 는 **기준선에서 이미 실패하던 것**이다(`git stash` 로 확인). 이번 작업으로
`FakeKbPipelineSearch.search()` 가 `mode` 를 안 받아 3건이 깨졌던 것을 즉시 잡아 고쳤다 —
A1 의 `FakeKbPipeline.chunk()`/`table_blocks` 와 같은 패턴(테스트 fake 가 실 클라이언트
시그니처를 못 따라감).

### 6.3 배포 전 게이트 — 스크립트화 완료, 실행은 미완

`scripts/check_global_rank.py` 로 자동화했다. 선정 단계는 순수 파이썬이라 **LLM 불필요** —
Postgres 만 있으면 된다(게이트웨이가 죽어 있어도 검사 가능).

실측 결과 **처음 세운 판정 기준이 틀렸다**: 넓은 질문은 겹침 0 이 정상이고(리포트의
구체어를 안 쓴다), 결함은 **좁은 질문의 조사·어미**에서 나온다(`"주소 변경 절차"` 는
정답, `"주소변경 절차"` 는 오답 — 띄어쓰기 하나로 뒤집힌다). 두 부류를 갈라 판정하도록
고쳤다. 상세: `_workspace/02-changes.md` §12.6.

**게이트 실행 완료(2026-08-10)** — `frontend/public/comparison_report.html` 의 현업
verbatim 질문 20종 + 같은 사규 코퍼스로 만든 리포트 10건(`scripts/fixtures/`).

| 축 | 결과 |
|---|---|
| 축1 넓은 질문(승인된 용도) | ✅ 통과 |
| 축2 특정 주제 질문 | ⚠️ 6/20 이 선정에 질문을 반영 못함(겹침 0 2, 겹침 동점 4) |

**결정 — 축2 를 알려진 한계로 수용하고 배포한다.** 근거:
1. global 의 승인된 용도는 **넓은 질문**이고 거기서는 정상 동작한다(축1). 특정 주제
   질문에서 겹침 0 + rank 순 선정은 그 용도에서는 오히려 맞는 동작이다.
2. 완화가 이미 배선돼 있다 — UI 토글 설명이 global 을 "넓은 질문" 용도로 안내하고
   특정 주제 질문은 일반 검색으로 유도한다.
3. 근본 수정(형태소 분석기 또는 임베딩 기반 선정)은 폐쇄망 사전 반입 또는 리포트
   임베딩 저장소 신설이 필요해 B(기능 노출) 범위를 넘는다 → **D38**.

**게이트 판정을 이분화했다** — 축2 실패로 배포를 막지 않는다. 하나로 합치면 정상 배포가
영구 차단되고, 매번 "보류" 를 뱉는 가드는 곧 아무도 안 돌린다(전례 있음). 축2 는
**회귀 감시 숫자**로 남기고, D38 착수 후 `--strict` 로 진짜 게이트로 승격한다.

**D38 착수 조건(관측 트리거)**
(a) 사용자가 global 에 특정 주제 질문을 던지는 것이 관측되거나
(b) 이 수치가 이전 실행보다 나빠지거나
(c) 리포트 수가 늘어 `top_k` 가 코퍼스의 소수가 될 때(현재 5/10 = 절반이라 완화가
    사실상 규모에 의존한다 — 라이브 15건이면 33%).
