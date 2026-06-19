"""Unit tests for kb_pipeline.search (W5 unified search).

Everything is MOCKED — no network, no DB. Covers:
  * route() heuristic + injected-LLM tie-break classification,
  * local_search header/payload wiring against a fake HTTP post,
  * global_search delegation (with build-if-missing skipped),
  * unified_search dispatch to local vs global with mocked sub-functions.
"""

import pytest

from kb_pipeline.search import (
    route,
    local_search,
    global_search,
    unified_search,
    GLOBAL_CUES,
    DEFAULT_TENANT_ID,
)


# --------------------------------------------------------------------------- #
# route() classification
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "question",
    [
        "이 문서가 다루는 핵심 주제를 요약해줘",
        "전체 내용을 정리해줘",
        "휴가의 종류는 무엇이 있나요",   # '종류' cue
        "이 문서의 개요를 알려줘",
        "이 문서가 무엇을 다루나요",
        "Give me an overview of the document",
        "summarize the key topics",
    ],
)
def test_route_global_cue_words_no_llm(question):
    # cue word present -> global, with NO llm consulted
    assert route(question, llm=None) == "global"


@pytest.mark.parametrize(
    "question",
    [
        "산전산후휴가 유산 기준 주수는?",
        "연차 휴가는 며칠인가요?",
        "담보신탁 프로세스의 첫 단계는 무엇인가요?",
        "제1조의 내용은?",
    ],
)
def test_route_specific_fact_defaults_local(question):
    # no cue word + no llm -> default local
    assert route(question, llm=None) == "local"


def test_route_every_cue_word_classifies_global():
    for cue in GLOBAL_CUES:
        q = f"질문 {cue} 관련"
        assert route(q, llm=None) == "global", cue


def test_route_llm_tiebreaks_global_when_no_cue():
    # no heuristic cue -> llm is consulted, and a GLOBAL verdict wins
    calls = []

    def llm(system, user):
        calls.append((system, user))
        return "GLOBAL"

    # a question with no cue word, so the heuristic falls through to the llm
    assert route("이 자료의 성격을 알려줘", llm=llm) == "global"
    assert len(calls) == 1


def test_route_llm_tiebreaks_local_when_no_cue():
    def llm(system, user):
        return "LOCAL"

    assert route("이 자료의 성격을 알려줘", llm=llm) == "local"


def test_route_cue_word_short_circuits_before_llm():
    # cue word present -> llm MUST NOT be called
    def llm(system, user):
        raise AssertionError("llm must not be consulted when a cue word matches")

    assert route("핵심 주제 요약", llm=llm) == "global"


def test_route_llm_failure_falls_back_to_local():
    def llm(system, user):
        raise RuntimeError("network down")

    # llm raises -> swallowed -> default local
    assert route("성격을 알려줘", llm=llm) == "local"


# --------------------------------------------------------------------------- #
# local_search wiring (fake HTTP)
# --------------------------------------------------------------------------- #
class _FakeResp:
    def __init__(self, payload):
        self._payload = payload
        self.raised = False

    def raise_for_status(self):
        self.raised = True

    def json(self):
        return self._payload


def test_local_search_sets_workspace_headers_and_hybrid_mode():
    captured = {}

    def http_post(url, json, headers, timeout):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        captured["timeout"] = timeout
        return _FakeResp(
            {
                "answer": "유산 기준 주수는 16주입니다.",
                "mode": "hybrid",
                "sources": [
                    {"source_type": "chunk", "id": "c1", "score": 0.9,
                     "snippet": "산전산후휴가 ..."},
                ],
                "stats": {"total_time_ms": 1234},
            }
        )

    out = local_search(
        "산전산후휴가 유산 기준 주수는?",
        "00000000-0000-0000-0000-000000000003",
        http_post=http_post,
    )

    # endpoint + workspace scoping headers
    assert captured["url"].endswith("/api/v1/query")
    assert captured["headers"]["x-workspace-id"] == "00000000-0000-0000-0000-000000000003"
    assert captured["headers"]["x-tenant-id"] == DEFAULT_TENANT_ID
    # hybrid mode + references requested
    assert captured["json"]["mode"] == "hybrid"
    assert captured["json"]["query"].startswith("산전산후휴가")
    assert captured["json"]["include_references"] is True

    # response relayed
    assert out["answer"] == "유산 기준 주수는 16주입니다."
    assert out["mode"] == "hybrid"
    assert out["sources"][0]["id"] == "c1"
    assert out["workspace_id"] == "00000000-0000-0000-0000-000000000003"
    assert out["stats"]["total_time_ms"] == 1234


def test_local_search_custom_tenant_and_base_url():
    captured = {}

    def http_post(url, json, headers, timeout):
        captured["url"] = url
        captured["headers"] = headers
        return _FakeResp({"answer": "a", "mode": "hybrid", "sources": []})

    local_search(
        "q",
        "ws-B",
        tenant_id="tenant-X",
        base_url="http://example:9999/",
        http_post=http_post,
    )
    assert captured["url"] == "http://example:9999/api/v1/query"
    assert captured["headers"]["x-tenant-id"] == "tenant-X"
    assert captured["headers"]["x-workspace-id"] == "ws-B"


def test_local_search_raises_on_http_error():
    class _ErrResp(_FakeResp):
        def raise_for_status(self):
            raise RuntimeError("500")

    def http_post(url, json, headers, timeout):
        return _ErrResp({})

    with pytest.raises(RuntimeError):
        local_search("q", "ws", http_post=http_post)


# --------------------------------------------------------------------------- #
# global_search delegation
# --------------------------------------------------------------------------- #
def test_global_search_delegates_to_community(monkeypatch):
    calls = {}

    def fake_global_query(question, workspace_id, *, llm, dsn, top_k, level):
        calls["question"] = question
        calls["workspace_id"] = workspace_id
        return {"answer": "핵심 주제 요약입니다.", "communities_used": [0, 2]}

    def fake_build(*a, **k):
        raise AssertionError("build must be skipped when build_if_missing=False")

    monkeypatch.setattr("kb_pipeline.search.community.global_query", fake_global_query)
    monkeypatch.setattr(
        "kb_pipeline.search.community.build_workspace_communities", fake_build
    )

    out = global_search(
        "이 문서의 핵심 주제 요약",
        "ws-A",
        llm=lambda s, u: "x",
        build_if_missing=False,
    )

    assert out["mode"] == "global"
    assert out["answer"] == "핵심 주제 요약입니다."
    assert out["sources"] == [0, 2]
    assert out["workspace_id"] == "ws-A"
    assert calls["workspace_id"] == "ws-A"


def test_global_search_builds_when_missing(monkeypatch):
    events = []

    monkeypatch.setattr(
        "kb_pipeline.search._reports_exist", lambda ws, dsn, level=0: False
    )
    monkeypatch.setattr(
        "kb_pipeline.search.community.build_workspace_communities",
        lambda ws, **k: events.append(("build", ws)),
    )
    monkeypatch.setattr(
        "kb_pipeline.search.community.global_query",
        lambda q, ws, **k: (events.append(("query", ws)) or
                            {"answer": "a", "communities_used": []}),
    )

    global_search("q", "ws-A", llm=lambda s, u: "x")
    assert events == [("build", "ws-A"), ("query", "ws-A")]


def test_global_search_skips_build_when_reports_exist(monkeypatch):
    events = []

    monkeypatch.setattr(
        "kb_pipeline.search._reports_exist", lambda ws, dsn, level=0: True
    )
    monkeypatch.setattr(
        "kb_pipeline.search.community.build_workspace_communities",
        lambda *a, **k: events.append("build"),
    )
    monkeypatch.setattr(
        "kb_pipeline.search.community.global_query",
        lambda q, ws, **k: {"answer": "a", "communities_used": [1]},
    )

    out = global_search("q", "ws-A", llm=lambda s, u: "x")
    assert events == []  # build skipped
    assert out["sources"] == [1]


# --------------------------------------------------------------------------- #
# unified_search dispatch
# --------------------------------------------------------------------------- #
def test_unified_search_dispatches_to_local():
    calls = {}

    def fake_route(question, *, llm):
        return "local"

    def fake_local(question, workspace_id, *, tenant_id, base_url):
        calls["local"] = (question, workspace_id, tenant_id, base_url)
        return {
            "answer": "구체적 사실 답변",
            "sources": [{"source_type": "chunk", "id": "c9"}],
        }

    def fake_global(*a, **k):
        raise AssertionError("global must not run for a local route")

    out = unified_search(
        "산전산후휴가 유산 기준 주수는?",
        "00000000-0000-0000-0000-000000000003",
        llm=lambda s, u: "x",
        local_fn=fake_local,
        global_fn=fake_global,
        route_fn=fake_route,
    )

    assert out["mode"] == "local"
    assert out["answer"] == "구체적 사실 답변"
    assert out["sources"][0]["id"] == "c9"
    assert out["workspace_id"] == "00000000-0000-0000-0000-000000000003"
    # local_fn got the workspace scoping args
    assert calls["local"][1] == "00000000-0000-0000-0000-000000000003"


def test_unified_search_dispatches_to_global():
    def fake_route(question, *, llm):
        return "global"

    def fake_global(question, workspace_id, *, llm, dsn, top_k, level):
        return {"answer": "전체 요약", "sources": [0, 1, 2]}

    def fake_local(*a, **k):
        raise AssertionError("local must not run for a global route")

    out = unified_search(
        "이 문서가 다루는 핵심 주제를 요약",
        "ws-B",
        llm=lambda s, u: "x",
        local_fn=fake_local,
        global_fn=fake_global,
        route_fn=fake_route,
    )

    assert out["mode"] == "global"
    assert out["answer"] == "전체 요약"
    assert out["sources"] == [0, 1, 2]
    assert out["workspace_id"] == "ws-B"


def test_unified_search_uses_real_route_with_cue_word():
    # don't inject route_fn -> exercise the real heuristic router end-to-end
    def fake_global(question, workspace_id, *, llm, dsn, top_k, level):
        return {"answer": "요약 답변", "sources": [0]}

    def fake_local(*a, **k):
        raise AssertionError("cue word '요약' must route global")

    out = unified_search(
        "핵심 주제를 요약",
        "ws-B",
        llm=lambda s, u: "LOCAL",  # llm would say local, but cue word wins
        local_fn=fake_local,
        global_fn=fake_global,
    )
    assert out["mode"] == "global"
    assert out["answer"] == "요약 답변"
