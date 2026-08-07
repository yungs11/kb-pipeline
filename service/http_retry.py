"""폴링용 HTTP 재시도 헬퍼 — keep-alive 경합으로 끊긴 연결을 흡수한다.

**왜 필요한가**(실측 2026-08-07, 폐쇄망 격리망 e2e):

facade 는 adaptive_chunk·edgequake 의 비동기 잡을 ``poll_interval=3.0`` 초 간격으로
GET 폴링한다. 그런데 두 서비스 모두 gunicorn 으로 뜨고 **gunicorn 의 기본 keep-alive 는
2초**다(``--keep-alive`` 미지정). 즉 서버는 유휴 연결을 2초에 닫는데 클라이언트는 3초 뒤
그 풀링된 연결을 재사용한다 — 닫는 순간과 재사용이 겹치면::

    httpx.RemoteProtocolError: Server disconnected without sending a response.
    httpx.ReadError: [Errno 104] Connection reset by peer

가 뜬다. 재시도가 없으면 **잡은 성공했는데 폴링 한 번 실패로 적재 전체가 실패**한다
(실측: adaptive_chunk 잡은 ``succeeded`` 인데 facade 는 "chunk 실패"로 문서를 failed 처리).
간헐적이라 재현이 들쭉날쭉하고, 원인이 폴링 대상 서비스가 아니라 **연결 수명**에 있어
로그만 봐서는 진단이 매우 어렵다.

**왜 재시도가 안전한가**: 대상은 전부 GET(잡 상태 조회) 또는 멱등 조회다. 게다가 요청이
서버에 **도달하지 못한** 전송 계층 실패만 잡는다(``httpx.TransportError``) — 응답을 받은
뒤의 HTTP 에러(4xx/5xx)는 여기서 삼키지 않고 그대로 올린다.
"""
from __future__ import annotations

import time

import httpx

#: 전송 계층 재시도 횟수. keep-alive 경합은 즉시 재연결하면 거의 항상 붙으므로 2회면 족하다
#: (서비스가 진짜 죽었으면 재시도해도 같은 예외가 나고 그때는 원래대로 올린다).
DEFAULT_RETRIES = 2
#: 재시도 간 짧은 백오프(초). 서버가 막 연결을 닫는 중일 수 있어 0 보다는 크게 둔다.
DEFAULT_BACKOFF = 0.5


def polling_client(timeout: float) -> httpx.Client:
    """폴링용 httpx 클라이언트 — **keep-alive 비활성**.

    재시도만으로는 부족했다(실측 2026-08-07): 죽은 연결이 풀에 남아 재시도까지 같은
    연결을 집어 2회 연속 실패 → 잡이 succeeded 인데도 적재가 실패했다. 유휴 연결을
    아예 두지 않으면(``max_keepalive_connections=0``) 매 요청이 새 연결이라 이 실패
    계열 자체가 사라진다. 비용은 폴링 주기(3초)마다 TCP 핸드셰이크 한 번 — 내부망
    평문 통신이라 무시할 수준이고, 간헐적 적재 실패를 없애는 값으로 싸다.
    ``get_with_retry`` 는 그래도 함께 쓴다(진짜 일시 장애 대비 이중 안전장치).
    """
    return httpx.Client(
        timeout=timeout,
        limits=httpx.Limits(max_keepalive_connections=0),
    )


def get_with_retry(client: httpx.Client, url: str, *, headers=None,
                   retries: int = DEFAULT_RETRIES,
                   backoff: float = DEFAULT_BACKOFF, **kwargs) -> httpx.Response:
    """``client.get`` + 전송 계층 실패 재시도.

    ``httpx.TransportError``(RemoteProtocolError/ReadError/ConnectError 등)만 재시도한다.
    마지막 시도까지 실패하면 원래 예외를 그대로 올린다(삼키지 않는다).
    """
    # headers 가 없으면 **인자 자체를 넘기지 않는다** — 호출 형태를 기존 `client.get(url)`
    # 과 동일하게 유지해, 얇은 페이크(테스트의 `def get(self, url)`)와 계약이 어긋나지 않게 한다.
    if headers is not None:
        kwargs["headers"] = headers
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return client.get(url, **kwargs)
        except httpx.TransportError as exc:
            last = exc
            if attempt >= retries:
                raise
            time.sleep(backoff)
    # 도달 불가(위 raise 로 빠진다) — 타입체커 안심용.
    raise last  # type: ignore[misc]
