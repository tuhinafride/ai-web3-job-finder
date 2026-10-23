"""Search/AI providers: Gemini grounding (only grounded URLs kept), Gemini LLM calls (429 backoff, free-tier caps),
provider failure back-off, optional OpenAI, and no trace of Grok/xAI. All HTTP is mocked: nothing real is called."""
import json
import pathlib

import httpx
import pytest

from app.config import get_settings
from app.outreach import llm
from app.outreach import search as S

ROOT = pathlib.Path(__file__).resolve().parent.parent
REDIR = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    st = get_settings()
    for k in ("llm_api_key", "openai_api_key", "serper_api_key", "brave_api_key", "x_bearer_token",
              "twitterapi_io_key", "gemini_api_key_raw"):
        monkeypatch.setattr(st, k, "")
    S.backoff.clear()
    llm.reset_limits()
    S._grounding_daily.update(day="", count=0)

    async def no_sleep(*a, **k):
        return None
    monkeypatch.setattr("app.outreach.llm.asyncio.sleep", no_sleep)
    yield
    S.backoff.clear()
    llm.reset_limits()


def _gemini_resp(text, chunks):
    return {"candidates": [{"content": {"parts": [{"text": text}], "role": "model"},
                            "groundingMetadata": {"webSearchQueries": ["web3 moderator hiring"],
                                                  "groundingChunks": [{"web": {"uri": u, "title": t}} for u, t in chunks]}}]}


PAGE = ("<html><head><title>Community Moderator - NovaChain</title></head><body>NovaChain (DeFi) is hiring a "
        "Discord Moderator. Paid $700/month, remote. Apply: https://novachain.xyz/apply or DM t.me/nova_hr_lead"
        "</body></html>")


def _mock_http(monkeypatch, post_json, redirects, pages=None, oembed=None, record=None):
    pages = pages or {}

    async def fake_post(self, url, json=None, headers=None, **kw):
        if record is not None:
            record.append({"url": url, "json": json, "headers": headers})
        body = post_json(url, json) if callable(post_json) else post_json
        if isinstance(body, httpx.Response):
            return body
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    async def fake_get(self, url, params=None, follow_redirects=None, **kw):
        req = httpx.Request("GET", url)
        if url.startswith(REDIR):
            real = redirects.get(url)
            if real is None:
                return httpx.Response(404, request=req)
            return httpx.Response(302, headers={"location": real}, request=req)
        if "publish.twitter.com/oembed" in url:
            if oembed is None:
                return httpx.Response(404, request=req)
            return httpx.Response(200, json={"html": f"<blockquote><p>{oembed}</p></blockquote>"}, request=req)
        if url in pages:
            return httpx.Response(200, text=pages[url], headers={"content-type": "text/html"}, request=req)
        return httpx.Response(404, request=req)
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)


# ------------------------------------------------------------------ Gemini grounding (Google)
async def test_gemini_grounding_keeps_only_grounded_urls(monkeypatch):
    st = get_settings()
    monkeypatch.setattr(st, "llm_api_key", "gem-key")
    real1, real2 = "https://novachain.xyz/careers/moderator", "https://jobs.example/orbit-cm"
    model_json = json.dumps({"jobs": [
        {"title": "Discord Moderator", "project": "NovaChain", "url": real1, "snippet": "Hiring a Discord moderator", "date": "2026-10-20"},
        {"title": "Community Manager", "project": "Ghost DAO", "url": "https://made-up.example/job", "snippet": "invented"},
    ]})
    sent = []
    _mock_http(monkeypatch, _gemini_resp(model_json, [(REDIR + "a1", "novachain.xyz"), (REDIR + "b2", "jobs.example"),
                                                      (REDIR + "dead", "gone.example")]),
               {REDIR + "a1": real1, REDIR + "b2": real2}, pages={real1: PAGE}, record=sent)
    out = await S.gemini_grounded_search(["web3 moderator hiring"])
    urls = [l.url for l in out]
    assert urls == [real1, real2]                       # only grounding chunks, redirects resolved
    assert "https://made-up.example/job" not in urls      # model-only result dropped
    assert all("vertexaisearch" not in u for u in urls)
    assert out[0].title == "Discord Moderator" and out[0].project == "NovaChain" and out[0].checked
    assert "$700/month" in out[0].text                    # real page text was read
    assert out[1].checked is False                        # page not readable: kept, but not "checked"
    req = sent[0]
    assert req["url"].endswith("/models/gemini-2.5-flash:generateContent")
    assert req["json"]["tools"] == [{"google_search": {}}]
    assert req["headers"]["x-goog-api-key"] == "gem-key"
    assert "responseMimeType" not in req["json"]["generationConfig"]   # JSON mode + tools not combined


async def test_gemini_grounding_without_chunks_returns_nothing(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_api_key", "gem-key")
    model_json = json.dumps({"jobs": [{"title": "Fake", "url": "https://x.example/1", "snippet": "invented"}]})
    _mock_http(monkeypatch, _gemini_resp(model_json, []), {})
    assert await S.gemini_grounded_search(["anything"]) == []


async def test_google_search_order_serper_then_gemini(monkeypatch):
    st = get_settings()
    monkeypatch.setattr(st, "llm_api_key", "gem-key")
    monkeypatch.setattr(st, "serper_api_key", "bad-serper")
    real1 = "https://novachain.xyz/careers/moderator"
    calls = []

    def post(url, body):
        calls.append(url)
        if "serper" in url:
            return httpx.Response(403, json={}, request=httpx.Request("POST", url))
        return _gemini_resp("{}", [(REDIR + "a1", "novachain.xyz")])
    _mock_http(monkeypatch, post, {REDIR + "a1": real1}, pages={real1: PAGE})
    out, errs = await S.google_search(["q1", "q2", "q3"])
    assert [l.url for l in out] == [real1]
    assert any("Serper HTTP 403" in e for e in errs)
    assert sum("generateContent" in u for u in calls) == 1      # ONE grounding call for all queries
    assert S.configured()["google_chain"] == ["serper", "gemini"]


# ------------------------------------------------------------------ Gemini grounding (X)
async def test_gemini_x_keeps_only_grounded_status_urls(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_api_key", "gem-key")
    posts = json.dumps({"posts": [
        {"url": "https://x.com/orbitproto/status/1850000000000000001", "author": "orbitproto", "name": "Orbit",
         "text": "model paraphrase", "date": "2026-10-21"},
        {"url": "https://x.com/fakeacct/status/1850000000000000999", "author": "fakeacct", "text": "invented"},
    ]})
    _mock_http(monkeypatch, _gemini_resp(posts, [(REDIR + "x1", "x.com"), (REDIR + "w1", "blog.example")]),
               {REDIR + "x1": "https://twitter.com/orbitproto/status/1850000000000000001?s=20",
                REDIR + "w1": "https://blog.example/web3-jobs"},
               oembed="We're hiring a paid Community Moderator for Orbit (web3). DM @orbit_cm")
    out = await S.gemini_x_search()
    assert [l.url for l in out] == ["https://x.com/orbitproto/status/1850000000000000001"]
    assert out[0].checked and out[0].author_x == "orbitproto"
    assert out[0].text.startswith("We're hiring a paid Community Moderator")   # real post text via oEmbed
    assert S.configured()["x"] == "gemini_x"


# ------------------------------------------------------------------ provider failures / back-off
async def test_provider_failure_backoff_and_no_fake_results(monkeypatch):
    st = get_settings()
    monkeypatch.setattr(st, "serper_api_key", "k")
    calls = []

    def post(url, body):
        calls.append(url)
        return httpx.Response(500, json={}, request=httpx.Request("POST", url))
    _mock_http(monkeypatch, post, {})
    out, errs = await S.google_search(["q1"])
    assert out == [] and "Serper HTTP 500" in errs[0]
    assert S.backoff.snapshot()["serper"]["fails"] == 1
    out, errs = await S.google_search(["q1"])          # second run: skipped, not called again
    assert out == [] and "backing off" in " ".join(errs)
    assert len(calls) == 1
    S.backoff.state["serper"]["until"] = 0               # back-off expired -> tried again, doubles
    await S.google_search(["q1"])
    assert len(calls) == 2 and S.backoff.snapshot()["serper"]["fails"] == 2


async def test_x_chain_falls_through_and_reports(monkeypatch):
    st = get_settings()
    monkeypatch.setattr(st, "x_bearer_token", "bad")
    _mock_http(monkeypatch, {}, {})

    async def fake_get(self, url, params=None, headers=None, **kw):
        return httpx.Response(401, json={}, request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    with pytest.raises(S.ProviderError) as e:
        await S.x_search()
    assert "X API HTTP 401" in str(e.value)


# ------------------------------------------------------------------ Gemini LLM (OpenAI-compatible)
async def test_gemini_chat_body_and_429_backoff(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_api_key", "gem-key")
    seen, n = [], {"i": 0}

    def post(url, body):
        seen.append((url, dict(body)))
        n["i"] += 1
        if n["i"] == 1:
            return httpx.Response(429, json={"error": {"details": [{"retryDelay": "3s"}]}},
                                  request=httpx.Request("POST", url))
        return {"choices": [{"message": {"content": '{"ok": true}'}}]}
    _mock_http(monkeypatch, post, {})
    assert await llm.chat_json("sys", "user") == {"ok": True}
    url, body = seen[-1]
    assert url == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    assert body["model"] == "gemini-2.5-flash" and body["reasoning_effort"] == "none"
    assert "max_completion_tokens" not in body and body["max_tokens"] == 700
    assert len(seen) == 2 and llm.provider_name("draft") == "gemini"


async def test_daily_quota_429_puts_provider_on_cooldown(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_api_key", "gem-key")
    calls = []

    def post(url, body):
        calls.append(url)
        return httpx.Response(429, text='{"error": {"message": "Quota exceeded: GenerateRequestsPerDayPerProjectPerModel"}}',
                              request=httpx.Request("POST", url))
    _mock_http(monkeypatch, post, {})
    with pytest.raises(llm.LLMUnavailable):
        await llm.chat_json("s", "u")
    with pytest.raises(llm.LLMUnavailable):
        await llm.chat_json("s", "u")
    assert len(calls) == 1                               # cool-down: no second request
    assert llm.status()["cooldown"].get("gemini", 0) > 0


async def test_daily_budget_cap(monkeypatch):
    st = get_settings()
    monkeypatch.setattr(st, "llm_api_key", "gem-key")
    monkeypatch.setattr(st, "llm_daily_call_cap", 2)
    _mock_http(monkeypatch, {"choices": [{"message": {"content": '{"a": 1}'}}]}, {})
    await llm.chat_json("s", "u")
    await llm.chat_json("s", "u")
    with pytest.raises(llm.LLMUnavailable):
        await llm.chat_json("s", "u")


async def test_openai_optional_fallback(monkeypatch):
    st = get_settings()
    assert not llm.enabled()                             # nothing required
    monkeypatch.setattr(st, "openai_api_key", "sk-x")
    assert llm.provider_name("qualify") == "chatgpt"
    monkeypatch.setattr(st, "llm_api_key", "gem-key")
    assert llm.provider_name("draft") == "gemini"        # Gemini first; OpenAI is only the fallback
    urls = []

    def post(url, body):
        urls.append(url)
        if "googleapis" in url:
            return httpx.Response(401, json={}, request=httpx.Request("POST", url))
        assert "max_completion_tokens" in body and "reasoning_effort" not in body
        return {"choices": [{"message": {"content": '{"m": "hi"}'}}]}
    _mock_http(monkeypatch, post, {})
    assert await llm.chat_json("s", "u", purpose="draft") == {"m": "hi"}
    assert urls[-1] == "https://api.openai.com/v1/chat/completions"


async def test_model_missing_uses_fallback_model(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_api_key", "gem-key")
    models = []

    def post(url, body):
        models.append(body["model"])
        if body["model"] == "gemini-2.5-flash":
            return httpx.Response(404, text='{"error": {"message": "models/gemini-2.5-flash is not found for API version v1beta"}}',
                                  request=httpx.Request("POST", url))
        return {"choices": [{"message": {"content": '{"ok": 1}'}}]}
    _mock_http(monkeypatch, post, {})
    assert await llm.chat_json("s", "u") == {"ok": 1}
    assert models == ["gemini-2.5-flash", "gemini-flash-latest"]


async def test_run_caps_llm_calls(monkeypatch):
    """Rules score everything; only the top LLM_MAX_CALLS_PER_RUN new leads go to the AI."""
    from app.db import init_db
    from app.outreach import agent as A
    from app.outreach import qualify as Q
    await init_db()
    st = get_settings()
    monkeypatch.setattr(st, "llm_max_calls_per_run", 2)
    monkeypatch.setattr(llm, "enabled", lambda *a: True)
    calls = []

    async def fake_chat(system, user, **kw):
        calls.append(user)
        return {"is_hiring": True, "role": "moderator"}
    monkeypatch.setattr(llm, "chat_json", fake_chat)

    async def fake_google(queries):
        return [S.RawLead("google", f"https://caps.example/job{i}", f"Web3 Discord Moderator #{i} hiring",
                          f"Crypto project number {i} is hiring a paid Discord moderator, $500/month, remote. "
                          f"Responsibilities include moderation of web3 community {i} unique words alpha{i} beta{i} "
                          f"gamma{i} delta{i} epsilon{i} zeta{i} eta{i} theta{i} iota{i} kappa{i}", checked=True)
                for i in range(6)], []
    monkeypatch.setattr(S, "google_search", fake_google)
    owner = 4242
    summary = await A.run_discovery(owner, ["google"])
    assert summary["new"] == 6 and len(calls) == 2 and summary["ai_calls"] == 2


# ------------------------------------------------------------------ Grok / xAI fully removed
def test_no_grok_or_xai_left():
    hits = []
    for p in list((ROOT / "app").rglob("*.py")) + list((ROOT / "webapp").glob("*")) + \
            [ROOT / ".env.example", ROOT / "render.yaml", ROOT / "README.md"]:
        txt = p.read_text(errors="ignore")
        for word in ("XAI_", "xai_", "grok", "Grok", "x.ai"):
            if word in txt:
                hits.append((p.name, word))
    assert hits == []
    assert not hasattr(get_settings(), "xai_api_key")


async def test_invalid_gemini_key_http_400_is_reported_and_cooled_down(monkeypatch):
    """Google returns HTTP 400 (not 401) for a bad key: report it clearly and stop retrying."""
    monkeypatch.setattr(get_settings(), "llm_api_key", "bad")
    calls = []
    bad = '{"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.", "status": "INVALID_ARGUMENT"}}'

    def post(url, body):
        calls.append(url)
        return httpx.Response(400, text=bad, request=httpx.Request("POST", url))
    _mock_http(monkeypatch, post, {})
    with pytest.raises(llm.LLMUnavailable) as e:
        await llm.chat_json("s", "u")
    assert "invalid API key" in str(e.value)
    with pytest.raises(llm.LLMUnavailable):
        await llm.chat_json("s", "u")
    assert len(calls) == 1
    with pytest.raises(S.ProviderError) as e:
        await S.gemini_generate_grounded("find jobs")
    assert "invalid API key" in str(e.value)
