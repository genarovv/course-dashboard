"""G4 (#11), FR-5/FR-8: свод-реконсиляция LLM-пар (ARCHITECTURE §5.1).

AC тикета #11:
  1. В конце каждого обхода все пары (EdgeDef × репозиторий со снапшотами обеих ролей)
     без валидного вердикта на текущую четвёрку идентифицированы
  2. На каждую — asyncio.create_task
  3. Deferred-пары и потерянные при рестарте задачи автоматически перепроверяются следующим сводом

Ядро FR-5 (coherence_analyzer) не кодится до Фазы 0 — воркер вердиктов инъецируется;
без воркера свод только идентифицирует пары (вычислимое состояние «проверяется», §5.1/В13).
"""

import asyncio
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
from app.services.sync_orchestrator import reconcile_llm_pairs, run_sync

LLM_MODEL = "deepseek-v4-flash"


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


def _seed_edge(session):
    """Занятия prd/data_model, ребро prd→data_model с рубрикой, репозиторий."""
    lesson5 = Lesson(number=5, title="PRD", date=datetime(2026, 6, 30).date())
    lesson6 = Lesson(number=6, title="Данные", date=datetime(2026, 7, 2).date())
    session.add_all([lesson5, lesson6])
    session.flush()
    adef_prd = ArtifactDef(lesson_id=lesson5.id, role="prd", expected_pattern="product/prd.md")
    adef_dm = ArtifactDef(lesson_id=lesson6.id, role="data_model", expected_pattern="data-model.md")
    session.add_all([adef_prd, adef_dm])
    rubric = store.register_rubric(session, type="edge", version="1.0", text="правило")
    session.flush()
    edge = store.config_create_edge_def(
        session, source_role="prd", target_role="data_model", rubric_id=rubric.id
    )
    repo = store.register_repository(
        session, repo_url="https://github.com/s/x", git_host=GitHost.GitHub
    )
    run = store.register_sync_run(session, triggered_by=SyncTrigger.manual)
    session.flush()
    return adef_prd, adef_dm, rubric, edge, repo, run


def _snap(session, run, repo, adef, content_hash, status=SnapshotStatus.found):
    fields = dict(
        sync_run_id=run.id, repository_id=repo.id, artifact_def_id=adef.id, status=status
    )
    if status != SnapshotStatus.not_found:
        fields.update(content_hash=content_hash, file_path="f.md", source_commit_sha="a" * 40)
    snap = store.register_snapshot(session, **fields)
    session.flush()
    return snap


class Recorder:
    def __init__(self):
        self.pairs = []

    async def __call__(self, pair):
        self.pairs.append(pair)


# ── AC 1/2: идентификация пар и создание задач ─────────────────────────────


@pytest.mark.anyio
async def test_pair_without_verdict_spawns_task(session):
    adef_prd, adef_dm, rubric, edge, repo, run = _seed_edge(session)
    _snap(session, run, repo, adef_prd, "a" * 64)
    _snap(session, run, repo, adef_dm, "b" * 64)
    worker = Recorder()

    pairs, tasks = await reconcile_llm_pairs(
        session, llm_model=LLM_MODEL, verdict_worker=worker
    )
    await asyncio.gather(*tasks)

    assert len(pairs) == 1
    assert len(tasks) == 1
    (pair,) = worker.pairs
    assert pair.source_content_hash == "a" * 64
    assert pair.target_content_hash == "b" * 64
    assert pair.rubric_id == rubric.id
    assert pair.llm_model == LLM_MODEL
    assert pair.edge_def_id == edge.id
    assert pair.repository_id == repo.id


@pytest.mark.anyio
async def test_pair_with_valid_verdict_skipped(session):
    """D25: валидный вердикт на текущую четвёрку — пара не пересчитывается (не мигаем)."""
    adef_prd, adef_dm, rubric, edge, repo, run = _seed_edge(session)
    snap_a = _snap(session, run, repo, adef_prd, "a" * 64)
    snap_b = _snap(session, run, repo, adef_dm, "b" * 64)
    store.register_verdict(
        session, edge_def_id=edge.id, source_snapshot_id=snap_a.id,
        target_snapshot_id=snap_b.id, source_content_hash="a" * 64,
        target_content_hash="b" * 64, rubric_id=rubric.id, llm_model=LLM_MODEL,
        verdict="ok", confidence="high",
    )
    session.flush()

    pairs, tasks = await reconcile_llm_pairs(
        session, llm_model=LLM_MODEL, verdict_worker=Recorder()
    )

    assert pairs == []
    assert tasks == []


@pytest.mark.anyio
async def test_deferred_verdict_retried_next_reconcile(session):
    """AC 3: deferred — не валидный вердикт, пара перепроверяется следующим сводом."""
    adef_prd, adef_dm, rubric, edge, repo, run = _seed_edge(session)
    snap_a = _snap(session, run, repo, adef_prd, "a" * 64)
    snap_b = _snap(session, run, repo, adef_dm, "b" * 64)
    store.register_verdict(
        session, edge_def_id=edge.id, source_snapshot_id=snap_a.id,
        target_snapshot_id=snap_b.id, source_content_hash="a" * 64,
        target_content_hash="b" * 64, rubric_id=rubric.id, llm_model=LLM_MODEL,
        verdict="deferred", deferred_reason="llm_unavailable", confidence="low",
    )
    session.flush()

    pairs, _tasks = await reconcile_llm_pairs(
        session, llm_model=LLM_MODEL, verdict_worker=Recorder()
    )

    assert len(pairs) == 1


@pytest.mark.anyio
async def test_reconcile_is_idempotent_recheck(session):
    """AC 3: потерянные задачи ретраятся сами — повторный свод снова ставит пару (без TaskTracker)."""
    adef_prd, adef_dm, rubric, edge, repo, run = _seed_edge(session)
    _snap(session, run, repo, adef_prd, "a" * 64)
    _snap(session, run, repo, adef_dm, "b" * 64)

    first, _ = await reconcile_llm_pairs(session, llm_model=LLM_MODEL, verdict_worker=None)
    second, _ = await reconcile_llm_pairs(session, llm_model=LLM_MODEL, verdict_worker=None)

    assert len(first) == len(second) == 1


@pytest.mark.anyio
async def test_missing_side_no_pair(session):
    """Пара требует текущих снапшотов обеих ролей."""
    adef_prd, adef_dm, rubric, edge, repo, run = _seed_edge(session)
    _snap(session, run, repo, adef_prd, "a" * 64)  # data_model не наблюдался

    pairs, _ = await reconcile_llm_pairs(session, llm_model=LLM_MODEL, verdict_worker=None)

    assert pairs == []


@pytest.mark.anyio
async def test_not_found_snapshot_no_pair(session):
    """not_found (без content_hash) не образует пару — нечего проверять."""
    adef_prd, adef_dm, rubric, edge, repo, run = _seed_edge(session)
    _snap(session, run, repo, adef_prd, "a" * 64)
    _snap(session, run, repo, adef_dm, None, status=SnapshotStatus.not_found)

    pairs, _ = await reconcile_llm_pairs(session, llm_model=LLM_MODEL, verdict_worker=None)

    assert pairs == []


@pytest.mark.anyio
async def test_without_worker_pairs_identified_no_tasks(session):
    """Ядро FR-5 не подключено (Фаза 0): пары видны, задач нет."""
    adef_prd, adef_dm, rubric, edge, repo, run = _seed_edge(session)
    _snap(session, run, repo, adef_prd, "a" * 64)
    _snap(session, run, repo, adef_dm, "b" * 64)

    pairs, tasks = await reconcile_llm_pairs(session, llm_model=LLM_MODEL, verdict_worker=None)

    assert len(pairs) == 1
    assert tasks == []


# ── AC 1: свод — в конце каждого обхода ────────────────────────────────────


class _EdgeFakeGit:
    async def fetch_default_branch(self, repo_url, git_host):
        return "main"

    async def get_tree(self, repo_url, git_host, ref="main"):
        return ["product/prd.md", "data-model.md"]

    async def get_file_content(self, repo_url, git_host, file_path, ref="main"):
        return f"содержимое {file_path}"

    async def get_head_sha(self, repo_url, git_host, ref="main"):
        return "e" * 40


@pytest.mark.anyio
async def test_run_sync_reconciles_at_end(session):
    adef_prd, adef_dm, rubric, edge, repo, run = _seed_edge(session)
    worker = Recorder()

    await run_sync(
        session, _EdgeFakeGit(), triggered_by=SyncTrigger.manual,
        llm_model=LLM_MODEL, verdict_worker=worker,
    )
    await asyncio.sleep(0)

    assert len(worker.pairs) == 1  # свод состоялся в конце обхода
