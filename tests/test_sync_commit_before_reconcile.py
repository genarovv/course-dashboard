"""FIX-I2: обход коммитит наблюдения до свода — иначе ядро FR-5 не видит
снапшоты текущего обхода и каждая пара падает в И2.

Боевой случай 2026-08-04 (обход 731b4991): статус completed, 4 новых снапшота
записаны, вердиктов ноль, в логе «И2: снапшоты пары не найдены» на каждую пару.
Причина: run_sync делал flush(), а коммит происходил позже — в зависимости
get_session, уже после того, как воркеры свода открыли собственные сессии
(coherence_analyzer.make_verdict_worker: «каждая пара — собственная сессия»).

Дефект тихий: система рапортует успех, работа не сделана. Проявляется только
когда артефакт изменился — при неизменном хеше D28 не пишет новый снапшот и
пара строится на снапшоте прошлого обхода, давно закоммиченном.
"""

import asyncio
from datetime import datetime

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from alembic import command
from app import store
from app.models import GitHost, SyncTrigger
from app.models.artifact_def import ArtifactDef
from app.models.artifact_snapshot import ArtifactSnapshot
from app.models.coherence_verdict import CoherenceVerdict
from app.models.lesson import Lesson
from app.services import sync_orchestrator
from app.services.coherence_analyzer import make_verdict_worker
from app.services.sync_orchestrator import run_sync

LLM_MODEL = "deepseek-v4-flash"

PRD_TEXT = "# PRD\nПродукт делает X."
DM_TEXT = "# Модель данных\nСущность X."


class FakeGitClient:
    """Репозиторий с двумя артефактами; содержимое одинаково для любой ревизии."""

    files = {"product/prd.md": PRD_TEXT, "data-model.md": DM_TEXT}

    async def get_tree(self, repo_url, git_host, ref="main"):
        return list(self.files)

    async def get_file_content(self, repo_url, git_host, file_path, ref="main"):
        return self.files[file_path]

    async def fetch_default_branch(self, repo_url, git_host):
        return "main"

    async def get_head_sha(self, repo_url, git_host, ref="main"):
        return "a" * 40

    async def get_head_commit_date(self, repo_url, git_host, ref="main"):
        return "2026-08-04T09:00:00Z"  # контракт клиента — ISO-строка хостинга


class FakeLLM:
    """Ядро FR-5: всегда «ok» — тест проверяет не вердикт, а что он вообще посчитан."""

    async def check_coherence(self, source_text, target_text, rubric_text, model=None):
        return {
            "verdict": "ok", "confidence": "high",
            "entities_checked": 1, "entities_found": 1,
            "entities_excluded": 0, "entities_lost": 0,
            "points": [], "notes": "связно",
        }

    async def aclose(self):
        return None


@pytest.fixture()
def engine(tmp_path):
    db_path = tmp_path / "fix-i2.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    eng = create_engine(f"sqlite:///{db_path}")
    yield eng
    eng.dispose()


def _seed_config(session):
    """Конфигурация курса до обхода: два артефакта, ребро между ними, репозиторий."""
    lesson5 = Lesson(number=5, title="PRD", date=datetime(2026, 6, 30).date())
    lesson6 = Lesson(number=6, title="Данные", date=datetime(2026, 7, 2).date())
    session.add_all([lesson5, lesson6])
    session.flush()
    session.add_all([
        ArtifactDef(lesson_id=lesson5.id, role="prd", expected_pattern="product/prd.md"),
        ArtifactDef(lesson_id=lesson6.id, role="data_model", expected_pattern="data-model.md"),
    ])
    rubric = store.register_rubric(session, type="edge", version="1.0", text="правило")
    session.flush()
    store.config_create_edge_def(
        session, source_role="prd", target_role="data_model", rubric_id=rubric.id
    )
    repo = store.register_repository(
        session, repo_url="https://github.com/s/x", git_host=GitHost.GitHub
    )
    session.commit()
    return repo


async def _run_sync_with_core(session, engine):
    """Обход с подключённым ядром: воркеры работают в собственных сессиях."""
    git = FakeGitClient()
    worker = make_verdict_worker(lambda: Session(engine), git, FakeLLM)
    run = await run_sync(
        session, git, triggered_by=SyncTrigger.manual,
        llm_model=LLM_MODEL, verdict_worker=worker,
    )
    # свод — fire-and-forget: дожидаемся задач, которые он поставил
    await asyncio.gather(*list(sync_orchestrator._pending_tasks))
    return run


@pytest.mark.anyio
async def test_snapshots_visible_to_other_session_right_after_sync(engine):
    """Наблюдения обхода закоммичены к моменту, когда свод отдаёт пары воркерам."""
    with Session(engine) as session:
        _seed_config(session)
        await _run_sync_with_core(session, engine)

    with Session(engine) as other:  # сессия воркера — своя, чужой транзакции не видит
        assert len(other.scalars(select(ArtifactSnapshot)).all()) == 2


@pytest.mark.anyio
async def test_verdict_computed_for_artifact_first_seen_in_this_sync(engine):
    """Вердикт считается в том же обходе, в котором артефакт впервые наблюдён.

    Регресс 2026-08-04: пары строились на снапшотах незакоммиченной транзакции,
    воркер их не находил и валился в И2 — вердиктов ноль при статусе completed.
    """
    with Session(engine) as session:
        _seed_config(session)
        await _run_sync_with_core(session, engine)

    with Session(engine) as other:
        verdicts = other.scalars(select(CoherenceVerdict)).all()
    assert len(verdicts) == 1, "пара обхода осталась без вердикта (И2)"
    assert verdicts[0].verdict.value == "ok"
