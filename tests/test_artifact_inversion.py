"""D13 (#57), J1/FR-5, риск PRD §11 (будни): инверсия сигнала + градация уверенности.

AC (спека §D13):
  1. Активный разрыв — структурированный чип в ячейке: сущность, счётчик, уверенность.
  2. Уверенность «высокая» — полный чип (chip-high); «средняя»/«низкая» —
     контурный (chip-medium/chip-low) с подписью уверенности словами ДО клика.
  3. Несколько разрывов разной уверенности — стиль по наивысшей, счётчик общий.
  4. Разрыв без точек — чип «разрыв» без сущности, не падение.
  5. Подложки статусов тихие, но статус остаётся словом (цвет — не единственный носитель).
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
    """Рёбра prd→data_model и prd→architecture (для двух разрывов на одной роли)."""
    lesson5 = Lesson(number=5, title="PRD", date=datetime(2026, 6, 30).date())
    lesson6 = Lesson(number=6, title="Данные", date=datetime(2026, 7, 2).date())
    lesson7 = Lesson(number=7, title="Архитектура", date=datetime(2026, 7, 7).date())
    s.add_all([lesson5, lesson6, lesson7])
    s.flush()
    adefs = {
        "prd": ArtifactDef(lesson_id=lesson5.id, role="prd", expected_pattern="product/prd.md"),
        "dm": ArtifactDef(lesson_id=lesson6.id, role="data_model", expected_pattern="data-model.md"),
        "arch": ArtifactDef(
            lesson_id=lesson7.id, role="architecture", expected_pattern="ARCHITECTURE.md"
        ),
    }
    s.add_all(adefs.values())
    r1 = store.register_rubric(s, type="edge", version="1.0", text="п1")
    r2 = store.register_rubric(s, type="edge", version="1.0", text="п2")
    s.flush()
    e1 = store.config_create_edge_def(
        s, source_role="prd", target_role="data_model", rubric_id=r1.id
    )
    e2 = store.config_create_edge_def(
        s, source_role="prd", target_role="architecture", rubric_id=r2.id
    )
    repo = store.register_repository(s, repo_url="https://github.com/s/x", git_host=GitHost.GitHub)
    run = store.register_sync_run(s, triggered_by=SyncTrigger.schedule)
    s.flush()
    snaps = {}
    for key, h, path in (("prd", "a", "product/prd.md"), ("dm", "b", "data-model.md"),
                         ("arch", "f", "ARCHITECTURE.md")):
        snaps[key] = store.register_snapshot(
            s, sync_run_id=run.id, repository_id=repo.id, artifact_def_id=adefs[key].id,
            status=SnapshotStatus.found, content_hash=h * 64, file_path=path,
            source_commit_sha="c" * 40,
        )
    store.register_sync_outcome(
        s, sync_run_id=run.id, repository_id=repo.id, outcome=SyncOutcome.ok_changed
    )
    s.flush()
    return repo, (e1, r1, snaps["prd"], snaps["dm"]), (e2, r2, snaps["prd"], snaps["arch"])


def _verdict(s, edge_pack, *, confidence="high", points=...):
    edge, rubric, snap_a, snap_b = edge_pack
    if points is ...:
        points = [{"entity": "Оксана", "quote": "цитата", "why": "не найдена"}]
    v = store.register_verdict(
        s, edge_def_id=edge.id, source_snapshot_id=snap_a.id, target_snapshot_id=snap_b.id,
        source_content_hash=snap_a.content_hash, target_content_hash=snap_b.content_hash,
        rubric_id=rubric.id, llm_model=LLM_MODEL, verdict="break", confidence=confidence,
        points=points,
    )
    s.flush()
    return v


def _cell(s, repo, role="prd"):
    return build_artifact_matrix(s, llm_model=LLM_MODEL)["cells"][repo.id][role]


# ── AC 1–2: структурированный чип с уверенностью ──────────────────────────


def test_break_chip_structure_low_confidence(engine):
    with Session(engine) as s:
        repo, pack1, _ = _seed(s)
        _verdict(s, pack1, confidence="low")
        s.commit()
        cell = _cell(s, repo)
    # поле new добавлено D16 (#60): один обход в сиде — метка свежести выключена
    assert cell["break"] == {"count": 1, "entity": "Оксана", "confidence": "low", "new": False}


def test_no_break_chip_when_clean(engine):
    with Session(engine) as s:
        repo, *_ = _seed(s)
        s.commit()
        cell = _cell(s, repo)
    assert cell["break"] is None


# ── AC 3: несколько разрывов — наивысшая уверенность, общий счётчик ───────


def test_multiple_breaks_highest_confidence(engine):
    with Session(engine) as s:
        repo, pack1, pack2 = _seed(s)
        _verdict(s, pack1, confidence="low")
        _verdict(s, pack2, confidence="high",
                 points=[{"entity": "жизненный цикл", "quote": "ц", "why": "нет"}])
        s.commit()
        cell = _cell(s, repo)
    assert cell["break"]["count"] == 2
    assert cell["break"]["confidence"] == "high"


# ── AC 4: разрыв без точек ────────────────────────────────────────────────


def test_break_without_points(engine):
    with Session(engine) as s:
        repo, pack1, _ = _seed(s)
        _verdict(s, pack1, points=None)
        s.commit()
        cell = _cell(s, repo)
    assert cell["break"]["entity"] is None
    assert "разрыв" in cell["summary"]


# ── AC 2/5, HTTP: чип и подпись уверенности видны до клика ────────────────


def test_page_chip_high_vs_low(client, engine):
    with Session(engine) as s:
        repo, pack1, _ = _seed(s)
        _verdict(s, pack1, confidence="low")
        s.commit()
    _login(client)
    html = client.get("/artifacts").text
    assert "break-chip" in html and "chip-low" in html
    assert "уверенность низкая" in html  # подпись словами до клика (риск §11)
    assert "есть" in html  # статус остаётся словом — цвет не единственный носитель
