"""X-Facade-Key 게이트 — 핸들러보다 먼저 도는 의존성이라 다운스트림과 무관해야 한다.

이 테스트들은 ``importlib.reload(service.app)`` 로 모듈을 통째로 다시 읽는다(게이트 키가
모듈 스코프에서 읽히기 때문). 그래서 **``service.app`` 모듈 스코프에 DB·MinIO 클라이언트
인스턴스를 만들면 안 된다** — reload 만으로 실 접속을 시도하게 된다(설계 §6 불변식).

과거에는 게이트 통과 확인이 살아있는 edgequake(:8081)에 의존했다. "may 5xx if edgequake
down — fine" 이라고 주석은 달려 있었지만 TestClient 가 서버 예외를 재발생시켜
``status_code`` 를 보기도 전에 터졌다. 게이트는 다운스트림 이전 단계이므로 fake 를 주입해
격리한다.
"""
import importlib

from fastapi.testclient import TestClient


class _FakeEdgequake:
    """게이트 통과 여부만 보면 되므로 검색은 빈 결과를 돌려주면 충분하다."""

    def ensure_workspace(self, workspace_id, name=None):
        return "00000000-0000-0000-0000-0000000000ff"

    def search(self, *, workspace_id, query, top_k):
        return {"answer": "", "sources": []}


def _client(app_mod):
    app_mod.app.dependency_overrides[app_mod.get_edgequake] = _FakeEdgequake
    return TestClient(app_mod.app)


def _reload(monkeypatch, key):
    import service.app as app_mod

    if key is None:
        monkeypatch.delenv("KBP_FACADE_KEY", raising=False)
    else:
        monkeypatch.setenv("KBP_FACADE_KEY", key)
    importlib.reload(app_mod)
    return app_mod


def test_search_rejected_without_key(monkeypatch):
    app_mod = _reload(monkeypatch, "s3cret")
    client = _client(app_mod)
    assert client.post("/search", json={"workspace_id": "kb-x", "query": "q"}).status_code == 401
    ok = client.post("/search", json={"workspace_id": "kb-x", "query": "q"},
                     headers={"X-Facade-Key": "s3cret"})
    assert ok.status_code == 200
    app_mod.app.dependency_overrides.clear()


def test_gate_disabled_when_env_unset(monkeypatch):
    app_mod = _reload(monkeypatch, None)
    client = _client(app_mod)
    assert client.post("/search", json={"workspace_id": "kb-x", "query": "q"}).status_code == 200
    app_mod.app.dependency_overrides.clear()


def test_reload_does_not_touch_db_or_minio(monkeypatch):
    """모듈 스코프 인스턴스 금지의 회귀 가드(설계 §6).

    DSN·MinIO 자격증명을 지운 뒤 reload 해도 예외가 없어야 한다. JobRepo/JobBlobStore 를
    모듈 스코프에 두면 여기서 깨진다.
    """
    monkeypatch.delenv("KBP_PG_DSN", raising=False)
    monkeypatch.delenv("MINIO_ENDPOINT", raising=False)
    app_mod = _reload(monkeypatch, None)
    assert app_mod.app is not None
