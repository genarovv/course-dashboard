"""D4 min (#14), FR-9: карточка студента — хронология + разрывы по 2 рёбрам.

AC тикета #14:
  1. По студенту — хронология артефактов с разрывами по 2 рёбрам
     (prd→data_model, data_model→architecture)
  2. Каждый разрыв: вердикт, уверенность, ≤5 точек с цитатами
  3. Хронология по observed_at, не по git-истории (D27: force-push неуязвимость)
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
from app.models import GitHost, SnapshotStatus, SyncTrigger
from app.models.artifact_def import ArtifactDef
from app.models.lesson import Lesson
from app.routes import get_session
from app.services.evidence_chain import build_student_card

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


def _seed(s):
    """3 занятия (prd, data_model, architecture), 2 ребра, репозиторий, обход."""
    lessons, adefs = {}, {}
    for number, role, pattern in [
        (5, "prd", "product/prd.md"),
        (6, "data_model", "data-model.md"),
        (7, "architecture", "ARCHITECTURE.md"),
    ]:
        lesson = Lesson(number=number, title=role, date=datetime(2026, 6, 20 + number).date())
        s.add(lesson)
        s.flush()
        adef = ArtifactDef(lesson_id=lesson.id, role=role, expected_pattern=pattern)
        s.add(adef)
        s.flush()
        lessons[role], adefs[role] = lesson, adef
    rubric = store.register_rubric(s, type="edge", version="1.0", text="правило")
    s.flush()
    edge1 = store.config_create_edge_def(
        s, source_role="prd", target_role="data_model", rubric_id=rubric.id
    )
    edge2 = store.config_create_edge_def(
        s, source_role="data_model", target_role="architecture", rubric_id=rubric.id
    )
    repo = store.register_repository(s, repo_url="https://github.com/s/x", git_host=GitHost.GitHub)
    run = store.register_sync_run(s, triggered_by=SyncTrigger.schedule)
    s.flush()
    return lessons, adefs, rubric, edge1, edge2, repo, run


def _snap(s, run, repo, adef, content_hash, observed_at, sha="c" * 40):
    snap = store.register_snapshot(
        s, sync_run_id=run.id, repository_id=repo.id, artifact_def_id=adef.id,
        status=SnapshotStatus.found, content_hash=content_hash,
        file_path=adef.expected_pattern, source_commit_sha=sha, observed_at=observed_at,
    )
    s.flush()
    return snap


# ── AC 3: хронология по observed_at ────────────────────────────────────────


def test_timeline_ordered_by_observed_at(session):
    lessons, adefs, rubric, e1, e2, repo, run = _seed(session)
    _snap(session, run, repo, adefs["data_model"], "b" * 64, datetime(2026, 7, 3, 12, 0))
    _snap(session, run, repo, adefs["prd"], "a" * 64, datetime(2026, 7, 1, 12, 0))
    # force-push: артефакт архитектуры наблюдён позже всех — идёт последним,
    # даже если его commit «старее» в git-истории
    _snap(session, run, repo, adefs["architecture"], "d" * 64, datetime(2026, 7, 10, 9, 0),
          sha="0" * 40)

    card = build_student_card(session, repo.id)

    roles = [entry["role"] for entry in card["timeline"]]
    assert roles == ["prd", "data_model", "architecture"]
    observed = [entry["observed_at"] for entry in card["timeline"]]
    assert observed == sorted(observed)


# ── AC 1/2: разрывы по 2 рёбрам, вердикт + уверенность + ≤5 точек ──────────


def test_edges_show_verdict_confidence_and_5_points_max(session):
    lessons, adefs, rubric, e1, e2, repo, run = _seed(session)
    snap_prd = _snap(session, run, repo, adefs["prd"], "a" * 64, datetime(2026, 7, 1))
    snap_dm = _snap(session, run, repo, adefs["data_model"], "b" * 64, datetime(2026, 7, 2))
    points = [
        {"entity": f"сущность {i}", "quote": f"цитата {i}", "why": f"не найдено {i}"}
        for i in range(7)
    ]
    store.register_verdict(
        session, edge_def_id=e1.id, source_snapshot_id=snap_prd.id,
        target_snapshot_id=snap_dm.id, source_content_hash="a" * 64,
        target_content_hash="b" * 64, rubric_id=rubric.id, llm_model=LLM_MODEL,
        verdict="break", confidence="medium", points=points,
    )
    session.flush()

    card = build_student_card(session, repo.id, llm_model=LLM_MODEL)

    assert len(card["edges"]) == 2  # оба ребра конвейера присутствуют
    edge_card = next(e for e in card["edges"] if e["source_role"] == "prd")
    assert edge_card["verdict"] == "break"
    assert edge_card["confidence"] == "medium"
    assert len(edge_card["points"]) == 5  # ≤5 точек (AC 2)
    assert edge_card["points"][0]["quote"] == "цитата 0"


def test_edge_without_verdict_is_pending(session):
    """Пара есть, вердикта нет → вычислимое «проверяется» (В13), не разрыв."""
    lessons, adefs, rubric, e1, e2, repo, run = _seed(session)
    _snap(session, run, repo, adefs["prd"], "a" * 64, datetime(2026, 7, 1))
    _snap(session, run, repo, adefs["data_model"], "b" * 64, datetime(2026, 7, 2))

    card = build_student_card(session, repo.id, llm_model=LLM_MODEL)

    edge_card = next(e for e in card["edges"] if e["source_role"] == "prd")
    assert edge_card["verdict"] is None
    assert edge_card["state"] == "pending"


def test_edge_without_snapshots_is_missing(session):
    """Нет снапшотов сторон → ребро «нет данных», не «проверяется»."""
    lessons, adefs, rubric, e1, e2, repo, run = _seed(session)

    card = build_student_card(session, repo.id, llm_model=LLM_MODEL)

    assert all(e["state"] == "no_data" for e in card["edges"])


def test_overridden_break_flagged(session):
    """FR-10: активная отметка «ложный разрыв» видна в карточке (для O2)."""
    lessons, adefs, rubric, e1, e2, repo, run = _seed(session)
    snap_prd = _snap(session, run, repo, adefs["prd"], "a" * 64, datetime(2026, 7, 1))
    snap_dm = _snap(session, run, repo, adefs["data_model"], "b" * 64, datetime(2026, 7, 2))
    verdict = store.register_verdict(
        session, edge_def_id=e1.id, source_snapshot_id=snap_prd.id,
        target_snapshot_id=snap_dm.id, source_content_hash="a" * 64,
        target_content_hash="b" * 64, rubric_id=rubric.id, llm_model=LLM_MODEL,
        verdict="break", confidence="high",
    )
    session.flush()
    store.register_override(session, coherence_verdict_id=verdict.id, reason="ложная сработка")
    session.flush()

    card = build_student_card(session, repo.id, llm_model=LLM_MODEL)

    edge_card = next(e for e in card["edges"] if e["source_role"] == "prd")
    assert edge_card["verdict"] == "break"
    assert edge_card["override_active"] is True


# ── Роут GET /students/{repository_id} ─────────────────────────────────────


@pytest.fixture()
def client_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CD_ADMIN_PASSWORD", "pw")
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


def test_student_card_route_requires_auth(client_env):
    client, _ = client_env
    assert client.get("/students/some-id", follow_redirects=False).status_code == 303


def test_student_card_route_renders(client_env):
    client, engine = client_env
    with Session(engine) as s:
        lessons, adefs, rubric, e1, e2, repo, run = _seed(s)
        snap_prd = _snap(s, run, repo, adefs["prd"], "a" * 64, datetime(2026, 7, 1))
        snap_dm = _snap(s, run, repo, adefs["data_model"], "b" * 64, datetime(2026, 7, 2))
        store.register_verdict(
            s, edge_def_id=e1.id, source_snapshot_id=snap_prd.id,
            target_snapshot_id=snap_dm.id, source_content_hash="a" * 64,
            target_content_hash="b" * 64, rubric_id=rubric.id, llm_model=LLM_MODEL,
            verdict="break", confidence="high",
            points=[{"entity": "Оксана", "quote": "персона Оксана потеряна", "why": "нет в B"}],
        )
        s.commit()
        repo_id = repo.id

    client.post("/login", data={"username": "admin", "password": "pw"})
    response = client.get(f"/students/{repo_id}")
    assert response.status_code == 200
    assert "https://github.com/s/x" in response.text
    assert "персона Оксана потеряна" in response.text  # цитата точки видна


def test_student_card_unknown_repo_404(client_env):
    client, _ = client_env
    client.post("/login", data={"username": "admin", "password": "pw"})
    assert client.get("/students/no-such-id").status_code == 404
