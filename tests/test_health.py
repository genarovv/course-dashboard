"""I2 (#13), FR-8: GET /health — счётчики из БД, без in-memory состояния (ARCHITECTURE §5.4).

AC тикета #13:
  1. GET /health возвращает время последнего обхода
  2. Количество пар без вердикта
  3. Количество deferred по причинам
(cron 2×/сут — носитель ОС, §5.5: crontab-строка задокументирована в README, не в коде.)
"""

from datetime import datetime

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from alembic import command
from app import store
from app.main import app
from app.models import GitHost, SnapshotStatus, SyncStatus, SyncTrigger
from app.models.artifact_def import ArtifactDef
from app.models.lesson import Lesson
from app.routes import get_session

LLM_MODEL = "deepseek-v4-flash"


@pytest.fixture()
def client_env(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")

    def override_session():
        with Session(engine) as s:
            yield s
            s.commit()

    app.dependency_overrides[get_session] = override_session
    yield TestClient(app), engine
    app.dependency_overrides.clear()
    engine.dispose()


def _seed_pair(s, *, with_deferred=False):
    """Ребро prd→data_model + репозиторий со снапшотами обеих ролей (= 1 пара без вердикта)."""
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
    snap_a = store.register_snapshot(
        s, sync_run_id=run.id, repository_id=repo.id, artifact_def_id=adef_prd.id,
        status=SnapshotStatus.found, content_hash="a" * 64, file_path="product/prd.md",
        source_commit_sha="c" * 40,
    )
    snap_b = store.register_snapshot(
        s, sync_run_id=run.id, repository_id=repo.id, artifact_def_id=adef_dm.id,
        status=SnapshotStatus.found, content_hash="b" * 64, file_path="data-model.md",
        source_commit_sha="c" * 40,
    )
    store.update_sync_run_status(s, run.id, SyncStatus.completed)
    s.flush()
    if with_deferred:
        store.register_verdict(
            s, edge_def_id=edge.id, source_snapshot_id=snap_a.id, target_snapshot_id=snap_b.id,
            source_content_hash="a" * 64, target_content_hash="b" * 64, rubric_id=rubric.id,
            llm_model=LLM_MODEL, verdict="deferred", deferred_reason="llm_unavailable",
            confidence="low",
        )
        s.flush()
    return run


def test_health_open_without_auth(client_env):
    client, _ = client_env
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_health_empty_db_zeros(client_env):
    client, _ = client_env
    body = client.get("/health").json()
    assert body["last_sync"] is None
    assert body["pairs_without_verdict"] == 0
    assert body["deferred"] == {"llm_unavailable": 0, "parse_error": 0}


def test_health_reports_last_sync_and_pairs(client_env):
    client, engine = client_env
    with Session(engine) as s:
        _seed_pair(s)
        s.commit()

    body = client.get("/health").json()
    assert body["last_sync"]["status"] == "completed"
    assert body["last_sync"]["started_at"]  # AC 1: время последнего обхода
    assert body["pairs_without_verdict"] == 1  # AC 2


def test_health_deferred_counts_by_reason(client_env):
    client, engine = client_env
    with Session(engine) as s:
        _seed_pair(s, with_deferred=True)
        s.commit()

    body = client.get("/health").json()
    assert body["pairs_without_verdict"] == 1  # deferred — не валидный вердикт
    assert body["deferred"]["llm_unavailable"] == 1  # AC 3
    assert body["deferred"]["parse_error"] == 0
