from fastapi.testclient import TestClient


def test_search_rejected_without_key(monkeypatch):
    monkeypatch.setenv("KBP_FACADE_KEY", "s3cret")
    import importlib, service.app as app_mod
    importlib.reload(app_mod)
    client = TestClient(app_mod.app)
    r = client.post("/search", json={"workspace_id": "kb-x", "query": "q"})
    assert r.status_code == 401
    r2 = client.post("/search", json={"workspace_id": "kb-x", "query": "q"},
                     headers={"X-Facade-Key": "s3cret"})
    assert r2.status_code != 401  # passes the gate (may 5xx if edgequake down — fine)


def test_gate_disabled_when_env_unset(monkeypatch):
    monkeypatch.delenv("KBP_FACADE_KEY", raising=False)
    import importlib, service.app as app_mod
    importlib.reload(app_mod)
    client = TestClient(app_mod.app)
    r = client.post("/search", json={"workspace_id": "kb-x", "query": "q"})
    assert r.status_code != 401  # gate disabled
