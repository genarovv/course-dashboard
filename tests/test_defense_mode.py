"""D18 (#62), US-C2 (утверждена CEO 2026-07-31), FR-9, риск PRD §11: режим защиты.

AC US-C2:
  1. Полноэкранное дело одного студента — без данных группы.
  2. Показаны только разрывы с уверенностью «высокая» и не погашенные;
     остальные отсутствуют (не «серые»).
  3. Кнопок «ложный разрыв» нет; у каждого вердикта — дата вычисления
     и коммиты обеих сторон.
  4. Хронология наблюдений с датами и коммитами — в том же экране.
  5. Выход из режима явный.
Негативные: нет уверенных разрывов — «разрывов для показа нет»;
репозиторий в слепой зоне — сообщение, не падение.
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
    """Три ребра: разрыв high (показывается), разрыв low (скрыт), связное ok."""
    lessons = [
        Lesson(number=5, title="PRD", date=datetime(2026, 6, 30).date()),
        Lesson(number=6, title="Данные", date=datetime(2026, 7, 2).date()),
        Lesson(number=7, title="Архитектура", date=datetime(2026, 7, 7).date()),
        Lesson(number=2, title="JTBD", date=datetime(2026, 6, 10).date()),
    ]
    s.add_all(lessons)
    s.flush()
    adefs = {}
    for role, lesson, pattern in (
        ("prd", lessons[0], "product/prd.md"),
        ("data_model", lessons[1], "data-model.md"),
        ("architecture", lessons[2], "ARCHITECTURE.md"),
        ("jtbd", lessons[3], "product/jtbd.md"),
    ):
        adefs[role] = ArtifactDef(lesson_id=lesson.id, role=role, expected_pattern=pattern)
    s.add_all(adefs.values())
    rubrics = {n: store.register_rubric(s, type="edge", version="1.0", text=f"п{n}") for n in (1, 2, 3)}
    s.flush()
    edges = {
        "high": store.config_create_edge_def(
            s, source_role="prd", target_role="data_model", rubric_id=rubrics[1].id
        ),
        "low": store.config_create_edge_def(
            s, source_role="prd", target_role="architecture", rubric_id=rubrics[2].id
        ),
        "ok": store.config_create_edge_def(
            s, source_role="jtbd", target_role="prd", rubric_id=rubrics[3].id
        ),
    }
    repo = store.register_repository(s, repo_url="https://github.com/s/x", git_host=GitHost.GitHub)
    run = store.register_sync_run(s, triggered_by=SyncTrigger.schedule)
    s.flush()
    snaps = {}
    for role, h in (("prd", "a"), ("data_model", "b"), ("architecture", "f"), ("jtbd", "j")):
        snaps[role] = store.register_snapshot(
            s, sync_run_id=run.id, repository_id=repo.id, artifact_def_id=adefs[role].id,
            status=SnapshotStatus.found, content_hash=h * 64,
            file_path=adefs[role].expected_pattern, source_commit_sha=(h * 40)[:40],
        )
    store.register_sync_outcome(
        s, sync_run_id=run.id, repository_id=repo.id, outcome=SyncOutcome.ok_changed
    )
    s.flush()
    verdicts = {}
    for key, (src, dst, val, conf) in {
        "high": ("prd", "data_model", "break", "high"),
        "low": ("prd", "architecture", "break", "low"),
        "ok": ("jtbd", "prd", "ok", "high"),
    }.items():
        verdicts[key] = store.register_verdict(
            s, edge_def_id=edges[key].id,
            source_snapshot_id=snaps[src].id, target_snapshot_id=snaps[dst].id,
            source_content_hash=snaps[src].content_hash, target_content_hash=snaps[dst].content_hash,
            rubric_id=edges[key].rubric_id, llm_model=LLM_MODEL, verdict=val, confidence=conf,
            points=[{"entity": "Оксана", "quote": "цитата", "why": "нет"}]
            if val == "break" else None,
        )
    s.flush()
    return repo, verdicts


# ── AC 2: фильтр уверенности и отметок ────────────────────────────────────


def test_only_high_active_breaks_shown(engine):
    with Session(engine) as s:
        repo, _ = _seed(s)
        s.commit()
        card = build_defense_card(s, repo.id, llm_model=LLM_MODEL)
    shown = {(str(e["source_role"]), str(e["target_role"])) for e in card["sure_breaks"]}
    assert shown == {("prd", "data_model")}  # low скрыт полностью
    assert {(str(e["source_role"]), str(e["target_role"])) for e in card["ok_edges"]} == {
        ("jtbd", "prd")
    }


def test_overridden_high_break_hidden(engine):
    with Session(engine) as s:
        repo, verdicts = _seed(s)
        store.register_override(s, coherence_verdict_id=verdicts["high"].id, reason="синоним")
        s.commit()
        card = build_defense_card(s, repo.id, llm_model=LLM_MODEL)
    assert card["sure_breaks"] == []


# ── AC 3: дата и коммиты обеих сторон у вердикта ──────────────────────────


def test_break_carries_dates_and_shas(engine):
    with Session(engine) as s:
        repo, _ = _seed(s)
        s.commit()
        (brk,) = build_defense_card(s, repo.id, llm_model=LLM_MODEL)["sure_breaks"]
    assert brk["computed_at"] is not None
    assert brk["source_file"] == "product/prd.md" and brk["source_sha"].startswith("a")
    assert brk["target_file"] == "data-model.md" and brk["target_sha"].startswith("b")


# ── негативные ────────────────────────────────────────────────────────────


def test_no_sure_breaks_message(client, engine):
    with Session(engine) as s:
        repo, verdicts = _seed(s)
        store.register_override(s, coherence_verdict_id=verdicts["high"].id, reason="синоним")
        s.commit()
        repo_id = repo.id
    _login(client)
    html = client.get(f"/students/{repo_id}/defense").text
    assert "разрывов для показа нет" in html


def test_blind_spot_note(client, engine):
    with Session(engine) as s:
        repo, _ = _seed(s)
        run2 = store.register_sync_run(s, triggered_by=SyncTrigger.schedule)
        s.flush()
        store.register_sync_outcome(
            s, sync_run_id=run2.id, repository_id=repo.id,
            outcome=SyncOutcome.repo_unavailable, detail="HTTP 404",
        )
        s.commit()
        repo_id = repo.id
    _login(client)
    response = client.get(f"/students/{repo_id}/defense")
    assert response.status_code == 200  # не падение
    assert "слепой зоне" in response.text


# ── AC 1/3/5, HTTP ────────────────────────────────────────────────────────


def test_defense_page_no_group_no_override_buttons(client, engine):
    with Session(engine) as s:
        repo, _ = _seed(s)
        s.commit()
        repo_id = repo.id
    _login(client)
    html = client.get(f"/students/{repo_id}/defense").text
    assert "Режим защиты" in html
    assert "override-toggle" not in html  # AC 3: курирования при комиссии нет
    assert "Оксана" in html and "цитата" in html  # точки с цитатами
    assert "Хронология" in html  # AC 4
    assert "выйти из режима" in html.lower()  # AC 5
    assert "/lessons" not in html and 'href="/"' not in html  # AC 1: без навигации в группу


def test_defense_requires_auth_and_404(client, engine):
    with Session(engine) as s:
        repo, _ = _seed(s)
        s.commit()
        repo_id = repo.id
    assert client.get(f"/students/{repo_id}/defense", follow_redirects=False).status_code == 303
    _login(client)
    assert client.get("/students/no-such/defense").status_code == 404


def test_entry_links_in_matrix_and_card(client, engine):
    with Session(engine) as s:
        repo, _ = _seed(s)
        s.commit()
        repo_id = repo.id
    _login(client)
    assert f"/students/{repo_id}/defense" in client.get("/artifacts").text
    assert f"/students/{repo_id}/defense" in client.get(f"/students/{repo_id}").text
