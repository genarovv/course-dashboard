"""S4 (#6), FR-2: config.yaml → конфиг-реконсиляция БД (ADR-005, ARCHITECTURE §3.4/§3.5 кат. 3).

AC тикета #6 (переписан по ADR-005):
  1. При старте и по POST /admin/reload-config config_manager читает YAML и реконсилирует
     Lesson, ArtifactDef, EdgeDef.rubric_id; новые версии рубрик — append-only (register_rubric).
  2. Reload идемпотентен: повторный прогон того же YAML ничего не меняет.
  3. Reload перенаправляет ребро на новую версию рубрики; старые вердикты
     не пересчитываются и не теряются (обязательный AC-тест ADR-005).
  4. Функции конфиг-реконсиляции store.py вызывает только config_manager
     (ограничитель ADR-005, закреплён тестом на импорт).
  5. Смена рубрики помечается в сводке как требующая прогона golden set
     (железное правило CLAUDE.md).
"""

from pathlib import Path

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app import store
from app.config import settings
from app.main import app
from app.models.coherence_verdict import CoherenceVerdict
from app.models.edge_def import EdgeDef
from app.models.rubric import Rubric
from app.routes import get_session
from app.services import config_manager

YAML_V1 = """
lessons:
  - number: 5
    title: "Документация требований"
    date: 2026-06-30
    artifacts:
      - role: prd
        expected_pattern: "product/prd.md"
  - number: 6
    title: "Проектирование структуры данных"
    date: 2026-07-02
    artifacts:
      - role: data_model
        expected_pattern: "data-model.md"
edges:
  - source_role: prd
    target_role: data_model
    rubric:
      version: "1.0"
      text: "Правило ребра v1: схема данных обязана опираться на PRD."
"""

# V2: у занятия 5 изменён title, у рубрики — версия и текст
YAML_V2 = YAML_V1.replace(
    'title: "Документация требований"', 'title: "Документация требований (PRD)"'
).replace('version: "1.0"', 'version: "1.1"').replace(
    "Правило ребра v1", "Правило ребра v1.1 (уточнено)"
)


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


def _reconcile_yaml(session, yaml_text: str):
    return config_manager.reconcile(session, config_manager.parse_config(yaml_text))


# ── AC 1: создание с нуля ───────────────────────────────────────────────────


def test_fresh_db_creates_lessons_artifacts_edges_rubrics(session):
    summary = _reconcile_yaml(session, YAML_V1)

    lessons = store.find_all_lessons(session)
    assert [lesson.number for lesson in lessons] == [5, 6]
    adefs_5 = store.find_artifact_defs_by_lesson(session, lessons[0].id)
    assert [(a.role, a.expected_pattern) for a in adefs_5] == [("prd", "product/prd.md")]

    edge = session.scalar(select(EdgeDef))
    assert edge is not None
    assert (edge.source_role, edge.target_role) == ("prd", "data_model")
    rubric = session.get(Rubric, edge.rubric_id)
    assert rubric.version == "1.0"

    assert summary.lessons_created == 2
    assert summary.artifact_defs_created == 2
    assert summary.edges_created == 1
    assert summary.rubrics_registered == 1


# ── AC 2: идемпотентность ───────────────────────────────────────────────────


def test_reload_same_yaml_is_idempotent(session):
    _reconcile_yaml(session, YAML_V1)
    session.commit()
    summary = _reconcile_yaml(session, YAML_V1)

    assert summary.lessons_created == summary.lessons_updated == 0
    assert summary.artifact_defs_created == summary.artifact_defs_updated == 0
    assert summary.edges_created == summary.edges_repointed == 0
    assert summary.rubrics_registered == 0
    assert len(list(session.scalars(select(Rubric)))) == 1  # append-only без дублей


def test_lesson_attribute_change_updates_in_place(session):
    _reconcile_yaml(session, YAML_V1)
    session.commit()
    summary = _reconcile_yaml(session, YAML_V2)

    lessons = store.find_all_lessons(session)
    assert lessons[0].title == "Документация требований (PRD)"
    assert len(lessons) == 2  # обновление, не дубль
    assert summary.lessons_created == 0
    assert summary.lessons_updated == 1


# ── AC 3: repoint рубрики, старые вердикты нетронуты (ADR-005) ─────────────


def test_rubric_change_repoints_edge_and_keeps_old_verdicts(session):
    _reconcile_yaml(session, YAML_V1)
    session.commit()
    edge = session.scalar(select(EdgeDef))
    old_rubric_id = edge.rubric_id

    # исторический вердикт на старую четвёрку (сеем напрямую — ядро FR-5 не кодится)
    lessons = store.find_all_lessons(session)
    adef_prd = store.find_artifact_defs_by_lesson(session, lessons[0].id)[0]
    adef_dm = store.find_artifact_defs_by_lesson(session, lessons[1].id)[0]
    repo = store.register_repository(
        session, repo_url="https://github.com/s/x", git_host="GitHub"
    )
    run = store.register_sync_run(session, triggered_by="manual")
    session.flush()
    snap_a = store.register_snapshot(
        session, sync_run_id=run.id, repository_id=repo.id, artifact_def_id=adef_prd.id,
        status="found", content_hash="a" * 64,
    )
    snap_b = store.register_snapshot(
        session, sync_run_id=run.id, repository_id=repo.id, artifact_def_id=adef_dm.id,
        status="found", content_hash="b" * 64,
    )
    session.flush()
    store.register_verdict(
        session, edge_def_id=edge.id, source_snapshot_id=snap_a.id,
        target_snapshot_id=snap_b.id, source_content_hash="a" * 64,
        target_content_hash="b" * 64, rubric_id=old_rubric_id,
        llm_model="deepseek-v4-flash", verdict="ok", confidence="high",
    )
    session.commit()

    summary = _reconcile_yaml(session, YAML_V2)

    session.refresh(edge)
    new_rubric = session.get(Rubric, edge.rubric_id)
    assert edge.rubric_id != old_rubric_id  # repoint состоялся
    assert new_rubric.version == "1.1"
    assert session.get(Rubric, old_rubric_id) is not None  # старая версия не удалена
    old_verdict = store.find_verdict_by_quadruple(
        session, source_content_hash="a" * 64, target_content_hash="b" * 64,
        rubric_id=old_rubric_id, llm_model="deepseek-v4-flash",
    )
    assert old_verdict is not None  # старый вердикт не потерян и не пересчитан
    assert len(list(session.scalars(select(CoherenceVerdict)))) == 1
    assert summary.edges_repointed == 1
    assert summary.rubrics_registered == 1


# ── AC 5: сводка требует прогона golden set при смене рубрики ──────────────


def test_rubric_change_flags_golden_set_run(session):
    _reconcile_yaml(session, YAML_V1)
    session.commit()
    summary = _reconcile_yaml(session, YAML_V2)
    assert summary.golden_set_required is True
    assert "prd→data_model" in summary.rubric_changes

    session.commit()
    summary_again = _reconcile_yaml(session, YAML_V2)
    assert summary_again.golden_set_required is False


# ── AC 4: ограничитель ADR-005 — единственный вызывающий ───────────────────


def test_config_reconciliation_imported_only_by_config_manager():
    """Функции config_* из store.py не упоминает никто, кроме config_manager и самого store."""
    app_dir = Path(__file__).parent.parent / "app"
    offenders = []
    for py_file in app_dir.rglob("*.py"):
        if py_file.name == "config_manager.py" or py_file.name == "store.py":
            continue
        text = py_file.read_text(encoding="utf-8")
        if any(name in text for name in (
            "config_upsert_lesson", "config_upsert_artifact_def",
            "config_create_edge_def", "config_repoint_edge_rubric",
        )):
            offenders.append(str(py_file))
    assert offenders == []


# ── AC 1: POST /admin/reload-config и старт приложения ─────────────────────


@pytest.fixture()
def client_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CD_ADMIN_PASSWORD", "pw")
    db_path = tmp_path / "test.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(YAML_V1, encoding="utf-8")
    monkeypatch.setattr(settings, "config_yaml_path", str(yaml_path))

    def override_session():
        with Session(engine) as s:
            yield s
            s.commit()

    app.dependency_overrides[get_session] = override_session
    monkeypatch.setattr(store, "SessionLocal", sessionmaker(bind=engine))
    yield TestClient(app), engine
    app.dependency_overrides.clear()
    engine.dispose()


def test_reload_config_route_requires_auth(client_env):
    client, _ = client_env
    assert client.post("/admin/reload-config").status_code == 401


def test_reload_config_route_reconciles(client_env):
    client, engine = client_env
    client.post("/login", data={"username": "admin", "password": "pw"})
    response = client.post("/admin/reload-config")
    assert response.status_code == 200
    body = response.json()
    assert body["lessons_created"] == 2
    with Session(engine) as s:
        assert len(store.find_all_lessons(s)) == 2


def test_startup_reads_config(client_env):
    """AC: «При старте config_manager читает YAML» — lifespan приложения."""
    client, engine = client_env
    with client:  # контекст-менеджер триггерит lifespan
        pass
    with Session(engine) as s:
        assert [lesson.number for lesson in store.find_all_lessons(s)] == [5, 6]


def test_startup_without_config_fails_fast(client_env, monkeypatch):
    """Отсутствие config.yaml — ошибка старта, а не молчаливый пропуск (§3.4: источник правды)."""
    client, _ = client_env
    monkeypatch.setattr(settings, "config_yaml_path", "no/such/config.yaml")
    with pytest.raises(FileNotFoundError):
        with client:
            pass
