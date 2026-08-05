"""JobRunner — kind 별 실행. DB·MinIO 없이 fake 로 돈다.

여기서 지키는 계약:
  * 응답 성형이 현행 ``service/app.py`` 와 동일하다(레거시 4경로가 이걸 그대로 돌려준다)
  * `set_stage` 가 `LeaseLost` 면 **다운스트림을 호출하지 않는다**(중복 적재 방어)
  * 실패 분류: 5xx/타임아웃 → 재시도, 4xx → 즉시 실패, insert 는 재시도 자체가 없음
  * `table_blocks` → `blocks` 이름 변환, ingest 내부 chunk 인자가 /chunk 와 다른 현행
"""
from __future__ import annotations

import uuid

import httpx
import pytest

from service.jobs.repo import LeaseLost
from service.jobs.runner import (JobAborted, JobFailed, JobRetryable, JobRunner,
                                 classify)


class FakeBlobs:
    def __init__(self, objects=None):
        self.objects = objects or {}

    def load_json(self, inline, ref):
        if ref:
            return self.objects[ref]
        return inline

    def get_bytes(self, ref):
        return self.objects[ref]


class FakeRepo:
    """`set_stage` 호출을 기록하고, 지정한 단계에서 LeaseLost 를 던진다."""

    def __init__(self, *, lose_lease_at=None, jobs=None):
        self.stages = []
        self.lose_lease_at = lose_lease_at
        self.jobs = jobs or {}

    def set_stage(self, job_id, *, worker_id, attempt, stage):
        self.stages.append(stage)
        if stage == self.lose_lease_at:
            raise LeaseLost(f"lost at {stage}")

    def get(self, job_id):
        return self.jobs.get(str(job_id))


class SpyEq:
    def __init__(self, result=None):
        self.insert_calls = 0
        self.result = result or {"document_id": "doc-1", "chunk_count": 2,
                                 "status": "indexed", "entity_count": 5,
                                 "relationship_count": 3, "phases": [{"name": "x", "ms": 1}]}

    def ensure_workspace(self, workspace_id, name=None):
        return "ws-uuid"

    def insert_chunks(self, **kw):
        self.insert_calls += 1
        self.last = kw
        return self.result


class SpyParse:
    def __init__(self, result=None, raises=None):
        self.result = result or {"enriched_content": "hello", "n_blocks": 1}
        self.raises = raises
        self.calls = []

    def parse(self, **kw):
        self.calls.append(kw)
        if self.raises:
            raise self.raises
        return self.result


class SpyChunk:
    def __init__(self, result=None):
        self.result = result or {
            "chunks": [{"chunk_index": 0, "chunk_text": "〈MODAL t〉표〈/MODAL〉",
                        "titles_context": "T", "chunk_pages": [1]}],
            "method_selected": "recursive_1100",
            "scores": {"recursive_1100": 0.9},
            "methods_compared": ["recursive_1100"],
            "timing_details": {"split_ms": 3},
        }
        self.calls = []

    def chunk(self, **kw):
        self.calls.append(kw)
        return self.result


def _job(kind, **kw):
    job = {"id": uuid.uuid4(), "kind": kind, "payload": {}, "payload_ref": None,
           "input_ref": None, "parent_job_id": None}
    job.update(kw)
    return job


def _runner(**kw):
    kw.setdefault("repo", FakeRepo())
    kw.setdefault("blobs", FakeBlobs())
    return JobRunner(**kw)


# ── 부작용 직전 lease 상실 (가장 중요) ─────────────────────────────────────

def test_lease_lost_before_insert_does_not_call_edgequake():
    """`stage='inserting'` 이 LeaseLost 면 edgequake 호출 카운트가 0이어야 한다.

    여기서 계속 진행하면 회수된 좀비가 문서를 한 번 더 제출한다 — 중복 적재.
    """
    eq = SpyEq()
    r = _runner(repo=FakeRepo(lose_lease_at="inserting"), eq_client=eq)
    job = _job("insert", payload={"workspace_id": "kb-1", "doc_id": "d1",
                                  "chunks": ["a", "b"]})
    with pytest.raises(JobAborted):
        r.run(job, worker_id="w1", attempt=1)
    assert eq.insert_calls == 0


def test_lease_lost_before_parse_does_not_call_parse_svc():
    pc = SpyParse()
    r = _runner(repo=FakeRepo(lose_lease_at="parsing"), parse_client=pc,
                blobs=FakeBlobs({"k": b"bytes"}))
    job = _job("parse", input_ref="k", payload={"filename": "a.pdf"})
    with pytest.raises(JobAborted):
        r.run(job, worker_id="w1", attempt=1)
    assert pc.calls == []


def test_ingest_lease_lost_at_insert_stage_does_not_submit():
    """ingest 는 parse·chunk 를 다 돌고 나서 insert 게이트를 만난다."""
    eq = SpyEq()
    r = _runner(repo=FakeRepo(lose_lease_at="inserting"), eq_client=eq,
                parse_client=SpyParse(), chunk_client=SpyChunk(),
                blobs=FakeBlobs({"k": b"bytes"}))
    job = _job("ingest", input_ref="k",
               payload={"filename": "a.pdf", "workspace_id": "kb-1", "doc_id": "d1"})
    with pytest.raises(JobAborted):
        r.run(job, worker_id="w1", attempt=1)
    assert eq.insert_calls == 0


# ── 응답 성형 (현행 app.py 와 동일) ────────────────────────────────────────

def test_parse_result_passes_through_and_adds_excel_compat_field():
    pc = SpyParse(result={"enriched_content": "", "chunk_needed": False,
                          "chunks": [{"text": "c"}]})
    r = _runner(parse_client=pc, blobs=FakeBlobs({"k": b"x"}))
    job = _job("parse", input_ref="k",
               payload={"filename": "표 (1).xlsx", "content_type": "application/xlsx",
                        "docs_id": "abc123"})
    out = r.run(job, worker_id="w1", attempt=1)
    assert out["chunk_strategy"] == "excel_rag_parser"     # 소비자 호환 재구성
    assert pc.calls[0]["filename"] == "표 (1).xlsx"          # 한글·공백 보존
    assert pc.calls[0]["docs_id"] == "abc123"               # 페이지 이미지 키 합의


def test_chunk_result_is_normalized_and_markers_stripped():
    ch = SpyChunk()
    r = _runner(chunk_client=ch)
    job = _job("chunk", payload={"enriched_content": "x", "doc_name": "d",
                                 "table_blocks": [{"category": "table"}]})
    out = r.run(job, worker_id="w1", attempt=1)
    assert out["chunks"][0]["text"] == "표"                  # 마커 스트립
    assert out["chunks"][0]["pages"] == [1]                  # chunk_pages → pages
    assert out["method_selected"] == "recursive_1100"
    assert out["timing_details"] == {"split_ms": 3}
    # facade 계약의 table_blocks 는 adaptive 에서 blocks 로 이름이 바뀐다
    assert ch.calls[0]["blocks"] == [{"category": "table"}]
    assert "table_blocks" not in ch.calls[0]


def test_insert_result_carries_graph_counts_and_workspace():
    eq = SpyEq()
    r = _runner(eq_client=eq)
    job = _job("insert", payload={"workspace_id": "kb-1", "doc_id": "d1",
                                  "chunks": ["a"]})
    out = r.run(job, worker_id="w1", attempt=1)
    assert out["edgequake_workspace_id"] == "ws-uuid"
    assert out["entity_count"] == 5 and out["relationship_count"] == 3
    assert out["phases"] == [{"name": "x", "ms": 1}]


def test_insert_title_falls_back_to_doc_id():
    eq = SpyEq()
    r = _runner(eq_client=eq)
    r.run(_job("insert", payload={"workspace_id": "kb-1", "doc_id": "d1",
                                  "chunks": ["a"]}), worker_id="w1", attempt=1)
    assert eq.last["title"] == "d1"


def test_ingest_chunk_call_omits_page_and_method_args():
    """ingest 내부 chunk 호출은 /chunk 와 인자가 다르다 — 현행 유지."""
    ch = SpyChunk()
    r = _runner(parse_client=SpyParse(), chunk_client=ch, eq_client=SpyEq(),
                blobs=FakeBlobs({"k": b"x"}))
    job = _job("ingest", input_ref="k",
               payload={"filename": "a.pdf", "workspace_id": "kb-1", "doc_id": "d1"})
    out = r.run(job, worker_id="w1", attempt=1)
    call = ch.calls[0]
    assert call["doc_name"] == "d1"
    assert call.get("page_spans") is None and call.get("blocks") is None
    assert out["chunking_selection"]["method_selected"] == "recursive_1100"
    assert out["edgequake_workspace_id"] == "ws-uuid"


def test_ingest_returns_parse_failure_body_as_success():
    """parse-svc {status:'failed'} 는 잡 실패가 아니다 — 현행이 200 + 원본을 준다."""
    pc = SpyParse(result={"status": "failed", "detail": "unsupported"})
    eq = SpyEq()
    r = _runner(parse_client=pc, eq_client=eq, blobs=FakeBlobs({"k": b"x"}))
    job = _job("ingest", input_ref="k",
               payload={"filename": "a.zip", "workspace_id": "kb-1", "doc_id": "d1"})
    out = r.run(job, worker_id="w1", attempt=1)
    assert out == {"status": "failed", "detail": "unsupported"}
    assert eq.insert_calls == 0


def test_ingest_excel_path_skips_chunker():
    pc = SpyParse(result={"chunk_needed": False, "chunks": [{"text": "row1"}]})
    ch = SpyChunk()
    eq = SpyEq()
    r = _runner(parse_client=pc, chunk_client=ch, eq_client=eq,
                blobs=FakeBlobs({"k": b"x"}))
    out = r.run(_job("ingest", input_ref="k",
                     payload={"filename": "a.xlsx", "workspace_id": "kb-1",
                              "doc_id": "d1"}), worker_id="w1", attempt=1)
    assert ch.calls == []
    assert out["chunking_selection"]["method_selected"] == "excel_rag_parser"
    assert eq.last["chunk_texts"] == ["row1"]


# ── 체인 해석 ──────────────────────────────────────────────────────────────

def test_chunk_reads_inputs_from_parent_parse_job():
    parent_id = uuid.uuid4()
    parent = {"status": "succeeded",
              "result": {"enriched_content": "from-parent", "page_spans": [{"p": 1}],
                         "pages": [{"page_number": 1}], "table_blocks": [{"t": 1}]},
              "result_ref": None}
    ch = SpyChunk()
    r = _runner(repo=FakeRepo(jobs={str(parent_id): parent}), chunk_client=ch)
    job = _job("chunk", parent_job_id=parent_id, payload={"doc_name": "d"})
    r.run(job, worker_id="w1", attempt=1)
    assert ch.calls[0]["text"] == "from-parent"
    assert ch.calls[0]["page_spans"] == [{"p": 1}]
    assert ch.calls[0]["blocks"] == [{"t": 1}]


def test_insert_reads_chunk_texts_from_parent_chunk_job():
    parent_id = uuid.uuid4()
    parent = {"status": "succeeded",
              "result": {"chunks": [{"text": "c0"}, {"text": "c1"}]}, "result_ref": None}
    eq = SpyEq()
    r = _runner(repo=FakeRepo(jobs={str(parent_id): parent}), eq_client=eq)
    job = _job("insert", parent_job_id=parent_id,
               payload={"workspace_id": "kb-1", "doc_id": "d1"})
    r.run(job, worker_id="w1", attempt=1)
    assert eq.last["chunk_texts"] == ["c0", "c1"]


def test_parent_job_gone_at_run_time_fails():
    """접수 시 검증만 믿지 않는다 — 실행 시점에 재확인한다."""
    r = _runner(repo=FakeRepo(jobs={}), chunk_client=SpyChunk())
    job = _job("chunk", parent_job_id=uuid.uuid4(), payload={})
    with pytest.raises(JobFailed):
        r.run(job, worker_id="w1", attempt=1)


def test_parent_job_not_succeeded_fails():
    parent_id = uuid.uuid4()
    r = _runner(repo=FakeRepo(jobs={str(parent_id): {"status": "running"}}),
                chunk_client=SpyChunk())
    job = _job("chunk", parent_job_id=parent_id, payload={})
    with pytest.raises(JobFailed):
        r.run(job, worker_id="w1", attempt=1)


def test_large_parent_result_is_restored_from_blob_ref():
    """임계 초과 결과가 MinIO 로 나가도 체인이 빈 본문을 보면 안 된다(§2.2)."""
    parent_id = uuid.uuid4()
    parent = {"status": "succeeded", "result": None, "result_ref": "kbp-jobs/p/result.json"}
    blobs = FakeBlobs({"kbp-jobs/p/result.json": {"enriched_content": "big"}})
    ch = SpyChunk()
    r = _runner(repo=FakeRepo(jobs={str(parent_id): parent}), blobs=blobs, chunk_client=ch)
    r.run(_job("chunk", parent_job_id=parent_id, payload={}), worker_id="w1", attempt=1)
    assert ch.calls[0]["text"] == "big"


# ── 실패 분류 ──────────────────────────────────────────────────────────────

def _http_error(code):
    req = httpx.Request("POST", "http://x/parse")
    return httpx.HTTPStatusError("boom", request=req,
                                 response=httpx.Response(code, request=req))


@pytest.mark.parametrize("code", [500, 502, 503, 429])
def test_server_errors_are_retryable(code):
    assert isinstance(classify(_http_error(code)), JobRetryable)


@pytest.mark.parametrize("code", [400, 401, 404, 422])
def test_client_errors_are_not_retryable(code):
    assert isinstance(classify(_http_error(code)), JobFailed)


def test_timeouts_are_retryable():
    assert isinstance(classify(httpx.ReadTimeout("slow")), JobRetryable)


def test_runner_normalizes_downstream_error():
    r = _runner(parse_client=SpyParse(raises=_http_error(502)),
                blobs=FakeBlobs({"k": b"x"}))
    job = _job("parse", input_ref="k", payload={"filename": "a.pdf"})
    with pytest.raises(JobRetryable):
        r.run(job, worker_id="w1", attempt=1)


def test_missing_staging_object_is_not_retryable():
    """없는 객체는 재시도해도 없다."""
    r = _runner(parse_client=SpyParse(), blobs=FakeBlobs({}))
    job = _job("parse", input_ref="gone", payload={"filename": "a.pdf"})
    with pytest.raises(JobFailed):
        r.run(job, worker_id="w1", attempt=1)


def test_unknown_kind_fails():
    r = _runner()
    with pytest.raises(JobFailed):
        r.run(_job("bogus"), worker_id="w1", attempt=1)


# ── community kind (D10) ───────────────────────────────────────────────────
#
# 예전에는 `/communities/build` 가 FastAPI BackgroundTask 로 돌아 유량제어 밖이었고,
# facade 웹 프로세스를 오래 점유했으며, 실패해도 흔적이 없었다.

def test_community_resolves_the_workspace_and_calls_the_builder(monkeypatch):
    monkeypatch.setenv("KBP_PG_DSN", "postgres://x/y")
    monkeypatch.setattr("service.llm.get_text_llm", lambda: (lambda p, pl: "요약"))
    seen = {}

    def builder(workspace_id, *, llm, dsn):
        seen.update(workspace_id=workspace_id, dsn=dsn)
        return {"reports_written": 3}

    eq = SpyEq()
    r = _runner(eq_client=eq, community_builder=builder)
    out = r.run(_job("community", payload={"workspace_id": "kb-1"}),
                worker_id="w", attempt=1)

    # kb id → edgequake workspace uuid 해석. 커뮤니티 행이 그 uuid 로 스코프된다.
    assert seen["workspace_id"] == eq.ensure_workspace("kb-1")
    assert seen["dsn"] == "postgres://x/y"
    assert out["workspace_id"] == seen["workspace_id"]
    assert out["result"] == {"reports_written": 3}


def test_community_commits_the_stage_before_building(monkeypatch):
    """lease 를 잃었으면 **빌드를 시작하지 않는다** — 다른 세대가 이미 돌고 있다."""
    monkeypatch.setenv("KBP_PG_DSN", "postgres://x/y")
    called = []
    r = _runner(repo=FakeRepo(lose_lease_at="building"), eq_client=SpyEq(),
                community_builder=lambda ws, **k: called.append(ws))
    with pytest.raises(JobAborted):
        r.run(_job("community", payload={"workspace_id": "kb-1"}),
              worker_id="w", attempt=1)
    assert called == []


def test_community_without_workspace_fails_fast():
    """재시도해도 없는 값은 안 생긴다 — JobFailed(재시도 대상 아님)."""
    r = _runner(eq_client=SpyEq(), community_builder=lambda ws, **k: None)
    with pytest.raises(JobFailed):
        r.run(_job("community", payload={}), worker_id="w", attempt=1)
