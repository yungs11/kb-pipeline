"""kb-pipeline FastAPI service (:19000).

Endpoints:
  * ``POST   /ingest``         multipart file + form ``workspace_id, doc_id, content_type?``
                               -> ``{document_id, chunk_count, status, detail}`` (BLOCKING;
                               parse-svc 위임 orchestration).
  * ``GET    /chunks``         query ``workspace_id, doc_id`` -> list of chunk rows
  * ``DELETE /doc``            query ``workspace_id, doc_id`` -> 204
  * ``GET    /healthz``        -> ``{status: "ok"}``

파싱은 전부 parse-svc(:19001) 소유 — facade 는 얇은 orchestration 만 한다(Phase 2d:
service/parsing.py·excel_parser_client.py·ingest.py 및 /ingest/submit,/ingest/status 제거).
"""
from __future__ import annotations

import logging
import os
import re

import httpx
import psycopg

from contextlib import asynccontextmanager

from fastapi import (FastAPI, UploadFile, File, Form, Depends,
                     Body, Header, HTTPException, Query, Response)

from service.jobs.api import get_job_blobs as _job_blobs
from service.jobs.api import get_job_repo as _job_repo
from service.jobs.api import router as _jobs_router
from service.edgequake import EdgequakeClient
from service.adaptive_chunk import AdaptiveChunkClient, MODAL_ATOMIC_MARKERS
from service.parse_client import ParseSvcClient
from service.llm import get_text_llm
from service.jobs import schema as _schema
from kb_pipeline.search import global_search, newest_report_time, reports_exist

logger = logging.getLogger("kb_pipeline.service")

#: shinhan_trust default tenant (구 service/ingest.py 에서 이동 — Phase 2d).
_TENANT_ID = "00000000-0000-0000-0000-000000000002"

#: 〈MODAL …〉 open 마커(U+3008/U+3009). /chunk 응답 text 는 표시사본(chunks_meta)으로
#: 저장되므로 마커를 스트립한다(청킹 INPUT 은 마커 유지 — 원자화용).
_MODAL_OPEN_RE = re.compile(r"〈MODAL[^〉]*〉")


def _strip_modal(s: str) -> str:
    """원자경계 마커(〈MODAL…〉·〈/MODAL〉)만 제거, 내부(제목+raw table HTML+각주)는 보존."""
    return _MODAL_OPEN_RE.sub("", s.replace("〈/MODAL〉", ""))


def _safe_basename(name: str) -> str:
    import os
    import unicodedata

    base = os.path.basename((name or "").replace("\\", "/")).replace("\x00", "")
    # 경로 탈출·제어문자는 차단하되 한글·공백·괄호 등 정상 유니코드는 보존한다.
    # 이 이름은 parse-svc가 document_title을 만들 때 사용하므로 ASCII 치환 시
    # 검색 청크 제목이 `____`로 영구 오염된다.
    base = "".join(
        "_" if unicodedata.category(char).startswith("C") else char
        for char in base
    ) or "upload"
    if base in {".", ".."}:
        base = "upload"
    return "_" + base if base.startswith(".") else base

#: Startup visibility only. The gate itself reads the env **per request** (below) —
#: 모듈 스코프에 고정하면 값을 바꾸는 유일한 방법이 ``importlib.reload`` 인데, reload 는
#: ``app`` 객체를 갈아치워 다른 테스트 모듈이 든 참조를 무효화한다(실제로 그렇게 깨졌다:
#: 오버라이드가 옛 app 에 걸려 단위 테스트가 진짜 parse-svc·MinIO 를 때렸다).
_FACADE_KEY = os.environ.get("KBP_FACADE_KEY")

if not (_FACADE_KEY or "").strip():
    # 빈 문자열도 경고 대상이다 — compose 가 `${KBP_FACADE_KEY:-}` 로 빈 값을 주입하면
    # 게이트가 조용히 꺼진 채 뜬다. 폐쇄망은 facade 가 호스트 포트로 노출되므로 그 상태가
    # 곧 무인증 적재·삭제다. 배포 전 차단은 scripts/airgap/verify-bundle.sh 가 한다.
    logger.warning(
        "KBP_FACADE_KEY is %s — the facade X-Facade-Key gate is DISABLED "
        "(dev mode). Set KBP_FACADE_KEY in production to lock down stateful "
        "endpoints (/search, /insert, /insert/status, /ingest, /chunks, /doc, "
        "/communities/build, /jobs, /gate, /objects).",
        "blank" if _FACADE_KEY is not None else "unset",
    )


def require_facade_key(x_facade_key: str | None = Header(default=None)):
    """Reject requests lacking a valid ``X-Facade-Key`` header (vs env key).

    키는 **요청 시점에** 읽는다. 모듈 스코프에 고정하면 값을 바꾸려면 모듈을 reload 해야
    하고, reload 는 ``app`` 객체를 새로 만들어 이미 그 객체를 import 해 둔 다른 테스트
    모듈의 ``dependency_overrides`` 를 통째로 무효화한다.

    ``KBP_FACADE_KEY`` 미설정이면 no-op(dev). 레거시 ``/parse``·``/chunk`` 는 stateless
    라 Phase 1 동안 게이트 대상이 아니다(신규 ``/jobs/*`` 는 대상).
    """
    key = os.environ.get("KBP_FACADE_KEY")
    if not (key or "").strip():
        # gate disabled (dev). 빈 문자열·공백뿐인 값도 미설정과 동일 취급한다 —
        # compose 가 `${KBP_FACADE_KEY:-}` 로 빈 값을 주입하거나 .env 에 공백이 섞이면,
        # 그걸 진짜 키로 보는 순간 게이트 대상 전 경로가 401 이 된다(스택 전면 정지).
        # 값을 strip 해서 쓰지는 않는다 — 소비자는 자기 env 값을 그대로 보내므로
        # 양쪽이 같은 문자열이어야 한다.
        return
    if x_facade_key != key:
        raise HTTPException(status_code=401, detail="invalid or missing X-Facade-Key")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """DB 스키마·버킷 준비. **모듈 import 시점이 아니라 여기서** 한다.

    import 시점에 하면 테스트 수집만으로 DB 접속을 시도한다. postgres 가 늦게 뜨는 첫
    기동은 ``ensure_schema`` 의 백오프(≥120s)가 흡수하고, 그래도 실패하면 컨테이너
    ``restart: unless-stopped`` 가 자가치유한다.
    """
    dsn = os.environ.get("KBP_PG_DSN")
    if dsn:
        try:
            from service.jobs.blobs import JobBlobStore
            from service.jobs.schema import ensure_schema

            ensure_schema(dsn)
            JobBlobStore.from_env().check_bucket()
        except Exception:  # noqa: BLE001 - 로그를 남기고 기동은 계속(가시성 우선)
            logger.exception("job queue bootstrap failed; /jobs/* will error")
    else:
        logger.warning("KBP_PG_DSN unset — job queue disabled")
    yield


app = FastAPI(title="kb-pipeline", lifespan=lifespan)


def get_edgequake():
    return EdgequakeClient(os.environ.get("KBP_EDGEQUAKE_URL", "http://localhost:8081"))


def get_adaptive_chunk():
    return AdaptiveChunkClient(os.environ.get("KBP_ADAPTIVE_CHUNK_URL", "http://localhost:18060"))


def get_doc_guard():
    """doc_guard 클라이언트. 소비자는 이 주소를 알 필요가 없다(§0)."""
    from service.doc_guard import get_doc_guard as _factory

    return _factory()


def get_parse_client():
    # Multi-table PDFs make parse-svc call the modal LLM once per table (sequential),
    # so a 4-table doc can take ~400s+. Default the read timeout high (1800s) and allow
    # env override so the facade does not ReadTimeout before parse-svc finishes.
    return ParseSvcClient(
        os.environ.get("KBP_PARSE_SVC_URL", "http://localhost:19001"),
        timeout=float(os.environ.get("KBP_PARSE_SVC_TIMEOUT", "1800")),
    )


# ── 잡 큐 배선 ─────────────────────────────────────────────────────────────
#
# 레거시 4경로는 **응답 계약을 유지**하되 내부적으로 잡을 경유한다(설계 §0.2). 소비자
# kb 는 `raise_for_status()` 후 `body.get("enriched_content") or ""` 라 202 를 예외 없이
# 삼켜 빈 문서를 적재하기 때문이다. 유량제어는 이 경유만으로 이미 확보된다 —
# 다운스트림 호출이 worker 슬롯 안에서만 일어난다.


def _resolve(dep):
    """의존성 팩토리를 **오버라이드를 존중해서** 호출한다.

    `app.dependency_overrides` 는 FastAPI 가 `Depends(dep)` 를 해석할 때만 적용된다.
    함수를 그대로 넘기면 오버라이드가 무시돼, 테스트가 fake 대신 진짜 parse-svc 를
    때린다(실제로 그렇게 깨졌다).

    runner 에 `Depends(get_parse_client)` 3개를 직접 걸지 않는 이유는, 그러면 레거시
    요청마다 안 쓰는 httpx 클라이언트 2개가 함께 생성되기 때문이다. 지연 팩토리로
    두면 그 kind 가 실제로 쓰는 것 하나만 만들어진다.
    """
    return app.dependency_overrides.get(dep, dep)()


def _job_runner(repo=Depends(_job_repo), blobs=Depends(_job_blobs)):
    from service.jobs.runner import JobRunner

    return JobRunner(repo=repo, blobs=blobs,
                     parse_factory=lambda: _resolve(get_parse_client),
                     chunk_factory=lambda: _resolve(get_adaptive_chunk),
                     eq_factory=lambda: _resolve(get_edgequake),
                     # 커뮤니티 빌더는 LLM·DB 를 직접 잡으므로 테스트가 갈아끼울 수
                     # 있어야 한다. 프로덕션에서는 None → runner 가 실물을 import 한다.
                     community_builder=getattr(app.state, "job_community_builder", None))


def _legacy_job(repo, blobs, runner, *, kind, payload, file_bytes=None,
                workspace_key=None):
    """제출 → 완료까지 대기 → **현행과 동일한 본문**을 반환한다.

    waiter permit 을 **잡 생성 전에** 잡는다. 제출-후-거절이면 worker 는 그 잡을 정상
    실행하는데 kb 는 5xx 재시도로 두 번째 잡을 만든다(멱등키 없음 — D1).
    """
    from service.jobs import api as jobs_api

    with jobs_api._Permit():
        job_id = jobs_api.submit_job(repo, blobs, kind=kind, payload=payload,
                                     file_bytes=file_bytes,
                                     workspace_key=workspace_key, legacy=True)
        row = jobs_api.wait_for_job(
            repo, blobs, job_id,
            timeout=float(os.environ.get("KBP_JOB_LEGACY_WAIT_SECONDS", "3300")),
            runner=runner, inline=bool(getattr(app.state, "job_inline", False)),
        )
    if row["status"] == "succeeded":
        return jobs_api.result_body(blobs, row)
    if row["status"] == "canceled":
        raise HTTPException(status_code=409, detail="job canceled")
    # 현행에서도 다운스트림 예외는 500 으로 샜다 — 계약 유지.
    raise HTTPException(status_code=500, detail=row.get("error") or "job failed")


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


# 신규 잡 경로는 전부 X-Facade-Key 게이트 대상이다(파일 staging·DB 행·worker 시간을
# 소비하므로). 레거시 4경로의 인증 요구는 Phase 1 동안 바뀌지 않는다 — /parse·/chunk 는
# 계속 무인증이다(문서가 "의도적으로 열려 있다"고 명시하고 있고, kb 파사드 키가 미설정인
# 배포에서 게이트를 채우면 kb 가 즉시 401 을 맞는다).
app.include_router(_jobs_router, dependencies=[Depends(require_facade_key)])


@app.post("/drm/unwrap")
def drm_unwrap(file: UploadFile = File(...), filename: str | None = Form(None)):
    """DRM(Fasoo) 해제 — parse-svc `/drm/unwrap` 순수 프록시(잡 큐 미경유).

    job 경유가 아닌 이유: 이건 java/OCR/LLM 같은 무거운 fleet 을 쓰지 않는 가벼운
    호출(~수백ms)이라 worker 슬롯·DB 잡 행을 쓸 이유가 없다 — `/parse`(무거운 파싱)와
    달리 유량제어 대상이 아니다. 호출자(kb-backend)가 provider 분기 이전 신호추출
    단계에서 raw 파일을 못 열 때(DRM 암호화) 재시도용으로 쓴다.
    """
    data = file.file.read()
    name = _safe_basename(filename or file.filename or "upload")
    client = _resolve(get_parse_client)
    try:
        content = client.unwrap_drm(file_bytes=data, filename=name)
    except Exception as e:  # noqa: BLE001 - parse-svc 오류를 502 로 표면화
        raise HTTPException(status_code=502, detail=str(e)) from e
    return Response(content=content, media_type="application/octet-stream")


@app.post("/parse")
def parse(file: UploadFile = File(...), content_type: str | None = Form(None),
                docs_id: str | None = Form(None),
                repo=Depends(_job_repo), blobs=Depends(_job_blobs),
                runner=Depends(_job_runner)):
    """Parse one upload — 전부 parse-svc 위임(Phase 2a: excel 분기는 parse-svc 로 이동).

    parse-svc 가 확장자 라우팅(pdf/excel/ocr/폴백)을 소유하고 ``chunk_needed`` 를 내린다.
    excel(chunk_needed=false) 소비자 호환을 위해 ``chunk_strategy`` 를 재구성한다.

    ``docs_id`` (optional form) is the orchestrator's ``content_hash(file_bytes)[:16]``;
    when present it is forwarded to parse-svc so the page-image MinIO keys agree with
    the keys the orchestrator/UI assemble. The response passes through the additive
    page fields (``docs_id``/``page_count``/``pages``/``page_spans``) unchanged.
    """
    # **동기 def 다.** async 로 두면 아래 대기(_legacy_job → wait_for_job)의 time.sleep 이
    # 이벤트루프를 통째로 막아 같은 프로세스의 /healthz·/jobs/* 가 전부 멎는다.
    # FastAPI 는 동기 def 를 threadpool 에서 돌리므로 루프가 살아 있다. 업로드는
    # SpooledTemporaryFile 을 직접 읽는다(await 불가).
    data = file.file.read()
    payload = {
        "filename": _safe_basename(file.filename or "upload"),
        # 폼 필드가 없으면 파트 헤더로 폴백 — worker 에는 UploadFile 이 없으므로
        # **접수 시점에 확정**해서 payload 에 넣는다.
        "content_type": content_type or file.content_type,
        "docs_id": docs_id,
    }
    return _legacy_job(repo, blobs, runner, kind="parse", payload=payload,
                       file_bytes=data)


@app.post("/chunk")
def chunk(enriched_content: str = Body(..., embed=True),
          doc_name: str = Body("", embed=True),
          page_spans: list | None = Body(None, embed=True),
          pages: list | None = Body(None, embed=True),
          table_blocks: list | None = Body(None, embed=True),
          methods: list | None = Body(None, embed=True),
          skip_scoring: bool = Body(False, embed=True),
          llm_regex_pattern: str | None = Body(None, embed=True),
          repo=Depends(_job_repo), blobs=Depends(_job_blobs),
          runner=Depends(_job_runner)):
    """Chunk enriched content via the adaptive_chunk hub (hidden) and normalize.

    Value added (R5, not a bare forward):
      * forwards the modal markers as ``atomic_markers`` so each 〈MODAL…〈/MODAL〉
        span stays a single atomic chunk;
      * normalizes the hub's R1 chunk schema (``chunk_text``/``chunk_pages``) into
        the facade contract (``text``/``pages``), dropping internal fields;
      * surfaces the real selection rationale (method_selected/scores/
        methods_compared) for the UI's "why this chunker" card.

    ``page_spans`` (``[{page_number, char_start, char_end}]``) and the optional
    ``pages`` (``[{page_number, markdown}]``) are additive body fields forwarded to
    adaptive so each chunk gets a ``chunk_pages`` attribution. The R1
    ``chunk_pages``→``pages`` normalization (below) is unchanged.

    ``methods``/``skip_scoring``/``llm_regex_pattern`` are the chunk-method
    selection passthrough (all optional). They are forwarded to the hub's
    ``options`` (validation/semantics owned by adaptive_chunk). When unspecified
    (``methods=None``, ``skip_scoring=False``, ``llm_regex_pattern=None``) the hub
    runs its default auto behavior (every method competes, then scored/selected) —
    byte-identical to the legacy request (regression).
    """
    # table_blocks(facade contract) → adaptive 는 blocks 로 이름이 바뀐다. 변환·정규화·
    # 마커 스트립은 전부 runner 가 한다(현행과 동일한 본문을 돌려준다).
    payload = {"enriched_content": enriched_content, "doc_name": doc_name,
               "page_spans": page_spans, "pages": pages, "table_blocks": table_blocks,
               "methods": methods, "skip_scoring": skip_scoring,
               "llm_regex_pattern": llm_regex_pattern}
    return _legacy_job(repo, blobs, runner, kind="chunk", payload=payload)


# ── global 검색 (B) ────────────────────────────────────────────────────────
#
# global 검색은 커뮤니티 리포트 map N + reduce 1 의 **순차 LLM** 이라 요청 하나가 분 단위로
# 스레드를 점유한다. D10 이 /communities/build 를 잡 큐로 옮긴 이유 중 하나가 정확히
# "facade 웹 프로세스 점유" 였는데, 이 경로는 동기 함수라 잡 큐의 상한이 적용되지 않는다.
# 완전히 잡 큐로 옮기면 "즉시 답하는 대화형 검색" 이라는 목적이 깨지므로 **동시 실행 수를
# 상한**한다.

def _env_float(name: str, default: float) -> float:
    """파싱 실패는 기본값 + warning(레포 관례). 설정 오타가 500 이나 기동 실패를 만들지 않는다."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("%s=%r 파싱 실패 — 기본값 %s 사용", name, raw, default)
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("%s=%r 파싱 실패 — 기본값 %s 사용", name, raw, default)
        return default


# ★ 설정은 **요청 시점에** 읽는다. 모듈 상수로 두면 (a) 빈/오타 값이 import 시점
#   ValueError → gunicorn 워커 전체 기동 실패로 /parse·/ingest 까지 동반 사망하고,
#   (b) 테스트가 monkeypatch.setenv 로 상한을 바꿔 검증할 수 없다.
def _global_llm_timeout() -> float:
    return _env_float("KBP_GLOBAL_LLM_TIMEOUT", 60.0)


def _global_concurrency() -> int:
    return _env_int("KBP_GLOBAL_SEARCH_CONCURRENCY", 2)


def _slot_ttl_seconds() -> float:
    """죽은 슬롯 TTL 을 **LLM 타임아웃에서 유도한다**(하드코딩 금지).

    요청 최악 소요 = (global_top_k 상한 5 + reduce 1) × KBP_GLOBAL_LLM_TIMEOUT.
    TTL 이 그보다 짧으면 **살아있는 요청의 슬롯이 매 acquire 마다 삭제**되고, 뒤이은
    release 는 없는 id 를 조용히 지워 성공하므로 **상한이 사실상 사라진다.**
    (기본 60s 면 6분인데 10분 하드코딩이 겨우 맞았을 뿐이고, KBP_LLM_TIMEOUT 관례대로
    300 을 넣으면 최악 30분이 되어 즉시 깨진다.) 여유배수 2 는 LLM 외 시간을 감싼다.
    """
    return (GLOBAL_TOP_K_MAX + 1) * _global_llm_timeout() * 2


#: global_top_k clamp 범위. facade 의 top_k(반환 청크 수)와 달리 global 의 top_k 는
#: **map 대상 = 순차 LLM 호출 수** 다. 그대로 흘리면 기본이 11회 직렬 LLM 이 된다.
GLOBAL_TOP_K_MIN, GLOBAL_TOP_K_MAX = 1, 5


def _llm_configured() -> bool:
    """세 변수를 **모두** 본다.

    compose 양쪽이 셋 다 ``${VAR}``(기본값 없음)로 주입하므로 미설정 시 **빈 문자열**이
    들어온다(KeyError 아님). 그리고 ``os.environ.get(k, default)`` 는 "있는데 빈 값" 에는
    default 를 적용하지 않아, base="" 로 httpx.post("/chat/completions") 를 호출해
    UnsupportedProtocol 로 **뒤늦게 500** 이 난다.
    """
    return all((os.environ.get(k) or "").strip()
               for k in ("KBP_OPENAI_API_KEY", "KBP_OPENAI_BASE_URL", "KBP_LLM_MODEL"))


def _acquire_global_slot(dsn: str, limit: int) -> int | None:
    """점유 수가 limit 미만이면 슬롯을 잡고 id 를, 아니면 None.

    ★ **트랜잭션 전체를 advisory lock 으로 직렬화한다.** SELECT count → INSERT 를 잠금
    없이 하면 커밋 전 INSERT 가 다른 트랜잭션의 count 에 보이지 않아, 워커 4개가 동시에
    누르면 각자 count<limit 을 읽고 각자 INSERT 한다 → **상한 2 가 4로 깨진다**(TOCTOU).
    그러면 threading.Semaphore 를 기각한 이유(프로세스마다 별도 인스턴스)가 새 구현에도
    그대로 남는다. 이 레포는 같은 문제를 claim 에서 이미 이렇게 풀었다(jobs/repo.py).
    락 안에서 다운스트림을 호출하지 않으므로 직렬화 비용이 없다(수 ms).
    """
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            # advisory lock 은 blocking 획득이라 상한이 없으면 무한정 매달린다.
            cur.execute("SET LOCAL lock_timeout = '5s'")
            cur.execute("SET LOCAL statement_timeout = '30s'")
            cur.execute("SELECT pg_advisory_xact_lock(%s, %s)",
                        (_schema.LOCK_CLASSID, _schema.LOCK_OBJ_GLOBAL_SEARCH))
            # 죽은 슬롯 청소(프로세스가 응답 없이 죽는 경우). 매 acquire 마다 함께 돈다.
            cur.execute("DELETE FROM kbp.global_search_slots"
                        " WHERE claimed_at < now() - make_interval(secs => %s)",
                        (_slot_ttl_seconds(),))
            cur.execute("SELECT count(*) FROM kbp.global_search_slots")
            # ★ row_factory 를 지정하지 않았으므로 **tuple** 이다(JobRepo 는
            #   dict_row 를 쓰지만 여기서는 커넥션을 직접 연다). 실 PG 로 돌려야만
            #   드러나는 차이라 requires_pg 테스트가 이걸 잡았다.
            if int(cur.fetchone()[0]) >= limit:
                return None
            cur.execute("INSERT INTO kbp.global_search_slots"
                        " DEFAULT VALUES RETURNING id")
            slot_id = int(cur.fetchone()[0])
            conn.commit()
            return slot_id


def _release_global_slot(dsn: str, slot_id: int) -> None:
    """★ **예외를 삼킨다(로그만).**

    이 호출은 ``finally`` 절에서 새 커넥션을 여는데, 그 시점에 PG 가 흔들리면
    psycopg 예외가 **이미 완성된 성공 응답(분 단위·LLM 여러 회분)이나 HTTPException(422)
    을 500 으로 바꾼다** → kb 클라이언트의 재시도 조건(>=500)에 걸려 map-reduce 를 3회
    반복한다. 422 를 고른 유일한 목적을 자기 finally 절이 되돌리는 셈이다.
    슬롯 회수는 위 TTL 청소가 담당한다.
    """
    try:
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM kbp.global_search_slots WHERE id = %s",
                            (slot_id,))
                conn.commit()
    except Exception:  # noqa: BLE001 — 성공한 응답을 뒤집지 않는다
        logger.error("global 검색 슬롯 반납 실패 id=%s (TTL 청소가 회수한다)", slot_id,
                     exc_info=True)


@app.post("/search", dependencies=[Depends(require_facade_key)])
def search(workspace_id: str = Body(..., embed=True),
           query: str = Body(..., embed=True),
           top_k: int = Body(10, embed=True),
           mode: str = Body("local", embed=True),
           global_top_k: int = Body(5, embed=True),
           eq=Depends(get_edgequake)):
    """Search a workspace via edgequake ``/api/v1/query`` (edgequake hidden).

    Value added (R5): resolves the kb id to the edgequake workspace UUID so the
    retrieval is workspace-scoped (isolation), maps ``top_k`` to edgequake's
    ``max_results``, and normalizes edgequake's ``sources`` into a stable
    ``results`` shape (chunk_id/text/score/document_id) plus the generated answer.

    ``mode`` (B): ``"local"``(기본, 기존 동작 그대로) 또는 ``"global"``(커뮤니티 리포트
    map-reduce). 기본값이 local 이라 **기존 호출자 전원이 무변경**이다. 응답에 ``mode`` 를
    더한다(local 도).
    """
    # ★ 검증을 ensure_workspace **앞**에 둔다 — 뒤에 두면 mode=bogus 요청이 workspace 를
    #   만들고 나서 400 이 된다(부작용을 남기는 잘못된 요청).
    if mode not in ("local", "global"):
        raise HTTPException(status_code=400, detail="mode must be 'local' or 'global'")

    eq_ws = eq.ensure_workspace(workspace_id, name=workspace_id)

    if mode == "global":
        # ★ eq_ws 로 부른다. community_reports.workspace_id 는 eq UUID 인데 facade 가 받는
        #   것은 kb id 다. kb id 를 그대로 넘기면 리포트 조회가 **영구히 0행**이 되어 오류
        #   없이 매번 "야간 배치 이후 사용 가능" 이라는 거짓 안내가 뜬다.
        return _search_global(eq_ws, query, global_top_k)

    res = eq.search(workspace_id=eq_ws, query=query, top_k=top_k)
    results = [
        {
            "chunk_id": src.get("id"),
            "text": src.get("snippet") or "",
            "score": src.get("score"),
            "document_id": src.get("document_id"),
        }
        for src in (res.get("sources") or [])
    ]
    return {"answer": res.get("answer"), "results": results, "mode": "local"}


def _search_global(eq_ws: str, query: str, k: int) -> dict:
    """커뮤니티 리포트 map-reduce. **응답 키 집합은 두 분기(ready/not-ready)에서 동일하다.**

    오류 의미론(§2.2): 이 함수 안의 **모든 DB 호출**은 ``psycopg.Error`` 를 503 으로 바꾼다
    — 500 대역은 어떤 경로로도 내지 않는다. kb 클라이언트가 ``429`` 또는 ``>=500`` 을
    재시도하므로, 500 을 내면 분 단위 map-reduce 가 3회 반복된다.
    """
    dsn = os.environ.get("KBP_PG_DSN")   # 인덱싱 금지 — facade 는 DSN 없이도 뜬다
    if not dsn:
        raise HTTPException(status_code=503,
                            detail="global search unavailable: KBP_PG_DSN unset")
    if not _llm_configured():
        raise HTTPException(status_code=503,
                            detail="global search unavailable: LLM not configured")

    k = max(GLOBAL_TOP_K_MIN, min(GLOBAL_TOP_K_MAX, int(k)))

    # 슬롯 획득도 DB 호출이다 — 테이블 부재(UndefinedTable)나 접속 실패가 500 이 되면 안
    # 된다. facade lifespan 이 ensure_schema 실패를 삼키고 기동을 계속하므로(가시성 우선),
    # **스키마 없이 정상 응답하는 배포가 설계상 존재한다.**
    try:
        slot_id = _acquire_global_slot(dsn, _global_concurrency())
    except psycopg.Error as exc:
        raise HTTPException(
            status_code=503,
            detail=f"global search unavailable: {exc}") from exc
    if slot_id is None:
        # 큐잉하지 않고 즉시 503 — 사용자가 명시적으로 누른 동작이니 "잠깐 기다리라" 보다
        # 즉시 실패 후 재시도가 명확하다. (503 은 kb 재시도 조건에 없다.)
        raise HTTPException(status_code=503,
                            detail="global search busy: too many concurrent requests")
    try:
        try:
            ready = reports_exist(eq_ws, dsn)
            newest, oldest, n_reports = newest_report_time(eq_ws, dsn)
        except psycopg.Error as exc:
            raise HTTPException(
                status_code=503,
                detail=f"global search unavailable: {exc}") from exc

        if not ready:
            return {"answer": None, "results": [], "communities": [],
                    "mode": "global", "community_reports_ready": False,
                    "report_newest_at": None, "report_oldest_at": None,
                    "report_count": 0}

        try:
            out = global_search(query, eq_ws,
                                llm=get_text_llm(timeout=_global_llm_timeout()),
                                dsn=dsn, top_k=k, build_if_missing=False)
        except httpx.HTTPError as exc:
            # ★ httpx.HTTPError 로 잡는다. (TimeoutException, HTTPStatusError) 로 좁게
            #   쓰면 형제인 ConnectError·ReadError·RemoteProtocolError(폐쇄망에서 흔한
            #   connection-refused 등)가 새어 500 이 되고, kb 가 3회 재시도한다.
            # ★ 422 를 고른 이유: kb 재시도 조건이 429 또는 >=500 이라 4xx 는 재시도되지
            #   않는다. 분 단위 map-reduce 를 3배로 만들지 않는다.
            raise HTTPException(
                status_code=422,
                detail=f"global search LLM call failed: {exc}") from exc

        return {"answer": out.get("answer"), "results": [],
                "communities": out.get("sources") or [],
                "mode": "global", "community_reports_ready": True,
                "report_newest_at": newest.isoformat() if newest else None,
                "report_oldest_at": oldest.isoformat() if oldest else None,
                "report_count": n_reports}
    finally:
        _release_global_slot(dsn, slot_id)


@app.post("/insert", dependencies=[Depends(require_facade_key)])
def insert(workspace_id: str = Body(..., embed=True),
           doc_id: str = Body(..., embed=True),
           title: str = Body("", embed=True),
           chunks: list[str] = Body(..., embed=True),
           extract_graph: bool = Body(True, embed=True),
           repo=Depends(_job_repo), blobs=Depends(_job_blobs),
           runner=Depends(_job_runner)):
    """Insert pre-chunked texts into edgequake as a passthrough document.

    Value added (R5, policy ownership): the consumer hands a list of chunk texts
    and never touches edgequake — the facade resolves the kb id to the edgequake
    workspace UUID, joins the chunks with the U+001E passthrough separator, submits
    a passthrough document, and polls to terminal. Returns the stable
    ``{document_id, chunk_count, status}`` contract.
    """
    # 반환 필드(edgequake_workspace_id/entity_count/relationship_count/phases)는
    # runner 가 현행과 동일하게 성형한다.
    payload = {"workspace_id": workspace_id, "doc_id": doc_id, "chunks": chunks,
               "title": title, "extract_graph": extract_graph}
    return _legacy_job(repo, blobs, runner, kind="insert", payload=payload,
                       workspace_key=workspace_id)


@app.get("/insert/status", dependencies=[Depends(require_facade_key)])
def insert_status(workspace_id: str, doc_id: str, eq=Depends(get_edgequake)):
    """Relay the live edgequake phase for a passthrough insert (edgequake hidden).

    ``doc_id`` is the edgequake document_id returned by ``/insert``. Returns
    ``{phase, chunk_count, terminal, succeeded}`` from ``document_phase`` so the
    consumer's UI ticks without knowing edgequake's internal vocabulary.
    """
    eq_ws = eq.ensure_workspace(workspace_id, name=workspace_id)
    ph = eq.document_phase(eq_ws, doc_id)
    return {
        "phase": ph.get("phase"),
        "chunk_count": ph.get("chunk_count"),
        "terminal": ph.get("terminal"),
        "succeeded": ph.get("succeeded"),
    }


@app.post("/ingest", dependencies=[Depends(require_facade_key)])
def ingest(file: UploadFile = File(...), workspace_id: str = Form(...),
                 doc_id: str = Form(...), content_type: str | None = Form(None),
                 repo=Depends(_job_repo), blobs=Depends(_job_blobs),
                 runner=Depends(_job_runner)):
    """End-to-end orchestration (parse→chunk→insert) for one-shot consumers.

    Value added (R5, orchestration ownership): drives the three capabilities in
    order so a consumer that doesn't want phase-by-phase control still gets the
    SAME result as the step-by-step path — including the real chunking selection
    rationale. Returns ``{document_id, chunk_count, status, chunking_selection}``.
    """
    data = file.file.read()   # 동기 def — 이유는 /parse 주석 참조
    payload = {
        # ingest 의 filename 폴백은 parse 와 **다르다** — doc_id 로 떨어진다(현행 유지).
        "filename": _safe_basename(file.filename or doc_id),
        "content_type": content_type or file.content_type,
        "workspace_id": workspace_id, "doc_id": doc_id,
    }
    return _legacy_job(repo, blobs, runner, kind="ingest", payload=payload,
                       file_bytes=data, workspace_key=workspace_id)


@app.get("/chunks", dependencies=[Depends(require_facade_key)])
def chunks(workspace_id: str, doc_id: str, eq=Depends(get_edgequake)):
    eq_ws = eq.ensure_workspace(workspace_id, name=workspace_id)
    return eq.fetch_chunks(eq_ws, doc_id)


@app.delete("/doc", status_code=204, dependencies=[Depends(require_facade_key)])
def delete(workspace_id: str, doc_id: str, eq=Depends(get_edgequake)):
    eq_ws = eq.ensure_workspace(workspace_id, name=workspace_id)
    eq.delete_doc(eq_ws, doc_id)


# ── 게이트 (doc_guard 은닉) ────────────────────────────────────────────────
#
# kb 가 doc_guard 를 직접 찌르던 것을 facade 뒤로 넣는다. 응답은 doc_guard 원형을 그대로
# 통과시킨다 — 소비자가 `result`/`findings`/`customer_message` 를 직접 읽는다.
#
# `/parse` 에 합치지 않는 이유: 소비자가 **파싱 없이** 게이트만 돌리고 싶을 수 있고
# (룰 변경 후 재검증), 산출(parse-svc)과 판정(doc_guard)이 다른 서비스라 판정 룰이
# 바뀔 때마다 재파싱하게 만들면 안 된다.


@app.post("/gate/check-excel", dependencies=[Depends(require_facade_key)])
def gate_check_excel(filename: str = Body(..., embed=True),
                     gate_summary: dict = Body(..., embed=True),
                     dg=Depends(get_doc_guard)):
    """파서-후단 엑셀 게이트 판정. ``gate_summary`` 는 ``/parse`` 응답의 그 필드다."""
    return dg.check_excel(filename=filename, gate_summary=gate_summary)


@app.get("/gate/rules", dependencies=[Depends(require_facade_key)])
def gate_rules(dg=Depends(get_doc_guard)):
    """룰 카탈로그 패스스루 — 소비자 UI 체크박스 구성용."""
    return dg.list_rules()


# ── 오브젝트 (MinIO 은닉) ──────────────────────────────────────────────────
#
# **제어평면만** 여기로 온다(설계 §3.3). 브라우저의 썸네일·인용 이미지 읽기는 계속
# `/obj/*` same-origin 프록시로 간다 — 실측상 검색 1회에 최대 ~4MB 라 facade 를 정적
# 파일 서버로 만들면 잡 접수·`/healthz` 가 스레드를 못 얻는다.
#
# 여기서 얻는 값은 **키 규칙 소유**다. `{docs_id}/original/{name}` 같은 규칙을 지금은
# kb·parse-svc·facade 셋이 각자 알고 있어, 한 곳이 바뀌면 조용히 어긋난다.


def get_object_store():
    from service.objects import ObjectStore

    return ObjectStore.from_env()


def _object_error(exc: Exception) -> HTTPException:
    # 키 규칙 위반은 소비자 잘못이다 — 400 으로 돌려줘야 원인이 보인다.
    return HTTPException(status_code=400, detail=str(exc))


@app.put("/objects/{scope}/{rest:path}", dependencies=[Depends(require_facade_key)])
def object_put(scope: str, rest: str,
               file: UploadFile = File(...),
               content_type: str | None = Form(None),
               store=Depends(get_object_store)):
    """바이트 업로드 → ``{"key": ...}``.

    ``rest`` 는 scope 마다 다르게 쪼갠다 — ``original``/``page`` 는 첫 세그먼트가
    ``doc_id``, 나머지가 이름이다. ``staging`` 은 kb `BlobStore` 계약이 **평평한 키**라
    ``rest`` 전체가 이름이다.
    """
    from service.objects import ObjectStoreError, build_key

    if scope == "staging":
        doc_id, name = "", rest
    else:
        doc_id, _, name = rest.partition("/")
    try:
        key = build_key(scope, doc_id, name)
    except ObjectStoreError as exc:
        raise _object_error(exc) from exc
    data = file.file.read()
    mime = content_type or file.content_type or "application/octet-stream"
    return {"key": store.put(key, data, content_type=mime)}


@app.get("/objects", dependencies=[Depends(require_facade_key)])
def object_get(key: str, store=Depends(get_object_store)):
    """바이트 회수 — **staging 회수용**이지 썸네일 서빙용이 아니다(§3.3)."""
    if not key.strip():
        raise HTTPException(status_code=400, detail="key is required")
    data = store.get(key)
    if data is None:
        raise HTTPException(status_code=404, detail="object not found")
    return Response(content=data, media_type="application/octet-stream")


@app.delete("/objects", dependencies=[Depends(require_facade_key)])
def object_delete(key: str | None = None, prefix: str | None = None,
                  store=Depends(get_object_store)):
    """단건(``key``) 또는 프리픽스(``prefix``) 삭제.

    둘 다 주거나 둘 다 없으면 거부한다 — 어느 쪽이 무시됐는지 모른 채 "지웠다"는
    응답을 받으면 남은 객체가 고아로 쌓인다.
    """
    from service.objects import ObjectStoreError

    if bool(key) == bool(prefix):
        raise HTTPException(status_code=400, detail="pass exactly one of key, prefix")
    if key:
        return {"deleted": 1 if store.delete(key) else 0}
    try:
        return {"deleted": store.delete_prefix(prefix)}
    except ObjectStoreError as exc:
        raise _object_error(exc) from exc


@app.post("/communities/build", status_code=202,
          dependencies=[Depends(require_facade_key)])
def communities_build(workspace_id: str,
                      repo=Depends(_job_repo), blobs=Depends(_job_blobs),
                      eq=Depends(get_edgequake)):
    """커뮤니티 재빌드를 **잡 큐에 넣는다**(D10). 202 + `job_id`.

    예전에는 FastAPI ``BackgroundTask`` 로 돌려 세 가지가 샜다 — 유량제어 밖(버킷 상한
    계산에 안 들어감), facade 웹 프로세스 점유(응답 뒤 같은 워커에서 LLM 장시간 작업),
    흔적 없음(상태·재시도·취소 불가). 이제 `community` kind 로 큐를 탄다.

    **응답 계약 유지**: 기존 소비자가 읽는 `status`·`workspace_id` 를 그대로 둔다.
    `job_id` 는 더한 것이라 옛 소비자를 깨지 않는다.

    멱등키를 workspace 로 잡는다 — 적재마다 디바운스 없이 들어와도(실측 2026-08-05:
    한 배치에 같은 workspace 로 3회) 살아있는 빌드가 있으면 그 잡 id 를 돌려준다.
    """
    from service.jobs import api as jobs_api

    eq_ws = eq.ensure_workspace(workspace_id, name=workspace_id)
    job_id = jobs_api.submit_job(
        repo, blobs, kind="community",
        payload={"workspace_id": workspace_id},
        workspace_key=eq_ws,
        idem_key=f"community:{eq_ws}",
    )
    return {"status": "started", "workspace_id": eq_ws, "job_id": str(job_id)}
