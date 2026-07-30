"""#40 (FR-12): сбор MR-наблюдений в обходе + process_markers + вердикт ревьюера.

Негативные сценарии — ADR-007: отрицание в тексте вердикта не считается принятием;
ошибка MR-чтения не роняет обход (NFR-2); state merged/closed фиксируются как есть.
"""


import pytest
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from alembic import command
from app import store
from app.clients.git_client import GitRepoUnavailableError, MrInfo
from app.models import GitHost, SyncStatus, SyncTrigger
from app.services.config_manager import ProcessMarkerConfig
from app.services.sync_orchestrator import run_sync

MARKERS = [
    ProcessMarkerConfig(key="prichina", pattern=r"(?im)^причина:"),
    ProcessMarkerConfig(key="mutation", pattern=r"(?im)^мутация:"),
]


class FakeGit:
    def __init__(self, mrs=None, notes=None, mr_error=None):
        self.mrs = mrs or []
        self.notes = notes or {}
        self.mr_error = mr_error
        self.notes_calls = []

    async def get_tree(self, repo_url, git_host, ref="main"):
        return []

    async def get_file_content(self, repo_url, git_host, file_path, ref="main"):
        return ""

    async def get_head_sha(self, repo_url, git_host, ref="main"):
        return "a" * 40

    async def fetch_default_branch(self, repo_url, git_host):
        return "main"

    async def list_merge_requests(self, repo_url, git_host):
        if self.mr_error:
            raise self.mr_error
        return self.mrs

    async def list_mr_notes(self, repo_url, git_host, number):
        from app.clients.git_client import NoteInfo

        self.notes_calls.append(number)
        return [
            note if isinstance(note, NoteInfo)
            else NoteInfo(body=note, created_at="2026-07-28T09:00:00Z")
            for note in self.notes.get(number, [])
        ]


def _mr(number, state="opened", description="", branch="S5-auth"):
    return MrInfo(
        number=number, title=f"MR {number}", source_branch=branch, state=state,
        head_sha="b" * 40, updated_at="2026-07-28T10:00:00Z", description=description,
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


def _repo(s):
    repo = store.register_repository(s, repo_url="https://github.com/s/x", git_host=GitHost.GitHub)
    s.flush()
    return repo


async def _sync(s, client):
    run = await run_sync(
        s, client, triggered_by=SyncTrigger.manual, process_markers=MARKERS
    )
    s.flush()
    return run


@pytest.mark.anyio
async def test_mrs_observed_with_markers_and_quote(session):
    repo = _repo(session)
    client = FakeGit(mrs=[_mr(7, description="Итог.\nПричина: гонка транзакций.\nФикс готов.")])

    run = await _sync(session, client)

    (row,) = store.find_mr_observations(session, repo.id, run.id)
    assert row.mr_number == 7
    assert row.state == "opened"
    assert row.markers["prichina"]["found"] is True
    assert "гонка транзакций" in row.markers["prichina"]["quote"]
    assert row.markers["mutation"]["found"] is False


@pytest.mark.anyio
async def test_reviewer_approved_from_notes(session):
    repo = _repo(session)
    client = FakeGit(
        mrs=[_mr(7)], notes={7: ["REWORK: 2 находки", "Принято"]}
    )

    run = await _sync(session, client)

    (row,) = store.find_mr_observations(session, repo.id, run.id)
    assert row.reviewer_approved is True


@pytest.mark.anyio
async def test_negated_verdict_not_approved(session):
    """ADR-007: «пока не принято» — не принятие."""
    repo = _repo(session)
    client = FakeGit(mrs=[_mr(7)], notes={7: ["пока не принято", "не принято, доработать"]})

    run = await _sync(session, client)

    (row,) = store.find_mr_observations(session, repo.id, run.id)
    assert row.reviewer_approved is False


@pytest.mark.anyio
async def test_notes_fetched_for_opened_and_merged_not_closed(session):
    """Порядок 2026-07-29 (мержат сами): вердикт нужен и у merged; closed не читаем.

    Требование изменено решением CEO 2026-07-29 — ранее notes читались только у opened.
    """
    _repo(session)
    client = FakeGit(mrs=[_mr(7, state="opened"), _mr(6, state="merged"), _mr(5, state="closed")])

    await _sync(session, client)

    assert sorted(client.notes_calls) == [6, 7]


@pytest.mark.anyio
async def test_approved_inherited_without_refetch(session):
    """Экономия API: вердикт «принято» при неизменной голове наследуется без повторного чтения."""
    repo = _repo(session)
    client = FakeGit(mrs=[_mr(6, state="merged")], notes={6: ["принято"]})
    await _sync(session, client)
    calls_after_first = len(client.notes_calls)

    run2 = await _sync(session, client)

    assert len(client.notes_calls) == calls_after_first  # второй обход нот не читал
    (row,) = store.find_mr_observations(session, repo.id, run2.id)
    assert row.reviewer_approved is True


@pytest.mark.anyio
async def test_mr_step_error_does_not_crash_sync(session):
    """NFR-2: недоступность MR API — warning, обход завершается, артефактный исход не портится."""
    repo = _repo(session)
    client = FakeGit(mr_error=GitRepoUnavailableError("503"))

    run = await _sync(session, client)

    assert run.status == SyncStatus.completed
    assert store.find_mr_observations(session, repo.id, run.id) == []


@pytest.mark.anyio
async def test_without_markers_config_step_skipped(session):
    """process_markers=None — MR-шаг выключен (конфиг не задан), обход как раньше."""
    repo = _repo(session)
    client = FakeGit(mrs=[_mr(7)])

    run = await run_sync(session, client, triggered_by=SyncTrigger.manual)
    session.flush()

    assert store.find_mr_observations(session, repo.id, run.id) == []


# ── fix по ревью #38–#41 ────────────────────────────────────────────────────


class FakeGitNotes(FakeGit):
    """Ноты с временем (NoteInfo) — для сценария 5 ADR-007."""

    def __init__(self, mrs=None, notes_info=None, **kw):
        super().__init__(mrs=mrs, **kw)
        self.notes_info = notes_info or {}

    async def list_mr_notes(self, repo_url, git_host, number):
        self.notes_calls.append(number)
        return self.notes_info.get(number, [])


@pytest.mark.anyio
async def test_stale_approval_after_force_push(session):
    """Сценарий 5 ADR-007: «принято» до force-push не переносится на новую голову."""
    from app.clients.git_client import NoteInfo

    repo = _repo(session)
    old_note = NoteInfo(body="принято", created_at="2026-07-27T10:00:00Z")
    client = FakeGitNotes(
        mrs=[_mr(7)], notes_info={7: [old_note]}
    )
    run1 = await _sync(session, client)  # первое наблюдение: head b*40, принято
    (first,) = store.find_mr_observations(session, repo.id, run1.id)
    assert first.reviewer_approved is True

    # force-push: новая голова, ноты те же (старое «принято»)
    client.mrs = [MrInfo(number=7, title="MR 7", source_branch="S5-auth", state="opened",
                         head_sha="f" * 40, updated_at="2026-07-28T12:00:00Z",
                         description="")]
    run2 = await _sync(session, client)

    (second,) = store.find_mr_observations(session, repo.id, run2.id)
    assert second.reviewer_approved is False  # вердикт устарел вместе с головой


@pytest.mark.anyio
async def test_new_approval_after_force_push_counts(session):
    """Новая нота «принято» после смены головы — валидна.

    Дата ноты вычисляется относительно текущего времени (тикет #49): хардкод
    «2026-07-29» протухал по календарю — cutoff сравнивается с wall-clock
    observed_at первого наблюдения, и нота из прошлого переставала засчитываться.
    """
    from datetime import timedelta

    from app.clients.git_client import NoteInfo
    from app.timeutil import utcnow

    repo = _repo(session)
    client = FakeGitNotes(mrs=[_mr(7)], notes_info={7: [NoteInfo("REWORK", "2026-07-27T10:00:00Z")]})
    await _sync(session, client)

    # нота гарантированно позже observed_at первого наблюдения (cutoff ADR-007 сц. 5)
    after_cutoff = (utcnow() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    client.mrs = [MrInfo(number=7, title="MR 7", source_branch="S5-auth", state="opened",
                         head_sha="f" * 40, updated_at="2026-07-28T12:00:00Z", description="")]
    client.notes_info = {7: [NoteInfo("REWORK", "2026-07-27T10:00:00Z"),
                             NoteInfo("Принято", after_cutoff)]}
    run2 = await _sync(session, client)

    (second,) = store.find_mr_observations(session, repo.id, run2.id)
    assert second.reviewer_approved is True


@pytest.mark.anyio
async def test_zero_mrs_is_not_error(session):
    """Сценарий 6 ADR-007: 0 MR — пустой журнал, обход completed."""
    repo = _repo(session)
    client = FakeGit(mrs=[])

    run = await _sync(session, client)

    assert run.status == SyncStatus.completed
    assert store.find_mr_observations(session, repo.id, run.id) == []


@pytest.mark.anyio
async def test_recreated_mr_two_numbers_same_branch(session):
    """Сценарий 4 ADR-007: пересозданный MR — два номера одной веткой, оба в журнале."""
    repo = _repo(session)
    client = FakeGit(mrs=[_mr(9, state="closed", branch="S5-auth"),
                          _mr(10, state="opened", branch="S5-auth")])

    run = await _sync(session, client)

    rows = store.find_mr_observations(session, repo.id, run.id)
    assert {r.mr_number for r in rows} == {9, 10}


@pytest.mark.anyio
async def test_duplicate_mr_numbers_from_pagination_deduped(session):
    """Дубль номера из сдвига offset-пагинации не роняет обход через И12."""
    repo = _repo(session)
    client = FakeGit(mrs=[_mr(7), _mr(7)])

    run = await _sync(session, client)

    rows = store.find_mr_observations(session, repo.id, run.id)
    assert [r.mr_number for r in rows] == [7]


# ── смена порядка сдачи (решение CEO 2026-07-29): мержат сами, мимо кнопки ──


@pytest.mark.anyio
async def test_merged_mr_verdict_is_read(session):
    """merged больше не значит «преподаватель принял» — вердикт читается и у merged."""
    repo = _repo(session)
    client = FakeGit(mrs=[_mr(6, state="merged")], notes={6: ["принято"]})

    run = await _sync(session, client)

    (row,) = store.find_mr_observations(session, repo.id, run.id)
    assert row.reviewer_approved is True


@pytest.mark.anyio
async def test_merged_without_verdict_flagged(session):
    """Смержен без «принято» — сигнал «мимо ревью», а не сданная работа."""
    repo = _repo(session)
    client = FakeGit(mrs=[_mr(6, state="merged")], notes={6: ["выглядит неплохо"]})

    run = await _sync(session, client)

    (row,) = store.find_mr_observations(session, repo.id, run.id)
    assert row.reviewer_approved is False
