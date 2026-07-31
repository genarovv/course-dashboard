"""D38/D39 (итерация 5): P1-пакет и P2-полировка.

Спека: plans/доводки-ux-5-2026-07-31.md §D38, §D39 (решение CEO «на все да»).
D38: легенда сводки строки; активная сортировка помечена (и «по реестру» на
дефолте); сущность в чипе усечена по границе слова; кнопка курирования —
глагол «пометить ложным» + «отметка обратима» во всех местах; подсказки
заголовков колонок. D39: заголовок модалки «Роль — имя репо»; «ожидался,
не найден»; равные хеши — один @sha; легенда матрицы занятий; статусы MR
по-русски; формулировка MR-канала без двойного отрицания.
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
LONG_ENTITY = (
    "Полный жизненный цикл обработки заявки от подачи через все стадии "
    "модерации до финального решения"
)


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


def _seed(s, *, entity="Оксана", mr_lesson=False):
    lesson5 = Lesson(number=5, title="PRD", date=datetime(2026, 6, 30).date())
    lesson6 = Lesson(number=6, title="Данные", date=datetime(2026, 7, 2).date())
    lessons = [lesson5, lesson6]
    if mr_lesson:
        lessons.append(Lesson(
            number=11, title="Тесты", date=datetime(2026, 7, 14).date(),
            submission_channel="mr",
        ))
    s.add_all(lessons)
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
        points=[{"entity": entity, "quote": "ц", "why": "нет"}], notes="Потеряна.",
    )
    store.register_mr_observation(
        s, sync_run_id=run.id, repository_id=repo.id, mr_number=7, title="t",
        source_branch="feat/x", state="merged", reviewer_approved=False,
        markers=None, head_sha="a" * 40, updated_at=datetime(2026, 7, 20, 10, 0),
    )
    s.flush()
    return repo


# ── D38 ───────────────────────────────────────────────────────────────────


def test_legend_explains_row_summary(client, engine):
    with Session(engine) as s:
        _seed(s)
        s.commit()
    _login(client)
    legend = client.get("/artifacts").text.split('class="legend"')[1].split("</div>")[0]
    assert "непогашенные разрывы" in legend  # ⚠ N
    assert "сдано артефактов из ожидаемых" in legend  # X/M
    assert "без вердикта ревьюера" in legend  # мимо ревью


def test_active_sort_marked_including_default(client, engine):
    with Session(engine) as s:
        _seed(s)
        s.commit()
    _login(client)
    html = client.get("/artifacts").text
    assert "по реестру ▼" in html  # дефолт — тоже состояние, а не пустота
    lag = client.get("/artifacts?sort=lag").text
    assert "по отставанию ▼" in lag and "по реестру ▼" not in lag


def test_chip_entity_truncated_at_word_boundary(engine):
    with Session(engine) as s:
        repo = _seed(s, entity=LONG_ENTITY)
        s.commit()
        matrix = build_artifact_matrix(s, llm_model=LLM_MODEL)
    chip = matrix["cells"][repo.id]["prd"]["break"]["entity"]
    assert chip.endswith("…") and len(chip) <= 81
    assert not chip[:-1].endswith(("требовани", "каждо"))  # не посреди слова
    assert chip[:-1] == chip[:-1].rstrip() + ""  # без висячего пробела
    assert chip[:-1].rstrip() == chip[:-1]


def test_override_button_verb_and_note_everywhere(client, engine):
    with Session(engine) as s:
        repo = _seed(s)
        s.commit()
        repo_id = repo.id
    _login(client)
    modal = client.get(f"/artifacts/{repo_id}/prd").text
    card = client.get(f"/students/{repo_id}").text
    lessons = client.get("/lessons").text
    for html in (modal, card, lessons):
        assert "пометить ложным" in html  # глагол-действие
        assert "отметка обратима" in html  # подпись во всех трёх местах


def test_column_headers_have_tooltips(client, engine):
    with Session(engine) as s:
        _seed(s)
        s.commit()
    _login(client)
    html = client.get("/artifacts").text
    assert "описание продукта" in html  # PRD — title-подсказка
    assert "схема данных проекта" in html  # data_model — расшифровка


# ── D39 ───────────────────────────────────────────────────────────────────


def test_modal_title_includes_repo_name(client, engine):
    with Session(engine) as s:
        repo = _seed(s)
        s.commit()
        repo_id = repo.id
    _login(client)
    assert "PRD — x" in client.get(f"/artifacts/{repo_id}/prd").text


def test_modal_missing_file_expected_wording(client, engine):
    with Session(engine) as s:
        repo = _seed(s, mr_lesson=False)
        s.commit()
        # роль без наблюдений вовсе: третий adef другой роли
        lesson = s.query(Lesson).filter(Lesson.number == 5).one()
        s.add(ArtifactDef(
            lesson_id=lesson.id, role="jtbd", expected_pattern="product/jtbd.md"
        ))
        s.commit()
        repo_id = repo.id
    _login(client)
    html = client.get(f"/artifacts/{repo_id}/jtbd").text
    assert "ожидался, не найден" in html
    assert "не наблюдался" not in html


def test_defense_equal_shas_collapsed(client, engine):
    with Session(engine) as s:
        repo = _seed(s)
        s.commit()
        repo_id = repo.id
    _login(client)
    html = client.get(f"/students/{repo_id}/defense").text
    assert "@cccccccc → @cccccccc" not in html  # равные хеши — один @sha
    assert "@cccccccc" in html


def test_lessons_matrix_has_legend(client, engine):
    with Session(engine) as s:
        _seed(s, mr_lesson=True)
        s.commit()
    _login(client)
    html = client.get("/lessons").text
    assert 'class="legend"' in html
    assert "сдача через merge request" in html


def test_mr_states_russian_in_card_and_defense(client, engine):
    with Session(engine) as s:
        repo = _seed(s)
        s.commit()
        repo_id = repo.id
    _login(client)
    card = client.get(f"/students/{repo_id}").text
    defense = client.get(f"/students/{repo_id}/defense").text
    assert "влит" in card  # merged → влит
    assert ">merged<" not in card
    assert "влит" in defense


def test_mr_channel_wording_no_double_negative(client, engine):
    with Session(engine) as s:
        _seed(s, mr_lesson=True)
        s.commit()
    _login(client)
    html = client.get("/lessons").text
    assert "сдача через MR, не наблюдается" not in html
    assert "файлов в основной ветке нет" in html
