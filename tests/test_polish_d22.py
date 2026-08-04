"""D22 (#68): полировка — доверие и язык интерфейса.

Спека: plans/доводки-ux-2026-07-31.md (группа B: «4/10 отполированность»).
AC (каждый пункт самостоятелен):
  1. Легенда без жаргона: «не наблюдался» → «нет данных»,
     «сдача через MR» → «сдача через merge request (запрос на слияние)».
  2. Переносы в колонке репо — по границам слов (overflow-wrap), не в случайном месте.
  3. URL в шапках модалки и карточки студента → ссылка «открыть репозиторий».
  4. «защита» → «защита проекта».
  5. Кнопка «обновить сейчас» на стикере актуальности (POST /sync с подтверждением).
  6. Пустые MR-колонки матрицы занятий схлопнуты в одну «занятия N–M: сдача через MR».
  7. Таймстемпы интерфейса без микросекунд везде (карточка студента — хронология).
Негативные: одна пустая MR-колонка не схлопывается (нечего объединять);
занятая MR-колонка (есть файлы хоть у одного) не схлопывается.
"""

import re
from datetime import datetime

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from alembic import command
from app import store
from app.main import app
from app.models import GitHost, SnapshotStatus, SyncOutcome, SyncStatus, SyncTrigger
from app.models.artifact_def import ArtifactDef
from app.models.lesson import Lesson
from app.routes import get_session

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


def _seed(s, *, mr_lessons=(), mr_files=False):
    """Занятие 5 (files) + MR-занятия по списку номеров; один репозиторий."""
    lesson5 = Lesson(number=5, title="PRD", date=datetime(2026, 6, 30).date())
    lessons = [lesson5]
    for num in mr_lessons:
        lessons.append(Lesson(
            number=num, title=f"Тема {num}", date=datetime(2026, 7, num).date(),
            submission_channel="mr",
        ))
    s.add_all(lessons)
    s.flush()
    adefs = {5: ArtifactDef(lesson_id=lesson5.id, role="prd", expected_pattern="product/prd.md")}
    for lesson in lessons[1:]:
        adefs[lesson.number] = ArtifactDef(
            lesson_id=lesson.id, role="tests", expected_pattern=f"tests/l{lesson.number}/*.py"
        )
    s.add_all(adefs.values())
    repo = store.register_repository(s, repo_url="https://github.com/s/x", git_host=GitHost.GitHub)
    run = store.register_sync_run(s, triggered_by=SyncTrigger.schedule)
    s.flush()
    store.register_snapshot(
        s, sync_run_id=run.id, repository_id=repo.id, artifact_def_id=adefs[5].id,
        status=SnapshotStatus.found, content_hash="a" * 64,
        file_path="product/prd.md", source_commit_sha="c" * 40,
    )
    if mr_files and mr_lessons:
        store.register_snapshot(
            s, sync_run_id=run.id, repository_id=repo.id,
            artifact_def_id=adefs[mr_lessons[0]].id,
            status=SnapshotStatus.found, content_hash="b" * 64,
            file_path=f"tests/l{mr_lessons[0]}/test_x.py", source_commit_sha="c" * 40,
        )
    store.register_sync_outcome(
        s, sync_run_id=run.id, repository_id=repo.id, outcome=SyncOutcome.ok_changed
    )
    # D42: обход фикстуры завершён — иначе кнопка «обновить сейчас» честно
    # погашена как «обход идёт», и AC 5 проверял бы несуществующее состояние
    store.update_sync_run_status(s, run.id, SyncStatus.completed)
    s.flush()
    return repo


# ── AC 1: легенда без жаргона ─────────────────────────────────────────────


def test_legend_without_jargon(client, engine):
    """tests-change D22 (#68): формулировки легенды заменены по спеке —
    состав легенды (все состояния + оба образца чипа) не менялся."""
    with Session(engine) as s:
        _seed(s)
        s.commit()
    _login(client)
    html = client.get("/artifacts").text
    legend = html.split('class="legend"')[1].split("</div>")[0]
    assert "нет данных" in legend
    assert "не наблюдался" not in legend
    assert "сдача через merge request (запрос на слияние)" in legend


# ── AC 2: переносы в колонке репо ─────────────────────────────────────────


def test_repo_column_wraps_on_word_boundaries():
    css = open("app/static/css/app.css", encoding="utf-8").read()
    assert "overflow-wrap" in css  # перенос по границам слов, не в случайном месте


# ── AC 3: «открыть репозиторий» вместо URL-простыни ───────────────────────


def test_student_card_header_link(client, engine):
    with Session(engine) as s:
        repo = _seed(s)
        s.commit()
        repo_id, repo_url = repo.id, repo.repo_url
    _login(client)
    html = client.get(f"/students/{repo_id}").text
    assert "открыть репозиторий" in html
    assert f'href="{repo_url}"' in html


def test_modal_header_link(client, engine):
    with Session(engine) as s:
        repo = _seed(s)
        s.commit()
        repo_id, repo_url = repo.id, repo.repo_url
    _login(client)
    html = client.get(f"/artifacts/{repo_id}/prd").text
    assert "открыть репозиторий" in html
    assert f'href="{repo_url}"' in html


# ── AC 4: «защита проекта» ────────────────────────────────────────────────


def test_defense_links_named_zashchita_proekta(client, engine):
    with Session(engine) as s:
        repo = _seed(s)
        s.commit()
        repo_id = repo.id
    _login(client)
    assert "защита проекта" in client.get("/artifacts").text
    assert "защита проекта" in client.get(f"/students/{repo_id}").text


# ── AC 5: кнопка «обновить сейчас» ────────────────────────────────────────


def test_sync_now_button_with_confirm(client, engine):
    with Session(engine) as s:
        _seed(s)
        s.commit()
    _login(client)
    for path in ("/artifacts", "/lessons"):
        html = client.get(path).text
        assert "обновить сейчас" in html
        assert "confirm(" in html  # POST /sync — только с подтверждением


# ── AC 6: схлопывание пустых MR-колонок ───────────────────────────────────


def test_empty_mr_columns_collapsed(client, engine):
    with Session(engine) as s:
        _seed(s, mr_lessons=(11, 12, 13))
        s.commit()
    _login(client)
    html = client.get("/lessons").text
    assert "занятия 11–13: сдача через MR" in html
    assert "11. Тема 11" not in html  # отдельных колонок больше нет
    assert "13. Тема 13" not in html


def test_single_empty_mr_column_not_collapsed(client, engine):
    with Session(engine) as s:
        _seed(s, mr_lessons=(11,))
        s.commit()
    _login(client)
    html = client.get("/lessons").text
    assert "занятия 11" not in html  # схлопывать нечего
    assert "11. Тема 11" in html


def test_non_contiguous_empty_mr_columns_listed_not_ranged(client, engine):
    """Ревью итерации 4, находка 2: пустые MR-занятия 11 и 13 при занятом 12
    подписываются перечислением, а не лживым диапазоном «11–13»."""
    with Session(engine) as s:
        _seed(s, mr_lessons=(11, 12, 13), mr_files=False)
        # займём занятие 12 файлом — 11 и 13 остаются пустыми, несмежными
        s.flush()
        adef12 = s.query(ArtifactDef).join(Lesson, ArtifactDef.lesson_id == Lesson.id).filter(
            Lesson.number == 12
        ).one()
        run = store.register_sync_run(s, triggered_by=SyncTrigger.schedule)
        repo = store.find_active_repositories(s)[0]
        s.flush()
        store.register_snapshot(
            s, sync_run_id=run.id, repository_id=repo.id, artifact_def_id=adef12.id,
            status=SnapshotStatus.found, content_hash="d" * 64,
            file_path="tests/l12/test_x.py", source_commit_sha="c" * 40,
        )
        s.commit()
    _login(client)
    html = client.get("/lessons").text
    assert "занятия 11, 13: сдача через MR" in html
    assert "занятия 11–13" not in html
    assert "12. Тема 12" in html  # занятая колонка живёт отдельно


def test_mr_column_with_files_not_collapsed(client, engine):
    with Session(engine) as s:
        _seed(s, mr_lessons=(11, 12), mr_files=True)
        s.commit()
    _login(client)
    html = client.get("/lessons").text
    assert "11. Тема 11" in html  # занятая колонка живёт отдельно
    assert "занятия 11–12" not in html


# ── AC 7: таймстемпы без микросекунд ──────────────────────────────────────


def test_student_card_times_no_microseconds(client, engine):
    with Session(engine) as s:
        repo = _seed(s)
        s.commit()
        repo_id = repo.id
    _login(client)
    html = client.get(f"/students/{repo_id}").text
    assert not re.search(r"\.\d{6}", html)
    assert not re.search(r"\d{4}-\d{2}-\d{2}T", html)  # сырых ISO-строк нет
