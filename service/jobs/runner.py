"""kind 별 잡 실행 — 다운스트림 호출이 **여기서만** 일어난다.

설계 §4.1/§5.1/§5.2/§6.2.

현행 ``service/app.py`` 핸들러 본문을 그대로 옮긴 것이다. 응답 성형까지 동일해야 한다 —
레거시 4경로가 이 결과를 그대로 돌려주고, ``GET /jobs/{id}/result`` 도 같은 본문을 준다.

다운스트림 클라이언트는 **생성자로 주입**한다. FastAPI DI 밖에서 만들면 기존 테스트의
``app.dependency_overrides`` 가 무효가 되기 때문이다(§6.2).
"""
from __future__ import annotations

import logging
import time
import os
import re
import uuid
from typing import Any, Callable

import httpx

from service.adaptive_chunk import MODAL_ATOMIC_MARKERS
from service.jobs.repo import LeaseLost

log = logging.getLogger("kb_pipeline.service.jobs.runner")

#: shinhan_trust default tenant — 현행 ``app.py`` 의 ``_TENANT_ID`` 와 같은 값.
TENANT_ID = "00000000-0000-0000-0000-000000000002"

_MODAL_OPEN_RE = re.compile(r"〈MODAL[^〉]*〉")


def strip_modal(s: str) -> str:
    """원자경계 마커만 제거, 내부(제목+raw table HTML+각주)는 보존."""
    return _MODAL_OPEN_RE.sub("", s.replace("〈/MODAL〉", ""))


def is_domain_failure(result: Any) -> bool:
    """job 은 succeeded 로 끝나지만 본문이 도메인 실패(parse-svc {"status":"failed"})인가.

    parse/ingest kind 의 "파싱 실패는 잡 실패가 아니다"(§리뷰 B10) 계약 자체는 유지한다
    (HTTP 200 + parse-svc 원본 그대로 반환) — 다만 그 결과를 **명시적 idem_key 로 영구
    캐싱**하면(2026-08-06 실관측: html 문서 하나가 이 상태로 굳어 이후 모든 재파싱 시도가
    같은 빈 enriched_content 를 영원히 돌려받음) 근본 원인을 고쳐도 재현이 안 된다.
    complete() 호출부가 이 값으로 `clear_idem` 을 결정한다.
    """
    return isinstance(result, dict) and result.get("status") == "failed"


class JobFailed(Exception):
    """재시도해도 소용없는 실패 — 즉시 ``failed``(§5.1)."""


class JobRetryable(Exception):
    """일시적 실패 — ``queued`` 로 되돌린다(§5.1)."""


class JobAborted(Exception):
    """lease 를 잃어 부작용 전에 중단했다. 상태를 건드리지 않는다(§3.3)."""


def classify(exc: BaseException) -> Exception:
    """다운스트림 예외를 재시도 가능/불가로 가른다(§5.1).

    5xx·타임아웃·커넥션 오류는 재시도, 4xx 는 즉시 실패다. 4xx 를 재시도하면 같은
    요청으로 같은 거절을 반복할 뿐이다.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code >= 500 or code == 429:
            return JobRetryable(f"downstream {code}: {exc}")
        return JobFailed(f"downstream {code}: {exc}")
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return JobRetryable(f"downstream transport error: {exc}")
    return JobFailed(str(exc))


class JobRunner:
    """잡 하나를 실행해 결과 dict 를 돌려준다.

    ``repo``/``blobs`` 는 참조 잡 결과를 읽고 ``stage`` 를 커밋하는 데 쓴다.
    다운스트림 클라이언트 셋은 ``None`` 이면 env 로 조립한다(현행 팩토리 재사용).
    """

    def __init__(
        self,
        *,
        repo,
        blobs,
        parse_client=None,
        chunk_client=None,
        eq_client=None,
        parse_factory: Callable[[], Any] | None = None,
        chunk_factory: Callable[[], Any] | None = None,
        eq_factory: Callable[[], Any] | None = None,
        community_builder: Callable[..., Any] | None = None,
    ) -> None:
        self.repo = repo
        self.blobs = blobs
        self._parse = parse_client
        self._chunk = chunk_client
        self._eq = eq_client
        self._parse_factory = parse_factory
        self._chunk_factory = chunk_factory
        self._eq_factory = eq_factory
        # 커뮤니티 빌더는 주입 가능해야 테스트가 LLM·DB 없이 돈다.
        self._community_builder = community_builder

    # ── 클라이언트 (지연 조립) ─────────────────────────────────────────────

    @property
    def parse_client(self):
        if self._parse is None:
            from service.app import get_parse_client

            self._parse = (self._parse_factory or get_parse_client)()
        return self._parse

    @property
    def chunk_client(self):
        if self._chunk is None:
            from service.app import get_adaptive_chunk

            self._chunk = (self._chunk_factory or get_adaptive_chunk)()
        return self._chunk

    @property
    def eq_client(self):
        if self._eq is None:
            from service.app import get_edgequake

            self._eq = (self._eq_factory or get_edgequake)()
        return self._eq

    # ── 진입점 ─────────────────────────────────────────────────────────────

    def run(self, job: dict[str, Any], *, worker_id: str, attempt: int) -> dict[str, Any]:
        """잡을 실행한다. 예외는 ``JobFailed``/``JobRetryable``/``JobAborted`` 로 정규화."""
        kind = job["kind"]
        payload = self.blobs.load_json(job.get("payload"), job.get("payload_ref")) or {}
        ctx = _Ctx(job=job, payload=payload, worker_id=worker_id, attempt=attempt)
        try:
            if kind == "parse":
                return self._run_parse(ctx)
            if kind == "chunk":
                return self._run_chunk(ctx)
            if kind == "insert":
                return self._run_insert(ctx)
            if kind == "ingest":
                return self._run_ingest(ctx)
            if kind == "community":
                return self._run_community(ctx)
        except (JobFailed, JobRetryable, JobAborted):
            raise
        except Exception as exc:  # noqa: BLE001 - 다운스트림 예외 정규화
            raise classify(exc) from exc
        raise JobFailed(f"unknown job kind: {kind!r}")

    # ── kind 별 실행 ───────────────────────────────────────────────────────

    def _run_parse(self, ctx: "_Ctx") -> dict[str, Any]:
        """현행 ``app.py`` ``/parse`` 와 동일 — 응답을 거의 그대로 통과시킨다."""
        self._stage(ctx, "parsing")
        data = self._input_bytes(ctx)
        parsed = self.parse_client.parse(
            file_bytes=data,
            filename=ctx.payload["filename"],
            content_type=ctx.payload.get("content_type"),
            docs_id=ctx.payload.get("docs_id"),
        )
        # excel(chunk_needed=false) 소비자 호환 필드 — 현행 app.py 와 동일.
        if parsed.get("chunk_needed") is False:
            parsed.setdefault("chunk_strategy", "excel_rag_parser")
        return parsed

    def _run_chunk(self, ctx: "_Ctx") -> dict[str, Any]:
        self._stage(ctx, "chunking")
        src = self._chunk_inputs(ctx)
        res = self.chunk_client.chunk(
            text=src["enriched_content"],
            doc_name=ctx.payload.get("doc_name", ""),
            atomic_markers=MODAL_ATOMIC_MARKERS,
            page_spans=src.get("page_spans"),
            pages=src.get("pages"),
            # facade 계약의 table_blocks → adaptive 는 blocks 로 이름이 바뀐다.
            blocks=src.get("table_blocks"),
            methods=ctx.payload.get("methods"),
            skip_scoring=ctx.payload.get("skip_scoring", False),
            llm_regex_pattern=ctx.payload.get("llm_regex_pattern"),
        )
        return _shape_chunk_result(res)

    def _run_insert(self, ctx: "_Ctx") -> dict[str, Any]:
        chunks = self._insert_chunks(ctx)
        workspace_id = ctx.payload["workspace_id"]
        doc_id = ctx.payload["doc_id"]
        eq = self.eq_client
        eq_ws = eq.ensure_workspace(workspace_id, name=workspace_id)

        # 부작용 직전 게이트 — 여기서 lease 를 잃으면 edgequake 를 호출하지 않는다.
        self._stage(ctx, "inserting")
        res = eq.insert_chunks(
            workspace_id=eq_ws, tenant_id=TENANT_ID,
            title=ctx.payload.get("title") or doc_id,
            chunk_texts=chunks,
            skip_graph=not ctx.payload.get("extract_graph", True),
        )
        # 야간 배치(A1)의 후보 증거. **그래프를 실제로 건드렸을 때만** 남긴다 —
        # extract_graph=False 인 vector-only 적재까지 남기면 그 KB 가 현행 0회에서
        # 매일 1회 LLM 빌드로 나빠진다(kb 트리거는 그 경우 enqueue 하지 않는다).
        if ctx.payload.get("extract_graph", True):
            self._touch_graph_safe(workspace_id)
        return _shape_insert_result(res, eq_ws)

    def _run_ingest(self, ctx: "_Ctx") -> dict[str, Any]:
        """parse→chunk→insert 연속 수행. 현행 ``app.py`` ``/ingest`` 와 동일 순서."""
        doc_id = ctx.payload["doc_id"]
        workspace_id = ctx.payload["workspace_id"]

        self._stage(ctx, "parsing")
        parsed = self.parse_client.parse(
            file_bytes=self._input_bytes(ctx),
            filename=ctx.payload["filename"],
            content_type=ctx.payload.get("content_type"),
        )
        # 파싱 실패는 잡 실패가 아니다 — 현행이 200 + parse-svc 원본을 돌려주는
        # 정상 경로다(app.py 의 "v2(리뷰 B10)" 주석). 잡은 succeeded 로 끝난다.
        if parsed.get("status") == "failed":
            return parsed

        if parsed.get("chunk_needed", True):
            self._stage(ctx, "chunking")
            # ingest 내부 chunk 호출은 /chunk 와 인자가 **다르다**(현행 유지) —
            # page_spans/pages/blocks/methods 를 넘기지 않는다.
            chunk_res = self.chunk_client.chunk(
                text=parsed.get("enriched_content", ""),
                doc_name=doc_id,
                atomic_markers=MODAL_ATOMIC_MARKERS,
            )
            chunk_texts = [c.get("chunk_text", "") for c in (chunk_res.get("chunks") or [])]
            selection = {
                "method_selected": chunk_res.get("method_selected"),
                "scores": chunk_res.get("scores") or {},
                "methods_compared": chunk_res.get("methods_compared") or [],
            }
        else:
            chunk_texts = [c.get("text", "") for c in (parsed.get("chunks") or [])]
            selection = {"method_selected": "excel_rag_parser",
                         "scores": {}, "methods_compared": []}

        eq = self.eq_client
        eq_ws = eq.ensure_workspace(workspace_id, name=workspace_id)
        self._stage(ctx, "inserting")
        # /ingest 는 skip_graph 를 전달하지 않는다(현행 유지).
        ins = eq.insert_chunks(workspace_id=eq_ws, tenant_id=TENANT_ID,
                               title=doc_id, chunk_texts=chunk_texts)
        # /ingest 는 skip_graph 를 전달하지 않아 **항상 그래프를 추출한다** → 무조건 남긴다.
        # 위치가 함수 끝이 아니라 여기인 이유: 앞의 `status == "failed"` 조기 반환은
        # HTTP 200 이지만 그래프를 전혀 안 건드린다(그 경로는 touch 하면 안 된다).
        self._touch_graph_safe(workspace_id)
        return {
            "document_id": ins.get("document_id"),
            "chunk_count": ins.get("chunk_count"),
            "status": ins.get("status"),
            "chunking_selection": selection,
            "edgequake_workspace_id": eq_ws,
        }

    def _run_community(self, ctx: "_Ctx") -> dict[str, Any]:
        """커뮤니티 재빌드(Louvain + 커뮤니티당 LLM 요약).

        예전에는 `/communities/build` 가 FastAPI ``BackgroundTask`` 로 돌았다. 세 가지가
        문제였다(D10):

        * **유량제어 밖** — 잡 큐의 버킷 상한 계산에 안 들어가, 무거운 적재 4건이 도는
          중에 겹쳐도 아무도 안 막았다. "facade 가 동시처리량을 소유한다" 는 전제의 구멍.
        * **facade 웹 프로세스 점유** — BackgroundTask 는 응답 후 **같은 워커**에서 돈다.
          LLM 이 섞인 장시간 작업이라 gunicorn 워커 하나가 그동안 요청을 못 받는다.
        * **흔적 없음** — 잡 행이 없으니 상태 조회·재시도·취소가 없고, 죽으면 로그뿐이다.

        멱등하다 — 같은 workspace 를 다시 빌드하면 결과가 같으므로 재시도가 안전하다
        (`insert` 와 다른 점이고, 그래서 `max_attempts` 가 기본값이다).
        """
        workspace_id = ctx.payload.get("workspace_id")
        if not workspace_id:
            raise JobFailed("community job has no workspace_id")

        # edgequake workspace UUID 로 해석한다 — 커뮤니티 행이 그 UUID 로 스코프된다.
        eq_ws = self.eq_client.ensure_workspace(workspace_id, name=workspace_id)

        # ★ 빌드가 그래프를 **읽기 시작한 시각**. 완료 시각을 쓰면 수십 분짜리 빌드 도중
        #   성공한 적재가 다음 밤 후보에서 영구 탈락한다(빌드는 진입 시점 그래프만 본다).
        #   DB 시계로 받는다 — graph_touch.touched_at 이 서버 now() 라 축을 맞춰야 한다.
        snapshot_at = self.repo.db_now()

        self._stage(ctx, "building")
        builder = self._community_builder
        if builder is None:
            from kb_pipeline.community import build_workspace_communities as builder

        from service.llm import get_text_llm

        try:
            result = builder(eq_ws, llm=get_text_llm(), dsn=os.environ["KBP_PG_DSN"])
        except JobAborted:
            # lease 상실은 **다른 세대가 이 workspace 를 소유**한 것이라 이쪽의 실패가
            # 아니다. 실패로 기록하면 그쪽 세대의 이력을 덮어쓴다.
            raise
        except Exception:
            self._record_community_failure_safe(workspace_id, eq_ws)
            raise

        self._record_community_success_safe(workspace_id, eq_ws, snapshot_at)
        return {"workspace_id": eq_ws,
                "result": result if isinstance(result, (dict, list, str, int)) else None}

    # ── 야간 커뮤니티 배치(A1) 이력 기록 — 전부 best-effort ────────────────
    # 공통 이유: 이 호출들은 **부작용(edgequake 적재·LLM 빌드)이 끝난 뒤**다. 여기서
    # 예외를 올리면 run() 의 공통 except 가 잡아 잡을 failed 로 정규화하는데, 그러면
    # 이미 성공한 비싼 작업이 실패로 뒤집힌다.

    def _touch_graph_safe(self, workspace_key: str) -> None:
        """야간 후보의 증거를 남긴다. 실패해도 적재는 성공으로 끝낸다.

        ⚠️ "실패해도 그 밤 한 번 거를 뿐" 이 아니다 — ``graph_touch`` 는 후보의
        **유일한 증거원**이고 GC 대상도 아니라, 놓치면 그 workspace 는 **다음 적재가
        올 때까지 무기한** 후보가 아니다. 그래서 1회 재시도하고 실패는 error 로 남긴다.
        """
        for attempt in (1, 2):
            try:
                self.repo.touch_graph(workspace_key)
                return
            except Exception:  # noqa: BLE001 — 적재 성공을 뒤집지 않는다
                if attempt == 2:
                    log.error(
                        "graph_touch 기록 실패(재시도 후) — 이 workspace 는 다음 적재까지 "
                        "야간 커뮤니티 후보가 되지 않는다 ws=%s", workspace_key,
                        exc_info=True)
                else:
                    time.sleep(0.5)

    def _record_community_success_safe(self, workspace_key: str, eq_ws: str,
                                       snapshot_at: Any) -> None:
        try:
            self.repo.record_community_success(workspace_key, eq_ws, snapshot_at)
        except Exception:  # noqa: BLE001 — 수십 분짜리 빌드를 뒤집지 않는다
            log.error("community_builds 성공 기록 실패 ws=%s (다음 밤 재빌드된다)",
                      workspace_key, exc_info=True)

    def _record_community_failure_safe(self, workspace_key: str,
                                       eq_ws: str | None) -> None:
        try:
            self.repo.record_community_failure(workspace_key, eq_ws)
        except Exception:  # noqa: BLE001 — 원래 예외를 가리지 않는다
            log.error("community_builds 실패 기록 실패 ws=%s", workspace_key,
                      exc_info=True)

    # ── 보조 ───────────────────────────────────────────────────────────────

    def _stage(self, ctx: "_Ctx", stage: str) -> None:
        """단계를 커밋한다. lease 를 잃었으면 **부작용 전에** 중단한다(§3.3).

        ``stage='inserting'`` 은 중복 적재 방어의 유일한 게이트라, 여기서 "로그만 남기고
        계속" 하면 회수된 좀비가 edgequake 에 문서를 한 번 더 제출한다.
        """
        try:
            self.repo.set_stage(
                ctx.job["id"], worker_id=ctx.worker_id, attempt=ctx.attempt, stage=stage
            )
        except LeaseLost as exc:
            log.warning("lease lost before %s; aborting job %s", stage, ctx.job["id"])
            raise JobAborted(str(exc)) from exc

    def _input_bytes(self, ctx: "_Ctx") -> bytes:
        ref = ctx.job.get("input_ref")
        if not ref:
            raise JobFailed("job has no input_ref (staged upload missing)")
        try:
            return self.blobs.get_bytes(ref)
        except Exception as exc:  # noqa: BLE001 - 없는 객체는 재시도해도 없다
            raise JobFailed(f"staging object not found: {ref} ({exc})") from exc

    def _parent_result(self, ctx: "_Ctx", *, expect: str) -> dict[str, Any]:
        """참조 잡의 결과를 **실행 시점에 재확인**해서 가져온다(§5.3).

        접수 시 검증만 믿지 않는다 — 대기·재시도 중 상황이 바뀔 수 있다.
        """
        parent_id = ctx.job.get("parent_job_id")
        if not parent_id:
            raise JobFailed("no parent_job_id")
        parent = self.repo.get(parent_id)
        if parent is None:
            raise JobFailed(f"referenced {expect} job no longer exists: {parent_id}")
        if parent["status"] != "succeeded":
            raise JobFailed(
                f"referenced {expect} job is {parent['status']}, not succeeded"
            )
        result = self.blobs.load_json(parent.get("result"), parent.get("result_ref"))
        if not result:
            raise JobFailed(f"referenced {expect} job has no result")
        return result

    def _chunk_inputs(self, ctx: "_Ctx") -> dict[str, Any]:
        if ctx.job.get("parent_job_id"):
            parent = self._parent_result(ctx, expect="parse")
            return {
                "enriched_content": parent.get("enriched_content", ""),
                "page_spans": parent.get("page_spans"),
                "pages": parent.get("pages"),
                "table_blocks": parent.get("table_blocks"),
            }
        return {
            "enriched_content": ctx.payload.get("enriched_content", ""),
            "page_spans": ctx.payload.get("page_spans"),
            "pages": ctx.payload.get("pages"),
            "table_blocks": ctx.payload.get("table_blocks"),
        }

    def _insert_chunks(self, ctx: "_Ctx") -> list[str]:
        """``chunk_job_id`` 를 주면 chunk 잡 결과의 ``chunks[].text`` 를 쓴다.

        kb 의 현행 단계별 경로와 같다 — kb 는 ``/chunk`` 응답의 ``text``(마커 스트립된
        표시사본)를 그대로 ``/insert`` 에 넘긴다. ``eq.insert_chunks`` 가 어차피
        ``_strip_modal`` 을 다시 적용하므로 저장물은 동일하다.
        """
        if ctx.job.get("parent_job_id"):
            parent = self._parent_result(ctx, expect="chunk")
            return [c.get("text", "") for c in (parent.get("chunks") or [])]
        chunks = ctx.payload.get("chunks")
        if chunks is None:
            raise JobFailed("insert job has neither chunk_job_id nor chunks")
        return list(chunks)


class _Ctx:
    __slots__ = ("job", "payload", "worker_id", "attempt")

    def __init__(self, *, job, payload, worker_id, attempt):
        self.job = job
        self.payload = payload
        self.worker_id = worker_id
        self.attempt = attempt


# ── 결과 성형 (현행 app.py 응답과 동일해야 한다) ───────────────────────────


def _shape_chunk_result(res: dict[str, Any]) -> dict[str, Any]:
    chunks = [
        {
            "chunk_index": ch.get("chunk_index"),
            # 표시싱크: 소비자가 이 text 를 표시사본으로 저장 → 마커 스트립.
            "text": strip_modal(ch.get("chunk_text", "")),
            "titles_context": ch.get("titles_context"),
            "pages": ch.get("chunk_pages") or [],
        }
        for ch in (res.get("chunks") or [])
    ]
    return {
        "chunks": chunks,
        "method_selected": res.get("method_selected"),
        "scores": res.get("scores") or {},
        "methods_compared": res.get("methods_compared") or [],
        "timing_details": res.get("timing_details"),
    }


def _shape_insert_result(res: dict[str, Any], eq_ws: str) -> dict[str, Any]:
    return {
        "document_id": res.get("document_id"),
        "chunk_count": res.get("chunk_count"),
        "status": res.get("status"),
        "edgequake_workspace_id": eq_ws,
        "entity_count": res.get("entity_count"),
        "relationship_count": res.get("relationship_count"),
        "phases": res.get("phases") or [],
    }
