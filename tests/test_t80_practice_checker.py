"""#80 (FR-14 этап 1): practice_checker — проверки применения приёмов курса без LLM.

AC 1–8 спеки plans/проверка-приёмов-этап-1-2026-08-14.md. Фейковый git-клиент со
счётчиками вызовов (образец test_mr_sync.py); логгер перехватывается monkeypatch,
не caplog (fileConfig алембика в фикстуре снимает обработчики root-логгера).
"""

import json
from datetime import datetime, timedelta

import pytest
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from alembic import command
from app import store
from app.clients.git_client import GitRepoUnavailableError, MrCommit, MrInfo, NoteInfo
from app.models import GitHost, PracticeStatus, SyncStatus, SyncTrigger
from app.services.config_manager import PracticeCheckConfig
from app.timeutil import utcnow

OLD = datetime(2026, 7, 28, 10, 0)  # updated_at хостинга — заведомо старше наблюдений


def _check(**kw) -> PracticeCheckConfig:
    kw.setdefault("lesson", 11)
    kw.setdefault("label", kw["key"])
    return PracticeCheckConfig(**kw)


TESTS_FIRST = _check(key="tests_first", kind="mr_commit_pattern", pattern="tests?[ -]first")
DOCS_SYNC = _check(
    key="docs_sync", kind="mr_docs_sync", waiver_pattern="докам обновление не требуется"
)
TICKET_SHARE = _check(
    key="ticket_id_share", kind="commit_id_share",
    pattern=r"(#\d+|[A-Z]{1,6}-\d+|T-?\d{1,4})", threshold=0.5,
)
REVIEW_ROUND = _check(key="review_round", kind="review_round")
PUBLIC_URL = _check(key="public_url", kind="readme_url")
ENV_EXAMPLE = _check(
    key="env_example", kind="tree_probe",
    tree_patterns=["**/*.env.example", "**/env.example", ".env.example"],
)
DEPS_PINNED = _check(
    key="deps_pinned", kind="tree_probe", tree_patterns=["**/*.lock", "**/requirements*.txt"],
)


class FakeGit:
    """Фейк только тех методов, которые нужны проверкам; всё — со счётчиками."""

    def __init__(self, mr_commits=None, mr_changes=None, branch_commits=None,
                 notes=None, files=None, errors=None):
        self.mr_commits = mr_commits or {}       # mr_number -> [(sha, message)]
        self.mr_changes = mr_changes or {}       # mr_number -> [paths]
        self.branch_commits = branch_commits or []  # [(sha, message)]
        self.notes = notes or {}                 # mr_number -> [NoteInfo]
        self.files = files or {}                 # path -> content
        self.errors = errors or {}               # имя метода -> исключение
        self.commit_calls: list[int] = []        # номера MR, чьи коммиты читались

    def _raise_if(self, method):
        if method in self.errors:
            raise self.errors[method]

    async def list_mr_commits(self, repo_url, git_host, mr_number):
        self._raise_if("list_mr_commits")
        self.commit_calls.append(mr_number)
        return [
            MrCommit(sha=sha, message=message, position=i)
            for i, (sha, message) in enumerate(self.mr_commits.get(mr_number, []))
        ]

    async def list_commits(self, repo_url, git_host, ref="main", limit=100):
        self._raise_if("list_commits")
        return [
            MrCommit(sha=sha, message=message, position=i)
            for i, (sha, message) in enumerate(self.branch_commits[:limit])
        ]

    async def list_mr_changes(self, repo_url, git_host, mr_number):
        self._raise_if("list_mr_changes")
        return self.mr_changes.get(mr_number, [])

    async def list_mr_notes(self, repo_url, git_host, number):
        self._raise_if("list_mr_notes")
        return self.notes.get(number, [])

    async def get_file_content(self, repo_url, git_host, file_path, ref="main"):
        self._raise_if("get_file_content")
        return self.files[file_path]


@pytest.fixture()
def session(tmp_path):
    db_path = tmp_path / "t80.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as s:
        yield s
    engine.dispose()


def _seed(s):
    repo = store.register_repository(s, repo_url="https://github.com/s/x", git_host=GitHost.GitHub)
    run = store.register_sync_run(s, triggered_by=SyncTrigger.manual)
    s.flush()
    return repo, run


def _mr_row(s, run, repo, number, *, state="merged", approved=False, markers=None,
            updated_at=OLD):
    store.register_mr_observation(
        s, sync_run_id=run.id, repository_id=repo.id, mr_number=number,
        title=f"MR {number}", source_branch=f"b{number}", state=state,
        reviewer_approved=approved, markers=markers or {},
        head_sha="a" * 40, updated_at=updated_at,
    )
    s.flush()


async def _run_checks(s, git, run, repo, checks, tree=(), http_get=None):
    from app.services.practice_checker import run_practice_checks

    mrs = store.find_latest_mr_observations(s, repo.id)
    await run_practice_checks(
        s, git, run.id, repo, checks, mrs, list(tree), http_get=http_get
    )
    s.flush()


def _row(s, repo, key):
    return store.find_last_practice_observation(s, repo.id, key)


# ── AC 1: tests_first (mr_commit_pattern) ───────────────────────────────────


@pytest.mark.anyio
async def test_tests_first_passed_with_evidence(session):
    """Красная фаза отдельным коммитом раньше кода → passed, evidence называет sha и MR."""
    repo, run = _seed(session)
    _mr_row(session, run, repo, 7)
    git = FakeGit(mr_commits={7: [("a" * 40, "T5: tests first — lockout"),
                                  ("b" * 40, "T5: impl")]})

    await _run_checks(session, git, run, repo, [TESTS_FIRST])

    row = _row(session, repo, "tests_first")
    assert row.status == PracticeStatus.passed
    assert row.evidence[0]["mr_number"] == 7
    assert row.evidence[0]["sha"] == "a" * 40


@pytest.mark.anyio
async def test_tests_first_failed_single_commit(session):
    """Один коммит «всё сразу» — следа перехода красный→зелёный нет."""
    repo, run = _seed(session)
    _mr_row(session, run, repo, 7)
    git = FakeGit(mr_commits={7: [("c" * 40, "T5: всё сразу")]})

    await _run_checks(session, git, run, repo, [TESTS_FIRST])

    assert _row(session, repo, "tests_first").status == PracticeStatus.failed


@pytest.mark.anyio
async def test_tests_first_pattern_last_commit_failed(session):
    """Коммит с паттерном последний — после него нет кода, перехода не видно."""
    repo, run = _seed(session)
    _mr_row(session, run, repo, 7)
    git = FakeGit(mr_commits={7: [("b" * 40, "T5: impl"),
                                  ("a" * 40, "T5: tests first")]})

    await _run_checks(session, git, run, repo, [TESTS_FIRST])

    assert _row(session, repo, "tests_first").status == PracticeStatus.failed


@pytest.mark.anyio
async def test_tests_first_no_merged_mrs_is_no_data(session):
    repo, run = _seed(session)
    _mr_row(session, run, repo, 7, state="opened")

    await _run_checks(session, FakeGit(), run, repo, [TESTS_FIRST])

    assert _row(session, repo, "tests_first").status == PracticeStatus.no_data


# ── AC 2: docs_sync (mr_docs_sync) ──────────────────────────────────────────


@pytest.mark.anyio
async def test_docs_sync_code_without_md_failed(session):
    repo, run = _seed(session)
    _mr_row(session, run, repo, 7)
    git = FakeGit(mr_changes={7: ["app/auth.py", "app/store.py"]})

    await _run_checks(session, git, run, repo, [DOCS_SYNC])

    row = _row(session, repo, "docs_sync")
    assert row.status == PracticeStatus.failed
    assert row.evidence[0]["mr_number"] == 7


@pytest.mark.anyio
async def test_docs_sync_waiver_in_markers_passed(session):
    """Явный отказ «докам обновление не требуется, потому что …» — приём соблюдён."""
    repo, run = _seed(session)
    waiver = {"docs_waiver": {"found": True,
                              "quote": "докам обновление не требуется, потому что интерфейс не менялся"}}
    _mr_row(session, run, repo, 7, markers=waiver)
    git = FakeGit(mr_changes={7: ["app/auth.py"]})

    await _run_checks(session, git, run, repo, [DOCS_SYNC])

    assert _row(session, repo, "docs_sync").status == PracticeStatus.passed


@pytest.mark.anyio
async def test_docs_sync_code_with_md_passed(session):
    repo, run = _seed(session)
    _mr_row(session, run, repo, 7)
    git = FakeGit(mr_changes={7: ["app/auth.py", "README.md"]})

    await _run_checks(session, git, run, repo, [DOCS_SYNC])

    assert _row(session, repo, "docs_sync").status == PracticeStatus.passed


@pytest.mark.anyio
async def test_docs_sync_md_only_mr_not_counted(session):
    """MR только с .md — не считается: единственный такой MR даёт no_data."""
    repo, run = _seed(session)
    _mr_row(session, run, repo, 7)
    git = FakeGit(mr_changes={7: ["README.md", "CHANGELOG.md"]})

    await _run_checks(session, git, run, repo, [DOCS_SYNC])

    assert _row(session, repo, "docs_sync").status == PracticeStatus.no_data


# ── AC 3: ticket_id_share (commit_id_share) ────────────────────────────────


@pytest.mark.anyio
async def test_ticket_share_60_of_100_passed(session):
    repo, run = _seed(session)
    commits = [(f"{i:040x}", "#12 шаг") for i in range(60)]
    commits += [(f"{i:040x}", "правки") for i in range(60, 100)]
    git = FakeGit(branch_commits=commits)

    await _run_checks(session, git, run, repo, [TICKET_SHARE])

    row = _row(session, repo, "ticket_id_share")
    assert row.status == PracticeStatus.passed
    assert "60/100" in json.dumps(row.evidence, ensure_ascii=False)


@pytest.mark.anyio
async def test_ticket_share_30_of_100_failed(session):
    repo, run = _seed(session)
    commits = [(f"{i:040x}", "T80: шаг") for i in range(30)]
    commits += [(f"{i:040x}", "правки") for i in range(30, 100)]
    git = FakeGit(branch_commits=commits)

    await _run_checks(session, git, run, repo, [TICKET_SHARE])

    assert _row(session, repo, "ticket_id_share").status == PracticeStatus.failed


@pytest.mark.anyio
async def test_ticket_share_no_commits_no_data(session):
    repo, run = _seed(session)

    await _run_checks(session, FakeGit(branch_commits=[]), run, repo, [TICKET_SHARE])

    assert _row(session, repo, "ticket_id_share").status == PracticeStatus.no_data


# ── AC 4: review_round ──────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_review_round_note_before_verdict_passed(session):
    repo, run = _seed(session)
    _mr_row(session, run, repo, 7, approved=True)
    git = FakeGit(notes={7: [NoteInfo("REWORK: 2 находки", "2026-07-28T09:00:00Z"),
                             NoteInfo("Принято", "2026-07-28T10:00:00Z")]})

    await _run_checks(session, git, run, repo, [REVIEW_ROUND])

    row = _row(session, repo, "review_round")
    assert row.status == PracticeStatus.passed
    assert row.evidence[0]["mr_number"] == 7


@pytest.mark.anyio
async def test_review_round_verdict_without_notes_failed(session):
    """Вердикт без обсуждения: единственная нота — само «принято»."""
    repo, run = _seed(session)
    _mr_row(session, run, repo, 7, approved=True)
    git = FakeGit(notes={7: [NoteInfo("принято", "2026-07-28T10:00:00Z")]})

    await _run_checks(session, git, run, repo, [REVIEW_ROUND])

    assert _row(session, repo, "review_round").status == PracticeStatus.failed


@pytest.mark.anyio
async def test_review_round_no_approved_mrs_no_data(session):
    repo, run = _seed(session)
    _mr_row(session, run, repo, 7, approved=False)

    await _run_checks(session, FakeGit(), run, repo, [REVIEW_ROUND])

    assert _row(session, repo, "review_round").status == PracticeStatus.no_data


# ── AC 5: public_url (readme_url) ───────────────────────────────────────────


def _seed_readme(s, repo, content_marker="h1"):
    from app.models.artifact_def import ArtifactDef
    from app.models.lesson import Lesson

    lesson = Lesson(number=10, title="Документация", date=datetime(2026, 7, 16).date())
    s.add(lesson)
    s.flush()
    adef = ArtifactDef(lesson_id=lesson.id, role="readme", expected_pattern="README.md")
    s.add(adef)
    s.flush()
    run = store.register_sync_run(s, triggered_by=SyncTrigger.manual)
    s.flush()
    store.register_snapshot(
        s, sync_run_id=run.id, repository_id=repo.id, artifact_def_id=adef.id,
        status="found", file_path="README.md", content_hash=content_marker,
    )
    s.flush()


@pytest.mark.anyio
async def test_public_url_reachable_passed(session):
    repo, run = _seed(session)
    _seed_readme(session, repo)
    # адреса хостингов пропускаются — ищется именно прототип, не ссылка на репо
    readme = "Код: https://github.com/s/x\nДемо: https://example.app/demo\n"
    git = FakeGit(files={"README.md": readme})
    fetched: list[str] = []

    async def http_get(url):
        fetched.append(url)
        return 200

    await _run_checks(session, git, run, repo, [PUBLIC_URL], http_get=http_get)

    row = _row(session, repo, "public_url")
    assert row.status == PracticeStatus.passed
    assert fetched == ["https://example.app/demo"]


@pytest.mark.anyio
async def test_public_url_absent_no_data(session):
    repo, run = _seed(session)
    _seed_readme(session, repo)
    git = FakeGit(files={"README.md": "Только код: https://github.com/s/x"})

    async def http_get(url):  # pragma: no cover — вызываться не должен
        raise AssertionError("URL нет — сеть не трогаем")

    await _run_checks(session, git, run, repo, [PUBLIC_URL], http_get=http_get)

    assert _row(session, repo, "public_url").status == PracticeStatus.no_data


@pytest.mark.anyio
async def test_public_url_http_error_failed(session):
    repo, run = _seed(session)
    _seed_readme(session, repo)
    git = FakeGit(files={"README.md": "Демо: https://example.app/down"})

    async def http_get(url):
        return 503

    await _run_checks(session, git, run, repo, [PUBLIC_URL], http_get=http_get)

    assert _row(session, repo, "public_url").status == PracticeStatus.failed


# ── AC 6: env_example (tree_probe с чтением файла) ──────────────────────────


@pytest.mark.anyio
async def test_env_example_names_only_passed(session):
    repo, run = _seed(session)
    git = FakeGit(files={".env.example": "CD_SECRET_KEY=\nCD_ADMIN_PASSWORD=\n# порт\nPORT=8000\n"})

    await _run_checks(session, git, run, repo, [ENV_EXAMPLE], tree=[".env.example"])

    assert _row(session, repo, "env_example").status == PracticeStatus.passed


@pytest.mark.anyio
async def test_env_example_with_secret_value_failed(session):
    """Значение ≥8 символов в образце — failed; само значение в evidence не утекает."""
    repo, run = _seed(session)
    git = FakeGit(files={".env.example": "API_KEY=sk-abc12345\n"})

    await _run_checks(session, git, run, repo, [ENV_EXAMPLE], tree=[".env.example"])

    row = _row(session, repo, "env_example")
    assert row.status == PracticeStatus.failed
    assert "sk-abc12345" not in json.dumps(row.evidence)


@pytest.mark.anyio
async def test_tree_probe_not_found_failed(session):
    repo, run = _seed(session)

    await _run_checks(session, FakeGit(), run, repo, [DEPS_PINNED], tree=["app/main.py"])

    assert _row(session, repo, "deps_pinned").status == PracticeStatus.failed


@pytest.mark.anyio
async def test_tree_probe_found_passed_with_path(session):
    repo, run = _seed(session)

    await _run_checks(session, FakeGit(), run, repo, [DEPS_PINNED], tree=["uv.lock"])

    row = _row(session, repo, "deps_pinned")
    assert row.status == PracticeStatus.passed
    assert row.evidence[0]["path"] == "uv.lock"


# ── AC 7: деградация NFR-2 ─────────────────────────────────────────────────


@pytest.mark.anyio
async def test_check_error_is_no_data_and_others_alive(session, monkeypatch):
    repo, run = _seed(session)
    _mr_row(session, run, repo, 7)
    git = FakeGit(errors={"list_mr_commits": GitRepoUnavailableError("503")})
    from app.services import practice_checker

    warnings: list[str] = []
    monkeypatch.setattr(
        practice_checker.logger, "warning",
        lambda msg, *args, **kw: warnings.append(msg % args if args else msg),
    )

    await _run_checks(session, git, run, repo, [TESTS_FIRST, DEPS_PINNED], tree=["uv.lock"])

    assert _row(session, repo, "tests_first").status == PracticeStatus.no_data
    assert _row(session, repo, "deps_pinned").status == PracticeStatus.passed
    assert any("tests_first" in w for w in warnings)


# ── AC 8: кэш — MR без изменений не перечитываются ─────────────────────────


@pytest.mark.anyio
async def test_unchanged_mrs_not_refetched(session):
    repo, run1 = _seed(session)
    _mr_row(session, run1, repo, 7)  # updated_at = OLD, заведомо старше первой проверки
    git = FakeGit(mr_commits={7: [("a" * 40, "T5: tests first"), ("b" * 40, "T5: impl")]})

    await _run_checks(session, git, run1, repo, [TESTS_FIRST])
    calls_after_first = len(git.commit_calls)

    run2 = store.register_sync_run(session, triggered_by=SyncTrigger.manual)
    session.flush()
    await _run_checks(session, git, run2, repo, [TESTS_FIRST])

    assert len(git.commit_calls) == calls_after_first  # второй обход коммиты не читал
    row = _row(session, repo, "tests_first")
    assert row.sync_run_id == run2.id  # журнал пополнен новой строкой
    assert row.status == PracticeStatus.passed


@pytest.mark.anyio
async def test_updated_mr_is_refetched(session):
    """MR обновился после последней проверки — кэш не действует."""
    repo, run1 = _seed(session)
    _mr_row(session, run1, repo, 7)
    git = FakeGit(mr_commits={7: [("a" * 40, "T5: tests first"), ("b" * 40, "T5: impl")]})
    await _run_checks(session, git, run1, repo, [TESTS_FIRST])
    calls_after_first = len(git.commit_calls)

    run2 = store.register_sync_run(session, triggered_by=SyncTrigger.manual)
    session.flush()
    _mr_row(session, run2, repo, 7, updated_at=utcnow() + timedelta(hours=1))
    await _run_checks(session, git, run2, repo, [TESTS_FIRST])

    assert len(git.commit_calls) == calls_after_first + 1


# ── бюджет API: не больше 3 MR на репозиторий (образец D43) ────────────────


@pytest.mark.anyio
async def test_mr_budget_three_freshest_and_warning(session, monkeypatch):
    repo, run = _seed(session)
    base = datetime(2026, 7, 20)
    for number in range(1, 6):  # 5 смерженных MR, свежесть растёт с номером
        _mr_row(session, run, repo, number, updated_at=base + timedelta(days=number))
    git = FakeGit(mr_commits={n: [(f"{n:040x}", "правки")] for n in range(1, 6)})
    from app.services import practice_checker

    warnings: list[str] = []
    monkeypatch.setattr(
        practice_checker.logger, "warning",
        lambda msg, *args, **kw: warnings.append(msg % args if args else msg),
    )

    await _run_checks(session, git, run, repo, [TESTS_FIRST])

    assert sorted(git.commit_calls) == [3, 4, 5]  # три самых свежих
    assert any("не провер" in w for w in warnings)  # урезание названо поимённо


# ── проводка в run_sync (после _observe_mrs, деградация независимая) ───────


class FakeGitFull(FakeGit):
    """Фейк полного обхода: дерево + MR + коммиты (для run_sync)."""

    def __init__(self, tree=(), mrs=(), **kw):
        super().__init__(**kw)
        self.tree = list(tree)
        self.mrs = list(mrs)

    async def get_tree(self, repo_url, git_host, ref="main"):
        return self.tree

    async def get_head_sha(self, repo_url, git_host, ref="main"):
        return "f" * 40

    async def fetch_default_branch(self, repo_url, git_host):
        return "main"

    async def list_merge_requests(self, repo_url, git_host):
        return self.mrs


def _mr_info(number, state="merged"):
    return MrInfo(
        number=number, title=f"MR {number}", source_branch=f"b{number}", state=state,
        head_sha="a" * 40, updated_at="2026-07-28T10:00:00Z", description="",
    )


@pytest.mark.anyio
async def test_run_sync_writes_practice_observations(session):
    from app.services.sync_orchestrator import run_sync

    repo, _run = _seed(session)
    git = FakeGitFull(
        tree=["uv.lock"], mrs=[_mr_info(7)],
        mr_commits={7: [("a" * 40, "T5: tests first"), ("b" * 40, "T5: impl")]},
    )

    run = await run_sync(
        session, git, triggered_by=SyncTrigger.manual,
        process_markers=[], practice_checks=[TESTS_FIRST, DEPS_PINNED],
    )
    session.flush()

    assert run.status == SyncStatus.completed
    assert _row(session, repo, "tests_first").status == PracticeStatus.passed
    assert _row(session, repo, "deps_pinned").status == PracticeStatus.passed


@pytest.mark.anyio
async def test_run_sync_without_practice_checks_writes_nothing(session):
    from app.services.sync_orchestrator import run_sync

    repo, _run = _seed(session)
    git = FakeGitFull(tree=["uv.lock"], mrs=[_mr_info(7)])

    await run_sync(session, git, triggered_by=SyncTrigger.manual, process_markers=[])
    session.flush()

    assert store.find_last_practice_observations(session, repo.id) == []


@pytest.mark.anyio
async def test_run_sync_check_error_does_not_break_sync(session):
    """NFR-2: ошибка проверки — no_data и warning, обход завершается."""
    from app.services.sync_orchestrator import run_sync

    repo, _run = _seed(session)
    git = FakeGitFull(
        tree=["uv.lock"], mrs=[_mr_info(7)],
        errors={"list_mr_commits": GitRepoUnavailableError("503")},
    )

    run = await run_sync(
        session, git, triggered_by=SyncTrigger.manual,
        process_markers=[], practice_checks=[TESTS_FIRST],
    )
    session.flush()

    assert run.status == SyncStatus.completed
    assert _row(session, repo, "tests_first").status == PracticeStatus.no_data
