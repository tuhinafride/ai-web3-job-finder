"""Optional LLM client (any OpenAI-compatible Chat Completions API). Default: Google Gemini (free tier).

Configure with LLM_API_KEY (+ optional LLM_BASE_URL, LLM_MODEL). Default base is Gemini's
OpenAI-compatible endpoint and model gemini-2.5-flash. OPENAI_API_KEY (ChatGPT) is optional: it is used
when LLM_API_KEY is empty, or as a fallback when the main provider fails.
When nothing is configured every caller falls back to deterministic rules/templates.

Free-tier guards (Gemini free keys have low requests-per-minute and per-day quotas):
- calls are spaced by LLM_MIN_INTERVAL_SEC (default 4 s, about 15/min max),
- at most LLM_DAILY_CALL_CAP calls per UTC day (in memory; resets on restart),
- HTTP 429 is retried with backoff (Retry-After / Gemini "retryDelay"), then the provider is put on a
  cool-down so the rest of the run uses rules instead of hammering the API.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone

import httpx

from ..config import get_settings

log = logging.getLogger("llm")


class LLMUnavailable(Exception):
    pass


OPENAI_BASE = "https://api.openai.com/v1"
MAX_RETRY_WAIT = 30.0

_lock = asyncio.Lock()
_last_call = {"t": 0.0}
_cooldown: dict[str, float] = {}             # provider name -> monotonic time until which it is skipped
_daily = {"day": "", "count": 0}
_last_error: dict[str, str] = {}


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def budget_left() -> int:
    if _daily["day"] != _today():
        _daily.update(day=_today(), count=0)
    return max(0, get_settings().llm_daily_call_cap - _daily["count"])


def _take_budget():
    if budget_left() <= 0:
        raise LLMUnavailable("daily AI budget used")
    _daily["count"] += 1


def reset_limits():
    """Test helper / admin: clears cool-downs and the daily counter."""
    _cooldown.clear()
    _daily.update(day=_today(), count=0)
    _last_call["t"] = 0.0
    _last_error.clear()


async def throttle(min_interval: float | None = None):
    """Spaces calls to the AI provider (shared by chat calls and Gemini grounding)."""
    gap = get_settings().llm_min_interval_sec if min_interval is None else min_interval
    async with _lock:
        wait = _last_call["t"] + gap - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call["t"] = time.monotonic()


def is_gemini(base: str) -> bool:
    return "generativelanguage.googleapis.com" in base


def _providers(purpose: str) -> list[tuple[str, str, str, str]]:
    """[(name, base_url, key, model)] in the order they are tried. Gemini/LLM_API_KEY first."""
    st = get_settings()
    out = []
    if st.llm_api_key:
        name = "gemini" if is_gemini(st.llm_base_url) else ("chatgpt" if "api.openai.com" in st.llm_base_url else "llm")
        out.append((name, st.llm_base_url, st.llm_api_key, st.llm_model))
    if st.openai_api_key and not any(p[0] == "chatgpt" for p in out):
        out.append(("chatgpt", OPENAI_BASE, st.openai_api_key, st.openai_model))
    return out


def enabled(purpose: str = "qualify") -> bool:
    return bool(_providers(purpose))


def provider_name(purpose: str) -> str:
    p = _providers(purpose)
    return p[0][0] if p else ""


def status() -> dict:
    now = time.monotonic()
    return {"budget_left": budget_left(), "daily_cap": get_settings().llm_daily_call_cap,
            "cooldown": {k: int(v - now) for k, v in _cooldown.items() if v > now},
            "last_error": dict(_last_error)}


def retry_delay(r: httpx.Response, attempt: int) -> float:
    """Seconds to wait after a 429: Retry-After header, Gemini's RetryInfo.retryDelay, else 2,4,8…"""
    ra = r.headers.get("retry-after")
    if ra:
        try:
            return min(MAX_RETRY_WAIT, max(1.0, float(ra)))
        except ValueError:
            pass
    m = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', r.text or "")
    if m:
        return min(MAX_RETRY_WAIT, max(1.0, float(m.group(1))))
    return min(MAX_RETRY_WAIT, 2.0 * (2 ** attempt))


def _is_daily_quota(text: str) -> bool:
    return bool(re.search(r"PerDay|per day|RequestsPerDay", text or "", re.I))


def _model_missing(r: httpx.Response) -> bool:
    return r.status_code in (400, 403, 404) and bool(re.search(
        r"model.{0,80}(not found|not supported|no longer available|is not available|does not have access)|"
        r"(not found|unknown).{0,40}model", r.text or "", re.I))


def bad_key(r: httpx.Response) -> bool:
    """Google answers an invalid key with HTTP 400 INVALID_ARGUMENT ("API key not valid"), not 401."""
    return r.status_code in (400, 401, 403) and bool(re.search(r"API[_ ]key", r.text or "", re.I)) and \
        bool(re.search(r"not valid|invalid|expired|missing|pass a valid", r.text or "", re.I))


def build_body(name: str, model: str, system: str, user: str, max_tokens: int) -> dict:
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_object"},
    }
    if name == "chatgpt":
        # gpt-5 / o-series: no custom temperature, uses max_completion_tokens (reasoning tokens count too)
        body["max_completion_tokens"] = max_tokens * 4
        if not model.startswith(("gpt-5", "o")):
            body["temperature"] = 0.3
    elif name == "gemini":
        body["temperature"] = 0.3
        # Gemini 2.5 Flash thinks by default and thinking tokens count against max_tokens, which can leave
        # an empty answer. "none" turns thinking off on 2.5 Flash/Flash-Lite; other models get "low".
        body["reasoning_effort"] = "none" if re.search(r"2\.5-flash", model) else "low"
        body["max_tokens"] = max_tokens + (0 if body["reasoning_effort"] == "none" else 1024)
    else:
        body["temperature"] = 0.3
        body["max_tokens"] = max_tokens
    return body


async def _call_one(name: str, base: str, key: str, model: str, system: str, user: str,
                    max_tokens: int, timeout: float) -> str:
    st = get_settings()
    if _cooldown.get(name, 0) > time.monotonic():
        raise LLMUnavailable(f"{name} cooling down after rate limit")
    url = base.rstrip("/") + "/chat/completions"
    body = build_body(name, model, system, user, max_tokens)
    headers = {"Authorization": f"Bearer {key}"}
    swapped_model = False
    async with httpx.AsyncClient(timeout=timeout) as c:
        for attempt in range(6):
            _take_budget()
            await throttle()
            r = await c.post(url, json=body, headers=headers)
            if r.status_code == 200:
                break
            if r.status_code == 429:
                if _is_daily_quota(r.text):
                    _cooldown[name] = time.monotonic() + 3600
                    _last_error[name] = "daily quota reached (429)"
                    raise LLMUnavailable(f"{name} daily quota reached")
                if attempt >= 2:
                    _cooldown[name] = time.monotonic() + 120
                    _last_error[name] = "rate limited (429)"
                    raise LLMUnavailable(f"{name} rate limited")
                await asyncio.sleep(retry_delay(r, attempt))
                continue
            if bad_key(r):
                _cooldown[name] = time.monotonic() + 1800   # bad key: stop retrying for a while
                _last_error[name] = f"invalid API key (HTTP {r.status_code})"
                raise LLMUnavailable(f"{name} invalid API key (HTTP {r.status_code})")
            if r.status_code == 400:
                txt = r.text or ""
                changed = False
                for param in ("reasoning_effort", "temperature", "response_format"):
                    if param in body and param in txt:
                        body.pop(param)
                        changed = True
                if changed:
                    continue
            if _model_missing(r) and not swapped_model and name == "gemini" and st.llm_fallback_model \
                    and body["model"] != st.llm_fallback_model:
                log.warning("model %s unavailable, trying %s", body["model"], st.llm_fallback_model)
                body["model"] = st.llm_fallback_model
                body["reasoning_effort"] = "low"
                body["max_tokens"] = max_tokens + 1024
                swapped_model = True
                continue
            _last_error[name] = f"HTTP {r.status_code}"
            log.warning("LLM %s HTTP %s", name, r.status_code)
            if r.status_code in (401, 403):
                _cooldown[name] = time.monotonic() + 1800   # bad key: stop retrying for a while
            raise LLMUnavailable(f"{name} HTTP {r.status_code}")
        else:
            raise LLMUnavailable(f"{name} gave up")
    try:
        content = r.json()["choices"][0]["message"].get("content") or ""
    except Exception:
        raise LLMUnavailable("bad response")
    _last_error.pop(name, None)
    return content


async def chat_json(system: str, user: str, max_tokens: int = 700, timeout: float = 60,
                    purpose: str = "qualify") -> dict:
    """Asks the model for a JSON object. Raises LLMUnavailable on any problem."""
    provs = _providers(purpose)
    if not provs:
        raise LLMUnavailable("no key")
    last = "no provider"
    for name, base, key, model in provs:
        try:
            content = await _call_one(name, base, key, model, system, user, max_tokens, timeout)
        except LLMUnavailable as e:
            last = str(e)
            continue
        except Exception as e:
            log.warning("LLM call failed: %s", type(e).__name__)
            last = type(e).__name__
            continue
        m = re.search(r"\{.*\}", content, re.S)
        if not m:
            last = "no json"
            continue
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            last = "bad json"
            continue
    raise LLMUnavailable(last)
