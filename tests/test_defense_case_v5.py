"""D36 (итерация 5): дело защиты — шапка-резюме, «погашенных: 0», прямой вход /defense.

Спека: plans/доводки-ux-5-2026-07-31.md §D36; решения CEO №3 (явный ноль погашенных —
отмена негативного AC D19) и №4 (нейтральный вход без общей матрицы на проекторе).
Негативные: нет MR и снапшотов — резюме с прочерками; пустой реестр — «реестр пуст».
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
from app.services.evidence_chain import build_defense_card

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


def _seed(s):
    """2 роли (обе found), разрыв high, 2 MR (merged без ревью + opened)."""
    lesson5 = Lesson(number=5, title="PRD", date=datetime(2026, 6, 30).date())
    lesson6 = Lesson(number=6, title="Данные", date=datetime(2026, 7, 2).date())
    s.add_all([lesson5, lesson6])
    s.flush()
    a1 = ArtifactDef(lesson_id=lesson5.id, role="prd", expected_pattern="product/prd.md")
    a2 = ArtifactDef(lesson_id=lesson6.id, role="data_model", expected_pattern="data-model.md")
    s.add_all([a1, a2])
    rubric = store.register_rubric(s, type="edge", version="1.0", text="п")
    s.flush()
    edge = store.config_create_edge_def(
        s, source_role="prd", target_role="data_model", rubric_id=rubric.id
    )
    repo = store.register_repository(s, repo_url="https://github.com/s/x", git_host=GitHost.GitHub)
    run = store.register_sync_run(s, triggered_by=SyncTrigger.schedule)
    s.flush()
    snap_a = store.register_snapshot(
        s, sync_run_id=run.id, repository_id=repo.id, artifact_def_id=a1.id,
        status=SnapshotStatus.found, content_hash="a" * 64,
        file_path="product/prd.md", source_commit_sha="c" * 40,
    )
    snap_b = store.register_snapshot(
        s, sync_run_id=run.id, repository_id=repo.id, artifact_def_id=a2.id,
        status=SnapshotStatus.found, content_hash="b" * 64,
        file_path="data-model.md", source_commit_sha="c" * 40,
    )
    store.register_sync_outcome(
        s, sync_run_id=run.id, repository_id=repo.id, outcome=SyncOutcome.ok_changed
    )
    s.flush()
    store.register_verdict(
        s, edge_def_id=edge.id, source_snapshot_id=snap_a.id, target_snapshot_id=snap_b.id,
        source_content_hash=snap_a.content_hash, target_content_hash=snap_b.content_hash,
        rubric_id=rubric.id, llm_model=LLM_MODEL, verdict="break", confidence="high",
        points=[{"entity": "X", "quote": "ц", "why": "нет"}], notes="Потеряна X.",
    )
    for num, state, approved, when in (
        (1, "merged", False, datetime(2026, 7, 19, 10, 0)),
        (2, "opened", False, datetime(2026, 7, 30, 12, 0)),
    ):
        store.register_mr_observation(
            s, sync_run_id=run.id, repository_id=repo.id, mr_number=num, title="t",
            source_branch=f"feat/{num}", state=state, reviewer_approved=approved,
            markers=None, head_sha=str(num) * 40, updated_at=when,
        )
    s.flush()
    return repo


def test_case_summary_values(engine):
    with Session(engine) as s:
        repo = _seed(s)
        s.commit()
        card = build_defense_card(s, repo.id, llm_model=LLM_MODEL)
    summary = card["case_summary"]
    assert summary["submitted"] == {"x": 2, "m": 2}
    assert summary["breaks"] == 1
    assert summary["mrs_total"] == 2 and summary["no_review"] == 1
    # период работы — по независимым датам хостинга
    assert summary["work_from"] == datetime(2026, 7, 19, 10, 0)
    assert summary["work_to"] == datetime(2026, 7, 30, 12, 0)


def test_case_summary_rendered(client, engine):
    with Session(engine) as s:
        repo = _seed(s)
        s.commit()
        repo_id = repo.id
    _login(client)
    html = client.get(f"/students/{repo_id}/defense").text
    assert "сдано 2/2" in html
    assert "мимо ревью: 1" in html
    assert "работа 19.07 — 30.07" in html


def test_zero_muted_breaks_explicit(client, engine):
    """tests-change D36 (решение CEO №3, 2026-07-31): пустая секция погашенных
    больше не скрывается — явный ноль легитимизирует процесс курирования."""
    with Session(engine) as s:
        repo = _seed(s)
        s.commit()
        repo_id = repo.id
    _login(client)
    assert "погашенных разрывов: 0" in client.get(f"/students/{repo_id}/defense").text


def test_defense_index_neutral(client, engine):
    with Session(engine) as s:
        _seed(s)
        s.commit()
    _login(client)
    html = client.get("/defense").text
    assert "Защита проектов" in html and 'href="/students/' in html
    assert "x" in html  # короткое имя репо
    assert "2/2" not in html and "⚠" not in html  # нейтрально: без чужих результатов
    assert "мимо ревью" not in html


def test_defense_index_empty_and_auth(client, engine):
    assert client.get("/defense", follow_redirects=False).status_code == 303
    _login(client)
    assert "реестр пуст" in client.get("/defense").text.lower()


def test_matrix_links_defense_index(client, engine):
    with Session(engine) as s:
        _seed(s)
        s.commit()
    _login(client)
    assert 'href="/defense"' in client.get("/artifacts").text
