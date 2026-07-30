"""T3 (#43) + пакет «12 артефактов» (решение CEO 2026-07-30): конфиг покрывает
описание курса, а не структуру демо-проекта.

Драйвер — ручной аудит лучшей репы (С-01): 10 из 12 артефактов есть, конфиг видел 1.
Причины: Python-центричные паттерны code/tests, единственный путь на роль,
отсутствие ролей jtbd/readme/changelog/adr/claude_md/roles_roster.
"""

from alembic.config import Config
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.models import ArtifactRole
from app.models.artifact_def import ArtifactDef
from app.models.edge_def import EdgeDef
from app.services import config_manager


def load_real_config():
    return config_manager.load_config()


def _defs_by_role(config):
    by_role: dict[str, list[str]] = {}
    for lesson in config.lessons:
        for artifact in lesson.artifacts:
            by_role.setdefault(str(artifact.role), []).append(artifact.expected_pattern)
    return by_role


# ── новые роли артефактов (арт. 1, 2, 7, 9 из описания курса) ───────────────


def test_new_roles_exist_in_enum():
    for name in ("jtbd", "readme", "changelog", "adr", "claude_md", "roles_roster"):
        assert hasattr(ArtifactRole, name), f"нет роли {name}"


def test_real_config_covers_new_roles():
    by_role = _defs_by_role(load_real_config())
    for role in ("jtbd", "readme", "adr", "claude_md", "roles_roster"):
        assert role in by_role, f"роль {role} не привязана ни к одному занятию"


# ── альтернативные пути: конфиг матчит и шаблон, и стиль лучшей репы ────────


def test_alternative_patterns_for_existing_roles():
    by_role = _defs_by_role(load_real_config())
    # схема данных: из-за единственного пути была причина «0 пар» боевого обхода
    assert any("DATA_MODEL" in p for p in by_role["data_model"])
    assert "data-model.md" in by_role["data_model"]
    # требования: С-01 ведёт REQUIREMENTS.md
    assert any("REQUIREMENTS" in p for p in by_role["prd"])
    assert "product/prd.md" in by_role["prd"]
    # план: С-01 ведёт plan.md в корне
    assert "plan.md" in by_role["plan"] or "plans/plan.md" in by_role["plan"]
    assert any(p == "plan.md" for p in by_role["plan"])


def test_code_and_tests_are_stack_agnostic():
    by_role = _defs_by_role(load_real_config())
    code = " ".join(by_role["code"])
    tests = " ".join(by_role["tests"])
    # не только Python: Flutter/Dart (С-01), JS (functions), общий src/
    assert ".dart" in code and ".js" in code and "src/" in code
    assert ".dart" in tests or "test/**" in tests


# ── ребро prd → architecture (живые вердикты уже на текущих данных) ─────────


def test_edge_prd_to_architecture_configured():
    config = load_real_config()
    edges = {(str(e.source_role), str(e.target_role)) for e in config.edges}
    assert ("prd", "architecture") in edges
    assert ("prd", "data_model") in edges  # старые рёбра не потеряны
    assert ("data_model", "architecture") in edges


# ── реконсиляция реального конфига в чистую БД ──────────────────────────────


def test_reconcile_real_config(tmp_path):
    db_path = tmp_path / "cfg.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as session:
        config_manager.reconcile(session, load_real_config())
        session.commit()
        roles = {d.role for d in session.scalars(select(ArtifactDef))}
        for role in (ArtifactRole.jtbd, ArtifactRole.readme, ArtifactRole.claude_md,
                     ArtifactRole.roles_roster, ArtifactRole.adr):
            assert role in roles
        edge_pairs = {(e.source_role, e.target_role) for e in session.scalars(select(EdgeDef))}
        assert (ArtifactRole.prd, ArtifactRole.architecture) in edge_pairs
    engine.dispose()


# ── fix: несколько паттернов одной роли переживают реконсиляцию ─────────────


def _mini_config(patterns):
    """Мини-конфиг: одно занятие, роль data_model с заданными паттернами."""
    from app.services.config_manager import ConfigYAML

    return ConfigYAML.model_validate({
        "lessons": [{
            "number": 6,
            "title": "Данные",
            "date": "2026-07-02",
            "artifacts": [
                {"role": "data_model", "expected_pattern": p} for p in patterns
            ],
        }],
        "edges": [],
    })


def test_reconcile_keeps_multiple_patterns_per_role(tmp_path):
    """Ключ дефа — (занятие, роль, паттерн): альтернативные пути не схлопываются.

    Боевой прогон 2026-07-30 показал: со старым ключом (занятие, роль) второй
    паттерн затирал первый — конфиг «подгонялся» под последнего студента.
    """
    db_path = tmp_path / "multi.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as session:
        config_manager.reconcile(session, _mini_config(["data-model.md", "DATA_MODEL.md"]))
        session.commit()
        patterns = sorted(
            d.expected_pattern for d in session.scalars(select(ArtifactDef))
        )
        assert patterns == ["DATA_MODEL.md", "data-model.md"]

        # идемпотентность: повторный reconcile ничего не плодит
        summary = config_manager.reconcile(session, _mini_config(["data-model.md", "DATA_MODEL.md"]))
        session.commit()
        assert summary.artifact_defs_created == 0
        assert len(list(session.scalars(select(ArtifactDef)))) == 2
    engine.dispose()


def test_real_config_reconciles_all_patterns(tmp_path):
    db_path = tmp_path / "full.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    config = load_real_config()
    expected = sum(len(lesson.artifacts) for lesson in config.lessons)
    with Session(engine) as session:
        config_manager.reconcile(session, config)
        session.commit()
        assert len(list(session.scalars(select(ArtifactDef)))) == expected
    engine.dispose()


# ── fix: агрегация ячейки — альтернативы внутри роли по лучшему статусу ─────


def test_cell_aggregation_alternatives_best_wins(tmp_path):
    """Внутри роли (альтернативные пути) побеждает лучший статус; между ролями —
    прежнее строгое правило (все found → found, все not_found → not_found, иначе partial)."""
    from datetime import datetime

    from app import store
    from app.models import GitHost, SnapshotStatus, SyncTrigger
    from app.models.lesson import Lesson
    from app.services.matrix_builder import _aggregate_cell

    db_path = tmp_path / "agg.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as s:
        lesson = Lesson(number=5, title="PRD", date=datetime(2026, 6, 30).date())
        s.add(lesson)
        s.flush()
        d1 = ArtifactDef(lesson_id=lesson.id, role="prd", expected_pattern="product/prd.md")
        d2 = ArtifactDef(lesson_id=lesson.id, role="prd", expected_pattern="REQUIREMENTS.md")
        s.add_all([d1, d2])
        repo = store.register_repository(s, repo_url="https://github.com/u/r", git_host=GitHost.GitHub)
        run = store.register_sync_run(s, triggered_by=SyncTrigger.manual)
        s.flush()
        # альтернатива 1 не найдена, альтернатива 2 найдена → роль prd = found
        store.register_snapshot(s, sync_run_id=run.id, repository_id=repo.id,
                                artifact_def_id=d1.id, status=SnapshotStatus.not_found)
        store.register_snapshot(s, sync_run_id=run.id, repository_id=repo.id,
                                artifact_def_id=d2.id, status=SnapshotStatus.found,
                                content_hash="a" * 64, file_path="REQUIREMENTS.md",
                                source_commit_sha="c" * 40)
        s.flush()
        cell = _aggregate_cell(s, repo.id, [d1, d2])
        assert cell["status"] == SnapshotStatus.found  # а не partial
    engine.dispose()


def test_evidence_chain_prefers_found_alternative_over_fresh_template_copy(tmp_path):
    """Блокер ревью T3: карточка выбирала альтернативу роли по свежести —
    свежая partial-заготовка шаблона побеждала настоящий found-документ,
    и вердикт на защите считался бы по пустой заготовке (ложный break)."""
    from datetime import datetime, timedelta

    from app import store
    from app.models import GitHost, SnapshotStatus, SyncTrigger
    from app.models.lesson import Lesson
    from app.services.evidence_chain import _latest_snapshot_for_role

    db_path = tmp_path / "chain.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as s:
        lesson = Lesson(number=5, title="PRD", date=datetime(2026, 6, 30).date())
        s.add(lesson)
        s.flush()
        d_tpl = ArtifactDef(lesson_id=lesson.id, role="prd", expected_pattern="product/prd.md")
        d_req = ArtifactDef(lesson_id=lesson.id, role="prd", expected_pattern="REQUIREMENTS.md")
        s.add_all([d_tpl, d_req])
        repo = store.register_repository(s, repo_url="https://github.com/u/r", git_host=GitHost.GitHub)
        run = store.register_sync_run(s, triggered_by=SyncTrigger.manual)
        s.flush()
        older = datetime(2026, 7, 20, 10, 0)
        real = store.register_snapshot(
            s, sync_run_id=run.id, repository_id=repo.id, artifact_def_id=d_req.id,
            status=SnapshotStatus.found, content_hash="a" * 64,
            file_path="REQUIREMENTS.md", source_commit_sha="c" * 40,
        )
        real.observed_at = older
        template_copy = store.register_snapshot(
            s, sync_run_id=run.id, repository_id=repo.id, artifact_def_id=d_tpl.id,
            status=SnapshotStatus.partial, content_hash="b" * 64,
            file_path="product/prd.md", source_commit_sha="c" * 40,
            partial_reason=["template_copy"],
        )
        template_copy.observed_at = older + timedelta(days=1)  # заготовка свежее
        s.flush()

        chosen = _latest_snapshot_for_role(s, repo.id, "prd")
        assert chosen.file_path == "REQUIREMENTS.md"  # found важнее свежести заготовки
    engine.dispose()
