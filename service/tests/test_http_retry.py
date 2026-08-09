"""폴링 전송 계층 재시도 — keep-alive 경합 회귀 가드.

실측(2026-08-07, 폐쇄망 격리망 e2e): adaptive_chunk 잡이 `succeeded` 인데도 facade 의
폴링 GET 한 번이 `RemoteProtocolError: Server disconnected without sending a response.`
로 죽어 **적재 전체가 실패**했다. 원인은 poll_interval(3s) > gunicorn 기본 keep-alive(2s)
경합. 이 테스트는 "첫 GET 이 전송 계층 오류로 죽어도 폴링이 결과를 얻어낸다"를 고정한다.
"""
from __future__ import annotations

import httpx
import pytest

from service.http_retry import get_with_retry


def _max_keepalive(client: httpx.Client) -> int:
    """httpx 내부 커넥션 풀의 유휴 연결 상한(공개 API 가 없어 풀을 직접 본다)."""
    return client._transport._pool._max_keepalive_connections


class _FlakyClient:
    """첫 N회는 TransportError, 그 다음부터 정상 응답."""

    def __init__(self, fail_times: int, exc: Exception | None = None):
        self.fail_times = fail_times
        self.calls = 0
        self.exc = exc or httpx.RemoteProtocolError(
            "Server disconnected without sending a response."
        )

    def get(self, url, headers=None, **kw):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc
        return httpx.Response(200, json={"status": "succeeded"}, request=httpx.Request("GET", url))


def test_retries_transport_error_then_succeeds():
    c = _FlakyClient(fail_times=1)
    r = get_with_retry(c, "http://x/chunk/jobs/1", backoff=0)
    assert r.status_code == 200
    assert r.json()["status"] == "succeeded"
    assert c.calls == 2, "첫 실패 후 정확히 한 번 더 시도해야 한다"


def test_read_error_is_also_retried():
    """edgequake 경로에서 실측된 ReadError(Connection reset by peer)도 같은 계열."""
    c = _FlakyClient(fail_times=1, exc=httpx.ReadError("[Errno 104] Connection reset by peer"))
    r = get_with_retry(c, "http://x/api/v1/tasks/1", backoff=0)
    assert r.status_code == 200
    assert c.calls == 2


def test_gives_up_and_reraises_after_retries():
    """서비스가 진짜 죽었으면 삼키지 않고 원래 예외를 올린다."""
    c = _FlakyClient(fail_times=99)
    with pytest.raises(httpx.RemoteProtocolError):
        get_with_retry(c, "http://x/chunk/jobs/1", retries=2, backoff=0)
    assert c.calls == 3, "최초 1회 + 재시도 2회"


def test_http_error_response_is_not_retried():
    """4xx/5xx 는 응답을 받은 것이므로 재시도 대상이 아니다(그대로 반환)."""

    class _Always500:
        def __init__(self):
            self.calls = 0

        def get(self, url, headers=None, **kw):
            self.calls += 1
            return httpx.Response(500, request=httpx.Request("GET", url))

    c = _Always500()
    r = get_with_retry(c, "http://x/y", backoff=0)
    assert r.status_code == 500
    assert c.calls == 1, "HTTP 에러는 전송 계층 실패가 아니므로 재시도하지 않는다"


def test_polling_client_disables_keepalive():
    """폴링 클라이언트는 유휴 연결을 두지 않는다 — 죽은 keep-alive 재사용 실패 계열 제거.

    실측(2026-08-07): 재시도만으로는 부족했다(풀에 남은 죽은 연결을 재시도까지 집어
    2회 연속 실패 → 잡이 succeeded 인데도 적재 실패).
    """
    from service.http_retry import polling_client

    c = polling_client(600.0)
    assert _max_keepalive(c) == 0
    c.close()


def test_clients_use_polling_client():
    """adaptive_chunk·edgequake 클라이언트가 실제로 그 설정을 쓴다(회귀 가드)."""
    from service.adaptive_chunk import AdaptiveChunkClient
    from service.edgequake import EdgequakeClient

    for cli in (AdaptiveChunkClient("http://a:1"), EdgequakeClient("http://b:2")):
        assert _max_keepalive(cli.http) == 0
        cli.http.close()


# ─────────────────────────────────────────────────────────────────────────────
# check() — 실패 응답의 **본문**을 로그에 남긴다.
#
# 왜 필요한가(실측 2026-08-07): 클라이언트들이 전부 `raise_for_status()` 만 써서
# 예외 메시지에 상태코드·URL 밖에 없었다. 정작 실패 이유는 응답 본문에 있는데
# facade 로그 어디에도 안 남아, 장애 때 다운스트림 컨테이너 로그를 따로 뒤져야 했다.
# 아래 테스트는 "실패하면 본문이 로그에 남는다 + 동작(예외)은 그대로"를 고정한다.
# ─────────────────────────────────────────────────────────────────────────────
import logging

from service.http_retry import BODY_LOG_LIMIT, check


def _resp(status: int, body: str = "", url: str = "http://eq:8081/api/v1/query"):
    return httpx.Response(status, text=body, request=httpx.Request("POST", url))


def test_check_2xx_는_그대로_통과하고_로그를_남기지_않는다(caplog):
    r = _resp(200, "ok")
    with caplog.at_level(logging.DEBUG, logger="kb_pipeline.service.http_retry"):
        assert check(r, what="검색") is r
    assert caplog.records == []


def test_check_실패시_상태_URL_본문을_모두_로그에_남긴다(caplog):
    r = _resp(502, '{"detail":"upstream refused"}')
    with caplog.at_level(logging.ERROR, logger="kb_pipeline.service.http_retry"):
        with pytest.raises(httpx.HTTPStatusError):   # 동작은 기존과 동일
            check(r, what="edgequake 검색")
    msg = caplog.text
    assert "edgequake 검색" in msg          # 어느 호출인지
    assert "502" in msg                      # 상태
    assert "/api/v1/query" in msg            # URL
    assert "upstream refused" in msg         # ← 기존에 없던 것: 본문


def test_check_는_거대한_본문을_잘라_로그를_뒤덮지_않는다(caplog):
    r = _resp(500, "x" * (BODY_LOG_LIMIT * 5))
    with caplog.at_level(logging.ERROR, logger="kb_pipeline.service.http_retry"):
        with pytest.raises(httpx.HTTPStatusError):
            check(r, what="대용량")
    assert caplog.text.count("x") <= BODY_LOG_LIMIT


def test_check_는_얇은_페이크_응답에도_안전하다(caplog):
    """테스트용 가짜 응답(status_code 만 있는 객체)에서 AttributeError 로 죽지 않는다."""
    class _Thin:
        status_code = 503
        text = "boom"

    with caplog.at_level(logging.ERROR, logger="kb_pipeline.service.http_retry"):
        check(_Thin(), what="얇은페이크")     # raise_for_status 가 없으면 예외 없이 통과
    assert "boom" in caplog.text


def test_재시도가_일어나면_경고로_흔적이_남는다(caplog):
    """재시도는 성공하면 흔적이 없어, 경합이 늘고 있는지 알 수 없었다."""
    c = _FlakyClient(fail_times=1)
    with caplog.at_level(logging.WARNING, logger="kb_pipeline.service.http_retry"):
        get_with_retry(c, "http://ac:18060/chunk/jobs/1", backoff=0)
    assert "재시도" in caplog.text
