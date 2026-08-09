"""doc_guard(:8000) 얇은 클라이언트 — facade 가 게이트를 은닉하기 위한 것.

설계: ``docs/superpowers/specs/2026-08-04-facade-gate-object-api-design.md``

지금까지 kb 가 doc_guard 를 **직접** 찔렀다(`docguard_base_url = localhost:8000`).
그래서 주소가 어긋나도 소비자 쪽에서만 터졌다 — 실측(2026-08-04) 당시 kb 는 `:8000` 을
보는데 doc_guard 는 `:8001` 에 있어 xlsx 적재가 게이트에서 통째로 실패하고 있었다.
facade 뒤로 넣으면 주소를 아는 곳이 compose 의 인트라스택 DNS 한 곳으로 줄어든다.

**응답을 변형하지 않는다.** 소비자(kb 의 `_build_gate_popup`)가 doc_guard 원형 필드
(`result`·`findings`·`customer_message`·`summary`)를 그대로 읽는다. 정규화하면 소비자와
프론트가 함께 깨진다. facade 가 더하는 값은 은닉과 재사용 가능성뿐이다.
"""
from __future__ import annotations

import os
from typing import Any

import httpx
from service.http_retry import check


class DocGuardClient:
    def __init__(self, base_url: str, timeout: float = 60.0) -> None:
        self.base = base_url.rstrip("/")
        self.http = httpx.Client(timeout=timeout)

    def check_excel(self, *, filename: str, gate_summary: dict[str, Any]) -> dict[str, Any]:
        """파서-후단 엑셀 게이트 — ``gate_summary`` 로 판정을 받는다.

        ``gate_summary`` 는 parse-svc 가 in-process 로 계산해 ``/parse`` 응답에 실어준 것이다.
        산출(parse-svc)과 판정(doc_guard)이 다른 서비스라, 판정 룰이 바뀌어도 재파싱 없이
        이 엔드포인트만 다시 부르면 된다.
        """
        r = self.http.post(
            f"{self.base}/v1/check-excel",
            json={"filename": filename, "gate_summary": gate_summary},
        )
        check(r, what="doc_guard /v1/check-excel")
        return r.json() or {}

    def list_rules(self) -> Any:
        """룰 카탈로그 패스스루 — 소비자 UI 의 체크박스 구성용."""
        r = self.http.get(f"{self.base}/v1/rules")
        check(r, what="doc_guard /v1/rules")
        return r.json()


def get_doc_guard() -> DocGuardClient:
    """env 로 조립. 컨테이너에서는 인트라스택 DNS(`http://doc_guard:8000`)를 쓴다."""
    return DocGuardClient(
        os.environ.get("KBP_DOC_GUARD_URL", "http://localhost:8000"),
        timeout=float(os.environ.get("KBP_DOC_GUARD_TIMEOUT", "60")),
    )
