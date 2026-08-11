"""D44 (#70), недоделанный критерий приёмки: контрактный путь под лишним префиксом.

В AC тикета записано: кандидатом считается и «контрактный путь с одним лишним
префиксным сегментом (`<что-угодно>/product/prd.md`)». Реализованы были только
два якоря — имя файла и имя последнего каталога. Для паттернов вида
`src/**/*.py` оба якоря бессильны: предпоследний сегмент — это `**`, якоря нет,
и поиск сдавался, чтобы не выродиться в «любой py-файл где угодно».

Цена пробела найдена на живом репозитории 2026-08-11: у студента С-02 весь код
лежит в `target/src/API/...`, паттерн `src/**/*.py` не совпадает, и роль «код»
показывалась пустой при 32 файлах кода в репозитории.

Правило узкое: совпадает вся структура контрактного пути целиком, просто под
произвольным префиксом. Это не ослабление контракта — «не там» по-прежнему не
равно «сдано».
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
from app.services.sync_orchestrator import _displaced_candidates, run_sync

REPO_URL = "https://github.com/s1/r"
CODE = "def main():\n    return 1\n"


@pytest.fixture()
def session(tmp_path):
    db_path = tmp_path / "prefix.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as s:
        yield s
    engine.dispose()


class FakeGitClient:
    def __init__(self, files: dict):
        self.files = files

    async def get_tree(self, repo_url, git_host, ref="main"):
        return list(self.files.keys())

    async def get_file_content(self, repo_url, git_host, file_path, ref="main"):
        return self.files[file_path]

    async def fetch_default_branch(self, repo_url, git_host):
        return "main"

    async def get_head_sha(self, repo_url, git_host, ref="main"):
        return "f" * 40


def _seed(session, pattern, role):
    lesson = Lesson(number=9, title="Разработка", date=datetime(2026, 7, 14).date())
    session.add(lesson)
    session.flush()
    adef = ArtifactDef(lesson_id=lesson.id, role=role, expected_pattern=pattern)
    session.add(adef)
    repo = store.register_repository(session, repo_url=REPO_URL, git_host=GitHost.GitHub)
    session.flush()
    return adef, repo


# ── чистая функция отбора ────────────────────────────────────────────────────


def test_glob_pattern_matches_under_extra_prefix():
    """Случай С-02: src/**/*.py против target/src/API/example_api/client.py."""
    tree = ["target/src/API/example_api/client.py", "README.md"]

    assert _displaced_candidates("src/**/*.py", tree, set()) == [
        "target/src/API/example_api/client.py"
    ]


def test_plain_pattern_matches_under_extra_prefix():
    """Случай С-06: product/prd.md против d.kolesov/product/prd.md."""
    tree = ["d.kolesov/product/prd.md"]

    assert _displaced_candidates("product/prd.md", tree, set()) == ["d.kolesov/product/prd.md"]


def test_prefix_match_does_not_swallow_unrelated_files():
    """Негативный: структура пути обязана совпасть целиком, а не хвостом имени."""
    tree = ["docs/notes.py", "scripts/build.py", "src_backup/x.py"]

    assert _displaced_candidates("src/**/*.py", tree, set()) == []


def test_prefix_candidates_skip_noise_directories():
    """Шумные каталоги остаются шумными и под префиксом."""
    tree = ["node_modules/pkg/src/lib/a.py", "build/src/gen/b.py"]

    assert _displaced_candidates("src/**/*.py", tree, set()) == []


def test_shallowest_prefix_wins():
    """Детерминизм: ближайший к корню кандидат первый."""
    tree = ["a/b/c/src/deep.py", "target/src/near.py"]

    assert _displaced_candidates("src/**/*.py", tree, set())[0] == "target/src/near.py"


# ── поведение обхода ─────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_code_under_prefix_observed_as_wrong_place(session):
    """Роль «код» перестаёт быть пустой там, где код есть — но лежит под префиксом."""
    adef, repo = _seed(session, "src/**/*.py", "code")
    client = FakeGitClient({"target/src/API/client.py": CODE, "README.md": "x"})

    await run_sync(session, client, triggered_by=SyncTrigger.manual)
    session.flush()

    snap = store.find_last_snapshot(session, repo.id, adef.id)
    assert snap.status == SnapshotStatus.partial
    assert snap.partial_reason == ["wrong_place"]
    assert snap.file_path == "target/src/API/client.py"


@pytest.mark.anyio
async def test_contract_path_still_wins_over_prefixed(session):
    """Код по контрактному пути — по-прежнему «сдано», кандидаты не рассматриваются."""
    adef, repo = _seed(session, "src/**/*.py", "code")
    client = FakeGitClient({"src/main.py": CODE, "target/src/API/client.py": CODE})

    await run_sync(session, client, triggered_by=SyncTrigger.manual)
    session.flush()

    snap = store.find_last_snapshot(session, repo.id, adef.id)
    assert snap.status == SnapshotStatus.found
