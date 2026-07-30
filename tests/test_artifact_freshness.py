"""D16 (#60): метка свежести «новое с прошлого обхода».

AC (спека §D16):
  1. Чип разрыва, чей вердикт впервые вычислен в последнем обходе, помечен «новое».
  2. Перепрогон той же четвёрки не «мигает» (D25) — computed_at старый, метки нет.
  3. Изменение артефакта в последнем обходе — точка-метка свежести на ячейке.
  4. Первый обход с нуля — меток нет (иначе весь экран «новый»).
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
from app.models import GitHost, SnapshotStatus, SyncOutcome, SyncTrigger
from app.models.artifact_def import ArtifactDef
from app.models.lesson import Lesson
from app.routes import get_session
from app.services.artifact_matrix import build_artifact_matrix

LLM_MODEL = "deepseek-v4-flash"
PASSWORD = "correct-horse"


@pytest.fixture()
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("CD_ADMIN_PASSWORD", PASSWORD)
    db_path = tmp_path / "test.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    yield engine
    engine.dispose()


@pytest.fixture()
def client(engine):
    def override_session():
        with Session(engine) as s:
            yield s
            s.commit()

    app.dependency_overrides[get_session] = override_session
    yield TestClient(app)
    app.dependency_overrides.clear()


def _login(client):
    client.post("/login", data={"username": "admin", "password": PASSWORD})


T_RUN1 = datetime(2026, 7, 20, 10, 0)
T_RUN2 = datetime(2026, 7, 29, 10, 0)


def _seed(s, *, runs=2):
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
    run1 = store.register_sync_run(s, triggered_by=SyncTrigger.schedule)
    run1.started_at = T_RUN1
    s.flush()
    snaps = {}
    for key, adef, h in (("prd", adef_prd, "a"), ("dm", adef_dm, "b")):
        snaps[key] = store.register_snapshot(
            s, sync_run_id=run1.id, repository_id=repo.id, artifact_def_id=adef.id,
            status=SnapshotStatus.found, content_hash=h * 64,
            file_path=adef.expected_pattern, source_commit_sha="c" * 40,
        )
        snaps[key].observed_at = T_RUN1
    store.register_sync_outcome(
        s, sync_run_id=run1.id, repository_id=repo.id, outcome=SyncOutcome.ok_changed
    )
    run2 = None
    if runs >= 2:
        run2 = store.register_sync_run(s, triggered_by=SyncTrigger.schedule)
        run2.started_at = T_RUN2
        s.flush()
        store.register_sync_outcome(
            s, sync_run_id=run2.id, repository_id=repo.id, outcome=SyncOutcome.ok_unchanged
        )
    s.flush()
    return repo, edge, rubric, snaps, run1, run2


def _verdict(s, edge, rubric, snap_a, snap_b, *, computed_at):
    v = store.register_verdict(
        s, edge_def_id=edge.id, source_snapshot_id=snap_a.id, target_snapshot_id=snap_b.id,
        source_content_hash=snap_a.content_hash, target_content_hash=snap_b.content_hash,
        rubric_id=rubric.id, llm_model=LLM_MODEL, verdict="break", confidence="high",
        points=[{"entity": "X", "quote": "ц", "why": "нет"}],
    )
    v.computed_at = computed_at
    s.flush()
    return v


def test_break_computed_in_last_run_marked_new(engine):
    with Session(engine) as s:
        repo, edge, rubric, snaps, _, _ = _seed(s)
        _verdict(s, edge, rubric, snaps["prd"], snaps["dm"], computed_at=datetime(2026, 7, 29, 10, 5))
        s.commit()
        cell = build_artifact_matrix(s, llm_model=LLM_MODEL)["cells"][repo.id]["prd"]
    assert cell["break"]["new"] is True


def test_old_verdict_not_marked_new(engine):
    """AC 2/D25: та же четвёрка не пересчитывается — computed_at из прошлого обхода."""
    with Session(engine) as s:
        repo, edge, rubric, snaps, _, _ = _seed(s)
        _verdict(s, edge, rubric, snaps["prd"], snaps["dm"], computed_at=datetime(2026, 7, 20, 10, 5))
        s.commit()
        cell = build_artifact_matrix(s, llm_model=LLM_MODEL)["cells"][repo.id]["prd"]
    assert cell["break"]["new"] is False


def test_first_run_no_marks(engine):
    """AC 4: единственный обход — ни «новое», ни точки свежести."""
    with Session(engine) as s:
        repo, edge, rubric, snaps, _, _ = _seed(s, runs=1)
        _verdict(s, edge, rubric, snaps["prd"], snaps["dm"], computed_at=datetime(2026, 7, 20, 10, 5))
        s.commit()
        cell = build_artifact_matrix(s, llm_model=LLM_MODEL)["cells"][repo.id]["prd"]
    assert cell["break"]["new"] is False
    assert cell["fresh"] is False


def test_snapshot_in_last_run_fresh_dot(engine):
    """AC 3: артефакт изменился в последнем обходе — точка свежести."""
    with Session(engine) as s:
        repo, edge, rubric, snaps, _, run2 = _seed(s)
        fresh_snap = store.register_snapshot(
            s, sync_run_id=run2.id, repository_id=repo.id,
            artifact_def_id=snaps["prd"].artifact_def_id,
            status=SnapshotStatus.found, content_hash="d" * 64,
            file_path="product/prd.md", source_commit_sha="e" * 40,
        )
        fresh_snap.observed_at = datetime(2026, 7, 29, 10, 3)
        s.commit()
        cells = build_artifact_matrix(s, llm_model=LLM_MODEL)["cells"][repo.id]
    assert cells["prd"]["fresh"] is True
    assert cells["data_model"]["fresh"] is False  # наблюдался только в прошлом обходе


def test_page_renders_new_badge(client, engine):
    with Session(engine) as s:
        repo, edge, rubric, snaps, _, _ = _seed(s)
        _verdict(s, edge, rubric, snaps["prd"], snaps["dm"], computed_at=datetime(2026, 7, 29, 10, 5))
        s.commit()
    _login(client)
    html = client.get("/artifacts").text
    assert "новое" in html
