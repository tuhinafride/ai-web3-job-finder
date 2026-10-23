"""Optional LLM client (any OpenAI-compatible Chat Completions API).

Works with OpenAI, Google Gemini's OpenAI-compatible endpoint, Groq, OpenRouter, etc.
Configure with env vars LLM_API_KEY, LLM_BASE_URL, LLM_MODEL. When LLM_API_KEY is empty,
every caller falls back to deterministic rules/templates, so the app keeps working.
"""
from __future__ import annotations

import json
import logging
import re

import httpx

from ..config import get_settings

log = logging.getLogger("llm")


class LLMUnavailable(Exception):
    pass


def enabled() -> bool:
    return bool(get_settings().llm_api_key)


async def chat_json(system: str, user: str, max_tokens: int = 700, timeout: float = 40) -> dict:
    """Asks the model for a JSON object. Raises LLMUnavailable on any problem."""
    st = get_settings()
    if not st.llm_api_key:
        raise LLMUnavailable("no key")
    url = st.llm_base_url.rstrip("/") + "/chat/completions"
    body = {
        "model": st.llm_model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0.3,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(url, json=body, headers={"Authorization": f"Bearer {st.llm_api_key}"})
            if r.status_code == 400 and "response_format" in r.text:
                body.pop("response_format")
                r = await c.post(url, json=body, headers={"Authorization": f"Bearer {st.llm_api_key}"})
        if r.status_code != 200:
            log.warning("LLM HTTP %s", r.status_code)
            raise LLMUnavailable(f"HTTP {r.status_code}")
        content = r.json()["choices"][0]["message"]["content"] or ""
    except LLMUnavailable:
        raise
    except Exception as e:
        log.warning("LLM call failed: %s", type(e).__name__)
        raise LLMUnavailable(type(e).__name__)
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        raise LLMUnavailable("no json")
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        raise LLMUnavailable("bad json")
