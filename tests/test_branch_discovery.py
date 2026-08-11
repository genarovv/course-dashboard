"""D43 (#69): обход видит работу, лежащую вне дефолтной ветки.

Найдено ручным обходом 2026-08-11: у четверых из семи студентов работа есть, но
дашборд показывал пустые строки. Он читает `repo.default_branch` (ADR-006), а
работа лежала в других ветках — и в форках ЦУ попасть в `main` она не может:
ветка защищена, merge доступен только Maintainer, студенты — Developer.

Инструмент честно отвечал на вопрос «что лежит в дефолтной ветке», а читался его
ответ как «что сделал студент». Этап 1 не меняет канон вердиктов: они по-прежнему
считаются только по дефолтной ветке. Добавляется сигнал — «работа есть вон там».
"""

from datetime import datetime, timedelta

import pytest
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from alembic import command
from app import store
from app.clients.git_client import BranchInfo, GitRepoUnavailableError
from app.models import GitHost, SyncStatus, SyncTrigger
from app.models.artifact_def import ArtifactDef
from app.models.lesson import Lesson
from app.services.sync_orchestrator import run_sync

REPO_URL = "https://github.com/s1/r"
PRD = "# PRD\nПродукт делает X."
NEWER = "2026-08-10T10:00:00Z"
OLDER = "2026-06-01T10:00:00Z"


@pytest.fixture()
def session(tmp_path):
    db_path = tmp_path / "branches.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as s:
        yield s
    engine.dispose()


class FakeGitClient:
    """Фейк с деревьями по веткам: {ref: {path: content}} плюс список веток."""

    def __init__(self, trees: dict, branches: list, branches_error: Exception | None = None):
        self.trees = trees
        self.branches = branches
        self.branches_error = branches_error
        self.tree_calls: list[str] = []

    async def get_tree(self, repo_url, git_host, ref="main"):
        self.tree_calls.append(ref)
        return list(self.trees.get(ref, {}).keys())

    async def get_file_content(self, repo_url, git_host, file_path, ref="main"):
        return self.trees[ref][file_path]

    async def fetch_default_branch(self, repo_url, git_host):
        return "main"

    async def get_head_sha(self, repo_url, git_host, ref="main"):
        return "f" * 40

    async def list_branches(self, repo_url, git_host):
        if self.branches_error:
            raise self.branches_error
        return self.branches


def _seed(session):
    lesson = Lesson(number=5, title="PRD", date=datetime(2026, 6, 30).date())
    session.add(lesson)
    session.flush()
    adef = ArtifactDef(lesson_id=lesson.id, role="prd", expected_pattern="product/prd.md")
    session.add(adef)
    repo = store.register_repository(session, repo_url=REPO_URL, git_host=GitHost.GitHub)
    session.flush()
    return adef, repo


def _branch(name, date, sha="a" * 40):
    return BranchInfo(name=name, head_sha=sha, committed_date=date)


async def _sync(session, client):
    run = await run_sync(session, client, triggered_by=SyncTrigger.manual)
    session.flush()
    return run


@pytest.mark.anyio
async def test_branch_with_more_artifacts_is_recorded(session):
    """Случай С-05/С-07: в дефолтной ветке шаблон, вся работа — в другой ветке."""
    _seed(session)
    client = FakeGitClient(
        trees={"main": {"README.md": "x"}, "dev": {"product/prd.md": PRD, "README.md": "x"}},
        branches=[_branch("main", OLDER), _branch("dev", NEWER)],
    )

    run = await _sync(session, client)

    (hint,) = store.find_branch_hints(session, run.id)
    assert hint.branch_name == "dev"
    assert hint.artifacts_found == 1
    assert hint.head_date is not None


@pytest.mark.anyio
async def test_branch_older_than_default_is_ignored(session):
    """Ветка старше дефолтной — это её прошлое, а не спрятанная работа."""
    _seed(session)
    client = FakeGitClient(
        trees={"main": {"README.md": "x"}, "stale": {"product/prd.md": PRD}},
        branches=[_branch("main", NEWER), _branch("stale", OLDER)],
    )

    run = await _sync(session, client)

    assert store.find_branch_hints(session, run.id) == []


@pytest.mark.anyio
async def test_branch_without_extra_artifacts_is_ignored(session):
    """Ветка свежее, но артефактов не больше — сигналить не о чем (AC 6, защита от шума)."""
    _seed(session)
    client = FakeGitClient(
        trees={"main": {"product/prd.md": PRD}, "wip": {"product/prd.md": PRD, "notes.txt": "x"}},
        branches=[_branch("main", OLDER), _branch("wip", NEWER)],
    )

    run = await _sync(session, client)

    assert store.find_branch_hints(session, run.id) == []


@pytest.mark.anyio
async def test_default_branch_is_never_a_hint(session):
    """Дефолтная ветка — не «работа вне дефолтной ветки», как бы свежа ни была."""
    _seed(session)
    client = FakeGitClient(
        trees={"main": {"product/prd.md": PRD}},
        branches=[_branch("main", NEWER)],
    )

    run = await _sync(session, client)

    assert store.find_branch_hints(session, run.id) == []


@pytest.mark.anyio
async def test_branch_trees_do_not_produce_snapshots(session):
    """Канон вердиктов не трогаем: наблюдение по-прежнему только с дефолтной ветки."""
    adef, repo = _seed(session)
    client = FakeGitClient(
        trees={"main": {"README.md": "x"}, "dev": {"product/prd.md": PRD}},
        branches=[_branch("main", OLDER), _branch("dev", NEWER)],
    )

    await _sync(session, client)

    snap = store.find_last_snapshot(session, repo.id, adef.id)
    assert snap.status.value == "not_found"  # свод честно говорит «в main пусто»
    assert snap.file_path is None


@pytest.mark.anyio
async def test_branches_api_error_does_not_break_sync(session):
    """NFR-2: список веток недоступен — обход завершается, артефакты наблюдены."""
    adef, repo = _seed(session)
    client = FakeGitClient(
        trees={"main": {"product/prd.md": PRD}},
        branches=[],
        branches_error=GitRepoUnavailableError("503"),
    )

    run = await _sync(session, client)

    assert run.status == SyncStatus.completed
    assert store.find_last_snapshot(session, repo.id, adef.id).status.value == "found"
    assert store.find_branch_hints(session, run.id) == []


@pytest.mark.anyio
async def test_request_budget_is_one_tree_per_candidate_branch(session):
    """AC 7: дефолтная ветка + только ветки свежее её. Старые деревья не читаются."""
    _seed(session)
    client = FakeGitClient(
        trees={
            "main": {"README.md": "x"},
            "dev": {"product/prd.md": PRD},
            "old": {"product/prd.md": PRD},
        },
        branches=[_branch("main", OLDER), _branch("dev", NEWER), _branch("old", "2026-05-01T10:00:00Z")],
    )

    await _sync(session, client)

    assert client.tree_calls == ["main", "dev"]  # «old» старше main — не читается


@pytest.mark.anyio
async def test_many_candidate_branches_are_capped_and_logged(session, caplog):
    """Репозиторий с 17 ветками не должен стоить 17 запросов — и урезание не молчит."""
    _seed(session)
    names = [f"feat/{i}" for i in range(12)]
    trees = {"main": {"README.md": "x"}}
    for i, n in enumerate(names):
        trees[n] = {"product/prd.md": PRD, f"f{i}.md": "x"}
    base = datetime(2026, 8, 1)
    branches = [_branch("main", OLDER)] + [
        _branch(n, (base + timedelta(days=i)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        for i, n in enumerate(names)
    ]
    client = FakeGitClient(trees=trees, branches=branches)

    with caplog.at_level("WARNING"):
        run = await _sync(session, client)

    assert len(client.tree_calls) <= 1 + 5  # дефолтная + не больше пяти кандидатов
    assert len(store.find_branch_hints(session, run.id)) <= 5
    assert any("ветк" in r.getMessage().lower() for r in caplog.records)  # след урезания
