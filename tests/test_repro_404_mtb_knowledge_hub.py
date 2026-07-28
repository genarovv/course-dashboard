"""Репродукционный тест: 404 обходчика с mtb-knowledge-hub.

Воспроизводит сценарий, когда github.com/Intese-m9/mtb-knowledge-hub
недоступен (HTTP 404). Тест требует, чтобы ВСЕ 9 репозиториев успешно
обошлись — если хоть один (включая mtb-knowledge-hub) падает с 404,
тест падает. После дебага и починки тест позеленеет.

Flow:
  1. Сид 9 репозиториев из реального student-repos.csv
  2. FakeGitClient: mtb-knowledge-hub → 404, остальные → норм
  3. Запуск sync → должен завершиться Status.completed (все 9 ок)
"""

from datetime import datetime

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from alembic import command
from app import store
from app.clients.git_client import GitRepoUnavailableError
from app.models import GitHost, SyncOutcome, SyncStatus, SyncTrigger
from app.models.artifact_def import ArtifactDef
from app.models.lesson import Lesson
from app.models.sync_run_repository import SyncRunRepository
from app.services.sync_orchestrator import run_sync

MTH_URL = "https://github.com/Intese-m9/mtb-knowledge-hub"

STUDENT_URLS = [
    "https://git.culab.ru/course-projects/ai-agents-driven-software-development-v-1/ai-agents-driven-software-development-v-0.1_project-contex/e.shevchenko",
    "https://github.com/Hazretovich/QA_Agent",
    MTH_URL,
    "https://git.culab.ru/a.dvortsov/project_contex",
    "https://git.culab.ru/course-projects/ai-agents-driven-software-development-v-1/ai-agents-driven-software-development-v-0.1_project-contex/g.klishin",
    "https://git.culab.ru/course-projects/ai-agents-driven-software-development-v-1/ai-agents-driven-software-development-v-0.1_project-contex/d.kolesov",
    "https://git.culab.ru/course-projects/ai-agents-driven-software-development-v-1/ai-agents-driven-software-development-v-0.1_project-contex/a.gigolaev",
    "https://git.culab.ru/course-projects/ai-agents-driven-software-development-v-1/ai-agents-driven-software-development-v-0.1_project-contex/v.dorogovtsev",
    "https://git.culab.ru/course-projects/ai-agents-driven-software-development-v-1/ai-agents-driven-software-development-v-0.1_project-contex/d.taranenko",
]

HEAD_SHA = "f" * 40


class FakeGitClient:
    def __init__(self, repos: dict, branches: dict | None = None):
        self.repos = repos
        self._branches = branches or {}
        self._ref_errors: dict[str, set[str]] = {}

    def with_ref_error(self, url: str, ref: str):
        self._ref_errors.setdefault(url, set()).add(ref)
        return self

    def _entry(self, repo_url):
        entry = self.repos[repo_url]
        if isinstance(entry, Exception):
            raise entry
        return entry

    async def fetch_default_branch(self, repo_url, git_host):
        return self._branches.get(repo_url, "main")

    async def get_tree(self, repo_url, git_host, ref="main"):
        if repo_url in self._ref_errors and ref in self._ref_errors[repo_url]:
            raise GitRepoUnavailableError(f"HTTP 404 (ref={ref})")
        return list(self._entry(repo_url).keys())

    async def get_file_content(self, repo_url, git_host, file_path, ref="main"):
        self._entry(repo_url)
        return "dummy content"

    async def get_head_sha(self, repo_url, git_host, ref="main"):
        self._entry(repo_url)
        return HEAD_SHA


PRD_TEXT = "# PRD\nПродукт делает X."


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


def _seed(session):
    lesson = Lesson(number=5, title="PRD", date=datetime(2026, 6, 30).date())
    session.add(lesson)
    session.flush()
    adef = ArtifactDef(lesson_id=lesson.id, role="prd", expected_pattern="product/prd.md")
    session.add(adef)
    repos = [
        store.register_repository(session, repo_url=url,
                                  git_host=GitHost.GitHub if "github.com" in url.lower() else GitHost.GitLab)
        for url in STUDENT_URLS
    ]
    session.flush()
    return adef, repos


@pytest.mark.anyio
async def test_mtb_knowledge_hub_should_not_404(session):
    """Все 9 репозиториев должны успешно обходиться (mtb-knowledge-hub — тоже)."""
    adef, repos = _seed(session)
    fake = {}
    for repo in repos:
        if repo.repo_url == MTH_URL:
            fake[repo.repo_url] = {"product/prd.md": PRD_TEXT}
        else:
            fake[repo.repo_url] = {"product/prd.md": PRD_TEXT}
    client = FakeGitClient(
        fake,
        branches={MTH_URL: "master"},
    ).with_ref_error(MTH_URL, "main")

    run = await run_sync(session, client, triggered_by=SyncTrigger.manual)

    by_repo = {
        r.repository_id: r for r in session.scalars(select(SyncRunRepository))
    }

    for repo in repos:
        row = by_repo[repo.id]
        assert row.outcome in (SyncOutcome.ok_changed, SyncOutcome.ok_unchanged), (
            f"{repo.repo_url} должен успешно обходиться, но получил {row.outcome}"
            + (f" — detail: {row.detail}" if row.detail else "")
        )

    assert run.status == SyncStatus.completed, (
        f"sync-run должен иметь статус completed (все 9 репо успешны), "
        f"got {run.status}"
    )
