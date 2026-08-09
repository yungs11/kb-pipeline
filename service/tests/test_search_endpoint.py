"""Facade ``POST /search`` — edgequake ``/api/v1/query`` hidden, results normalized.

The facade ADDS VALUE (R5): it (a) resolves the kb id to the edgequake workspace
UUID so the query is workspace-scoped (isolation), (b) maps the consumer's
``top_k`` to edgequake's ``max_results``, and (c) normalizes edgequake's
``sources`` (source_type/id/snippet/score/document_id) into a stable ``results``
shape (chunk_id/text/score/document_id) plus the generated ``answer`` — the
consumer never sees edgequake's query schema.
"""
from fastapi.testclient import TestClient

from service.app import app, get_edgequake


EQ_WS = "99999999-9999-9999-9999-999999999999"


class FakeEq:
    def __init__(self):
        self.ensured = []
        self.search_calls = []

    def ensure_workspace(self, kb_id, name, tenant_id="00000000-0000-0000-0000-000000000002"):
        self.ensured.append((kb_id, name))
        return EQ_WS

    def search(self, *, workspace_id, query, top_k):
        # the resolved edgequake uuid (not the raw kb id) scopes the query.
        assert workspace_id == EQ_WS
        self.search_calls.append({"workspace_id": workspace_id, "query": query,
                                  "top_k": top_k})
        # edgequake /api/v1/query response shape.
        return {
            "answer": "the answer",
            "mode": "hybrid",
            "sources": [
                {"source_type": "chunk", "id": "d1-chunk-0", "score": 0.91,
                 "snippet": "alpha text", "document_id": "d1"},
                {"source_type": "chunk", "id": "d1-chunk-3", "score": 0.42,
                 "snippet": "beta text", "document_id": "d1"},
            ],
        }


def test_search_scopes_workspace_and_normalizes_results():
    eq = FakeEq()
    app.dependency_overrides[get_edgequake] = lambda: eq
    c = TestClient(app)
    r = c.post("/search", json={"workspace_id": "kb1", "query": "what?", "top_k": 5})
    assert r.status_code == 200
    j = r.json()

    # kb id resolved to the edgequake workspace uuid; query scoped to it.
    assert eq.ensured == [("kb1", "kb1")]
    assert eq.search_calls == [{"workspace_id": EQ_WS, "query": "what?", "top_k": 5}]

    # results normalized from edgequake sources; answer surfaced.
    assert j["answer"] == "the answer"
    assert j["results"] == [
        {"chunk_id": "d1-chunk-0", "text": "alpha text", "score": 0.91, "document_id": "d1"},
        {"chunk_id": "d1-chunk-3", "text": "beta text", "score": 0.42, "document_id": "d1"},
    ]
    app.dependency_overrides.clear()


def test_search_top_k_defaults():
    eq = FakeEq()
    app.dependency_overrides[get_edgequake] = lambda: eq
    c = TestClient(app)
    r = c.post("/search", json={"workspace_id": "kb1", "query": "q"})
    assert r.status_code == 200
    # a sensible default top_k is applied when the consumer omits it.
    assert eq.search_calls[0]["top_k"] == 10
    app.dependency_overrides.clear()


def test_edgequake_search_calls_query_with_workspace_header():
    """EdgequakeClient.search POSTs /api/v1/query with the workspace header and
    maps top_k -> max_results, returning the raw query response."""
    from service.edgequake import EdgequakeClient

    captured = {}

    class FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"answer": "a", "mode": "hybrid",
                    "sources": [{"source_type": "chunk", "id": "x-chunk-0",
                                 "score": 0.5, "snippet": "s", "document_id": "x"}]}

    class FakeHttp:
        def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return FakeResp()

    eq = EdgequakeClient.__new__(EdgequakeClient)
    eq.base = "http://eq:8081"
    eq.http = FakeHttp()

    out = eq.search(workspace_id=EQ_WS, query="hello", top_k=7)
    assert captured["url"] == "http://eq:8081/api/v1/query"
    # workspace-scoped via X-Workspace-ID header.
    assert captured["headers"].get("X-Workspace-ID") == EQ_WS
    # top_k mapped to edgequake's max_results.
    assert captured["json"]["query"] == "hello"
    assert captured["json"]["max_results"] == 7
    assert out["answer"] == "a"
    assert out["sources"][0]["id"] == "x-chunk-0"


# ── global 모드 (B) ────────────────────────────────────────────────────────
#
# 여기 있는 것은 **PG 불요** 항목뿐이다 — mode 검증 순서, 축(eq_ws), clamp, 설정 가드,
# 오류 매핑. advisory lock 의 실효 상한·테이블 부재 fail-open 같은 SQL 의미론은
# test_global_search_pg.py(requires_pg)가 맡는다.

import pytest

import service.app as app_mod


@pytest.fixture()
def _llm_env(monkeypatch):
    """세 변수를 채운다 — _llm_configured() 가 통과해야 그 뒤 분기가 검증된다."""
    monkeypatch.setenv("KBP_OPENAI_API_KEY", "k")
    monkeypatch.setenv("KBP_OPENAI_BASE_URL", "http://llm.internal/v1")
    monkeypatch.setenv("KBP_LLM_MODEL", "m")
    monkeypatch.setenv("KBP_PG_DSN", "postgres://x/y")


def _client(eq=None):
    fake = eq or FakeEq()
    app.dependency_overrides[get_edgequake] = lambda: fake
    return TestClient(app), fake


def test_local_mode_is_the_default_and_response_gains_mode():
    """기본값이 local 이라 기존 호출자는 무변경이다. 응답에 mode 만 더한다."""
    c, _ = _client()
    try:
        r = c.post("/search", json={"workspace_id": "kb-1", "query": "q"})
        assert r.status_code == 200
        body = r.json()
        assert body["mode"] == "local"
        assert body["answer"] == "the answer"
        assert len(body["results"]) == 2          # 기존 정규화 계약 유지
    finally:
        app.dependency_overrides.clear()


def test_bad_mode_is_rejected_before_creating_a_workspace():
    """검증이 ensure_workspace 앞에 있어야 한다 — 뒤면 잘못된 요청이 workspace 를 만든다."""
    c, fake = _client()
    try:
        r = c.post("/search", json={"workspace_id": "kb-1", "query": "q",
                                    "mode": "bogus"})
        assert r.status_code == 400
        assert fake.ensured == []                 # ★ 부작용 없음
    finally:
        app.dependency_overrides.clear()


def test_global_requires_dsn(monkeypatch):
    monkeypatch.delenv("KBP_PG_DSN", raising=False)
    c, _ = _client()
    try:
        r = c.post("/search", json={"workspace_id": "kb-1", "query": "q",
                                    "mode": "global"})
        assert r.status_code == 503 and "KBP_PG_DSN" in r.json()["detail"]
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("missing", ["KBP_OPENAI_API_KEY", "KBP_OPENAI_BASE_URL",
                                     "KBP_LLM_MODEL"])
def test_global_requires_all_three_llm_vars(monkeypatch, _llm_env, missing):
    """세 변수를 **모두** 봐야 한다.

    compose 가 셋 다 기본값 없이 주입하므로 미설정 시 **빈 문자열**이 들어온다.
    `os.environ.get(k, default)` 는 "있는데 빈 값" 에 default 를 적용하지 않아, base=""
    로 요청을 보내 UnsupportedProtocol 로 뒤늦게 500 이 난다.
    """
    monkeypatch.setenv(missing, "")               # 빈 문자열 = 미설정과 같은 효과
    c, _ = _client()
    try:
        r = c.post("/search", json={"workspace_id": "kb-1", "query": "q",
                                    "mode": "global"})
        assert r.status_code == 503 and "LLM not configured" in r.json()["detail"]
    finally:
        app.dependency_overrides.clear()


def test_global_uses_eq_uuid_not_kb_id(monkeypatch, _llm_env):
    """리포트 조회는 **eq UUID** 로 해야 한다.

    community_reports.workspace_id 가 그 축인데 facade 는 kb id 를 받는다. kb id 를
    그대로 넘기면 영구히 0행이 되어 오류 없이 매번 거짓 안내가 뜬다.
    """
    seen = {}
    monkeypatch.setattr(app_mod, "_acquire_global_slot", lambda dsn, limit: 1)
    monkeypatch.setattr(app_mod, "_release_global_slot", lambda dsn, sid: None)
    monkeypatch.setattr(app_mod, "reports_exist",
                        lambda ws, dsn, **kw: seen.setdefault("ws", ws) is None or False)
    c, _ = _client()
    try:
        c.post("/search", json={"workspace_id": "kb-1", "query": "q", "mode": "global"})
        assert seen["ws"] == EQ_WS
        assert seen["ws"] != "kb-1"
    finally:
        app.dependency_overrides.clear()


def test_global_not_ready_returns_the_same_key_set(monkeypatch, _llm_env):
    """ready/not-ready 두 분기의 **응답 키 집합이 같아야** 소비자가 분기하지 않는다."""
    monkeypatch.setattr(app_mod, "_acquire_global_slot", lambda dsn, limit: 1)
    monkeypatch.setattr(app_mod, "_release_global_slot", lambda dsn, sid: None)
    monkeypatch.setattr(app_mod, "reports_exist", lambda ws, dsn, **kw: False)
    monkeypatch.setattr(app_mod, "newest_report_time",
                        lambda ws, dsn, **kw: (None, None, 0))
    c, _ = _client()
    try:
        body = c.post("/search", json={"workspace_id": "kb-1", "query": "q",
                                       "mode": "global"}).json()
        assert body["community_reports_ready"] is False
        assert body["answer"] is None and body["communities"] == []
        not_ready_keys = set(body)
    finally:
        app.dependency_overrides.clear()

    monkeypatch.setattr(app_mod, "reports_exist", lambda ws, dsn, **kw: True)
    monkeypatch.setattr(app_mod, "newest_report_time",
                        lambda ws, dsn, **kw: (None, None, 3))
    monkeypatch.setattr(app_mod, "global_search",
                        lambda q, ws, **kw: {"answer": "A", "sources": [1, 2]})
    c, _ = _client()
    try:
        body = c.post("/search", json={"workspace_id": "kb-1", "query": "q",
                                       "mode": "global"}).json()
        assert body["community_reports_ready"] is True
        assert set(body) == not_ready_keys        # ★ 키 집합 동일
    finally:
        app.dependency_overrides.clear()


def test_global_top_k_is_clamped(monkeypatch, _llm_env):
    """facade 의 top_k 는 반환 청크 수지만 global 의 top_k 는 **순차 LLM 호출 수** 다.
    그대로 흘리면 기본이 11회 직렬 LLM 이 된다.
    """
    seen = {}
    monkeypatch.setattr(app_mod, "_acquire_global_slot", lambda dsn, limit: 1)
    monkeypatch.setattr(app_mod, "_release_global_slot", lambda dsn, sid: None)
    monkeypatch.setattr(app_mod, "reports_exist", lambda ws, dsn, **kw: True)
    monkeypatch.setattr(app_mod, "newest_report_time",
                        lambda ws, dsn, **kw: (None, None, 1))
    monkeypatch.setattr(app_mod, "global_search",
                        lambda q, ws, **kw: seen.update(top_k=kw["top_k"]) or
                        {"answer": "A", "sources": []})
    c, _ = _client()
    try:
        for asked, expect in ((99, 5), (0, 1), (3, 3)):
            c.post("/search", json={"workspace_id": "kb-1", "query": "q",
                                    "mode": "global", "global_top_k": asked})
            assert seen["top_k"] == expect, f"{asked} → {seen['top_k']} != {expect}"
    finally:
        app.dependency_overrides.clear()


def test_global_llm_failure_is_422_not_5xx(monkeypatch, _llm_env):
    """kb 클라이언트는 429 또는 >=500 을 재시도한다 — 5xx 면 분 단위 map-reduce 가 3배가 된다."""
    import httpx as _httpx

    monkeypatch.setattr(app_mod, "_acquire_global_slot", lambda dsn, limit: 1)
    monkeypatch.setattr(app_mod, "_release_global_slot", lambda dsn, sid: None)
    monkeypatch.setattr(app_mod, "reports_exist", lambda ws, dsn, **kw: True)
    monkeypatch.setattr(app_mod, "newest_report_time",
                        lambda ws, dsn, **kw: (None, None, 1))

    def boom(*a, **kw):
        # ★ 형제 클래스. (TimeoutException, HTTPStatusError) 로 좁게 잡으면 이게 새어
        #   500 이 된다 — httpx.HTTPError 로 잡아야 걸린다.
        raise _httpx.ConnectError("connection refused")

    monkeypatch.setattr(app_mod, "global_search", boom)
    c, _ = _client()
    try:
        r = c.post("/search", json={"workspace_id": "kb-1", "query": "q",
                                    "mode": "global"})
        assert r.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_global_busy_returns_503(monkeypatch, _llm_env):
    monkeypatch.setattr(app_mod, "_acquire_global_slot", lambda dsn, limit: None)
    c, _ = _client()
    try:
        r = c.post("/search", json={"workspace_id": "kb-1", "query": "q",
                                    "mode": "global"})
        assert r.status_code == 503 and "busy" in r.json()["detail"]
    finally:
        app.dependency_overrides.clear()


def test_slot_is_released_even_on_failure(monkeypatch, _llm_env):
    """finally 절이 슬롯을 반드시 반납한다 — 안 하면 상한이 조금씩 잠식된다."""
    import httpx as _httpx

    released = []
    monkeypatch.setattr(app_mod, "_acquire_global_slot", lambda dsn, limit: 7)
    monkeypatch.setattr(app_mod, "_release_global_slot",
                        lambda dsn, sid: released.append(sid))
    monkeypatch.setattr(app_mod, "reports_exist", lambda ws, dsn, **kw: True)
    monkeypatch.setattr(app_mod, "newest_report_time",
                        lambda ws, dsn, **kw: (None, None, 1))
    monkeypatch.setattr(app_mod, "global_search",
                        lambda *a, **kw: (_ for _ in ()).throw(_httpx.ReadTimeout("t")))
    c, _ = _client()
    try:
        c.post("/search", json={"workspace_id": "kb-1", "query": "q", "mode": "global"})
        assert released == [7]
    finally:
        app.dependency_overrides.clear()


def test_db_error_on_slot_acquire_is_503_not_500(monkeypatch, _llm_env):
    """테이블 부재는 가상이 아니다 — facade lifespan 이 ensure_schema 실패를 삼키고 뜬다.
    500 이면 kb 가 재시도한다.
    """
    import psycopg as _pg

    def boom(dsn, limit):
        raise _pg.errors.UndefinedTable("relation kbp.global_search_slots does not exist")

    monkeypatch.setattr(app_mod, "_acquire_global_slot", boom)
    c, _ = _client()
    try:
        r = c.post("/search", json={"workspace_id": "kb-1", "query": "q",
                                    "mode": "global"})
        assert r.status_code == 503
    finally:
        app.dependency_overrides.clear()


def test_settings_are_read_per_request(monkeypatch):
    """모듈 상수로 두면 monkeypatch.setenv 가 안 먹고, 오타가 import 시점 기동 실패가 된다."""
    monkeypatch.setenv("KBP_GLOBAL_SEARCH_CONCURRENCY", "7")
    assert app_mod._global_concurrency() == 7
    monkeypatch.setenv("KBP_GLOBAL_SEARCH_CONCURRENCY", "삼")   # 파싱 실패
    assert app_mod._global_concurrency() == 2                    # 기본값 폴백
    monkeypatch.setenv("KBP_GLOBAL_LLM_TIMEOUT", "")             # 빈 값
    assert app_mod._global_llm_timeout() == 60.0


def test_slot_ttl_follows_the_llm_timeout(monkeypatch):
    """TTL 하드코딩 금지 — TTL < 실제 소요면 살아있는 슬롯이 지워져 상한이 사라진다."""
    monkeypatch.setenv("KBP_GLOBAL_LLM_TIMEOUT", "60")
    assert app_mod._slot_ttl_seconds() == (5 + 1) * 60 * 2
    monkeypatch.setenv("KBP_GLOBAL_LLM_TIMEOUT", "300")          # 관례값을 넣어도
    assert app_mod._slot_ttl_seconds() == (5 + 1) * 300 * 2      # TTL 이 따라 커진다
