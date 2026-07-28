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


def test_github_head_sha():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/commits/" in request.url.path
        return httpx.Response(200, json={"sha": "a" * 40})

    sha = _run(_client(handler).get_head_sha("https://github.com/u/r", "GitHub"))
    assert sha == "a" * 40


def test_gitlab_head_sha():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/repository/commits/" in request.url.path
        return httpx.Response(200, json={"id": "b" * 40})

    sha = _run(_client(handler).get_head_sha("https://gitlab.com/g/r", "GitLab"))
    assert sha == "b" * 40


def test_gitlab_tree_and_file():
    def handler(request: httpx.Request) -> httpx.Response:
        if "/repository/tree" in request.url.path:
            if request.url.params["page"] == "1":  # пагинация: дочитываем страницы
                return httpx.Response(
                    200,
                    content=json.dumps([{"path": "prd.md", "type": "blob"}]),
                    headers={"x-next-page": "2"},
                )
            return httpx.Response(200, content=json.dumps([
                {"path": "data-model.md", "type": "blob"},
                {"path": "docs", "type": "tree"},
            ]))
        assert "/repository/files/" in request.url.path
        return httpx.Response(200, text="content")

    client = _client(handler)
    tree = _run(client.get_tree("https://gitlab.com/group/sub/repo", "GitLab"))
    assert tree == ["prd.md", "data-model.md"]
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


# ── FR-12 (#38): MR-эндпоинты ───────────────────────────────────────────────


def test_github_list_pulls_maps_states():
    """GitHub: open→opened; closed+merged_at→merged; closed без merged_at→closed."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/pulls" in request.url.path
        return httpx.Response(200, json=[
            {"number": 7, "title": "S5: auth", "state": "open", "merged_at": None,
             "head": {"ref": "S5-auth", "sha": "a" * 40}, "updated_at": "2026-07-28T10:00:00Z",
             "body": "Мутация: убрал проверку. Поймал: test_login"},
            {"number": 6, "title": "S4", "state": "closed", "merged_at": "2026-07-27T09:00:00Z",
             "head": {"ref": "S4", "sha": "b" * 40}, "updated_at": "2026-07-27T09:00:00Z",
             "body": None},
            {"number": 5, "title": "S3", "state": "closed", "merged_at": None,
             "head": {"ref": "S3", "sha": "c" * 40}, "updated_at": "2026-07-26T08:00:00Z",
             "body": ""},
        ])

    mrs = _run(_client(handler).list_merge_requests("https://github.com/u/r", "GitHub"))
    assert [(m.number, m.state) for m in mrs] == [(7, "opened"), (6, "merged"), (5, "closed")]
    assert mrs[0].source_branch == "S5-auth"
    assert mrs[0].head_sha == "a" * 40
    assert "Мутация" in mrs[0].description
    assert mrs[2].description == ""  # None/пусто нормализуются в строку


def test_gitlab_list_mrs_with_pagination():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/merge_requests" in request.url.path
        if request.url.params.get("page", "1") == "1":
            return httpx.Response(
                200,
                content=json.dumps([{
                    "iid": 41, "title": "D1: матрица", "state": "merged",
                    "source_branch": "D1-matrix", "sha": "d" * 40,
                    "updated_at": "2026-07-22T12:00:00Z", "description": "Причина: ...",
                }]),
                headers={"x-next-page": "2"},
            )
        return httpx.Response(200, content=json.dumps([{
            "iid": 40, "title": "S6", "state": "opened", "source_branch": "S6-1",
            "sha": "e" * 40, "updated_at": "2026-07-21T12:00:00Z", "description": None,
        }]))

    mrs = _run(_client(handler).list_merge_requests("https://gitlab.com/g/r", "GitLab"))
    assert [(m.number, m.state) for m in mrs] == [(41, "merged"), (40, "opened")]
    assert mrs[0].head_sha == "d" * 40


def test_github_mr_notes():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/issues/7/comments" in request.url.path
        return httpx.Response(200, json=[
            {"body": "REWORK: 2 находки"},
            {"body": "принято"},
        ])

    notes = _run(_client(handler).list_mr_notes("https://github.com/u/r", "GitHub", 7))
    assert notes == ["REWORK: 2 находки", "принято"]


def test_gitlab_mr_notes():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/merge_requests/41/notes" in request.url.path
        return httpx.Response(200, content=json.dumps([{"body": "принято"}]))

    notes = _run(_client(handler).list_mr_notes("https://gitlab.com/g/r", "GitLab", 41))
    assert notes == ["принято"]
