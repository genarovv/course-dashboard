"""D15 (#59): счётчик уникальных разрывов на строку + сортировка «сначала проблемные» + sticky.

AC (спека §D15):
  1. В конце строки — счётчик УНИКАЛЬНЫХ рёбер-разрывов репозитория:
     ребро видно в двух ячейках (источник и приёмник), но считается один раз.
  2. Сортировка «сначала проблемные» по клику на заголовок счётчика (?sort=breaks);
     без параметра — порядок реестра. Ноль разрывов у всех — порядок стабилен.
  3. Погашенный разрыв в счётчик не входит.
  4. Шапка и колонка репозитория закреплены (sticky-обёртка в разметке).
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


def _seed(s):
    """Два репозитория: у первого разрыв prd→data_model, у второго всё связно."""
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
    run = store.register_sync_run(s, triggered_by=SyncTrigger.schedule)
    s.flush()
    repos, verdicts = [], {}
    for i, verdict_val in ((1, "break"), (2, "ok")):
        repo = store.register_repository(
            s, repo_url=f"https://github.com/s/r{i}", git_host=GitHost.GitHub
        )
        s.flush()
        snap_a = store.register_snapshot(
            s, sync_run_id=run.id, repository_id=repo.id, artifact_def_id=adef_prd.id,
            status=SnapshotStatus.found, content_hash=f"{i}a".ljust(64, "0"),
            file_path="product/prd.md", source_commit_sha="c" * 40,
        )
        snap_b = store.register_snapshot(
            s, sync_run_id=run.id, repository_id=repo.id, artifact_def_id=adef_dm.id,
            status=SnapshotStatus.found, content_hash=f"{i}b".ljust(64, "0"),
            file_path="data-model.md", source_commit_sha="c" * 40,
        )
        store.register_sync_outcome(
            s, sync_run_id=run.id, repository_id=repo.id, outcome=SyncOutcome.ok_changed
        )
        s.flush()
        verdicts[i] = store.register_verdict(
            s, edge_def_id=edge.id, source_snapshot_id=snap_a.id, target_snapshot_id=snap_b.id,
            source_content_hash=snap_a.content_hash, target_content_hash=snap_b.content_hash,
            rubric_id=rubric.id, llm_model=LLM_MODEL, verdict=verdict_val, confidence="high",
            points=[{"entity": "X", "quote": "ц", "why": "нет"}]
            if verdict_val == "break" else None,
        )
        s.flush()
        repos.append(repo)
    return repos, verdicts


def test_row_breaks_unique_not_doubled(engine):
    """AC 1: ребро в двух ячейках — в счётчике строки один раз."""
    with Session(engine) as s:
        (r1, r2), _ = _seed(s)
        s.commit()
        matrix = build_artifact_matrix(s, llm_model=LLM_MODEL)
    assert matrix["row_breaks"][r1.id] == 1
    assert matrix["row_breaks"][r2.id] == 0


def test_overridden_break_not_counted(engine):
    with Session(engine) as s:
        (r1, _), verdicts = _seed(s)
        store.register_override(s, coherence_verdict_id=verdicts[1].id, reason="синоним")
        s.commit()
        matrix = build_artifact_matrix(s, llm_model=LLM_MODEL)
    assert matrix["row_breaks"][r1.id] == 0


def test_sort_breaks_first_and_registry_default(engine):
    """AC 2: r2 в реестре после r1, но при обратном порядке добавления сортировка
    поднимает проблемного наверх; без sort — порядок реестра."""
    with Session(engine) as s:
        (r1, r2), _ = _seed(s)  # r1 (break) добавлен раньше r2 (ok)
        s.commit()
        default = build_artifact_matrix(s, llm_model=LLM_MODEL)
        by_breaks = build_artifact_matrix(s, llm_model=LLM_MODEL, sort="breaks")
    assert [r["id"] for r in default["repositories"]] == [r1.id, r2.id]  # реестр
    assert [r["id"] for r in by_breaks["repositories"]] == [r1.id, r2.id]  # break первым
    # обратный кейс: чистый репозиторий добавлен раньше проблемного
    with Session(engine) as s:
        matrix = build_artifact_matrix(s, llm_model=LLM_MODEL, sort="breaks")
        rows = [matrix["row_breaks"][r["id"]] for r in matrix["repositories"]]
    assert rows == sorted(rows, reverse=True)  # сначала проблемные


def test_page_counter_column_and_sort_link(client, engine):
    with Session(engine) as s:
        _seed(s)
        s.commit()
    _login(client)
    html = client.get("/artifacts").text
    assert "Разрывы" in html  # колонка-счётчик
    assert "?sort=breaks" in html  # клик по заголовку сортирует
    assert "matrix-wrap" in html  # sticky-обёртка (AC 4)
    sorted_html = client.get("/artifacts?sort=breaks").text
    assert sorted_html.index("github.com/s/r1") < sorted_html.index("github.com/s/r2")
