"""#80 (FR-14 этап 1): git_client — коммиты MR, коммиты ветки, изменённые файлы MR.

MockTransport без сети (образец test_git_client.py). Оба хоста: GitHub отдаёт
коммиты MR хронологически, GitLab — в обратном порядке (клиент разворачивает).
Ошибки остаются GitClientError — маппинг NFR-2 не меняется.
"""

import asyncio
import json

import httpx
import pytest

from app.clients.git_client import GitClient, GitRepoUnavailableError


def _client(handler) -> GitClient:
    return GitClient(http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def _run(coro):
    return asyncio.run(coro)


# ── list_mr_commits ─────────────────────────────────────────────────────────


def test_github_mr_commits_chronological():
    """GitHub /pulls/{n}/commits отдаёт хронологию — позиции присваиваются как есть."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/pulls/7/commits" in request.url.path
        return httpx.Response(200, json=[
            {"sha": "a" * 40, "commit": {"message": "T5: tests first — login lockout"}},
            {"sha": "b" * 40, "commit": {"message": "T5: add login/logout"}},
        ])

    commits = _run(_client(handler).list_mr_commits("https://github.com/u/r", "GitHub", 7))
    assert [(c.sha, c.message, c.position) for c in commits] == [
        ("a" * 40, "T5: tests first — login lockout", 0),
        ("b" * 40, "T5: add login/logout", 1),
    ]


def test_gitlab_mr_commits_reversed_to_chronological():
    """GitLab /merge_requests/{iid}/commits отдаёт новейший первым — клиент разворачивает."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/merge_requests/41/commits" in request.url.path
        return httpx.Response(200, content=json.dumps([
            {"id": "b" * 40, "message": "D1: матрица"},
            {"id": "a" * 40, "message": "D1: tests first"},
        ]))

    commits = _run(_client(handler).list_mr_commits("https://gitlab.com/g/r", "GitLab", 41))
    assert [(c.sha, c.message, c.position) for c in commits] == [
        ("a" * 40, "D1: tests first", 0),
        ("b" * 40, "D1: матрица", 1),
    ]


def test_mr_commits_error_stays_git_client_error():
    """NFR-2: 404 по MR — GitRepoUnavailableError, как у остальных методов."""
    client = _client(lambda request: httpx.Response(404))
    with pytest.raises(GitRepoUnavailableError):
        _run(client.list_mr_commits("https://github.com/u/r", "GitHub", 7))


# ── list_commits ────────────────────────────────────────────────────────────


def test_github_list_commits_ref_and_limit_in_query():
    """GitHub /commits: ref уходит в ?sha= (слэш легален в query — правило T73), лимит в per_page."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/commits")
        assert request.url.params["sha"] == "feat/x"
        assert request.url.params["per_page"] == "50"
        return httpx.Response(200, json=[
            {"sha": "c" * 40, "commit": {"message": "#12 fix race"}},
        ])

    commits = _run(
        _client(handler).list_commits("https://github.com/u/r", "GitHub", ref="feat/x", limit=50)
    )
    assert [(c.sha, c.message) for c in commits] == [("c" * 40, "#12 fix race")]


def test_gitlab_list_commits_ref_name():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/repository/commits" in request.url.path
        assert request.url.params["ref_name"] == "main"
        assert request.url.params["per_page"] == "100"
        return httpx.Response(200, content=json.dumps([
            {"id": "d" * 40, "message": "T-005: слой данных"},
        ]))

    commits = _run(_client(handler).list_commits("https://gitlab.com/g/r", "GitLab"))
    assert [(c.sha, c.message) for c in commits] == [("d" * 40, "T-005: слой данных")]


# ── list_mr_changes ─────────────────────────────────────────────────────────


def test_github_mr_changes_filenames():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/pulls/7/files" in request.url.path
        return httpx.Response(200, json=[
            {"filename": "app/auth.py"},
            {"filename": "README.md"},
        ])

    paths = _run(_client(handler).list_mr_changes("https://github.com/u/r", "GitHub", 7))
    assert paths == ["app/auth.py", "README.md"]


def test_gitlab_mr_changes_via_diffs():
    """GitLab: эндпоинт diffs (API v4); deprecated `changes` не используется."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/merge_requests/41/diffs" in request.url.path
        assert "/changes" not in request.url.path
        return httpx.Response(200, content=json.dumps([
            {"new_path": "lib/main.dart", "old_path": "lib/main.dart"},
            {"new_path": "CHANGELOG.md", "old_path": "CHANGELOG.md"},
        ]))

    paths = _run(_client(handler).list_mr_changes("https://gitlab.com/g/r", "GitLab", 41))
    assert paths == ["lib/main.dart", "CHANGELOG.md"]
