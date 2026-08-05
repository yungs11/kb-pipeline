"""X-Facade-Key 게이트 — 핸들러보다 먼저 도는 의존성이라 다운스트림과 무관해야 한다.

게이트 키는 **요청 시점에** 읽는다(`service/app.py`). 예전에는 모듈 스코프에 고정돼 있어
값을 바꾸려면 `importlib.reload(service.app)` 를 해야 했는데, reload 는 `app` 객체를 새로
만들어 이미 `from service.app import app` 해 둔 다른 테스트 모듈의
`dependency_overrides` 를 통째로 무효화한다 — 그 모듈들이 fake 대신 **진짜
parse-svc·MinIO 를 때렸다**. 그래서 이 파일은 더 이상 reload 하지 않는다.

"모듈 스코프에 DB·MinIO 인스턴스를 두지 않는다"는 §6 불변식은 **서브프로세스**에서
확인한다(전역 상태를 오염시키지 않으려고).
"""
import subprocess
import sys

from fastapi.testclient import TestClient

import service.app as app_mod


class _FakeEdgequake:
    """게이트 통과 여부만 보면 되므로 검색은 빈 결과를 돌려주면 충분하다."""

    def ensure_workspace(self, workspace_id, name=None):
        return "00000000-0000-0000-0000-0000000000ff"

    def search(self, *, workspace_id, query, top_k):
        return {"answer": "", "sources": []}


def _client():
    app_mod.app.dependency_overrides[app_mod.get_edgequake] = _FakeEdgequake
    return TestClient(app_mod.app)


def test_search_rejected_without_key(monkeypatch):
    monkeypatch.setenv("KBP_FACADE_KEY", "s3cret")
    client = _client()
    body = {"workspace_id": "kb-x", "query": "q"}
    assert client.post("/search", json=body).status_code == 401
    assert client.post("/search", json=body,
                       headers={"X-Facade-Key": "s3cret"}).status_code == 200


def test_gate_disabled_when_env_unset(monkeypatch):
    monkeypatch.delenv("KBP_FACADE_KEY", raising=False)
    assert _client().post("/search",
                          json={"workspace_id": "kb-x", "query": "q"}).status_code == 200


def test_empty_key_is_treated_as_unset(monkeypatch):
    """빈 문자열은 미설정과 동일 취급.

    compose 에서 `${KBP_FACADE_KEY}` 가 미정의면 **빈 문자열**이 주입되는데, 이를 게이트
    ON 으로 보면 전 엔드포인트가 401 이 된다(폐쇄망 자산에 이 키가 아직 없다 — D12).
    """
    monkeypatch.setenv("KBP_FACADE_KEY", "")
    assert _client().post("/search",
                          json={"workspace_id": "kb-x", "query": "q"}).status_code == 200


def test_jobs_routes_are_gated(monkeypatch):
    """신규 /jobs/* 는 게이트 대상 — 파일 staging·DB 행·worker 시간을 소비한다."""
    monkeypatch.setenv("KBP_FACADE_KEY", "s3cret")
    c = _client()
    assert c.get("/jobs/workers").status_code == 401
    assert c.get("/jobs/workers", headers={"X-Facade-Key": "s3cret"}).status_code == 200


def test_legacy_parse_stays_ungated(monkeypatch):
    """레거시 /parse 의 인증 요구는 Phase 1 동안 바뀌지 않는다.

    문서가 "의도적으로 열려 있다"고 명시하고 있고, kb 파사드 키가 미설정인 배포에서
    게이트를 채우면 kb 가 즉시 401 을 맞는다.
    """
    monkeypatch.setenv("KBP_FACADE_KEY", "s3cret")
    r = _client().post("/parse", files={"file": ("a.txt", b"x", "text/plain")})
    assert r.status_code != 401


def test_import_does_not_touch_db_or_minio():
    """§6 불변식 — 모듈 import 만으로 DB·MinIO 에 붙지 않는다.

    `JobRepo`/`JobBlobStore` 를 모듈 스코프에 두면 여기서 깨진다. 서브프로세스로 도는
    이유는 `importlib.reload` 가 `app` 객체를 갈아치워 다른 테스트를 오염시키기 때문이다.
    """
    code = (
        "import os;"
        "os.environ.pop('KBP_PG_DSN', None);"
        "os.environ.pop('MINIO_ENDPOINT', None);"
        "import service.app as m;"
        "assert m.app is not None;"
        "print('ok')"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout


# ── D12: 폐쇄망 배포에서 게이트가 조용히 꺼지지 않게 ────────────────────────

def test_empty_key_disables_the_gate_like_unset(monkeypatch):
    """compose 가 `${KBP_FACADE_KEY:-}` 로 빈 값을 주입해도 401 폭탄이 되면 안 된다.

    반대로 `${KBP_FACADE_KEY:?}` 로 쓰면 스택이 기동조차 못 한다. 그래서 코드는 빈 값을
    미설정과 동일 취급하고, **배포 전 차단은 verify-bundle.sh 가** 한다(REQUIRED_ENV).
    """
    from service.app import require_facade_key

    monkeypatch.setenv("KBP_FACADE_KEY", "")
    require_facade_key(x_facade_key=None)      # 통과해야 한다(예외 없음)

    monkeypatch.setenv("KBP_FACADE_KEY", "   ")
    require_facade_key(x_facade_key=None)      # 공백만 있는 값도 미설정 취급


def test_verify_bundle_requires_the_facade_key():
    """빈 채로 배포되면 무인증 적재·삭제가 호스트 포트로 열린다 — 배포 게이트가 막아야 한다."""
    import pathlib
    import re

    src = pathlib.Path(__file__).resolve().parents[2] / "scripts/airgap/verify-bundle.sh"
    block = re.search(r"REQUIRED_ENV=\((.*?)\)", src.read_text(), re.S)
    assert block, "REQUIRED_ENV 블록을 찾지 못했다"
    assert "KBP_FACADE_KEY" in block.group(1)


def test_airgap_env_template_lists_the_facade_key():
    """템플릿에 없으면 배포자가 채워야 한다는 사실 자체를 모른다."""
    import pathlib

    tpl = (pathlib.Path(__file__).resolve().parents[2] / ".env.airgap.example").read_text()
    assert "\nKBP_FACADE_KEY=" in tpl


def test_airgap_compose_passes_the_key_to_facade_and_worker():
    """worker 도 facade 호출을 하므로 같은 키가 필요하다(x-facade-env 공유)."""
    import pathlib

    y = (pathlib.Path(__file__).resolve().parents[2] / "docker-compose.airgap.yml").read_text()
    assert "KBP_FACADE_KEY: ${KBP_FACADE_KEY:-}" in y
    # 두 서비스가 같은 앵커를 쓴다 — 한쪽만 키를 받으면 worker 호출이 401 이 된다.
    assert y.count("environment: *facade_env") >= 2
