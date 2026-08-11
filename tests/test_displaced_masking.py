"""D44 (#70): заготовка по контрактному пути не должна маскировать настоящий файл рядом.

Найдено ручным обходом 2026-08-11. У студента С-06 в корне форка лежат шаблонные
заглушки (`ARCHITECTURE.md`, `product/prd.md` из выданного шаблона), а вся настоящая
работа — на уровень ниже, в каталоге `d.kolesov/`. Обход давал `found=0` при восьми
`template_copy`: ветка `template_copy` срабатывала первой, и поиск «не там»
(`_find_displaced`) до настоящего файла не доходил никогда.

Заглушка вытесняет реальную работу — это хуже, чем «не нашли»: преподаватель читает
пустую строку как «студент не сделал ничего».

Два смежных пробела того же класса, оба у С-03:
  * регистр имени — паттерн `ARCHITECTURE.md` против `architecture/architecture.md`;
  * паттерны с глобом (`product/user-stories/*.md`) — поиск «не там» был выключен целиком.

Скоуп сознательно узкий: статус остаётся `partial` с причиной `wrong_place`. Контракт
путей (ADR-003) не ослабляется — «не там» это не «сдано».
"""

from datetime import datetime

import pytest
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from alembic import command
from app import store
from app.models import GitHost, SnapshotStatus, SyncTrigger
from app.models.artifact_def import ArtifactDef
from app.models.lesson import Lesson
from app.services.config_manager import TemplateRepoConfig
from app.services.sync_orchestrator import run_sync

TEMPLATE_URL = "https://github.com/genarovv/project-context-template"
REPO_URL = "https://github.com/s1/r"
STUB = "# PRD (шаблон)\nЗаполните разделы."
REAL = "# PRD\nПродукт делает X, потому что Y."
REAL_ARCH = "# Архитектура\nСлои: интерфейс, сервисы, хранилище."


@pytest.fixture()
def session(tmp_path):
    db_path = tmp_path / "masking.db"
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
        return "f" * 40


def _seed(session, pattern="product/prd.md", role="prd"):
    lesson = Lesson(number=5, title="PRD", date=datetime(2026, 6, 30).date())
    session.add(lesson)
    session.flush()
    adef = ArtifactDef(lesson_id=lesson.id, role=role, expected_pattern=pattern)
    session.add(adef)
    repo = store.register_repository(session, repo_url=REPO_URL, git_host=GitHost.GitHub)
    session.flush()
    return adef, repo


def _template_cfg():
    return TemplateRepoConfig(url=TEMPLATE_URL, git_host="GitHub")


async def _sync(session, client):
    run = await run_sync(
        session, client, triggered_by=SyncTrigger.manual, template_repo=_template_cfg()
    )
    session.flush()
    return run


# ── главный дефект: заглушка маскирует настоящий файл ────────────────────────


@pytest.mark.anyio
async def test_template_stub_does_not_mask_real_file_elsewhere(session):
    """Случай С-06: шаблон по контрактному пути + настоящая работа на уровень ниже."""
    adef, repo = _seed(session)
    client = FakeGitClient({
        TEMPLATE_URL: {"product/prd.md": STUB},
        REPO_URL: {"product/prd.md": STUB, "d.kolesov/product/prd.md": REAL},
    })

    await _sync(session, client)

    snap = store.find_last_snapshot(session, repo.id, adef.id)
    assert snap.status == SnapshotStatus.partial
    assert snap.partial_reason == ["wrong_place"]
    assert snap.file_path == "d.kolesov/product/prd.md"


@pytest.mark.anyio
async def test_empty_file_at_contract_path_does_not_mask_real_file(session):
    """Пустой файл по контрактному пути — тот же класс маскировки, что и шаблон."""
    adef, repo = _seed(session)
    client = FakeGitClient({
        TEMPLATE_URL: {"product/prd.md": STUB},
        REPO_URL: {"product/prd.md": "   \n", "docs/prd.md": REAL},
    })

    await _sync(session, client)

    snap = store.find_last_snapshot(session, repo.id, adef.id)
    assert snap.status == SnapshotStatus.partial
    assert snap.partial_reason == ["wrong_place"]
    assert snap.file_path == "docs/prd.md"


@pytest.mark.anyio
async def test_both_copies_template_keeps_template_copy(session):
    """Обе копии — заготовки: маскировать нечего, поведение прежнее (регрессия)."""
    adef, repo = _seed(session)
    client = FakeGitClient({
        TEMPLATE_URL: {"product/prd.md": STUB},
        REPO_URL: {"product/prd.md": STUB, "docs/prd.md": STUB},
    })

    await _sync(session, client)

    snap = store.find_last_snapshot(session, repo.id, adef.id)
    assert snap.status == SnapshotStatus.partial
    assert snap.partial_reason == ["template_copy"]
    assert snap.file_path == "product/prd.md"


@pytest.mark.anyio
async def test_real_file_at_contract_path_wins_over_displaced(session):
    """Контрактный путь заполнен по-настоящему — кандидаты не рассматриваются вовсе."""
    adef, repo = _seed(session)
    client = FakeGitClient({
        TEMPLATE_URL: {"product/prd.md": STUB},
        REPO_URL: {"product/prd.md": REAL, "docs/prd.md": REAL_ARCH},
    })

    await _sync(session, client)

    snap = store.find_last_snapshot(session, repo.id, adef.id)
    assert snap.status == SnapshotStatus.found
    assert snap.file_path == "product/prd.md"


# ── регистр имени ────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_displaced_match_is_case_insensitive(session):
    """Случай С-03: паттерн ARCHITECTURE.md против architecture/architecture.md."""
    adef, repo = _seed(session, pattern="ARCHITECTURE.md", role="architecture")
    client = FakeGitClient({
        TEMPLATE_URL: {"ARCHITECTURE.md": STUB},
        REPO_URL: {"architecture/architecture.md": REAL_ARCH},
    })

    await _sync(session, client)

    snap = store.find_last_snapshot(session, repo.id, adef.id)
    assert snap.status == SnapshotStatus.partial
    assert snap.partial_reason == ["wrong_place"]
    assert snap.file_path == "architecture/architecture.md"


# ── паттерны с глобом ────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_glob_pattern_matches_by_parent_directory(session):
    """Случай С-03: product/user-stories/*.md против user-stories/user-stories.md.

    Для глоб-паттернов якорь — имя последнего каталога, а не имя файла: иначе
    «*.md» выродился бы в «любой md-файл где угодно».
    """
    adef, repo = _seed(session, pattern="product/user-stories/*.md", role="user_story")
    client = FakeGitClient({
        TEMPLATE_URL: {"product/user-stories/README.md": STUB},
        REPO_URL: {"user-stories/user-stories.md": REAL},
    })

    await _sync(session, client)

    snap = store.find_last_snapshot(session, repo.id, adef.id)
    assert snap.status == SnapshotStatus.partial
    assert snap.partial_reason == ["wrong_place"]
    assert snap.file_path == "user-stories/user-stories.md"


@pytest.mark.anyio
async def test_glob_pattern_does_not_match_unrelated_directory(session):
    """Негативный: чужой каталог с md-файлами кандидатом не становится."""
    adef, repo = _seed(session, pattern="product/user-stories/*.md", role="user_story")
    client = FakeGitClient({
        TEMPLATE_URL: {"product/user-stories/README.md": STUB},
        REPO_URL: {"notes/random.md": REAL, "docs/whatever.md": REAL},
    })

    await _sync(session, client)

    snap = store.find_last_snapshot(session, repo.id, adef.id)
    assert snap.status == SnapshotStatus.not_found
    assert snap.file_path is None


# ── шум и детерминизм ────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_noise_directories_are_not_candidates(session):
    """archive/, node_modules/ и прочий шум кандидатами не считаются."""
    adef, repo = _seed(session)
    client = FakeGitClient({
        TEMPLATE_URL: {"product/prd.md": STUB},
        REPO_URL: {"archive/product/prd.md": REAL, "node_modules/pkg/prd.md": REAL},
    })

    await _sync(session, client)

    snap = store.find_last_snapshot(session, repo.id, adef.id)
    assert snap.status == SnapshotStatus.not_found


@pytest.mark.anyio
async def test_shallowest_candidate_wins(session):
    """Детерминизм: при нескольких кандидатах берётся ближайший к корню."""
    adef, repo = _seed(session)
    client = FakeGitClient({
        TEMPLATE_URL: {"product/prd.md": STUB},
        REPO_URL: {"a/b/c/prd.md": REAL, "docs/prd.md": REAL, "x/y/prd.md": REAL},
    })

    await _sync(session, client)

    snap = store.find_last_snapshot(session, repo.id, adef.id)
    assert snap.file_path == "docs/prd.md"
