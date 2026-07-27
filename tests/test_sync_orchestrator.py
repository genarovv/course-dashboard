"""G2 (#9), FR-8/FR-4: цикл обхода репозиториев (ARCHITECTURE §5.1).

AC тикета #9:
  1. POST /sync обходит активные репозитории (archived_at IS NULL)
  2. Каждый артефакт классифицирован: found / partial / not_found
  3. Исходы репозитория: ok_changed / ok_unchanged / repo_unavailable / auth_failed / skipped_rate_limit
  4. Создаётся SyncRun со статусом completed | partial | failed
Инкрементальность (D28): снапшот пишется только при изменении (content_hash / status).
"""

import hashlib
from datetime import datetime

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from alembic import command
from app import store
from app.clients.git_client import (
    GitAuthFailedError,
    GitRateLimitedError,
    GitRepoUnavailableError,
)
from app.main import app
from app.models import GitHost, SnapshotStatus, SyncOutcome, SyncStatus, SyncTrigger
from app.models.artifact_def import ArtifactDef
from app.models.artifact_snapshot import ArtifactSnapshot
from app.models.lesson import Lesson
from app.models.sync_run import SyncRun
from app.models.sync_run_repository import SyncRunRepository
from app.routes import get_session
from app.routes.admin import get_git_client
from app.services.sync_orchestrator import run_sync

PRD_TEXT = "# PRD\nПродукт делает X."


HEAD_SHA = "f" * 40


class FakeGitClient:
    """Фейк G1: словарь url → {files: {path: content}} или исключение."""

    def __init__(self, repos: dict):
        self.repos = repos

    def _entry(self, repo_url):
        entry = self.repos[repo_url]
        if isinstance(entry, Exception):
            raise entry
        return entry

    async def get_tree(self, repo_url, git_host, ref="main"):
        return list(self._entry(repo_url).keys())

    async def get_file_content(self, repo_url, git_host, file_path, ref="main"):
        return self._entry(repo_url)[file_path]

    async def get_head_sha(self, repo_url, git_host, ref="main"):
        self._entry(repo_url)
        return HEAD_SHA


@pytest.fixture()
def session(tmp_path):
    db_path = tmp_path / "test.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as s:
        yield s
    engine.dispose()


def _seed(session, repo_urls=("https://github.com/s1/r",)):
    lesson = Lesson(number=5, title="PRD", date=datetime(2026, 6, 30).date())
    session.add(lesson)
    session.flush()
    adef = ArtifactDef(lesson_id=lesson.id, role="prd", expected_pattern="product/prd.md")
    session.add(adef)
    repos = [
        store.register_repository(session, repo_url=url, git_host=GitHost.GitHub)
        for url in repo_urls
    ]
    session.flush()
    return adef, repos


async def _sync(session, client):
    run = await run_sync(session, client, triggered_by=SyncTrigger.manual)
    session.flush()
    return run


# ── AC 2: классификация found / not_found + инкрементальность ───────────────


@pytest.mark.anyio
async def test_found_artifact_snapshot_with_sha256(session):
    adef, (repo,) = _seed(session)
    client = FakeGitClient({repo.repo_url: {"product/prd.md": PRD_TEXT, "README.md": "x"}})

    run = await _sync(session, client)

    snap = store.find_last_snapshot(session, repo.id, adef.id)
    assert snap.status == SnapshotStatus.found
    assert snap.file_path == "product/prd.md"
    assert snap.content_hash == hashlib.sha256(PRD_TEXT.encode("utf-8")).hexdigest()
    assert run.status == SyncStatus.completed


@pytest.mark.anyio
async def test_missing_artifact_classified_not_found(session):
    adef, (repo,) = _seed(session)
    client = FakeGitClient({repo.repo_url: {"README.md": "x"}})

    await _sync(session, client)

    snap = store.find_last_snapshot(session, repo.id, adef.id)
    assert snap.status == SnapshotStatus.not_found
    assert snap.content_hash is None
    assert snap.file_path is None


@pytest.mark.anyio
async def test_unchanged_repo_no_new_snapshot_outcome_ok_unchanged(session):
    adef, (repo,) = _seed(session)
    client = FakeGitClient({repo.repo_url: {"product/prd.md": PRD_TEXT}})

    await _sync(session, client)
    session.commit()
    await _sync(session, client)

    snaps = list(session.scalars(select(ArtifactSnapshot)))
    assert len(snaps) == 1  # D28: «проверено, без изменений» — без дубля снапшота
    outcomes = [r.outcome for r in session.scalars(
        select(SyncRunRepository).order_by(SyncRunRepository.checked_at)
    )]
    assert outcomes == [SyncOutcome.ok_changed, SyncOutcome.ok_unchanged]


@pytest.mark.anyio
async def test_changed_content_new_snapshot_outcome_ok_changed(session):
    adef, (repo,) = _seed(session)
    client = FakeGitClient({repo.repo_url: {"product/prd.md": PRD_TEXT}})
    await _sync(session, client)
    session.commit()

    client.repos[repo.repo_url]["product/prd.md"] = PRD_TEXT + "\nНовая строка."
    await _sync(session, client)

    snaps = list(session.scalars(select(ArtifactSnapshot)))
    assert len(snaps) == 2  # append-only: новое наблюдение
    last_outcome = list(session.scalars(
        select(SyncRunRepository).order_by(SyncRunRepository.checked_at)
    ))[-1]
    assert last_outcome.outcome == SyncOutcome.ok_changed


@pytest.mark.anyio
async def test_multi_match_pattern_observes_all_files(session):
    """Артефакт с N совпадениями: изменение любого файла меняет наблюдение (fix по ревью, находка 1)."""
    adef, (repo,) = _seed(session)
    adef.expected_pattern = "product/interviews/*.md"
    session.flush()
    client = FakeGitClient({repo.repo_url: {
        "product/interviews/01.md": "первое",
        "product/interviews/02.md": "второе",
    }})
    await _sync(session, client)
    session.commit()

    client.repos[repo.repo_url]["product/interviews/02.md"] = "второе изменено"
    await _sync(session, client)

    snaps = list(session.scalars(select(ArtifactSnapshot)))
    assert len(snaps) == 2  # изменение НЕ первого файла тоже даёт новое наблюдение
    assert snaps[0].content_hash != snaps[1].content_hash
    assert all(s.file_path == "product/interviews/01.md" for s in snaps)  # представитель — min


@pytest.mark.anyio
async def test_double_star_pattern_matches_nested(session):
    """Паттерн app/**/*.py матчит вложенные пути (без зависимости от Python 3.13 full_match)."""
    adef, (repo,) = _seed(session)
    adef.expected_pattern = "app/**/*.py"
    session.flush()
    client = FakeGitClient({repo.repo_url: {"app/services/x.py": "код", "README.md": "н"}})

    await _sync(session, client)

    snap = store.find_last_snapshot(session, repo.id, adef.id)
    assert snap.status == SnapshotStatus.found
    assert snap.file_path == "app/services/x.py"


@pytest.mark.anyio
async def test_snapshot_records_head_commit_sha(session):
    """FR-9: снапшот found несёт source_commit_sha (fix по ревью, находка 2); not_found — без SHA (И8)."""
    adef, (repo,) = _seed(session)
    client = FakeGitClient({repo.repo_url: {"product/prd.md": PRD_TEXT}})
    await _sync(session, client)
    snap = store.find_last_snapshot(session, repo.id, adef.id)
    assert snap.source_commit_sha == HEAD_SHA

    client.repos[repo.repo_url] = {}  # артефакт исчез
    session.commit()
    await _sync(session, client)
    snap2 = store.find_last_snapshot(session, repo.id, adef.id)
    assert snap2.status == SnapshotStatus.not_found
    assert snap2.source_commit_sha is None


@pytest.mark.anyio
async def test_error_outcome_records_detail(session):
    """Причина ошибки сохраняется в SyncRunRepository.detail (fix по ревью, находка 5)."""
    adef, (repo,) = _seed(session)
    client = FakeGitClient({repo.repo_url: GitRepoUnavailableError("HTTP 404")})

    await _sync(session, client)

    row = session.scalar(select(SyncRunRepository))
    assert row.outcome == SyncOutcome.repo_unavailable
    assert "404" in row.detail


# ── AC 1: обходятся только активные ────────────────────────────────────────


@pytest.mark.anyio
async def test_archived_repo_skipped(session):
    adef, (repo,) = _seed(session)
    repo.archived_at = datetime.utcnow()
    session.flush()
    client = FakeGitClient({})  # обращение к нему упало бы KeyError

    run = await _sync(session, client)

    assert list(session.scalars(select(SyncRunRepository))) == []
    assert run.status == SyncStatus.completed


# ── AC 3: исходы ошибок; NFR-2: ошибка одного репо не валит обход ──────────


@pytest.mark.anyio
async def test_error_outcomes_and_partial_run(session):
    adef, repos = _seed(
        session,
        repo_urls=(
            "https://github.com/s1/ok",
            "https://github.com/s2/gone",
            "https://github.com/s3/auth",
            "https://github.com/s4/limit",
        ),
    )
    client = FakeGitClient({
        repos[0].repo_url: {"product/prd.md": PRD_TEXT},
        repos[1].repo_url: GitRepoUnavailableError("404"),
        repos[2].repo_url: GitAuthFailedError("401"),
        repos[3].repo_url: GitRateLimitedError("429"),
    })

    run = await _sync(session, client)

    by_repo = {
        r.repository_id: r.outcome for r in session.scalars(select(SyncRunRepository))
    }
    assert by_repo[repos[0].id] == SyncOutcome.ok_changed  # NFR-2: живой репо обработан
    assert by_repo[repos[1].id] == SyncOutcome.repo_unavailable
    assert by_repo[repos[2].id] == SyncOutcome.auth_failed
    assert by_repo[repos[3].id] == SyncOutcome.skipped_rate_limit
    assert run.status == SyncStatus.partial


@pytest.mark.anyio
async def test_all_repos_failed_run_failed(session):
    adef, (repo,) = _seed(session)
    client = FakeGitClient({repo.repo_url: GitRepoUnavailableError("404")})

    run = await _sync(session, client)

    assert run.status == SyncStatus.failed


# ── AC 4/AC 1: POST /sync ───────────────────────────────────────────────────


@pytest.fixture()
def client_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CD_ADMIN_PASSWORD", "pw")
    db_path = tmp_path / "test.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")

    with Session(engine) as s:
        _seed(s)
        s.commit()

    def override_session():
        with Session(engine) as s:
            yield s
            s.commit()

    fake = FakeGitClient({"https://github.com/s1/r": {"product/prd.md": PRD_TEXT}})
    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_git_client] = lambda: fake
    yield TestClient(app), engine
    app.dependency_overrides.clear()
    engine.dispose()


def test_sync_route_requires_auth_or_token(client_env):
    client, _ = client_env
    assert client.post("/sync").status_code == 401


def test_sync_route_with_sync_token_runs_walk(client_env, monkeypatch):
    from app.config import settings

    client, engine = client_env
    monkeypatch.setattr(settings, "sync_token", "cron-secret")
    response = client.post("/sync", headers={"X-Sync-Token": "cron-secret"})
    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    with Session(engine) as s:
        assert s.scalar(select(SyncRun)) is not None


def test_sync_route_with_session_auth(client_env):
    client, _ = client_env
    client.post("/login", data={"username": "admin", "password": "pw"})
    assert client.post("/sync").status_code == 200


def test_sync_route_wrong_token_401(client_env, monkeypatch):
    from app.config import settings

    client, _ = client_env
    monkeypatch.setattr(settings, "sync_token", "cron-secret")
    assert client.post("/sync", headers={"X-Sync-Token": "wrong"}).status_code == 401
