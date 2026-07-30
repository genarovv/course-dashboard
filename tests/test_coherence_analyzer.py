"""C2 (#36), FR-5: coherence_analyzer.ensure_verdict — ядро по контракту §5.2.

AC тикета #36: find_verdict_by_quadruple (D25, «не мигаем») → LLM → register_verdict
ok/break/deferred; проверка И2 (оба снапшота одного репозитория, роли соответствуют
ребру); подключение verdict_worker к своду G4 (инъекция готова).
Гейт Фазы 0 снят 2026-07-30 (ADR-004 Accepted) — ядро разблокировано.
"""

import asyncio
from datetime import datetime

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from alembic import command
from app import store
from app.clients.git_client import GitRepoUnavailableError
from app.clients.llm_client import LLMUnavailableError
from app.models import GitHost, SnapshotStatus, SyncTrigger, VerdictValue
from app.models.artifact_def import ArtifactDef
from app.models.coherence_verdict import CoherenceVerdict
from app.models.lesson import Lesson
from app.services.coherence_analyzer import ensure_verdict, make_verdict_worker
from app.services.sync_orchestrator import PendingPair

LLM_MODEL = "deepseek-v4-flash"

VALID_VERDICT = {
    "verdict": "ok",
    "confidence": "high",
    "entities_checked": 3,
    "entities_found": 3,
    "entities_excluded": 0,
    "entities_lost": 0,
    "points": [],
    "notes": "связно",
}


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


def _seed(session):
    lesson5 = Lesson(number=5, title="PRD", date=datetime(2026, 6, 30).date())
    lesson6 = Lesson(number=6, title="Данные", date=datetime(2026, 7, 2).date())
    session.add_all([lesson5, lesson6])
    session.flush()
    adef_prd = ArtifactDef(lesson_id=lesson5.id, role="prd", expected_pattern="product/prd.md")
    adef_dm = ArtifactDef(lesson_id=lesson6.id, role="data_model", expected_pattern="data-model.md")
    session.add_all([adef_prd, adef_dm])
    rubric = store.register_rubric(session, type="edge", version="1.0", text="правило ребра")
    session.flush()
    edge = store.config_create_edge_def(
        session, source_role="prd", target_role="data_model", rubric_id=rubric.id
    )
    repo = store.register_repository(
        session, repo_url="https://github.com/s/x", git_host=GitHost.GitHub
    )
    run = store.register_sync_run(session, triggered_by=SyncTrigger.manual)
    session.flush()
    return adef_prd, adef_dm, rubric, edge, repo, run


def _snap(session, run, repo, adef, content_hash, file_path="f.md"):
    snap = store.register_snapshot(
        session,
        sync_run_id=run.id,
        repository_id=repo.id,
        artifact_def_id=adef.id,
        status=SnapshotStatus.found,
        content_hash=content_hash,
        file_path=file_path,
        source_commit_sha="c" * 40,
    )
    session.flush()
    return snap


def _pair(edge, repo, snap_a, snap_b, rubric):
    return PendingPair(
        edge_def_id=edge.id,
        repository_id=repo.id,
        source_snapshot_id=snap_a.id,
        target_snapshot_id=snap_b.id,
        source_content_hash=snap_a.content_hash,
        target_content_hash=snap_b.content_hash,
        rubric_id=rubric.id,
        llm_model=LLM_MODEL,
    )


class FakeGit:
    """Тексты по (file_path) или исключение."""

    def __init__(self, files=None, error=None):
        self.files = files or {}
        self.error = error
        self.calls = []

    async def get_file_content(self, repo_url, git_host, file_path, ref="main"):
        self.calls.append((file_path, ref))
        if self.error:
            raise self.error
        return self.files[file_path]


class FakeLLM:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def check_coherence(self, source_text, target_text, rubric_text):
        self.calls.append((source_text, target_text, rubric_text))
        if self.error:
            raise self.error
        return self.result


def _setup_pair(session, files=("# PRD", "# DM")):
    adef_prd, adef_dm, rubric, edge, repo, run = _seed(session)
    snap_a = _snap(session, run, repo, adef_prd, "a" * 64, file_path="product/prd.md")
    snap_b = _snap(session, run, repo, adef_dm, "b" * 64, file_path="data-model.md")
    git = FakeGit(files={"product/prd.md": files[0], "data-model.md": files[1]})
    return _pair(edge, repo, snap_a, snap_b, rubric), git, rubric


def _verdicts(session):
    return list(session.scalars(select(CoherenceVerdict)))


# ── D25: существующий валидный вердикт не пересчитывается («не мигаем») ─────


@pytest.mark.anyio
async def test_existing_valid_verdict_returned_without_llm_call(session):
    pair, git, rubric = _setup_pair(session)
    existing = store.register_verdict(
        session,
        edge_def_id=pair.edge_def_id,
        source_snapshot_id=pair.source_snapshot_id,
        target_snapshot_id=pair.target_snapshot_id,
        source_content_hash=pair.source_content_hash,
        target_content_hash=pair.target_content_hash,
        rubric_id=pair.rubric_id,
        llm_model=pair.llm_model,
        verdict=VerdictValue.ok,
        confidence="high",
    )
    session.flush()
    llm = FakeLLM(result=dict(VALID_VERDICT))

    verdict = await ensure_verdict(session, git, llm, pair)

    assert verdict.id == existing.id
    assert llm.calls == []  # LLM не вызывалась
    assert len(_verdicts(session)) == 1


@pytest.mark.anyio
async def test_deferred_verdict_is_recomputed(session):
    """Deferred не блокирует пересчёт: find_verdict_by_quadruple его игнорирует."""
    pair, git, rubric = _setup_pair(session)
    store.register_verdict(
        session,
        edge_def_id=pair.edge_def_id,
        source_snapshot_id=pair.source_snapshot_id,
        target_snapshot_id=pair.target_snapshot_id,
        source_content_hash=pair.source_content_hash,
        target_content_hash=pair.target_content_hash,
        rubric_id=pair.rubric_id,
        llm_model=pair.llm_model,
        verdict=VerdictValue.deferred,
        deferred_reason="parse_error",
        confidence="low",
    )
    session.flush()
    llm = FakeLLM(result=dict(VALID_VERDICT))

    verdict = await ensure_verdict(session, git, llm, pair)
    session.flush()

    assert verdict.verdict == VerdictValue.ok
    assert len(llm.calls) == 1
    assert len(_verdicts(session)) == 2  # append-only: deferred остался в журнале


# ── ok / break регистрируются с полями контракта §5.2 ───────────────────────


@pytest.mark.anyio
async def test_ok_verdict_registered_with_counters(session):
    pair, git, rubric = _setup_pair(session)
    llm = FakeLLM(result=dict(VALID_VERDICT))

    verdict = await ensure_verdict(session, git, llm, pair)
    session.flush()

    assert verdict.verdict == VerdictValue.ok
    assert verdict.confidence == "high"
    assert verdict.entities_checked == 3
    assert verdict.source_content_hash == pair.source_content_hash
    assert verdict.rubric_id == pair.rubric_id
    assert verdict.llm_model == LLM_MODEL
    # рубрика и тексты дошли до LLM
    (call,) = llm.calls
    assert call == ("# PRD", "# DM", "правило ребра")


@pytest.mark.anyio
async def test_break_verdict_registered_with_points(session):
    pair, git, rubric = _setup_pair(session)
    result = dict(VALID_VERDICT)
    result.update(
        verdict="break",
        entities_found=1,
        entities_excluded=0,
        entities_lost=2,
        points=[{"entity": "оплата", "quote_a": "модуль оплаты", "why_not": "в B не найден"}],
    )
    llm = FakeLLM(result=result)

    verdict = await ensure_verdict(session, git, llm, pair)
    session.flush()

    assert verdict.verdict == VerdictValue.break_
    assert verdict.entities_lost == 2
    assert verdict.points[0]["entity"] == "оплата"


# ── деградации §5.2 ─────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_parse_error_registers_deferred(session):
    pair, git, rubric = _setup_pair(session)
    llm = FakeLLM(result=None)  # клиент исчерпал ретрай

    verdict = await ensure_verdict(session, git, llm, pair)
    session.flush()

    assert verdict.verdict == VerdictValue.deferred
    assert verdict.deferred_reason == "parse_error"


@pytest.mark.anyio
async def test_llm_unavailable_registers_deferred(session):
    pair, git, rubric = _setup_pair(session)
    llm = FakeLLM(error=LLMUnavailableError("сеть"))

    verdict = await ensure_verdict(session, git, llm, pair)
    session.flush()

    assert verdict.verdict == VerdictValue.deferred
    assert verdict.deferred_reason == "llm_unavailable"


@pytest.mark.anyio
async def test_git_failure_writes_nothing(session):
    """Недоступность репозитория — не вина LLM: ничего не пишем,
    пара без вердикта сама попадёт в следующий свод (§5.1)."""
    pair, _, rubric = _setup_pair(session)
    git = FakeGit(error=GitRepoUnavailableError("404"))
    llm = FakeLLM(result=dict(VALID_VERDICT))

    verdict = await ensure_verdict(session, git, llm, pair)

    assert verdict is None
    assert llm.calls == []
    assert _verdicts(session) == []


# ── И2: оба снапшота одного репозитория, роли соответствуют ребру ───────────


@pytest.mark.anyio
async def test_i2_different_repo_raises(session):
    pair, git, rubric = _setup_pair(session)
    other_repo = store.register_repository(
        session, repo_url="https://github.com/s/other", git_host=GitHost.GitHub
    )
    session.flush()
    broken = PendingPair(**{**pair.__dict__, "repository_id": other_repo.id})
    llm = FakeLLM(result=dict(VALID_VERDICT))

    with pytest.raises(ValueError):
        await ensure_verdict(session, git, llm, broken)
    assert _verdicts(session) == []


@pytest.mark.anyio
async def test_i2_role_mismatch_raises(session):
    """Снапшоты перепутаны местами: роль источника не совпадает с ребром."""
    pair, git, rubric = _setup_pair(session)
    swapped = PendingPair(**{
        **pair.__dict__,
        "source_snapshot_id": pair.target_snapshot_id,
        "target_snapshot_id": pair.source_snapshot_id,
    })
    llm = FakeLLM(result=dict(VALID_VERDICT))

    with pytest.raises(ValueError):
        await ensure_verdict(session, git, llm, swapped)
    assert _verdicts(session) == []


# ── подключение воркера к своду G4 ──────────────────────────────────────────


@pytest.mark.anyio
async def test_worker_processes_pair_in_own_session(session, tmp_path):
    """make_verdict_worker: воркер открывает собственную сессию и коммитит вердикт."""
    pair, git, rubric = _setup_pair(session)
    session.commit()  # воркер работает в отдельной сессии — данные должны быть в БД

    engine = session.get_bind()

    def session_factory():
        return Session(engine)

    llm = FakeLLM(result=dict(VALID_VERDICT))
    worker = make_verdict_worker(session_factory, git, llm)

    await worker(pair)

    with Session(engine) as check:
        rows = list(check.scalars(select(CoherenceVerdict)))
        assert len(rows) == 1
        assert rows[0].verdict == VerdictValue.ok


@pytest.mark.anyio
async def test_worker_serializes_writes(session):
    """Единый writer SQLite: конкурентные пары обрабатываются последовательно (Lock)."""
    pair, git, rubric = _setup_pair(session)
    session.commit()
    engine = session.get_bind()

    active = 0
    max_active = 0

    class SlowLLM:
        async def check_coherence(self, source_text, target_text, rubric_text):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return dict(VALID_VERDICT)

    worker = make_verdict_worker(lambda: Session(engine), git, SlowLLM())
    await asyncio.gather(worker(pair), worker(pair))

    assert max_active == 1  # сериализовано
