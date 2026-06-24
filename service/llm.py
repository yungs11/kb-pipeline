"""Text LLM callable (qwen via OpenRouter). Key comes from the environment."""
from __future__ import annotations

import os

import httpx


def get_text_llm():
    key = os.environ["KBP_OPENAI_API_KEY"]
    base = os.environ.get("KBP_OPENAI_BASE_URL", "https://openrouter.ai/api/v1")
    model = os.environ.get("KBP_LLM_MODEL", "qwen/qwen3.5-122b-a10b")
    # Per-call read timeout. A single modal call is ~10s, but the proxy can spike under
    # concurrent load; default 300s margin (env KBP_LLM_TIMEOUT) so a transient slow call
    # does not fail the whole document.
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
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    return call
