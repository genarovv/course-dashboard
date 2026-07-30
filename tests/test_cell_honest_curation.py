"""D21 (#67): честная ячейка и видимое курирование.

Спека: plans/доводки-ux-2026-07-31.md (FR-4/FR-5 — итог ячейки; FR-10 — кнопку не находят).
AC:
  1. Ячейка «есть» с активным разрывом — словесный итог «сдано, но разрыв»
     вместо «есть» (снимает конфликт «зелёное + бордовое»).
  2. Чип разрыва — 2 строки с многоточием (CSS line-clamp); название сущности
     в чипе усечено до ~80 символов, полный текст — в модалке.
  3. Кнопка «ложный разрыв» в модалке — видимая кнопка-действие в блоке разрыва
     с подписью «отметка обратима» (не текстовая строка в подвале).
Негативные: «частично» с разрывом — прежний свод (спека адресует только «есть»);
короткая сущность не усекается.
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
LONG_ENTITY = "Полный жизненный цикл обработки заявки от подачи через все стадии " \
    "модерации до финального решения с уведомлениями всех участников процесса"


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


def _seed(s, *, entity="Оксана", partial=False):
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
    fields_a = dict(
        sync_run_id=run.id, repository_id=repo.id, artifact_def_id=adef_prd.id,
        content_hash="a" * 64, file_path="product/prd.md", source_commit_sha="c" * 40,
    )
    if partial:
        fields_a.update(status=SnapshotStatus.partial, partial_reason=["template_copy"])
    else:
        fields_a.update(status=SnapshotStatus.found)
    snap_a = store.register_snapshot(s, **fields_a)
    snap_b = store.register_snapshot(
        s, sync_run_id=run.id, repository_id=repo.id, artifact_def_id=adef_dm.id,
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
        points=[{"entity": entity, "quote": "цитата", "why": "не нашёл"}],
    )
    s.flush()
    return repo


# ── AC 1: словесный итог ──────────────────────────────────────────────────


def test_found_with_break_says_sdano_no_razryv(engine):
    with Session(engine) as s:
        repo = _seed(s)
        s.commit()
        matrix = build_artifact_matrix(s, llm_model=LLM_MODEL)
    cell = matrix["cells"][repo.id]["prd"]
    assert cell["presence"] == "сдано, но разрыв"
    assert cell["summary"].startswith("сдано, но разрыв")
    assert "есть ·" not in cell["summary"]


def test_partial_with_break_keeps_partial_wording(engine):
    with Session(engine) as s:
        repo = _seed(s, partial=True)
        s.commit()
        matrix = build_artifact_matrix(s, llm_model=LLM_MODEL)
    cell = matrix["cells"][repo.id]["prd"]
    assert cell["presence"].startswith("частично")
    assert "разрыв" in cell["summary"]


# ── AC 2: усечение сущности в чипе, полный текст в модалке ────────────────


def test_long_entity_truncated_in_chip_full_in_modal(client, engine):
    with Session(engine) as s:
        repo = _seed(s, entity=LONG_ENTITY)
        s.commit()
        repo_id = repo.id
        matrix = build_artifact_matrix(s, llm_model=LLM_MODEL)
    chip_entity = matrix["cells"][repo_id]["prd"]["break"]["entity"]
    assert len(chip_entity) <= 81 and chip_entity.endswith("…")
    assert chip_entity.startswith(LONG_ENTITY[:40])  # начало не искажено
    _login(client)
    assert LONG_ENTITY in client.get(f"/artifacts/{repo_id}/prd").text  # модалка — полный


def test_short_entity_not_truncated(engine):
    with Session(engine) as s:
        repo = _seed(s)
        s.commit()
        matrix = build_artifact_matrix(s, llm_model=LLM_MODEL)
    assert matrix["cells"][repo.id]["prd"]["break"]["entity"] == "Оксана"


def test_chip_css_line_clamp_two_lines():
    css = open("app/static/css/app.css", encoding="utf-8").read()
    assert "line-clamp" in css  # чип обрезается до 2 строк с многоточием


# ── AC 3: видимая кнопка курирования ──────────────────────────────────────


def test_override_button_visible_with_reversible_note(client, engine):
    with Session(engine) as s:
        repo = _seed(s)
        s.commit()
        repo_id = repo.id
    _login(client)
    html = client.get(f"/artifacts/{repo_id}/prd").text
    assert 'class="override-button"' in html  # кнопка-действие, не текстовая строка
    assert "отметка обратима" in html
    # кнопка в блоке разрыва: до списка точек, а не в подвале карточки ребра
    assert html.index("override-button") < html.index("points")
