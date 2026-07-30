"""D7: матрица «репозиторий × артефакт» по макету CEO (maket 1, 2026-07-30).

AC (из макета и разбора с CEO):
  1. GET /artifacts — таблица: строки — репозитории, колонки — роли артефактов
     из конфига в порядке появления в курсе.
  2. Ячейка цветокодирована статусом (found/partial/not_found/пусто) и содержит
     усечённый результат анализа: наличие + причины «частично» + свод вердиктов
     рёбер FR-5, где роль участвует (разрыв → первая потерянная сущность).
  3. Клик по ячейке (HTMX) открывает модальное окно: найденные файлы, связи
     с другими артефактами (рёбра + точки с цитатами), заметки преподавателю,
     кнопка «ложный разрыв» (FR-10).
  4. Отметка «ложный разрыв» гасит разрыв в своде ячейки (как в FR-10).
  5. Страница и модалка закрыты логином (BR-4 teacher-only).
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
from app.services.artifact_matrix import build_artifact_matrix, build_cell_details

LLM_MODEL = "deepseek-v4-flash"
PASSWORD = "correct-horse"


@pytest.fixture()
def engine(tmp_path):
    db_path = tmp_path / "test.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    yield engine
    engine.dispose()


@pytest.fixture()
def client(engine, monkeypatch):
    monkeypatch.setenv("CD_ADMIN_PASSWORD", PASSWORD)

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
    """Занятия 2 (jtbd), 5 (prd — два альтернативных пути), 6 (data_model);
    ребро prd→data_model с break-вердиктом (точка «Оксана», заметка)."""
    lesson2 = Lesson(number=2, title="JTBD", date=datetime(2026, 6, 10).date())
    lesson5 = Lesson(number=5, title="PRD", date=datetime(2026, 6, 30).date())
    lesson6 = Lesson(number=6, title="Данные", date=datetime(2026, 7, 2).date())
    s.add_all([lesson2, lesson5, lesson6])
    s.flush()
    adef_jtbd = ArtifactDef(lesson_id=lesson2.id, role="jtbd", expected_pattern="product/jtbd.md")
    adef_prd = ArtifactDef(lesson_id=lesson5.id, role="prd", expected_pattern="product/prd.md")
    adef_prd_alt = ArtifactDef(lesson_id=lesson5.id, role="prd", expected_pattern="REQUIREMENTS.md")
    adef_dm = ArtifactDef(lesson_id=lesson6.id, role="data_model", expected_pattern="data-model.md")
    s.add_all([adef_jtbd, adef_prd, adef_prd_alt, adef_dm])
    rubric = store.register_rubric(s, type="edge", version="1.0", text="правило")
    s.flush()
    edge = store.config_create_edge_def(
        s, source_role="prd", target_role="data_model", rubric_id=rubric.id
    )
    repo = store.register_repository(s, repo_url="https://github.com/s/x", git_host=GitHost.GitHub)
    run = store.register_sync_run(s, triggered_by=SyncTrigger.schedule)
    s.flush()
    snap_prd = store.register_snapshot(
        s, sync_run_id=run.id, repository_id=repo.id, artifact_def_id=adef_prd.id,
        status=SnapshotStatus.found, content_hash="a" * 64, file_path="product/prd.md",
        source_commit_sha="c" * 40,
    )
    # альтернативный путь роли prd — заготовка: best-wins не должен её показать
    store.register_snapshot(
        s, sync_run_id=run.id, repository_id=repo.id, artifact_def_id=adef_prd_alt.id,
        status=SnapshotStatus.partial, partial_reason=["template_copy"],
        content_hash="t" * 64, file_path="REQUIREMENTS.md", source_commit_sha="c" * 40,
    )
    snap_dm = store.register_snapshot(
        s, sync_run_id=run.id, repository_id=repo.id, artifact_def_id=adef_dm.id,
        status=SnapshotStatus.found, content_hash="b" * 64, file_path="data-model.md",
        source_commit_sha="c" * 40,
    )
    store.register_snapshot(
        s, sync_run_id=run.id, repository_id=repo.id, artifact_def_id=adef_jtbd.id,
        status=SnapshotStatus.partial, partial_reason=["template_copy"],
        content_hash="d" * 64, file_path="product/jtbd.md", source_commit_sha="c" * 40,
    )
    store.register_sync_outcome(
        s, sync_run_id=run.id, repository_id=repo.id, outcome=SyncOutcome.ok_changed
    )
    s.flush()
    verdict = store.register_verdict(
        s, edge_def_id=edge.id, source_snapshot_id=snap_prd.id, target_snapshot_id=snap_dm.id,
        source_content_hash="a" * 64, target_content_hash="b" * 64, rubric_id=rubric.id,
        llm_model=LLM_MODEL, verdict="break", confidence="high",
        points=[{"entity": "Оксана", "quote": "другой преподаватель", "why": "не найдена в схеме"}],
        notes="проверь, где схема хранит роли будущих версий",
    )
    s.flush()
    return repo, verdict, edge


# ── уровень сервиса: build_artifact_matrix ─────────────────────────────────


def test_roles_ordered_by_first_lesson(engine):
    with Session(engine) as s:
        _seed(s)
        s.commit()
        matrix = build_artifact_matrix(s, llm_model=LLM_MODEL)
    assert [r["key"] for r in matrix["roles"]] == ["jtbd", "prd", "data_model"]
    assert matrix["roles"][1]["title"] == "PRD"  # человекочитаемый заголовок колонки


def test_cell_best_wins_between_alternative_paths(engine):
    """Роль prd имеет found и partial-заготовку — ячейка показывает лучший статус."""
    with Session(engine) as s:
        repo, *_ = _seed(s)
        s.commit()
        cell = build_artifact_matrix(s, llm_model=LLM_MODEL)["cells"][repo.id]["prd"]
    assert cell["status"] == SnapshotStatus.found


def test_cell_partial_summary_human_readable(engine):
    with Session(engine) as s:
        repo, *_ = _seed(s)
        s.commit()
        cell = build_artifact_matrix(s, llm_model=LLM_MODEL)["cells"][repo.id]["jtbd"]
    assert cell["status"] == SnapshotStatus.partial
    assert "заготовка из шаблона" in cell["summary"]  # не сырой код template_copy


def test_cell_break_summary_names_lost_entity(engine):
    """Разрыв ребра prd→data_model виден в ячейках обеих ролей с первой потерей."""
    with Session(engine) as s:
        repo, *_ = _seed(s)
        s.commit()
        cells = build_artifact_matrix(s, llm_model=LLM_MODEL)["cells"][repo.id]
    for role in ("prd", "data_model"):
        assert cells[role]["break_count"] == 1
        assert "разрыв" in cells[role]["summary"]
        assert "Оксана" in cells[role]["summary"]


def test_override_suppresses_break_in_cell(engine):
    """FR-10: отметка «ложный разрыв» гасит разрыв в своде ячейки."""
    with Session(engine) as s:
        repo, verdict, _ = _seed(s)
        store.register_override(s, coherence_verdict_id=verdict.id, reason="синоним")
        s.commit()
        cell = build_artifact_matrix(s, llm_model=LLM_MODEL)["cells"][repo.id]["prd"]
    assert cell["break_count"] == 0
    assert "разрыв" not in (cell["summary"] or "")


def test_empty_cell_when_no_snapshots(engine):
    """Репозиторий без наблюдений роли — пустая ячейка, не «нет»."""
    with Session(engine) as s:
        repo, *_ = _seed(s)
        run2 = store.register_sync_run(s, triggered_by=SyncTrigger.schedule)
        repo2 = store.register_repository(
            s, repo_url="https://github.com/s/y", git_host=GitHost.GitHub
        )
        s.flush()
        store.register_sync_outcome(
            s, sync_run_id=run2.id, repository_id=repo2.id, outcome=SyncOutcome.ok_changed
        )
        s.commit()
        cell = build_artifact_matrix(s, llm_model=LLM_MODEL)["cells"][repo2.id]["prd"]
    assert cell["status"] is None
    assert cell["summary"] is None


def test_as_of_contains_day_and_month(engine):
    """Макет: «Время и дата последнего анализа: День.Месяц ЧЧ:ММ»."""
    with Session(engine) as s:
        _seed(s)
        s.commit()
        matrix = build_artifact_matrix(s, llm_model=LLM_MODEL)
    assert "." in matrix["as_of"] and ":" in matrix["as_of"]


# ── уровень сервиса: build_cell_details (модалка) ──────────────────────────


def test_cell_details_lists_files_and_edges(engine):
    with Session(engine) as s:
        repo, verdict, _ = _seed(s)
        s.commit()
        details = build_cell_details(s, repo.id, "prd", llm_model=LLM_MODEL)
    paths = {f["file_path"] for f in details["files"]}
    assert {"product/prd.md", "REQUIREMENTS.md"} <= paths
    (edge,) = details["edges"]
    assert edge["source_role"] == "prd" and edge["target_role"] == "data_model"
    assert edge["points"][0]["entity"] == "Оксана"
    assert edge["notes"] == "проверь, где схема хранит роли будущих версий"


def test_cell_details_unknown_role_or_repo_none(engine):
    with Session(engine) as s:
        repo, *_ = _seed(s)
        s.commit()
        assert build_cell_details(s, repo.id, "no-such-role", llm_model=LLM_MODEL) is None
        assert build_cell_details(s, "no-such-repo", "prd", llm_model=LLM_MODEL) is None


# ── уровень HTTP ───────────────────────────────────────────────────────────


def test_artifacts_page_requires_login(client):
    assert client.get("/artifacts", follow_redirects=False).status_code == 303


def test_modal_requires_login(client, engine):
    with Session(engine) as s:
        repo, *_ = _seed(s)
        s.commit()
        repo_id = repo.id
    assert client.get(f"/artifacts/{repo_id}/prd", follow_redirects=False).status_code == 303


def test_artifacts_page_renders_colored_cells(client, engine):
    with Session(engine) as s:
        repo, *_ = _seed(s)
        s.commit()
    _login(client)
    html = client.get("/artifacts").text
    assert "https://github.com/s/x" in html
    assert "PRD" in html  # колонка роли
    assert "acell-found" in html and "acell-partial" in html  # цветовое кодирование
    assert "hx-get=" in html  # клик по ячейке открывает модалку


def test_modal_shows_details_and_override_button(client, engine):
    with Session(engine) as s:
        repo, verdict, _ = _seed(s)
        s.commit()
        repo_id, verdict_id = repo.id, verdict.id
    _login(client)
    html = client.get(f"/artifacts/{repo_id}/prd").text
    assert "product/prd.md" in html  # найденные файлы
    assert "Оксана" in html and "другой преподаватель" in html  # точка разрыва с цитатой
    assert "проверь, где схема хранит" in html  # заметка преподавателю
    assert f"/verdicts/{verdict_id}/override-toggle" in html  # FR-10 из модалки


def test_modal_unknown_role_404(client, engine):
    with Session(engine) as s:
        repo, *_ = _seed(s)
        s.commit()
        repo_id = repo.id
    _login(client)
    assert client.get(f"/artifacts/{repo_id}/no-such").status_code == 404
