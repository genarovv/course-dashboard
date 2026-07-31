"""D24 (итерация 5): хронология защиты — «перемещён» вместо флаппинга + MR-события.

Спека: plans/доводки-ux-5-2026-07-31.md §D24 (решение CEO «на все да»).
AC: пара «исчез»+«появился» одной роли в одном обходе схлопывается в
«перемещён: старый → новый путь»; MR-события с датами хостинга встроены
в общую ленту (независимые даты против «я сдавал раньше»).
Негативные: «исчез» без «появился» — остаётся; MR без даты — не в ленте.
"""

from datetime import datetime

import pytest
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from alembic import command
from app import store
from app.models import GitHost, SnapshotStatus, SyncOutcome, SyncTrigger
from app.models.artifact_def import ArtifactDef
from app.models.lesson import Lesson
from app.services.evidence_chain import build_defense_card

LLM_MODEL = "deepseek-v4-flash"


@pytest.fixture(autouse=True)
def _utc_display(monkeypatch):
    from app.config import settings as cfg

    monkeypatch.setattr(cfg, "tz_offset_minutes", 0)


@pytest.fixture()
def session(tmp_path, monkeypatch):
    monkeypatch.setenv("CD_ADMIN_PASSWORD", "pw")
    db_path = tmp_path / "test.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as s:
        yield s
    engine.dispose()


def _seed(s, *, second_alt_appears=True):
    lesson = Lesson(number=3, title="Интервью", date=datetime(2026, 6, 23).date())
    s.add(lesson)
    s.flush()
    alt1 = ArtifactDef(lesson_id=lesson.id, role="interview",
                       expected_pattern="product/interviews/*.md")
    alt2 = ArtifactDef(lesson_id=lesson.id, role="interview", expected_pattern="DISCOVERY.md")
    s.add_all([alt1, alt2])
    repo = store.register_repository(s, repo_url="https://github.com/s/x", git_host=GitHost.GitHub)
    r1 = store.register_sync_run(s, triggered_by=SyncTrigger.schedule)
    s.flush()
    store.register_snapshot(
        s, sync_run_id=r1.id, repository_id=repo.id, artifact_def_id=alt1.id,
        status=SnapshotStatus.found, content_hash="a" * 64,
        file_path="product/interviews/i1.md", source_commit_sha="c" * 40,
        observed_at=datetime(2026, 7, 1, 9, 0),
    )
    r2 = store.register_sync_run(s, triggered_by=SyncTrigger.schedule)
    s.flush()
    store.register_snapshot(
        s, sync_run_id=r2.id, repository_id=repo.id, artifact_def_id=alt1.id,
        status=SnapshotStatus.not_found, observed_at=datetime(2026, 7, 5, 9, 0),
    )
    if second_alt_appears:
        store.register_snapshot(
            s, sync_run_id=r2.id, repository_id=repo.id, artifact_def_id=alt2.id,
            status=SnapshotStatus.found, content_hash="a" * 64,
            file_path="DISCOVERY.md", source_commit_sha="d" * 40,
            observed_at=datetime(2026, 7, 5, 9, 0, 30),
        )
    store.register_sync_outcome(
        s, sync_run_id=r2.id, repository_id=repo.id, outcome=SyncOutcome.ok_changed
    )
    s.flush()
    return repo, r2


def _transitions(card):
    return [e for e in card["events"] if e["kind"] == "transition"]


def test_moved_pair_collapsed(session):
    repo, _ = _seed(session)
    session.commit()
    card = build_defense_card(session, repo.id, llm_model=LLM_MODEL)
    labels = [t["label"] for t in _transitions(card) if str(t["role"]) == "interview"]
    assert "исчез" not in labels and labels.count("появился") == 1  # только первый run
    moved = [t for t in _transitions(card) if t["label"] == "перемещён"]
    assert len(moved) == 1
    assert moved[0]["from_path"] == "product/interviews/i1.md"
    assert moved[0]["file_path"] == "DISCOVERY.md"


def test_moved_pair_collapsed_reversed_order(session):
    """Ревью итерации 5, находка 7: «появился» отсканирован раньше «исчез»
    того же обхода — пара всё равно схлопывается в «перемещён».
    Сеется напрямую в обратном порядке (журнал append-only, UPDATE запрещён И5)."""
    lesson = Lesson(number=3, title="Интервью", date=datetime(2026, 6, 23).date())
    session.add(lesson)
    session.flush()
    alt1 = ArtifactDef(lesson_id=lesson.id, role="interview",
                       expected_pattern="product/interviews/*.md")
    alt2 = ArtifactDef(lesson_id=lesson.id, role="interview", expected_pattern="DISCOVERY.md")
    session.add_all([alt1, alt2])
    repo = store.register_repository(
        session, repo_url="https://github.com/s/y", git_host=GitHost.GitHub
    )
    r1 = store.register_sync_run(session, triggered_by=SyncTrigger.schedule)
    session.flush()
    store.register_snapshot(
        session, sync_run_id=r1.id, repository_id=repo.id, artifact_def_id=alt1.id,
        status=SnapshotStatus.found, content_hash="a" * 64,
        file_path="product/interviews/i1.md", source_commit_sha="c" * 40,
        observed_at=datetime(2026, 7, 1, 9, 0),
    )
    r2 = store.register_sync_run(session, triggered_by=SyncTrigger.schedule)
    session.flush()
    # обратный порядок: сперва «появился» у alt2, потом «исчез» у alt1
    store.register_snapshot(
        session, sync_run_id=r2.id, repository_id=repo.id, artifact_def_id=alt2.id,
        status=SnapshotStatus.found, content_hash="a" * 64,
        file_path="DISCOVERY.md", source_commit_sha="d" * 40,
        observed_at=datetime(2026, 7, 5, 9, 0),
    )
    store.register_snapshot(
        session, sync_run_id=r2.id, repository_id=repo.id, artifact_def_id=alt1.id,
        status=SnapshotStatus.not_found, observed_at=datetime(2026, 7, 5, 9, 0, 30),
    )
    store.register_sync_outcome(
        session, sync_run_id=r2.id, repository_id=repo.id, outcome=SyncOutcome.ok_changed
    )
    session.commit()
    card = build_defense_card(session, repo.id, llm_model=LLM_MODEL)
    labels = [t["label"] for t in _transitions(card)]
    assert "исчез" not in labels
    assert labels.count("перемещён") == 1


def test_real_disappear_kept(session):
    repo, _ = _seed(session, second_alt_appears=False)
    session.commit()
    card = build_defense_card(session, repo.id, llm_model=LLM_MODEL)
    labels = [t["label"] for t in _transitions(card)]
    assert "исчез" in labels and "перемещён" not in labels


def test_mr_events_in_chronology(session):
    repo, run = _seed(session)
    store.register_mr_observation(
        session, sync_run_id=run.id, repository_id=repo.id, mr_number=42,
        title="итерация", source_branch="feat/x", state="merged",
        reviewer_approved=True, markers=None, head_sha="a" * 40,
        updated_at=datetime(2026, 7, 21, 8, 0),
    )
    store.register_mr_observation(
        session, sync_run_id=run.id, repository_id=repo.id, mr_number=43,
        title="без даты", source_branch="feat/y", state="opened",
        reviewer_approved=False, markers=None, head_sha="b" * 40,
        updated_at=None,
    )
    session.commit()
    card = build_defense_card(session, repo.id, llm_model=LLM_MODEL)
    mr_events = [e for e in card["events"] if e["kind"] == "mr"]
    assert len(mr_events) == 1  # MR без даты хостинга в ленту не попадает
    assert mr_events[0]["number"] == 42 and mr_events[0]["when"] == datetime(2026, 7, 21, 8, 0)
    whens = [e["when"] for e in card["events"]]
    assert whens == sorted(whens)  # MR-событие отсортировано в общей ленте
