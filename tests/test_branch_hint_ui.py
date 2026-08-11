"""D43 (#69), AC 3–4: подсказка «работа вне основной ветки» видна в интерфейсе.

Строка матрицы и дело защиты обязаны показать, что работа есть, но лежит в другой
ветке. Без этого пустая строка читается как «студент ничего не сделал» — ровно та
ошибка, ради которой этап 1 и делается.
"""

from datetime import datetime

import pytest
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from alembic import command
from app import store
from app.models import GitHost, SyncStatus, SyncTrigger
from app.models.artifact_def import ArtifactDef
from app.models.lesson import Lesson
from app.services.artifact_matrix import build_artifact_matrix

REPO_URL = "https://git.culab.ru/course/s5"


@pytest.fixture()
def session(tmp_path):
    db_path = tmp_path / "hint_ui.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as s:
        yield s
    engine.dispose()


def _seed_with_hint(session, branch="dev", found=7):
    lesson = Lesson(number=5, title="PRD", date=datetime(2026, 6, 30).date())
    session.add(lesson)
    session.flush()
    session.add(ArtifactDef(lesson_id=lesson.id, role="prd", expected_pattern="product/prd.md"))
    repo = store.register_repository(session, repo_url=REPO_URL, git_host=GitHost.GitHub)
    run = store.register_sync_run(session, triggered_by=SyncTrigger.manual)
    session.flush()
    store.register_sync_outcome(
        session, sync_run_id=run.id, repository_id=repo.id,
        outcome=store.SyncOutcome.ok_changed, detail=None,
    )
    store.update_sync_run_status(session, run.id, SyncStatus.completed)
    store.register_branch_hint(
        session, sync_run_id=run.id, repository_id=repo.id, branch_name=branch,
        # время хранится в UTC, показывается местное (#32): 10:00 UTC не переваливает
        # за полночь ни при одном разумном смещении, поэтому дата в метке — 15.07
        head_sha="a" * 40, head_date=datetime(2026, 7, 15, 10, 0),
        artifacts_found=found, artifacts_in_default=1,
    )
    session.flush()
    return repo, run


def test_matrix_row_carries_branch_hint(session):
    """Строка матрицы несёт подсказку: имя ветки, число артефактов, дата."""
    repo, _ = _seed_with_hint(session)

    matrix = build_artifact_matrix(session)

    (hint,) = matrix["row_branch_hints"][repo.id]
    assert hint["branch_name"] == "dev"
    assert hint["artifacts_found"] == 7
    assert "15.07" in hint["head_date_label"]


def test_repository_without_hint_has_empty_list(session):
    """Нет подсказок — пустой список, а не отсутствующий ключ: шаблон не падает."""
    lesson = Lesson(number=5, title="PRD", date=datetime(2026, 6, 30).date())
    session.add(lesson)
    session.flush()
    session.add(ArtifactDef(lesson_id=lesson.id, role="prd", expected_pattern="product/prd.md"))
    repo = store.register_repository(session, repo_url=REPO_URL, git_host=GitHost.GitHub)
    run = store.register_sync_run(session, triggered_by=SyncTrigger.manual)
    session.flush()
    store.register_sync_outcome(
        session, sync_run_id=run.id, repository_id=repo.id,
        outcome=store.SyncOutcome.ok_changed, detail=None,
    )
    store.update_sync_run_status(session, run.id, SyncStatus.completed)
    session.flush()

    matrix = build_artifact_matrix(session)

    assert matrix["row_branch_hints"][repo.id] == []


def test_only_latest_sync_hints_are_shown(session):
    """Журнал накапливает подсказки всех обходов — матрице нужен последний срез."""
    repo, _ = _seed_with_hint(session, branch="dev", found=7)
    run2 = store.register_sync_run(session, triggered_by=SyncTrigger.manual)
    session.flush()
    store.register_sync_outcome(
        session, sync_run_id=run2.id, repository_id=repo.id,
        outcome=store.SyncOutcome.ok_changed, detail=None,
    )
    store.update_sync_run_status(session, run2.id, SyncStatus.completed)
    store.register_branch_hint(
        session, sync_run_id=run2.id, repository_id=repo.id, branch_name="sleepy-code",
        head_sha="b" * 40, head_date=datetime(2026, 7, 20, 10, 0),
        artifacts_found=9, artifacts_in_default=1,
    )
    session.flush()

    matrix = build_artifact_matrix(session)

    names = [h["branch_name"] for h in matrix["row_branch_hints"][repo.id]]
    assert names == ["sleepy-code"]  # подсказка прошлого обхода не залипает
