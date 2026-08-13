"""#75: единая конфигурация логирования — формат, метки, lifecycle-записи обхода.

Покрывает: user_marker (короткий хеш без ПД), configure_logging (идемпотентность,
формат «время UTC, уровень, модуль»), три INFO-записи главного сценария run_sync
(начат / завершён / не завершился) и то, что молчаливые перехваты исключений
(branch_detect) логируются.

Логгеры обхода перехватываются напрямую, а не через caplog (см. test_branch_discovery.py:206):
фикстура прогоняет миграции, fileConfig алембика подменяет root-хендлеры, и записи
до caplog не доходят.
"""

import logging
import re
from datetime import datetime

import pytest
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from alembic import command
from app import store
from app.clients.git_client import GitRepoUnavailableError
from app.logging_config import UtcFormatter, configure_logging, user_marker
from app.models import GitHost, SyncTrigger
from app.models.artifact_def import ArtifactDef
from app.models.lesson import Lesson
from app.models.repository import Repository
from app.services import branch_detect
from app.services.sync_orchestrator import run_sync

PRD_TEXT = "# PRD\nПродукт делает X."
HEAD_SHA = "f" * 40
REPO_URL = "https://github.com/s1/r"


class _RecordListHandler(logging.Handler):
    """Собирает записи в список — перехват логгера без зависимости от root (алембик)."""

    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _capture(logger_name: str) -> tuple[logging.Logger, _RecordListHandler]:
    """Вешает собирающий хендлер на логгер и включает уровень DEBUG."""
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.DEBUG)
    handler = _RecordListHandler()
    handler.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    return logger, handler


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


class FakeGitClient:
    def __init__(self, repos: dict):
        self.repos = repos

    async def get_tree(self, repo_url, git_host, ref="main"):
        return list(self.repos[repo_url].keys())

    async def get_file_content(self, repo_url, git_host, file_path, ref="main"):
        return self.repos[repo_url][file_path]

    async def fetch_default_branch(self, repo_url, git_host):
        return "main"

    async def get_head_sha(self, repo_url, git_host, ref="main"):
        return HEAD_SHA


class BoomFetchClient(FakeGitClient):
    """default_branch падает не-GitClientError-исключением — ломает середину обхода."""

    async def fetch_default_branch(self, repo_url, git_host):
        raise RuntimeError("взрыв середины обхода")


class FailingFetchClient(FakeGitClient):
    """default_branch падает GitClientError — обычная деградация API (ADR-006)."""

    async def fetch_default_branch(self, repo_url, git_host):
        raise GitRepoUnavailableError("404")


def _seed(session) -> tuple[ArtifactDef, list[Repository]]:
    lesson = Lesson(number=5, title="PRD", date=datetime(2026, 6, 30).date())
    session.add(lesson)
    session.flush()
    adef = ArtifactDef(lesson_id=lesson.id, role="prd", expected_pattern="product/prd.md")
    session.add(adef)
    repos = [
        store.register_repository(session, repo_url=url, git_host=GitHost.GitHub)
        for url in (REPO_URL,)
    ]
    session.flush()
    return adef, repos


# ── user_marker: короткий хеш без ПД ─────────────────────────────────────────


def test_user_marker_deterministic_and_short():
    marker = user_marker("ceo@example.com")
    assert marker == user_marker("ceo@example.com")
    assert marker.startswith("u_")
    assert len(marker) == 10  # u_ + 8 гекс-символов
    assert re.fullmatch(r"u_[0-9a-f]{8}", marker)


def test_user_marker_hides_raw_id():
    user_id = "ceo@example.com"
    assert user_id not in user_marker(user_id)


def test_user_marker_differs_across_users():
    assert user_marker("user-a") != user_marker("user-b")


def test_user_marker_none_for_empty():
    assert user_marker(None) is None
    assert user_marker("") is None


# ── configure_logging: формат и идемпотентность ──────────────────────────────


def test_formatter_contains_time_level_module():
    record = logging.LogRecord(
        "app.services.sync_orchestrator", logging.WARNING, "app/services/sync_orchestrator.py",
        1, "контекст %s", ("значение",), None,
    )
    record.created = 1784100000.0  # фиксированный момент для стабильной проверки времени

    text = UtcFormatter().format(record)

    assert "WARNING" in text
    assert "app.services.sync_orchestrator" in text
    assert "контекст значение" in text
    assert re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+0000", text)  # UTC, ISO-8601


def test_configure_logging_is_idempotent():
    configure_logging()
    configure_logging()
    configured = [
        h for h in logging.getLogger().handlers if getattr(h, "_cd_logging_configured", False)
    ]
    assert len(configured) == 1


# ── run_sync: lifecycle-записи главного сценария ─────────────────────────────


async def _sync(session, client, **kwargs):
    return await run_sync(
        session, client, triggered_by=SyncTrigger.manual, **kwargs
    )


@pytest.mark.anyio
async def test_run_sync_logs_started_and_finished(session):
    _seed(session)
    client = FakeGitClient({REPO_URL: {"product/prd.md": PRD_TEXT}})
    logger, handler = _capture("app.services.sync_orchestrator")

    run = await _sync(session, client)
    logger.removeHandler(handler)

    messages = [r.getMessage() for r in handler.records if r.levelno >= logging.INFO]
    started = next(m for m in messages if "Обход начат" in m)
    finished = next(m for m in messages if "Обход завершён" in m)
    assert run.id in started
    assert "manual" in started
    assert "completed" in finished
    assert "репозиториев=" in finished


@pytest.mark.anyio
async def test_run_sync_started_carries_actor_marker(session):
    _seed(session)
    client = FakeGitClient({REPO_URL: {"product/prd.md": PRD_TEXT}})
    logger, handler = _capture("app.services.sync_orchestrator")

    marker = user_marker("ceo@example.com")
    await _sync(session, client, actor=marker)
    logger.removeHandler(handler)

    started = next(r.getMessage() for r in handler.records if "Обход начат" in r.getMessage())
    assert marker in started


@pytest.mark.anyio
async def test_run_sync_logs_failure_and_reraises(session):
    _seed(session)
    client = BoomFetchClient({REPO_URL: {"product/prd.md": PRD_TEXT}})
    logger, handler = _capture("app.services.sync_orchestrator")

    with pytest.raises(RuntimeError):
        await _sync(session, client)
    logger.removeHandler(handler)

    messages = [r.getMessage() for r in handler.records]
    assert any("Обход не завершился" in m for m in messages)


# ── молчаливые перехваты: branch_detect логирует, а не глотает ───────────────


@pytest.mark.anyio
async def test_refresh_default_branch_logs_failure(session):
    _, (repo,) = _seed(session)
    client = FailingFetchClient({REPO_URL: {"product/prd.md": PRD_TEXT}})
    logger, handler = _capture("app.services.branch_detect")

    await branch_detect.refresh_default_branch(session, client, repo)
    logger.removeHandler(handler)

    assert len(handler.records) == 1
    assert "default-ветку" in handler.records[0].getMessage()
