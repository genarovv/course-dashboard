"""D23 (итерация 5): дубли artifact_def — guard в реконсиляции + идемпотентная чистка.

Спека: plans/доводки-ux-5-2026-07-31.md §D23; решение CEO №1 (2026-07-31).
Verified-находка: смена ключа реконсиляции (занятие, роль) → (занятие, роль, паттерн)
в пакете «12 артефактов» оставила по два одинаковых определения у 7 ролей боевой БД.
AC: reconcile удаляет дубли по ключу (остаётся определение с самым ранним наблюдением);
снапшоты дубля удаляются (мусор бага, решение CEO), вердикты выживают — их
провенанс-FK перепривязывается на эквивалентный снапшот (тот же content_hash,
канон Б1 не меняется); счётчик deduped; повторный прогон — 0.
Негативные: дублей нет — не трогаем; разные паттерны одной роли — не дубль;
нет эквивалентного снапшота для вердикта — группа пропускается с warning (fail-safe).
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

LLM_MODEL = "deepseek-v4-flash"

YAML = """
lessons:
  - number: 5
    title: "PRD"
    date: 2026-06-30
    artifacts:
      - role: prd
        expected_pattern: "product/prd.md"
  - number: 6
    title: "Данные"
    date: 2026-07-02
    artifacts:
      - role: data_model
        expected_pattern: "data-model.md"
"""


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


def _seed_dup(s):
    """Дубль prd/product/prd.md: старый def (ранние снапшоты) + новый; вердикт на снапшот дубля."""
    config = config_manager.parse_config(YAML)
    config_manager.reconcile(s, config)
    s.flush()
    old = s.scalar(select(ArtifactDef).where(ArtifactDef.role == "prd"))
    dup = ArtifactDef(lesson_id=old.lesson_id, role="prd", expected_pattern="product/prd.md")
    s.add(dup)
    dm = s.scalar(select(ArtifactDef).where(ArtifactDef.role == "data_model"))
    repo = store.register_repository(s, repo_url="https://github.com/s/x", git_host=GitHost.GitHub)
    run = store.register_sync_run(s, triggered_by=SyncTrigger.schedule)
    s.flush()

    def snap(adef, h, when):
        return store.register_snapshot(
            s, sync_run_id=run.id, repository_id=repo.id, artifact_def_id=adef.id,
            status=SnapshotStatus.found, content_hash=h * 64,
            file_path=adef.expected_pattern, source_commit_sha="c" * 40, observed_at=when,
        )

    snap(old, "a", datetime(2026, 7, 28, 10, 0))  # старый — раннее наблюдение, его храним
    dup_snap = snap(dup, "a", datetime(2026, 7, 30, 10, 0))  # тот же контент у дубля
    dm_snap = snap(dm, "b", datetime(2026, 7, 30, 10, 1))
    rubric = store.register_rubric(s, type="edge", version="1.0", text="п")
    s.flush()
    edge = store.config_create_edge_def(
        s, source_role="prd", target_role="data_model", rubric_id=rubric.id
    )
    s.flush()
    verdict = store.register_verdict(
        s, edge_def_id=edge.id,
        source_snapshot_id=dup_snap.id, target_snapshot_id=dm_snap.id,  # провенанс — на дубль!
        source_content_hash=dup_snap.content_hash, target_content_hash=dm_snap.content_hash,
        rubric_id=rubric.id, llm_model=LLM_MODEL, verdict="ok", confidence="high",
    )
    s.flush()
    return config, old, dup, verdict


def test_reconcile_dedupes_and_repoints_verdict(session):
    config, old, dup, verdict = _seed_dup(session)
    summary = config_manager.reconcile(session, config)
    session.flush()
    assert summary.artifact_defs_deduped == 1
    left = list(session.scalars(select(ArtifactDef).where(ArtifactDef.role == "prd")))
    assert [d.id for d in left] == [old.id]  # остался старейший (раннее наблюдение)
    assert session.get(ArtifactDef, dup.id) is None
    # снапшоты дубля удалены, вердикт жив, провенанс — на эквивалент того же хеша
    dup_snaps = list(session.scalars(
        select(ArtifactSnapshot).where(ArtifactSnapshot.artifact_def_id == dup.id)
    ))
    assert dup_snaps == []
    session.refresh(verdict)
    src = session.get(ArtifactSnapshot, verdict.source_snapshot_id)
    assert src is not None and src.artifact_def_id == old.id
    assert src.content_hash == verdict.source_content_hash  # канон Б1 не тронут


def test_dedupe_idempotent(session):
    config, *_ = _seed_dup(session)
    config_manager.reconcile(session, config)
    session.flush()
    summary2 = config_manager.reconcile(session, config)
    assert summary2.artifact_defs_deduped == 0


def test_no_dups_untouched(session):
    config = config_manager.parse_config(YAML)
    config_manager.reconcile(session, config)
    session.flush()
    summary = config_manager.reconcile(session, config)
    assert summary.artifact_defs_deduped == 0
    assert len(list(session.scalars(select(ArtifactDef)))) == 2


def test_alternative_patterns_not_a_dup(session):
    config = config_manager.parse_config(YAML)
    config_manager.reconcile(session, config)
    session.flush()
    prd = session.scalar(select(ArtifactDef).where(ArtifactDef.role == "prd"))
    session.add(ArtifactDef(
        lesson_id=prd.lesson_id, role="prd", expected_pattern="REQUIREMENTS.md"
    ))
    session.flush()
    summary = config_manager.reconcile(session, config)
    assert summary.artifact_defs_deduped == 0  # альтернативный путь — не дубль
    assert len(list(session.scalars(select(ArtifactDef).where(ArtifactDef.role == "prd")))) == 2


def test_no_equivalent_snapshot_group_skipped(session):
    """Fail-safe: вердикт на снапшот дубля без эквивалента у хранимого — группу не трогаем."""
    config, old, dup, verdict = _seed_dup(session)
    # у хранимого нет снапшота с хешем «d» — а вердикт ссылается именно на такой
    run2 = store.register_sync_run(session, triggered_by=SyncTrigger.schedule)
    session.flush()
    repo_id = session.get(ArtifactSnapshot, verdict.target_snapshot_id).repository_id
    orphan = store.register_snapshot(
        session, sync_run_id=run2.id, repository_id=repo_id, artifact_def_id=dup.id,
        status=SnapshotStatus.found, content_hash="d" * 64,
        file_path="product/prd.md", source_commit_sha="c" * 40,
        observed_at=datetime(2026, 7, 30, 12, 0),
    )
    session.flush()
    store.register_verdict(
        session, edge_def_id=verdict.edge_def_id,
        source_snapshot_id=orphan.id, target_snapshot_id=verdict.target_snapshot_id,
        source_content_hash=orphan.content_hash, target_content_hash=verdict.target_content_hash,
        rubric_id=verdict.rubric_id, llm_model=LLM_MODEL, verdict="ok", confidence="high",
    )
    session.flush()
    summary = config_manager.reconcile(session, config)
    session.flush()
    assert summary.artifact_defs_deduped == 0  # группа пропущена, ничего не удалено
    assert session.get(ArtifactDef, dup.id) is not None
