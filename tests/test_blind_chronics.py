"""#18 (v1.1): слепая зона (D2, FR-6) и хроники (D3, FR-7). ARCHITECTURE §5.3.

Маппинг исходов на UI (§5.3):
  repo_unavailable / archived → слепая зона;
  auth_failed → баннер «обнови токен», НЕ слепая зона (проблема наша, не студента);
  skipped_rate_limit → честное «не проверялось»;
  ok_unchanged 2+ занятия подряд → хроники («студенты в тишине», US-B4).
"""

from datetime import date, datetime

import pytest
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from alembic import command
from app import store
from app.models import GitHost, SnapshotStatus, SyncOutcome, SyncTrigger
from app.models.artifact_def import ArtifactDef
from app.models.lesson import Lesson
from app.services.matrix_builder import build_matrix

TODAY = date(2026, 7, 28)


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


def _seed_lessons(s):
    """Занятия 11 (21.07), 12 (23.07), 13 (28.07) — интервалы для хроник."""
    adefs = {}
    for number, day in [(11, 21), (12, 23), (13, 28)]:
        lesson = Lesson(number=number, title=f"Занятие {number}", date=date(2026, 7, day))
        s.add(lesson)
        s.flush()
        adef = ArtifactDef(lesson_id=lesson.id, role="code", expected_pattern=f"l{number}/**")
        s.add(adef)
        s.flush()
        adefs[number] = adef
    return adefs


def _repo(s, url):
    repo = store.register_repository(s, repo_url=url, git_host=GitHost.GitHub)
    s.flush()
    return repo


def _outcome(s, repo, outcome, detail=None):
    run = store.register_sync_run(s, triggered_by=SyncTrigger.schedule)
    s.flush()
    store.register_sync_outcome(
        s, sync_run_id=run.id, repository_id=repo.id, outcome=outcome, detail=detail
    )
    s.flush()
    return run


# ── Слепая зона (FR-6) ─────────────────────────────────────────────────────


def test_repo_unavailable_is_blind_spot(session):
    _seed_lessons(session)
    repo = _repo(session, "https://github.com/s/gone")
    _outcome(session, repo, SyncOutcome.repo_unavailable, detail="HTTP 404")

    matrix = build_matrix(session, today=TODAY)

    (spot,) = matrix["blind_spots"]
    assert spot["repo_url"] == "https://github.com/s/gone"
    assert "404" in spot["detail"]


def test_recovered_repo_leaves_blind_spot(session):
    """Слепая зона считается по ПОСЛЕДНЕМУ исходу — восстановившийся репо выходит из неё."""
    _seed_lessons(session)
    repo = _repo(session, "https://github.com/s/x")
    _outcome(session, repo, SyncOutcome.repo_unavailable)
    _outcome(session, repo, SyncOutcome.ok_changed)

    assert build_matrix(session, today=TODAY)["blind_spots"] == []


def test_archived_repo_is_blind_spot_without_sync(session):
    _seed_lessons(session)
    repo = _repo(session, "https://github.com/s/arch")
    repo.archived_at = datetime(2026, 7, 20)
    session.flush()

    matrix = build_matrix(session, today=TODAY)

    assert [s["repo_url"] for s in matrix["blind_spots"]] == ["https://github.com/s/arch"]


def test_auth_failed_is_banner_not_blind_spot(session):
    """§5.3: auth_failed — проблема нашего токена, не студента."""
    _seed_lessons(session)
    repo = _repo(session, "https://github.com/s/x")
    _outcome(session, repo, SyncOutcome.auth_failed)

    matrix = build_matrix(session, today=TODAY)

    assert matrix["blind_spots"] == []
    assert matrix["auth_banner"] is True


def test_rate_limited_marked_unchecked(session):
    _seed_lessons(session)
    repo = _repo(session, "https://github.com/s/x")
    _outcome(session, repo, SyncOutcome.skipped_rate_limit)

    matrix = build_matrix(session, today=TODAY)

    assert [u["repo_url"] for u in matrix["unchecked"]] == ["https://github.com/s/x"]
    assert matrix["blind_spots"] == []


# ── Хроники (FR-7): нет новых артефактов 2+ занятия ────────────────────────


def _snapshot(s, run, repo, adef, observed_at):
    store.register_snapshot(
        s, sync_run_id=run.id, repository_id=repo.id, artifact_def_id=adef.id,
        status=SnapshotStatus.found, content_hash="h" * 64, file_path="f.md",
        source_commit_sha="c" * 40, observed_at=observed_at,
    )
    s.flush()


def test_silent_repo_two_lessons_is_chronic(session):
    """Последнее изменение до занятия 11, прошло 2 занятия (11 и 12) → хроника."""
    adefs = _seed_lessons(session)
    repo = _repo(session, "https://github.com/s/silent")
    run = _outcome(session, repo, SyncOutcome.ok_changed)
    _snapshot(session, run, repo, adefs[11], datetime(2026, 7, 15))
    _outcome(session, repo, SyncOutcome.ok_unchanged)

    matrix = build_matrix(session, today=TODAY)

    (chronic,) = matrix["chronics"]
    assert chronic["repo_url"] == "https://github.com/s/silent"
    assert chronic["lessons_silent"] >= 2


def test_recent_change_not_chronic(session):
    """Изменение после предпоследнего занятия → не хроника."""
    adefs = _seed_lessons(session)
    repo = _repo(session, "https://github.com/s/active")
    run = _outcome(session, repo, SyncOutcome.ok_changed)
    _snapshot(session, run, repo, adefs[12], datetime(2026, 7, 24))

    assert build_matrix(session, today=TODAY)["chronics"] == []


def test_blind_spot_rendered_on_dashboard(tmp_path, monkeypatch):
    """UI: блок «Слепая зона» виден на матрице (FR-6)."""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.routes import get_session

    monkeypatch.setenv("CD_ADMIN_PASSWORD", "pw")  # до миграции — сид читает env
    db_path = tmp_path / "ui.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as s:
        _seed_lessons(s)
        repo = _repo(s, "https://github.com/s/gone")
        _outcome(s, repo, SyncOutcome.repo_unavailable, detail="HTTP 404")
        s.commit()

    def override_session():
        with Session(engine) as s:
            yield s
            s.commit()

    app.dependency_overrides[get_session] = override_session
    try:
        client = TestClient(app)
        client.post("/login", data={"username": "admin", "password": "pw"})
        html = client.get("/").text
    finally:
        app.dependency_overrides.clear()
        engine.dispose()

    assert "Слепая зона" in html
    assert "https://github.com/s/gone" in html


def test_blind_spot_not_doubled_in_chronics(session):
    """Недоступный репо — слепая зона, а не хроника (не двоим сигнал)."""
    _seed_lessons(session)
    repo = _repo(session, "https://github.com/s/gone")
    _outcome(session, repo, SyncOutcome.repo_unavailable)

    matrix = build_matrix(session, today=TODAY)

    assert matrix["chronics"] == []
    assert len(matrix["blind_spots"]) == 1
