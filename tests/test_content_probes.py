"""T2 (#44): content_probes узкой редакцией — решение CEO 2026-07-28 (развилка 4).

Проба = regex contains/not_contains к содержимому артефакта, ТОЛЬКО к объявленным
требованиям курса (стандарты в CLAUDE.md, маркеры незаполненного шаблона).
Результат — отдельный признак карточки; статус ячейки НЕ меняется (BR-3 нетронут).
Обходчик уже качает контент — добавляется только матчинг.
"""

from datetime import datetime

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from alembic import command
from app import store
from app.models import GitHost, SnapshotStatus, SyncTrigger
from app.models.artifact_def import ArtifactDef
from app.models.artifact_snapshot import ArtifactSnapshot
from app.models.lesson import Lesson
from app.services import config_manager
from app.services.sync_orchestrator import run_sync


@pytest.fixture()
def session(tmp_path):
    db_path = tmp_path / "probes.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as s:
        yield s
    engine.dispose()


class FakeGit:
    def __init__(self, files):
        self.files = files

    async def get_tree(self, repo_url, git_host, ref="main"):
        return list(self.files)

    async def get_file_content(self, repo_url, git_host, file_path, ref="main"):
        return self.files[file_path]

    async def get_head_sha(self, repo_url, git_host, ref="main"):
        return "a" * 40

    async def fetch_default_branch(self, repo_url, git_host):
        return "main"


def _seed(session, probes):
    lesson = Lesson(number=2, title="Контекст", date=datetime(2026, 6, 18).date())
    session.add(lesson)
    session.flush()
    adef = ArtifactDef(
        lesson_id=lesson.id, role="claude_md", expected_pattern="CLAUDE.md",
        content_probes=probes,
    )
    session.add(adef)
    repo = store.register_repository(
        session, repo_url="https://github.com/s/x", git_host=GitHost.GitHub
    )
    session.flush()
    return adef, repo


PROBES = [
    {"key": "testing_standard", "contains": "Стандарт тестирования",
     "label": "раздел «Стандарт тестирования»"},
    {"key": "template_stub", "not_contains": "Стек: <",
     "label": "незаполненная строка стека из шаблона"},
]


# ── конфиг: пробы парсятся и реконсилируются ────────────────────────────────


def test_config_probes_parse_and_reconcile(session):
    yaml_cfg = config_manager.ConfigYAML.model_validate({
        "lessons": [{
            "number": 2, "title": "Контекст", "date": "2026-06-18",
            "artifacts": [{
                "role": "claude_md", "expected_pattern": "CLAUDE.md",
                "content_probes": PROBES,
            }],
        }],
        "edges": [],
    })
    config_manager.reconcile(session, yaml_cfg)
    session.flush()
    (adef,) = session.scalars(select(ArtifactDef))
    assert adef.content_probes and len(adef.content_probes) == 2
    assert adef.content_probes[0]["key"] == "testing_standard"


def test_real_config_declares_probes_only_for_declared_requirements():
    config = config_manager.load_config()
    probed = {
        str(a.role): a.content_probes
        for lesson in config.lessons
        for a in lesson.artifacts
        if a.content_probes
    }
    # узкая редакция: пробы только у claude_md и architecture (объявленные требования)
    assert set(probed) == {"claude_md", "architecture"}


# ── обход: probe_findings пишутся в снапшот, статус НЕ меняется (BR-3) ──────


@pytest.mark.anyio
async def test_sync_records_probe_findings_without_touching_status(session):
    _seed(session, PROBES)
    git = FakeGit({"CLAUDE.md": "# Правила\nСтек: <заполни>\nБез стандартов."})

    await run_sync(session, git, triggered_by=SyncTrigger.manual)
    session.flush()

    (snap,) = session.scalars(select(ArtifactSnapshot))
    assert snap.status == SnapshotStatus.found  # BR-3: проба статус не трогает
    findings = snap.probe_findings or []
    keys = {f["key"] for f in findings}
    assert keys == {"testing_standard", "template_stub"}  # обе пробы сработали


@pytest.mark.anyio
async def test_sync_clean_content_has_no_findings(session):
    _seed(session, PROBES)
    git = FakeGit({"CLAUDE.md": "# Правила\nСтек: Python\n## Стандарт тестирования\n..."})

    await run_sync(session, git, triggered_by=SyncTrigger.manual)
    session.flush()

    (snap,) = session.scalars(select(ArtifactSnapshot))
    assert snap.status == SnapshotStatus.found
    assert not (snap.probe_findings or [])


@pytest.mark.anyio
async def test_probe_change_is_part_of_observation(session):
    """Сработавшая проба входит в наблюдение D28: изменение контента,
    чинящее пробу, рождает новый снапшот с чистыми findings."""
    _seed(session, PROBES)
    git = FakeGit({"CLAUDE.md": "Стек: <заполни>\n## Стандарт тестирования"})
    await run_sync(session, git, triggered_by=SyncTrigger.manual)
    session.flush()

    git.files["CLAUDE.md"] = "Стек: Python\n## Стандарт тестирования"
    await run_sync(session, git, triggered_by=SyncTrigger.manual)
    session.flush()

    snaps = list(session.scalars(select(ArtifactSnapshot).order_by(ArtifactSnapshot.observed_at)))
    assert len(snaps) == 2
    assert {f["key"] for f in (snaps[0].probe_findings or [])} == {"template_stub"}
    assert not (snaps[1].probe_findings or [])


# ── карточка студента: признак виден ────────────────────────────────────────


@pytest.mark.anyio
async def test_probe_findings_visible_in_student_card(session):
    from app.services.evidence_chain import build_student_card

    _seed(session, PROBES)
    git = FakeGit({"CLAUDE.md": "Стек: <заполни>"})
    await run_sync(session, git, triggered_by=SyncTrigger.manual)
    session.flush()

    (repo_row,) = session.scalars(select(ArtifactSnapshot))
    card = build_student_card(session, repo_row.repository_id, llm_model="deepseek-v4-flash")
    probe_rows = card.get("probe_findings") or []
    assert any("незаполненная строка стека" in row["label"] for row in probe_rows)
