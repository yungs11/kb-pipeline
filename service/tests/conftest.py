"""facade 테스트 공통 배선 — 잡 큐를 인메모리 + 인라인으로 돌린다.

설계 §6.2.

레거시 4경로(`/parse`·`/chunk`·`/insert`·`/ingest`)가 잡을 경유하게 되면서, 이 경로를
테스트하려면 (1) 잡을 저장할 곳과 (2) 실행할 주체가 필요해졌다. 기존 테스트 파일들은
`TestClient` + `dependency_overrides` 로만 돌고 DB·MinIO fixture 가 없다.

여기서 두 가지를 자동 주입해 **기존 단언을 그대로 살린다**:

  1. `get_job_repo`/`get_job_blobs` → 인메모리 더블
  2. `app.state.job_inline = True` → 제출 즉시 같은 프로세스에서 runner 실행

인라인 모드는 **테스트에서만** 켠다. 프로덕션 코드 경로에는 이걸 켜는 env 도 기본값도
없다(`app.state` 를 세우는 곳이 여기뿐이다).

각 테스트가 `dependency_overrides[get_parse_client]` 등으로 주입한 fake 는 그대로
유효하다 — `_job_runner` 가 app 의 팩토리를 통과시키므로 오버라이드가 살아난다.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def job_queue_inline():
    """모든 facade 테스트에서 잡 큐를 인메모리·인라인으로 돌린다."""
    from service.app import _job_blobs, _job_repo, app
    from service.jobs.memory import InMemoryBlobStore, InMemoryJobRepo

    repo = InMemoryJobRepo()
    blobs = InMemoryBlobStore()
    app.dependency_overrides[_job_repo] = lambda: repo
    app.dependency_overrides[_job_blobs] = lambda: blobs
    app.state.job_inline = True
    try:
        yield {"repo": repo, "blobs": blobs}
    finally:
        app.dependency_overrides.pop(_job_repo, None)
        app.dependency_overrides.pop(_job_blobs, None)
        app.state.job_inline = False


@pytest.fixture(autouse=True)
def _clear_overrides():
    """테스트가 남긴 다운스트림 오버라이드가 다음 테스트로 새지 않게 한다."""
    from service.app import app

    yield
    for dep in list(app.dependency_overrides):
        name = getattr(dep, "__name__", "")
        if name in {"get_parse_client", "get_adaptive_chunk", "get_edgequake"}:
            app.dependency_overrides.pop(dep, None)
