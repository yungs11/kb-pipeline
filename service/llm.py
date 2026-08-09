"""Text LLM callable (qwen via OpenRouter). Key comes from the environment."""
from __future__ import annotations

import os

import httpx
from service.http_retry import check


def get_text_llm(*, timeout: float | None = None):
    """텍스트 LLM 호출자. ``timeout`` 은 **키워드 전용 + 기본값**이다.

    기본값을 두는 이유는 **기존 무인자 호출자를 보존**하기 위해서다 —
    ``service/jobs/runner.py``·``parse_service/app.py`` 가 무인자로 부르고,
    ``service/tests/test_job_runner.py`` 는 ``lambda: …``(무인자 람다)로 monkeypatch 한다.
    키워드 전용이라 위치 인자로 잘못 넘길 수도 없다.

    global 검색은 map N + reduce 1 의 **순차** LLM 이라 요청 하나가
    ``(N+1) × timeout`` 을 점유한다. 그 경로만 300s 기본값보다 짧게 잡아야 하므로
    호출 시점에 값을 준다 — **프로세스 전역 env 를 호출 시점에 바꾸는 방식은 금지**다
    (같은 워커의 다른 경로와 경합한다).
    """
    key = os.environ["KBP_OPENAI_API_KEY"]
    base = os.environ.get("KBP_OPENAI_BASE_URL", "https://openrouter.ai/api/v1")
    model = os.environ.get("KBP_LLM_MODEL", "qwen/qwen3.5-122b-a10b")
    # Per-call read timeout. A single modal call is ~10s, but the proxy can spike under
    # concurrent load; default 300s margin (env KBP_LLM_TIMEOUT) so a transient slow call
    # does not fail the whole document.
    if timeout is None:
        timeout = float(os.environ.get("KBP_LLM_TIMEOUT", "300"))

    def call(prompt: str, payload: str) -> str:
        r = httpx.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": f"{prompt}\n\n{payload}"}],
                # qwen3.5 는 reasoning(thinking) 모델. 모달 요약/경계판정(JSON 추출)은 추론이
                # 불필요한데, thinking ON 이면 호출마다 추론 토큰을 생성해 표/이미지당 지연이
                # 크다(검증: 표 1건 6.1s→2.9s, reasoning_tokens 0). OpenRouter reasoning 파라미터로
                # thinking 을 꺼 호출당 지연을 제거한다(프롬프트·응답 JSON 형식은 동일).
                "reasoning": {"enabled": False},
            },
            timeout=timeout,
        )
        check(r, what="LLM 호출")
        return r.json()["choices"][0]["message"]["content"]

    return call
