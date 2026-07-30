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
