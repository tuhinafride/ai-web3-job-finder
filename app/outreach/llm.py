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


OPENAI_BASE = "https://api.openai.com/v1"


def _provider(purpose: str) -> tuple[str, str, str, bool] | None:
    """(base_url, key, model, is_openai). Drafts prefer ChatGPT; scoring prefers LLM_API_KEY."""
    st = get_settings()
    generic = (st.llm_base_url, st.llm_api_key, st.llm_model, "api.openai.com" in st.llm_base_url) if st.llm_api_key else None
    openai = (OPENAI_BASE, st.openai_api_key, st.openai_model, True) if st.openai_api_key else None
    return (openai or generic) if purpose == "draft" else (generic or openai)


def enabled(purpose: str = "qualify") -> bool:
    return _provider(purpose) is not None


def provider_name(purpose: str) -> str:
    p = _provider(purpose)
    if not p:
        return ""
    return "chatgpt" if p[3] else ("gemini" if "googleapis" in p[0] else "llm")


async def chat_json(system: str, user: str, max_tokens: int = 700, timeout: float = 60,
                    purpose: str = "qualify") -> dict:
    """Asks the model for a JSON object. Raises LLMUnavailable on any problem."""
    prov = _provider(purpose)
    if not prov:
        raise LLMUnavailable("no key")
    base, key, model, is_openai = prov
    url = base.rstrip("/") + "/chat/completions"
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_object"},
    }
    if is_openai:
        # gpt-5 family: no custom temperature, uses max_completion_tokens (reasoning tokens count too)
        body["max_completion_tokens"] = max_tokens * 4
        if not model.startswith(("gpt-5", "o")):
            body["temperature"] = 0.3
    else:
        body["temperature"] = 0.3
        body["max_tokens"] = max_tokens
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(url, json=body, headers={"Authorization": f"Bearer {key}"})
            if r.status_code == 400 and "temperature" in r.text and "temperature" in body:
                body.pop("temperature")
                r = await c.post(url, json=body, headers={"Authorization": f"Bearer {key}"})
            if r.status_code == 400 and "response_format" in r.text:
                body.pop("response_format")
                r = await c.post(url, json=body, headers={"Authorization": f"Bearer {key}"})
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
