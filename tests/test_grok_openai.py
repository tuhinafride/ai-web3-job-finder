import json
import pytest
import httpx
from app.config import get_settings
from app.outreach import search as S, llm


def _resp(posts, cited):
    return {"output": [{"type": "message", "content": [{"type": "output_text",
            "text": json.dumps({"posts": posts}),
            "annotations": [{"type": "url_citation", "url": u} for u in cited]}]}]}


@pytest.mark.asyncio
async def test_grok_keeps_only_cited_posts(monkeypatch):
    st = get_settings()
    monkeypatch.setattr(st, "xai_api_key", "k")
    posts = [{"url": "https://x.com/orbit/status/111", "author": "orbit", "name": "Orbit",
              "text": "We're hiring a community moderator, paid, remote. DM @orbit_cmo", "date": "2026-10-20"},
             {"url": "https://x.com/fake/status/222", "author": "fake", "text": "made up", "date": "2026-10-20"}]
    body = _resp(posts, ["https://x.com/orbit/status/111"])
    seen = {}

    async def fake_post(self, url, json=None, headers=None):
        seen["url"], seen["body"] = url, json
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    out = await S.x_search()
    assert seen["url"] == "https://api.x.ai/v1/responses"
    assert seen["body"]["tools"][0]["type"] == "x_search"
    assert [l.url for l in out] == ["https://x.com/orbit/status/111"]
    assert out[0].checked and out[0].author_x == "orbit"
    assert S.configured()["x"] == "grok"


def test_drafts_prefer_chatgpt(monkeypatch):
    st = get_settings()
    monkeypatch.setattr(st, "openai_api_key", "o")
    monkeypatch.setattr(st, "llm_api_key", "g")
    assert llm.provider_name("draft") == "chatgpt"
    assert llm.provider_name("qualify") == "gemini"
    monkeypatch.setattr(st, "llm_api_key", "")
    assert llm.provider_name("qualify") == "chatgpt"
