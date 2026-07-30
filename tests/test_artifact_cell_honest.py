"""D11 (#55), FR-5/FR-10/BR-2: честный свод ячейки артефактной матрицы.

AC (спека §D11):
  1. «Частично» + активный разрыв — свод называет и причину, и разрыв.
  2. Погашенный разрыв — «помечен ложным», не «связность ок».
  3. Активный разрыв приоритетнее погашенного.
  4. Приоритет свода: разрыв > проверяется > помечен ложным > ок > статус.
Негативный (регресс FR-10): новая четвёрка не наследует старую отметку.
"""

from datetime import datetime

import pytest
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from alembic import command
from app import store
from app.models import GitHost, SnapshotStatus, SyncOutcome, SyncTrigger
from app.models.artifact_def import ArtifactDef
from app.models.lesson import Lesson
from app.services.artifact_matrix import build_artifact_matrix

LLM_MODEL = "deepseek-v4-flash"


@pytest.fixture()
def engine(tmp_path):
    db_path = tmp_path / "test.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    yield engine
    engine.dispose()


def _seed(s, *, prd_status=SnapshotStatus.found, prd_reason=None):
    """Ребро prd→data_model; снапшоты по параметрам; вердикт создаёт вызывающий."""
    lesson5 = Lesson(number=5, title="PRD", date=datetime(2026, 6, 30).date())
    lesson6 = Lesson(number=6, title="Данные", date=datetime(2026, 7, 2).date())
    s.add_all([lesson5, lesson6])
    s.flush()
    adef_prd = ArtifactDef(lesson_id=lesson5.id, role="prd", expected_pattern="product/prd.md")
    adef_dm = ArtifactDef(lesson_id=lesson6.id, role="data_model", expected_pattern="data-model.md")
    s.add_all([adef_prd, adef_dm])
    rubric = store.register_rubric(s, type="edge", version="1.0", text="правило")
    s.flush()
    edge = store.config_create_edge_def(
        s, source_role="prd", target_role="data_model", rubric_id=rubric.id
    )
    repo = store.register_repository(s, repo_url="https://github.com/s/x", git_host=GitHost.GitHub)
    run = store.register_sync_run(s, triggered_by=SyncTrigger.schedule)
    s.flush()
    prd_kwargs = {"partial_reason": prd_reason} if prd_reason else {}
    snap_prd = store.register_snapshot(
        s, sync_run_id=run.id, repository_id=repo.id, artifact_def_id=adef_prd.id,
        status=prd_status, content_hash="a" * 64,
        file_path="product/prd.md", source_commit_sha="c" * 40, **prd_kwargs,
    )
    snap_dm = store.register_snapshot(
        s, sync_run_id=run.id, repository_id=repo.id, artifact_def_id=adef_dm.id,
        status=SnapshotStatus.found, content_hash="b" * 64, file_path="data-model.md",
        source_commit_sha="c" * 40,
    )
    store.register_sync_outcome(
        s, sync_run_id=run.id, repository_id=repo.id, outcome=SyncOutcome.ok_changed
    )
    s.flush()
    return repo, run, edge, rubric, adef_prd, snap_prd, snap_dm


def _verdict(s, edge, rubric, snap_a, snap_b, *, verdict="break", entity="Оксана"):
    v = store.register_verdict(
        s, edge_def_id=edge.id, source_snapshot_id=snap_a.id, target_snapshot_id=snap_b.id,
        source_content_hash=snap_a.content_hash, target_content_hash=snap_b.content_hash,
        rubric_id=rubric.id, llm_model=LLM_MODEL, verdict=verdict, confidence="high",
        points=[{"entity": entity, "quote": "цитата", "why": "не найдена"}]
        if verdict == "break" else None,
    )
    s.flush()
    return v


def _cell(s, repo, role="prd"):
    return build_artifact_matrix(s, llm_model=LLM_MODEL)["cells"][repo.id][role]


# ── AC 1: «частично» + разрыв — свод называет оба ─────────────────────────


def test_partial_cell_names_break(engine):
    with Session(engine) as s:
        repo, run, edge, rubric, adef, snap_prd, snap_dm = _seed(
            s, prd_status=SnapshotStatus.partial, prd_reason=["template_copy"]
        )
        _verdict(s, edge, rubric, snap_prd, snap_dm)
        s.commit()
        cell = _cell(s, repo)
    assert "заготовка из шаблона" in cell["summary"]
    assert "разрыв" in cell["summary"] and "Оксана" in cell["summary"]
    assert cell["break_count"] == 1


# ── AC 2: override — «помечен ложным», не «связность ок» ──────────────────


def test_override_labelled_not_ok(engine):
    with Session(engine) as s:
        repo, run, edge, rubric, adef, snap_prd, snap_dm = _seed(s)
        v = _verdict(s, edge, rubric, snap_prd, snap_dm)
        store.register_override(s, coherence_verdict_id=v.id, reason="синоним")
        s.commit()
        cell = _cell(s, repo)
    assert "помечен ложным" in cell["summary"]
    assert "связность ок" not in cell["summary"]
    assert cell["break_count"] == 0


# ── AC 3: активный разрыв приоритетнее погашенного ────────────────────────


def test_active_break_beats_overridden(engine):
    with Session(engine) as s:
        repo, run, edge, rubric, adef_prd, snap_prd, snap_dm = _seed(s)
        old = _verdict(s, edge, rubric, snap_prd, snap_dm, entity="старая")
        store.register_override(s, coherence_verdict_id=old.id, reason="ложный")
        # студент изменил PRD → новый снапшот и новый активный break (новая четвёрка)
        run2 = store.register_sync_run(s, triggered_by=SyncTrigger.schedule)
        s.flush()
        snap_prd2 = store.register_snapshot(
            s, sync_run_id=run2.id, repository_id=repo.id, artifact_def_id=adef_prd.id,
            status=SnapshotStatus.found, content_hash="d" * 64,
            file_path="product/prd.md", source_commit_sha="e" * 40,
        )
        s.flush()
        _verdict(s, edge, rubric, snap_prd2, snap_dm, entity="новая")
        s.commit()
        cell = _cell(s, repo)
    # регресс FR-10: новая четвёрка не наследует отметку — разрыв активен
    assert cell["break_count"] == 1
    assert "разрыв" in cell["summary"] and "новая" in cell["summary"]
    assert "помечен ложным" not in cell["summary"]


# ── AC 4: «проверяется» приоритетнее «помечен ложным» ─────────────────────


def test_pending_beats_overridden(engine):
    with Session(engine) as s:
        repo, run, edge, rubric, adef_prd, snap_prd, snap_dm = _seed(s)
        v = _verdict(s, edge, rubric, snap_prd, snap_dm)
        store.register_override(s, coherence_verdict_id=v.id, reason="ложный")
        # второе ребро prd→architecture без вердикта → pending
        lesson7 = Lesson(number=7, title="Архитектура", date=datetime(2026, 7, 7).date())
        s.add(lesson7)
        s.flush()
        adef_arch = ArtifactDef(
            lesson_id=lesson7.id, role="architecture", expected_pattern="ARCHITECTURE.md"
        )
        s.add(adef_arch)
        rubric2 = store.register_rubric(s, type="edge", version="1.0", text="правило2")
        s.flush()
        store.config_create_edge_def(
            s, source_role="prd", target_role="architecture", rubric_id=rubric2.id
        )
        store.register_snapshot(
            s, sync_run_id=run.id, repository_id=repo.id, artifact_def_id=adef_arch.id,
            status=SnapshotStatus.found, content_hash="f" * 64,
            file_path="ARCHITECTURE.md", source_commit_sha="c" * 40,
        )
        s.commit()
        cell = _cell(s, repo)
    assert "проверяется" in cell["summary"]
    assert "помечен ложным" not in cell["summary"]
