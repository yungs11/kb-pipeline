"""Text LLM callable (qwen via OpenRouter). Key comes from the environment."""
from __future__ import annotations

import os

import httpx


def get_text_llm():
    key = os.environ["KBP_OPENAI_API_KEY"]
    base = os.environ.get("KBP_OPENAI_BASE_URL", "https://openrouter.ai/api/v1")
    model = os.environ.get("KBP_LLM_MODEL", "qwen/qwen3.5-122b-a10b")

    def call(prompt: str, payload: str) -> str:
        r = httpx.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": model, "messages": [{"role": "user", "content": f"{prompt}\n\n{payload}"}]},
            timeout=120,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    return call
