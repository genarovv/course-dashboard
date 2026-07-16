"""G1 (#4): git_client — критерии приёмки на httpx.MockTransport (без сети)."""

import asyncio
import json

import httpx
import pytest

from app.clients.git_client import GitAuthFailedError, GitClient, GitRateLimitedError, GitRepoUnavailableError


def _client(handler) -> GitClient:
    return GitClient(http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def _run(coro):
    return asyncio.run(coro)


def test_github_tree_and_file():
    def handler(request: httpx.Request) -> httpx.Response:
        if "/git/trees/" in request.url.path:
            return httpx.Response(200, json={
                "tree": [
                    {"path": "product/prd.md", "type": "blob"},
                    {"path": "product", "type": "tree"},
                ]
            })
        assert "/contents/" in request.url.path
        return httpx.Response(200, text="# PRD")

    client = _client(handler)
    tree = _run(client.get_tree("https://github.com/user/repo.git", "GitHub"))
    assert tree == ["product/prd.md"]
    content = _run(client.get_file_content("https://github.com/user/repo", "GitHub", "product/prd.md"))
    assert content == "# PRD"


def test_gitlab_tree_and_file():
    def handler(request: httpx.Request) -> httpx.Response:
        if "/repository/tree" in request.url.path:
            return httpx.Response(200, content=json.dumps([
                {"path": "prd.md", "type": "blob"},
                {"path": "docs", "type": "tree"},
            ]))
        assert "/repository/files/" in request.url.path
        return httpx.Response(200, text="content")

    client = _client(handler)
    tree = _run(client.get_tree("https://gitlab.com/group/sub/repo", "GitLab"))
    assert tree == ["prd.md"]
    assert _run(client.get_file_content("https://gitlab.com/group/sub/repo", "GitLab", "prd.md")) == "content"


def test_expired_token_raises_auth_failed():
    client = _client(lambda request: httpx.Response(401))
    with pytest.raises(GitAuthFailedError):
        _run(client.get_tree("https://github.com/u/r", "GitHub"))


def test_rate_limit_pauses_and_retries(monkeypatch):
    calls = {"n": 0}
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json={"tree": []})

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr("app.clients.git_client.asyncio.sleep", fake_sleep)
    client = _client(handler)
    assert _run(client.get_tree("https://github.com/u/r", "GitHub")) == []
    assert calls["n"] == 2  # пауза + повтор
    assert sleeps == [1.0]


def test_rate_limit_exhausted_raises(monkeypatch):
    async def fake_sleep(seconds):
        pass

    monkeypatch.setattr("app.clients.git_client.asyncio.sleep", fake_sleep)
    client = _client(lambda request: httpx.Response(429))
    with pytest.raises(GitRateLimitedError):
        _run(client.get_tree("https://github.com/u/r", "GitHub"))


def test_one_repo_error_does_not_break_next_call():
    def handler(request: httpx.Request) -> httpx.Response:
        if "broken" in request.url.path:
            return httpx.Response(404)
        return httpx.Response(200, json={"tree": []})

    client = _client(handler)
    with pytest.raises(GitRepoUnavailableError):
        _run(client.get_tree("https://github.com/u/broken", "GitHub"))
    assert _run(client.get_tree("https://github.com/u/alive", "GitHub")) == []
