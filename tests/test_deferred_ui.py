"""D6 (#37): deferred и вердикты в UI + копилка мелочей ревью ядра/R/T2.

Карточка различает три состояния пары: «проверяется» (вердикта нет),
«отложено: LLM недоступна», «отложено: ответ не распарсился» — раньше все три
сливались в pending. Плюс: жизненный цикл LLMClient (клиент на пару, закрывается),
валидация элементов points на границе, probe_findings без json-null в БД.
"""

import asyncio
from datetime import datetime

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from alembic import command
from app import store
from app.models import GitHost, SnapshotStatus, SyncTrigger, VerdictValue
from app.models.artifact_def import ArtifactDef
from app.models.lesson import Lesson
from app.services.evidence_chain import edge_states

LLM_MODEL = "deepseek-v4-flash"


@pytest.fixture()
def session(tmp_path):
    db_path = tmp_path / "d6.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as s:
        yield s
    engine.dispose()


def _seed_edge_pair(session):
    lesson5 = Lesson(number=5, title="PRD", date=datetime(2026, 6, 30).date())
    lesson6 = Lesson(number=6, title="Данные", date=datetime(2026, 7, 2).date())
    session.add_all([lesson5, lesson6])
    session.flush()
    adef_prd = ArtifactDef(lesson_id=lesson5.id, role="prd", expected_pattern="product/prd.md")
    adef_dm = ArtifactDef(lesson_id=lesson6.id, role="data_model", expected_pattern="data-model.md")
    session.add_all([adef_prd, adef_dm])
    rubric = store.register_rubric(session, type="edge", version="1.0", text="правило")
    session.flush()
    edge = store.config_create_edge_def(
        session, source_role="prd", target_role="data_model", rubric_id=rubric.id
    )
    repo = store.register_repository(
        session, repo_url="https://github.com/s/x", git_host=GitHost.GitHub
    )
    run = store.register_sync_run(session, triggered_by=SyncTrigger.manual)
    session.flush()
    snaps = {}
    for adef, ch, path in ((adef_prd, "a", "product/prd.md"), (adef_dm, "b", "data-model.md")):
        snaps[adef.role] = store.register_snapshot(
            session, sync_run_id=run.id, repository_id=repo.id, artifact_def_id=adef.id,
            status=SnapshotStatus.found, content_hash=ch * 64,
            file_path=path, source_commit_sha="c" * 40,
        )
    session.flush()
    return edge, repo, rubric, snaps


def _register_deferred(session, edge, rubric, snaps, reason):
    store.register_verdict(
        session,
        edge_def_id=edge.id,
        source_snapshot_id=snaps["prd"].id,
        target_snapshot_id=snaps["data_model"].id,
        source_content_hash="a" * 64,
        target_content_hash="b" * 64,
        rubric_id=rubric.id,
        llm_model=LLM_MODEL,
        verdict=VerdictValue.deferred,
        deferred_reason=reason,
        confidence="low",
    )
    session.flush()


# ── состояния ребра в карточке ──────────────────────────────────────────────


def test_edge_without_verdict_is_pending(session):
    edge, repo, rubric, snaps = _seed_edge_pair(session)
    (card,) = edge_states(session, repo.id, LLM_MODEL)
    assert card["state"] == "pending"
    assert card.get("deferred_reason") is None


def test_edge_with_llm_unavailable_shows_deferred_reason(session):
    edge, repo, rubric, snaps = _seed_edge_pair(session)
    _register_deferred(session, edge, rubric, snaps, "llm_unavailable")
    (card,) = edge_states(session, repo.id, LLM_MODEL)
    assert card["state"] == "deferred"
    assert card["deferred_reason"] == "llm_unavailable"


def test_edge_with_parse_error_shows_deferred_reason(session):
    edge, repo, rubric, snaps = _seed_edge_pair(session)
    _register_deferred(session, edge, rubric, snaps, "parse_error")
    (card,) = edge_states(session, repo.id, LLM_MODEL)
    assert card["state"] == "deferred"
    assert card["deferred_reason"] == "parse_error"


def test_student_card_template_renders_deferred_reason(tmp_path, monkeypatch):
    """Шаблон карточки показывает человекочитаемую причину deferred."""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.routes import get_session

    monkeypatch.setenv("CD_ADMIN_PASSWORD", "pw")  # ДО миграции: сид админа читает env
    db_path = tmp_path / "render.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    session = Session(engine)
    edge, repo, rubric, snaps = _seed_edge_pair(session)
    _register_deferred(session, edge, rubric, snaps, "llm_unavailable")
    repo_id = repo.id  # до закрытия сессии (detached instance)
    session.commit()
    session.close()

    def override_session():
        with Session(engine) as s:
            yield s
            s.commit()

    app.dependency_overrides[get_session] = override_session
    try:
        client = TestClient(app)
        client.post("/login", data={"username": "admin", "password": "pw"})
        html = client.get(f"/students/{repo_id}").text
        assert "LLM недоступна" in html
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


# ── копилка: жизненный цикл LLMClient — клиент на пару, закрывается ─────────


def test_worker_closes_llm_client_per_pair(session):
    from app.services.coherence_analyzer import make_verdict_worker
    from app.services.sync_orchestrator import PendingPair

    edge, repo, rubric, snaps = _seed_edge_pair(session)
    session.commit()
    engine = session.get_bind()

    created, closed = [], []

    class FakeLLM:
        def __init__(self):
            created.append(self)

        async def check_coherence(self, source_text, target_text, rubric_text, model=None):
            return {
                "verdict": "ok", "confidence": "high",
                "entities_checked": 1, "entities_found": 1,
                "entities_excluded": 0, "entities_lost": 0,
                "points": [], "notes": "",
            }

        async def aclose(self):
            closed.append(self)

    class FakeGit:
        async def get_file_content(self, repo_url, git_host, file_path, ref="main"):
            return "текст"

    pair = PendingPair(
        edge_def_id=edge.id, repository_id=repo.id,
        source_snapshot_id=snaps["prd"].id, target_snapshot_id=snaps["data_model"].id,
        source_content_hash="a" * 64, target_content_hash="b" * 64,
        rubric_id=rubric.id, llm_model=LLM_MODEL,
    )
    worker = make_verdict_worker(lambda: Session(engine), FakeGit(), FakeLLM)
    asyncio.run(worker(pair))

    assert len(created) == 1
    assert closed == created  # клиент пары закрыт после обработки


# ── копилка: валидация элементов points на границе ──────────────────────────


def test_validate_rejects_point_without_entity():
    import json

    from app.clients.llm_client import validate_llm_response

    data = {
        "verdict": "break", "confidence": "high",
        "entities_checked": 1, "entities_found": 0,
        "entities_excluded": 0, "entities_lost": 1,
        "points": [{"quote": "цитата", "why": "не нашёл"}],  # нет entity
        "notes": "",
    }
    assert validate_llm_response(json.dumps(data, ensure_ascii=False)) is None


def test_validate_rejects_non_dict_point():
    import json

    from app.clients.llm_client import validate_llm_response

    data = {
        "verdict": "break", "confidence": "high",
        "entities_checked": 1, "entities_found": 0,
        "entities_excluded": 0, "entities_lost": 1,
        "points": ["строка вместо объекта"],
        "notes": "",
    }
    assert validate_llm_response(json.dumps(data, ensure_ascii=False)) is None


# ── копилка: probe_findings — SQL NULL, а не json-null ──────────────────────


def test_probe_findings_absent_is_sql_null(session):
    _seed_edge_pair(session)
    session.commit()
    raw = session.execute(
        text("SELECT COUNT(*) FROM artifact_snapshot WHERE probe_findings IS NULL")
    ).scalar()
    total = session.execute(text("SELECT COUNT(*) FROM artifact_snapshot")).scalar()
    assert raw == total  # без проб колонка — настоящий SQL NULL
