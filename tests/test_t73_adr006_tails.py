"""#73: хвосты ревью ADR-006 — кодирование ref в git_client и контракт store §3.5."""

import asyncio
import inspect
import json
import pathlib
import re

import httpx

from app import store
from app.clients.git_client import GitClient

# Реальный кейс: ветка студента С-03 со слэшем — без %2F путевой сегмент
# распадается на два, и хостинг отвечает 404 (репро того же класса, что ADR-006).
_BRANCH = "feature/T-005-data-layer"
_ENCODED = "feature%2FT-005-data-layer"


def _client(handler) -> GitClient:
    return GitClient(http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def _run(coro):
    return asyncio.run(coro)


# ── Хвост 1: ref в путевом сегменте кодируется, в query доходит неискажённым ─


def test_github_tree_with_slash_ref_encoded():
    """git/trees/{ref}: правило #51 (GitLab) действует и для GitHub-путей."""
    def handler(request: httpx.Request) -> httpx.Response:
        if f"/git/trees/{_ENCODED}" in request.url.raw_path.decode():
            return httpx.Response(200, json={"tree": [{"path": "prd.md", "type": "blob"}]})
        return httpx.Response(404)

    tree = _run(_client(handler).get_tree("https://github.com/u/r", "GitHub", ref=_BRANCH))
    assert tree == ["prd.md"]


def test_github_head_sha_with_slash_ref_encoded():
    """commits/{ref}: слэш-ветка кодируется — иначе 404 и потеря SHA для FR-9."""
    def handler(request: httpx.Request) -> httpx.Response:
        if f"/commits/{_ENCODED}" in request.url.raw_path.decode():
            return httpx.Response(200, json={"sha": "a" * 40})
        return httpx.Response(404)

    sha = _run(_client(handler).get_head_sha("https://github.com/u/r", "GitHub", ref=_BRANCH))
    assert sha == "a" * 40


def test_github_head_commit_date_with_slash_ref_encoded():
    """commits/{ref} для даты (D19): тот же путевой сегмент, то же правило."""
    def handler(request: httpx.Request) -> httpx.Response:
        if f"/commits/{_ENCODED}" in request.url.raw_path.decode():
            return httpx.Response(
                200, json={"commit": {"committer": {"date": "2026-08-01T10:00:00Z"}}}
            )
        return httpx.Response(404)

    date = _run(
        _client(handler).get_head_commit_date("https://github.com/u/r", "GitHub", ref=_BRANCH)
    )
    assert date == "2026-08-01T10:00:00Z"


def test_gitlab_tree_query_ref_passes_slash_branch():
    """?ref= — query-параметр: слэш там легален, ветка обязана дойти неискажённой."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/repository/tree" in request.url.path
        assert request.url.params["ref"] == _BRANCH
        return httpx.Response(200, content=json.dumps([{"path": "prd.md", "type": "blob"}]))

    tree = _run(_client(handler).get_tree("https://gitlab.com/g/r", "GitLab", ref=_BRANCH))
    assert tree == ["prd.md"]


def test_file_content_query_ref_passes_slash_branch():
    """Чтение файла: у обоих хостингов ref в query — ветка доходит неискажённой."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["ref"] == _BRANCH
        return httpx.Response(200, text="content")

    assert _run(
        _client(handler).get_file_content("https://github.com/u/r", "GitHub", "prd.md", ref=_BRANCH)
    ) == "content"
    assert _run(
        _client(handler).get_file_content("https://gitlab.com/g/r", "GitLab", "prd.md", ref=_BRANCH)
    ) == "content"


# ── Хвост 2: §3.5 и store.py называют одну поверхность мутаций состояния ─────

# Полная поверхность мутаций состояния: 5 узких update_* + пара archive/restore (#71).
_STATE_MUTATORS = [
    "archive_repository",
    "restore_repository",
    "update_credential_validity",
    "update_override_revoked",
    "update_repository_default_branch",
    "update_sync_run_status",
    "update_user_lockout",
]


def _section_35() -> str:
    text = (pathlib.Path(store.__file__).parents[1] / "ARCHITECTURE.md").read_text(encoding="utf-8")
    return text[text.index("### 3.5"):text.index("## 4.")]


def test_architecture_35_names_every_state_mutator():
    """Находка 3 ревью ADR-006: §3.5 обязан называть всю поверхность мутаций.

    archive_/restore_repository (#71) мутируют Repository.archived_at через store,
    но в §3.5 не значились — документ обещал «ровно 5 update_*» и молчал про пару.
    """
    section = _section_35()
    assert [name for name in _STATE_MUTATORS if name not in section] == []


def test_store_state_mutation_surface_is_closed():
    """Поверхность закрыта: кроме 5 update_* и пары archive/restore мутаторов нет."""
    names = [n for n, _ in inspect.getmembers(store, inspect.isfunction)]
    mutators = sorted(n for n in names if n.startswith(("update_", "archive_", "restore_")))
    assert mutators == _STATE_MUTATORS


def test_services_do_not_mutate_archived_at_directly():
    """Ограничитель по образцу #50: archived_at присваивается только внутри store."""
    app_dir = pathlib.Path(store.__file__).parent
    offenders = [
        path.name
        for path in [*(app_dir / "services").glob("*.py"), *(app_dir / "routes").glob("*.py")]
        if re.search(r"\.archived_at\s*=(?!=)", path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
