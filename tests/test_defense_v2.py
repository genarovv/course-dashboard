"""D19 (#65): дело защиты v2 — доказательная хронология и русская витрина.

Спека: plans/доводки-ux-2026-07-31.md (решения CEO по UX-фокус-группам 2026-07-31).
AC:
  1. Шапка: «Защита: <короткое имя репо>», полный URL мелко, дата открытия режима.
  2. notes — основной текст карточки разрыва; файловая детализация точек — по <details>.
  3. Хронология = переходы состояний (появился / статус изменился / исчез) + вехи
     занятий; дата коммита `source_commit_date` (миграция + сбор в обходе), для старых
     снапшотов NULL → дата наблюдения с пометкой «зафиксировано обходом»;
     время ДД.ММ ЧЧ:ММ без микросекунд; полный лог — по раскрытию.
  4. Свёрнутые секции: MR-история и погашенные разрывы («снят преподавателем ДД.ММ»).
  5. Кнопок FR-10 нет, данных группы нет — контракт D18 (test_defense_mode.py) не ослаблен.
Негативные: нет MR-наблюдений — секции нет; нет погашенных — секции нет;
API не отдал дату коммита — NULL, обход не падает.
"""

import re
from datetime import datetime

import httpx
import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from alembic import command
from app import store
from app.clients.git_client import GitClient, GitRepoUnavailableError
from app.main import app
from app.models import GitHost, SnapshotStatus, SyncOutcome, SyncTrigger
from app.models.artifact_def import ArtifactDef
from app.models.lesson import Lesson
from app.routes import get_session
from app.services.evidence_chain import build_defense_card
from app.services.sync_orchestrator import run_sync

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


def _seed_roles(s):
    lessons = [
        Lesson(number=5, title="PRD", date=datetime(2026, 6, 30).date()),
        Lesson(number=6, title="Данные", date=datetime(2026, 7, 2).date()),
        Lesson(number=7, title="Архитектура", date=datetime(2026, 7, 7).date()),
    ]
    s.add_all(lessons)
    s.flush()
    adefs = {}
    for role, lesson, pattern in (
        ("prd", lessons[0], "product/prd.md"),
        ("data_model", lessons[1], "data-model.md"),
        ("architecture", lessons[2], "ARCHITECTURE.md"),
    ):
        adefs[role] = ArtifactDef(lesson_id=lesson.id, role=role, expected_pattern=pattern)
    s.add_all(adefs.values())
    repo = store.register_repository(s, repo_url="https://github.com/s/x", git_host=GitHost.GitHub)
    s.flush()
    return adefs, repo


def _run(s, when):
    run = store.register_sync_run(s, triggered_by=SyncTrigger.schedule)
    run.started_at = when
    s.flush()
    return run


def _snap(s, run, repo, adef, status, h, when, commit_date=None, reason=None):
    fields = dict(
        sync_run_id=run.id, repository_id=repo.id, artifact_def_id=adef.id,
        status=status, observed_at=when,
    )
    if status != SnapshotStatus.not_found:
        fields.update(
            content_hash=h * 64, file_path=adef.expected_pattern,
            source_commit_sha=(h * 40)[:40],
        )
    if commit_date is not None:
        fields["source_commit_date"] = commit_date
    if reason is not None:
        fields["partial_reason"] = reason
    return store.register_snapshot(s, **fields)


def _seed_transitions(s):
    """prd: появился (commit-дата есть) → смена контента (НЕ переход).
    data_model: появился (без даты коммита) → статус изменился → исчез.
    architecture: первый снапшот not_found — перехода нет вовсе."""
    adefs, repo = _seed_roles(s)
    r1 = _run(s, datetime(2026, 7, 1, 9, 0))
    _snap(s, r1, repo, adefs["prd"], SnapshotStatus.found, "a",
          datetime(2026, 7, 1, 9, 0), commit_date=datetime(2026, 6, 28, 10, 0))
    _snap(s, r1, repo, adefs["data_model"], SnapshotStatus.found, "b",
          datetime(2026, 7, 1, 9, 1))
    _snap(s, r1, repo, adefs["architecture"], SnapshotStatus.not_found, None,
          datetime(2026, 7, 1, 9, 2))
    r2 = _run(s, datetime(2026, 7, 5, 9, 0))
    _snap(s, r2, repo, adefs["prd"], SnapshotStatus.found, "c",
          datetime(2026, 7, 5, 9, 0), commit_date=datetime(2026, 7, 4, 18, 30))
    _snap(s, r2, repo, adefs["data_model"], SnapshotStatus.partial, "d",
          datetime(2026, 7, 5, 9, 1), reason=["template_copy"])
    r3 = _run(s, datetime(2026, 7, 10, 9, 0))
    _snap(s, r3, repo, adefs["data_model"], SnapshotStatus.not_found, None,
          datetime(2026, 7, 10, 9, 0))
    store.register_sync_outcome(
        s, sync_run_id=r3.id, repository_id=repo.id, outcome=SyncOutcome.ok_changed
    )
    s.flush()
    return adefs, repo


# ── source_commit_date: модель и миграция ─────────────────────────────────


def test_snapshot_accepts_and_defaults_source_commit_date(engine):
    with Session(engine) as s:
        adefs, repo = _seed_roles(s)
        r1 = _run(s, datetime(2026, 7, 1))
        with_date = _snap(s, r1, repo, adefs["prd"], SnapshotStatus.found, "a",
                          datetime(2026, 7, 1), commit_date=datetime(2026, 6, 28, 10, 0))
        without = _snap(s, r1, repo, adefs["data_model"], SnapshotStatus.found, "b",
                        datetime(2026, 7, 1))
        s.commit()
        assert with_date.source_commit_date == datetime(2026, 6, 28, 10, 0)
        assert without.source_commit_date is None  # старые снапшоты — NULL


# ── git_client: дата головного коммита ────────────────────────────────────


def _gc(handler) -> GitClient:
    return GitClient(http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


@pytest.mark.anyio
async def test_head_commit_date_github():
    def handler(request):
        assert "/commits/" in request.url.path
        return httpx.Response(200, json={
            "sha": "f" * 40,
            "commit": {"committer": {"date": "2026-07-28T10:00:00Z"}},
        })

    raw = await _gc(handler).get_head_commit_date("https://github.com/s/x", "GitHub", ref="main")
    assert raw == "2026-07-28T10:00:00Z"


@pytest.mark.anyio
async def test_head_commit_date_gitlab():
    def handler(request):
        return httpx.Response(200, json={
            "id": "f" * 40, "committed_date": "2026-07-28T14:00:00+04:00",
        })

    raw = await _gc(handler).get_head_commit_date("https://gitlab.com/s/x", "GitLab", ref="main")
    assert raw == "2026-07-28T14:00:00+04:00"


@pytest.mark.anyio
async def test_head_commit_date_missing_in_answer_is_none():
    def handler(request):
        return httpx.Response(200, json={"sha": "f" * 40, "commit": {}})

    raw = await _gc(handler).get_head_commit_date("https://github.com/s/x", "GitHub", ref="main")
    assert raw is None  # API не отдал дату — None, не падение


# ── обход собирает дату; сбой даты не валит обход ─────────────────────────


class FakeGit:
    """Фейк клиента для run_sync: дерево из одного prd + управляемая дата коммита."""

    def __init__(self, date_raw="2026-07-28T10:00:00Z", date_error=False):
        self.date_raw = date_raw
        self.date_error = date_error

    async def get_tree(self, repo_url, git_host, ref="main"):
        return ["product/prd.md"]

    async def get_file_content(self, repo_url, git_host, file_path, ref="main"):
        return "# PRD\nсодержимое"

    async def fetch_default_branch(self, repo_url, git_host):
        return "main"

    async def get_head_sha(self, repo_url, git_host, ref="main"):
        return "f" * 40

    async def get_head_commit_date(self, repo_url, git_host, ref="main"):
        if self.date_error:
            raise GitRepoUnavailableError("нет даты")
        return self.date_raw


@pytest.mark.anyio
async def test_sync_writes_source_commit_date(engine):
    with Session(engine) as s:
        adefs, repo = _seed_roles(s)
        s.commit()
        await run_sync(s, FakeGit(), triggered_by=SyncTrigger.manual)
        s.flush()
        snap = store.find_last_snapshot(s, repo.id, adefs["prd"].id)
        assert snap.source_commit_date == datetime(2026, 7, 28, 10, 0)  # naive UTC


@pytest.mark.anyio
async def test_sync_date_failure_gives_null_not_crash(engine):
    with Session(engine) as s:
        adefs, repo = _seed_roles(s)
        s.commit()
        run = await run_sync(s, FakeGit(date_error=True), triggered_by=SyncTrigger.manual)
        s.flush()
        snap = store.find_last_snapshot(s, repo.id, adefs["prd"].id)
        assert snap is not None and snap.source_commit_date is None
        assert run.status.value == "completed"  # сбой даты — не сбой обхода


# ── хронология переходов ──────────────────────────────────────────────────


def _transitions(card):
    return [e for e in card["events"] if e["kind"] == "transition"]


def test_transitions_collapse_to_state_changes(engine):
    with Session(engine) as s:
        _, repo = _seed_transitions(s)
        s.commit()
        card = build_defense_card(s, repo.id, llm_model=LLM_MODEL)
    rows = _transitions(card)
    by_role = {}
    for row in rows:
        by_role.setdefault(str(row["role"]), []).append(row["label"])
    assert by_role["prd"] == ["появился"]  # смена контента без смены статуса — не переход
    assert by_role["data_model"] == ["появился", "статус изменился", "исчез"]
    assert "architecture" not in by_role  # первый not_found — нечему появляться


def test_lesson_milestones_in_events_sorted(engine):
    with Session(engine) as s:
        _, repo = _seed_transitions(s)
        s.commit()
        card = build_defense_card(s, repo.id, llm_model=LLM_MODEL)
    events = card["events"]
    lessons = [e for e in events if e["kind"] == "lesson"]
    assert [e["number"] for e in lessons] == [5, 6, 7]
    whens = [e["when"] for e in events]
    assert whens == sorted(whens)  # вехи вперемешку с переходами, по времени


def test_transition_date_prefers_commit_date(engine):
    with Session(engine) as s:
        _, repo = _seed_transitions(s)
        s.commit()
        card = build_defense_card(s, repo.id, llm_model=LLM_MODEL)
    rows = {(str(r["role"]), r["label"]): r for r in _transitions(card)}
    prd = rows[("prd", "появился")]
    assert prd["when"] == datetime(2026, 6, 28, 10, 0) and not prd["by_observation"]
    dm = rows[("data_model", "появился")]
    assert dm["when"] == datetime(2026, 7, 1, 9, 1) and dm["by_observation"]


# ── HTML: шапка, notes крупно, details, секции, формат времени ────────────


def _seed_break_with_override(s, *, override=False, mr=False):
    adefs, repo = _seed_roles(s)
    r1 = _run(s, datetime(2026, 7, 1, 9, 0))
    snap_a = _snap(s, r1, repo, adefs["prd"], SnapshotStatus.found, "a",
                   datetime(2026, 7, 1, 9, 0))
    snap_b = _snap(s, r1, repo, adefs["data_model"], SnapshotStatus.found, "b",
                   datetime(2026, 7, 1, 9, 1))
    rubric = store.register_rubric(s, type="edge", version="1.0", text="правило")
    s.flush()
    edge = store.config_create_edge_def(
        s, source_role="prd", target_role="data_model", rubric_id=rubric.id
    )
    store.register_sync_outcome(
        s, sync_run_id=r1.id, repository_id=repo.id, outcome=SyncOutcome.ok_changed
    )
    s.flush()
    verdict = store.register_verdict(
        s, edge_def_id=edge.id,
        source_snapshot_id=snap_a.id, target_snapshot_id=snap_b.id,
        source_content_hash=snap_a.content_hash, target_content_hash=snap_b.content_hash,
        rubric_id=rubric.id, llm_model=LLM_MODEL, verdict="break", confidence="high",
        points=[{"entity": "Оксана", "quote": "цитата", "why": "не нашёл в B"}],
        notes="Потеряна сущность «Оксана» — в B нет ни упоминания, ни исключения.",
    )
    s.flush()
    if override:
        ovr = store.register_override(s, coherence_verdict_id=verdict.id, reason="синоним")
        ovr.created_at = datetime(2026, 7, 20, 12, 0)
    if mr:
        store.register_mr_observation(
            s, sync_run_id=r1.id, repository_id=repo.id, mr_number=42,
            title="итерация 1", source_branch="feat/iter1", state="merged",
            reviewer_approved=True, markers=None, head_sha="a" * 40,
            updated_at=datetime(2026, 7, 20, 15, 30, 45, 123456),
        )
    s.flush()
    return repo


def test_header_short_name_and_opened_at(client, engine):
    with Session(engine) as s:
        repo = _seed_break_with_override(s)
        s.commit()
        repo_id = repo.id
    _login(client)
    html = client.get(f"/students/{repo_id}/defense").text
    assert "Защита: x" in html  # короткое имя, не простыня URL
    assert "https://github.com/s/x" in html  # полный адрес — мелко, но есть
    assert "режим открыт" in html


def test_notes_are_main_text_points_in_details(client, engine):
    with Session(engine) as s:
        repo = _seed_break_with_override(s)
        s.commit()
        repo_id = repo.id
    _login(client)
    html = client.get(f"/students/{repo_id}/defense").text
    assert "Потеряна сущность «Оксана»" in html
    assert 'class="defense-notes"' in html  # notes — основной текст карточки
    assert "файловая детализация" in html  # точки — по раскрытию
    assert "<details" in html and "Оксана" in html


def test_muted_breaks_section(client, engine):
    with Session(engine) as s:
        repo = _seed_break_with_override(s, override=True)
        s.commit()
        repo_id = repo.id
    _login(client)
    html = client.get(f"/students/{repo_id}/defense").text
    assert "Погашенные разрывы" in html
    assert "снят преподавателем 20.07" in html
    assert "override-toggle" not in html  # по-прежнему без кнопок FR-10


def test_no_muted_breaks_no_section(client, engine):
    with Session(engine) as s:
        repo = _seed_break_with_override(s)
        s.commit()
        repo_id = repo.id
    _login(client)
    assert "Погашенные разрывы" not in client.get(f"/students/{repo_id}/defense").text


def test_mr_history_section(client, engine):
    with Session(engine) as s:
        repo = _seed_break_with_override(s, mr=True)
        s.commit()
        repo_id = repo.id
    _login(client)
    html = client.get(f"/students/{repo_id}/defense").text
    assert "merge requests" in html
    assert "feat/iter1" in html and "!42" in html
    assert "20.07 15:30" in html  # независимая дата хостинга, без микросекунд


def test_no_mrs_no_section(client, engine):
    with Session(engine) as s:
        repo = _seed_break_with_override(s)
        s.commit()
        repo_id = repo.id
    _login(client)
    assert "merge requests" not in client.get(f"/students/{repo_id}/defense").text


def test_defense_times_no_microseconds_no_iso(client, engine):
    with Session(engine) as s:
        repo = _seed_break_with_override(s, override=True, mr=True)
        s.commit()
        repo_id = repo.id
    _login(client)
    html = client.get(f"/students/{repo_id}/defense").text
    assert not re.search(r"\.\d{6}", html)  # микросекунд нет нигде
    assert not re.search(r"\d{4}-\d{2}-\d{2}T", html)  # сырых ISO-строк нет


def test_observation_flag_note(client, engine):
    with Session(engine) as s:
        _, repo = _seed_transitions(s)
        s.commit()
        repo_id = repo.id
    _login(client)
    html = client.get(f"/students/{repo_id}/defense").text
    assert "зафиксировано обходом" in html  # у переходов без даты коммита
    assert "занятие 5" in html  # вехи занятий в хронологии
    assert "полный лог" in html  # полный лог наблюдений — по раскрытию
