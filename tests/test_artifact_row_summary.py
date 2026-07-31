"""D20 (#66): сводная колонка строки + две сортировки.

Спека: plans/доводки-ux-2026-07-31.md (J1/J3, цель §4 «≤5 минут»).
AC:
  1. В закреплённой зоне рядом с именем репо — сводка: «⚠ N» (уникальные
     разрывы, как D15), «X/M» (сдано ролей из ожидаемых; found+partial = сдано,
     BR-3), шильдик «мимо ревью: K» при K>0.
  2. Две сортировки в шапке: «по разрывам» (D15) и «по отставанию»
     (по возрастанию X/M); подпись «сначала проблемные» уходит.
  3. Колонка «Разрывы» справа удаляется (один факт — в одном месте).
Негативные: пустая строка (0/M) — «по отставанию» ставит её первой;
обе сортировки стабильны к порядку реестра; N=0 — чипа нет; K=0 — шильдика нет.
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


def _seed(s, *, mr_lesson=False):
    """r1: обе роли found + разрыв; r2: prd partial, dm нет наблюдения; r3: пусто."""
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
    adef_prd = ArtifactDef(lesson_id=lesson5.id, role="prd", expected_pattern="product/prd.md")
    adef_dm = ArtifactDef(lesson_id=lesson6.id, role="data_model", expected_pattern="data-model.md")
    adefs = [adef_prd, adef_dm]
    if mr_lesson:
        adefs.append(ArtifactDef(
            lesson_id=lessons[2].id, role="tests", expected_pattern="tests/**/*.py"
        ))
    s.add_all(adefs)
    rubric = store.register_rubric(s, type="edge", version="1.0", text="правило")
    s.flush()
    edge = store.config_create_edge_def(
        s, source_role="prd", target_role="data_model", rubric_id=rubric.id
    )
    run = store.register_sync_run(s, triggered_by=SyncTrigger.schedule)
    s.flush()

    repos = []
    for i in (1, 2, 3):
        repo = store.register_repository(
            s, repo_url=f"https://github.com/s/r{i}", git_host=GitHost.GitHub
        )
        repos.append(repo)
    s.flush()
    r1, r2, r3 = repos

    snap_a = store.register_snapshot(
        s, sync_run_id=run.id, repository_id=r1.id, artifact_def_id=adef_prd.id,
        status=SnapshotStatus.found, content_hash="1a".ljust(64, "0"),
        file_path="product/prd.md", source_commit_sha="c" * 40,
    )
    snap_b = store.register_snapshot(
        s, sync_run_id=run.id, repository_id=r1.id, artifact_def_id=adef_dm.id,
        status=SnapshotStatus.found, content_hash="1b".ljust(64, "0"),
        file_path="data-model.md", source_commit_sha="c" * 40,
    )
    store.register_snapshot(
        s, sync_run_id=run.id, repository_id=r2.id, artifact_def_id=adef_prd.id,
        status=SnapshotStatus.partial, content_hash="2a".ljust(64, "0"),
        file_path="product/prd.md", source_commit_sha="c" * 40,
        partial_reason=["template_copy"],
    )
    store.register_snapshot(
        s, sync_run_id=run.id, repository_id=r3.id, artifact_def_id=adef_prd.id,
        status=SnapshotStatus.not_found,
    )
    for repo in repos:
        store.register_sync_outcome(
            s, sync_run_id=run.id, repository_id=repo.id, outcome=SyncOutcome.ok_changed
        )
    s.flush()
    verdict = store.register_verdict(
        s, edge_def_id=edge.id, source_snapshot_id=snap_a.id, target_snapshot_id=snap_b.id,
        source_content_hash=snap_a.content_hash, target_content_hash=snap_b.content_hash,
        rubric_id=rubric.id, llm_model=LLM_MODEL, verdict="break", confidence="high",
        points=[{"entity": "X", "quote": "ц", "why": "нет"}],
    )
    s.flush()
    return repos, run, verdict


# ── AC 1: счётчики строки ─────────────────────────────────────────────────


def test_row_submitted_found_and_partial_count(engine):
    with Session(engine) as s:
        (r1, r2, r3), _, _ = _seed(s)
        s.commit()
        matrix = build_artifact_matrix(s, llm_model=LLM_MODEL)
    assert matrix["row_submitted"][r1.id] == {"x": 2, "m": 2}
    assert matrix["row_submitted"][r2.id] == {"x": 1, "m": 2}  # partial = сдано (BR-3)
    assert matrix["row_submitted"][r3.id] == {"x": 0, "m": 2}


def test_mr_channel_role_excluded_from_expected(engine):
    """Роль MR-занятия без файла — «сдача через MR», в знаменатель M не входит."""
    with Session(engine) as s:
        (r1, _, _), _, _ = _seed(s, mr_lesson=True)
        s.commit()
        matrix = build_artifact_matrix(s, llm_model=LLM_MODEL)
    assert matrix["row_submitted"][r1.id] == {"x": 2, "m": 2}  # tests не в M


def test_row_no_review_counts_merged_without_approval(engine):
    with Session(engine) as s:
        (r1, r2, _), run, _ = _seed(s)
        store.register_mr_observation(
            s, sync_run_id=run.id, repository_id=r1.id, mr_number=1, title="t",
            source_branch="b", state="merged", reviewer_approved=False,
            markers=None, head_sha="a" * 40, updated_at=None,
        )
        store.register_mr_observation(
            s, sync_run_id=run.id, repository_id=r2.id, mr_number=2, title="t",
            source_branch="b", state="merged", reviewer_approved=True,
            markers=None, head_sha="b" * 40, updated_at=None,
        )
        s.commit()
        matrix = build_artifact_matrix(s, llm_model=LLM_MODEL)
    assert matrix["row_no_review"][r1.id] == 1
    assert matrix["row_no_review"][r2.id] == 0


# ── AC 2: сортировка «по отставанию» ──────────────────────────────────────


def test_sort_lag_puts_empty_row_first(engine):
    with Session(engine) as s:
        (r1, r2, r3), _, _ = _seed(s)
        s.commit()
        default = build_artifact_matrix(s, llm_model=LLM_MODEL)
        by_lag = build_artifact_matrix(s, llm_model=LLM_MODEL, sort="lag")
    assert [r["id"] for r in default["repositories"]] == [r1.id, r2.id, r3.id]  # реестр
    assert [r["id"] for r in by_lag["repositories"]] == [r3.id, r2.id, r1.id]  # 0/2 первым


def test_sort_lag_stable_for_equal_ratio(engine):
    """Равные X/M сохраняют порядок реестра (стабильность) — ревью итерации 4,
    находка 5: в сиде есть пара репозиториев с одинаковой долей."""
    with Session(engine) as s:
        (r1, r2, r3), run, _ = _seed(s)
        r4 = store.register_repository(
            s, repo_url="https://github.com/s/r4", git_host=GitHost.GitHub
        )
        s.flush()
        store.register_sync_outcome(
            s, sync_run_id=run.id, repository_id=r4.id, outcome=SyncOutcome.ok_changed
        )
        s.commit()  # r4 пуст, как r3 (0/2): равные доли, r3 раньше в реестре
        by_lag = build_artifact_matrix(s, llm_model=LLM_MODEL, sort="lag")
        order = [r["id"] for r in by_lag["repositories"]]
        ratios = [
            matrix_ratio(by_lag["row_submitted"][r["id"]]) for r in by_lag["repositories"]
        ]
    assert order.index(r3.id) < order.index(r4.id)  # стабильность при равной доле
    assert ratios == sorted(ratios)


def matrix_ratio(rs):
    return rs["x"] / rs["m"] if rs["m"] else 1.0


# ── AC 1–3: HTML ──────────────────────────────────────────────────────────


def test_summary_in_repo_cell_and_no_right_column(client, engine):
    with Session(engine) as s:
        (r1, _, _), run, _ = _seed(s)
        store.register_mr_observation(
            s, sync_run_id=run.id, repository_id=r1.id, mr_number=1, title="t",
            source_branch="b", state="merged", reviewer_approved=False,
            markers=None, head_sha="a" * 40, updated_at=None,
        )
        s.commit()
    _login(client)
    html = client.get("/artifacts").text
    assert "⚠ 1" in html  # чип разрывов в сводке строки
    assert "2/2" in html and "0/2" in html  # X/M
    assert "мимо ревью: 1" in html
    assert "col-breaks" not in html  # правая колонка удалена
    assert "сначала проблемные" not in html  # подпись ушла
    assert "?sort=breaks" in html and "?sort=lag" in html  # обе сортировки в шапке
    assert "по разрывам" in html and "по отставанию" in html


def test_no_breaks_no_chip_no_review_badge(client, engine):
    with Session(engine) as s:
        (_, _, _), _, verdict = _seed(s)
        store.register_override(s, coherence_verdict_id=verdict.id, reason="синоним")
        s.commit()
    _login(client)
    html = client.get("/artifacts").text
    assert "⚠ 1" not in html  # погашенный разрыв чипа не рождает
    assert "мимо ревью:" not in html  # K=0 — шильдика нет (легенда «мимо ревью» без двоеточия — D38)


def test_sort_lag_via_http(client, engine):
    with Session(engine) as s:
        _seed(s)
        s.commit()
    _login(client)
    html = client.get("/artifacts?sort=lag").text
    assert html.index("github.com/s/r3") < html.index("github.com/s/r1")
