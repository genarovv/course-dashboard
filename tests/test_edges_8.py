"""R (#42): рёбра связности 2→8 (+prd→architecture = 9) — волна 3, решение CEO 2026-07-28.

Полный конвейер курса: jtbd→US, persona→US, US→prd, prd→data_model,
data_model→architecture, architecture→plan, plan→code, code→tests
+ prd→architecture (пакет «12 артефактов»). Тексты рубрик — драфт агента,
утверждение CEO; версия 1.0; смена текста = новая строка Rubric + прогон golden set.
"""

from alembic.config import Config
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.models.edge_def import EdgeDef
from app.models.rubric import Rubric
from app.services import config_manager

EXPECTED_EDGES = {
    ("jtbd", "user_story"),
    ("persona", "user_story"),
    ("user_story", "prd"),
    ("prd", "data_model"),
    ("data_model", "architecture"),
    ("architecture", "plan"),
    ("plan", "code"),
    ("code", "tests"),
    ("prd", "architecture"),
}


def test_config_has_all_nine_edges():
    config = config_manager.load_config()
    edges = {(str(e.source_role), str(e.target_role)) for e in config.edges}
    assert edges == EXPECTED_EDGES


def test_each_rubric_has_version_and_output_contract():
    config = config_manager.load_config()
    for edge in config.edges:
        rubric = edge.rubric
        assert rubric.version, f"нет версии рубрики у {edge.source_role}->{edge.target_role}"
        # контракт выхода §5.2 обязан быть в каждом тексте — иначе ответ не распарсится
        assert "entities_checked" in rubric.text, (
            f"рубрика {edge.source_role}->{edge.target_role} без контракта счётчиков"
        )
        assert len(rubric.text) > 200, (
            f"рубрика {edge.source_role}->{edge.target_role} подозрительно коротка"
        )


def test_rubrics_are_edge_specific():
    """Каждая рубрика говорит о своей паре ролей, а не копия соседней."""
    config = config_manager.load_config()
    markers = {
        ("jtbd", "user_story"): "JTBD",
        ("persona", "user_story"): "персон",
        ("user_story", "prd"): "user stor",
        ("architecture", "plan"): "план",
        ("plan", "code"): "код",
        ("code", "tests"): "тест",
    }
    texts = {
        (str(e.source_role), str(e.target_role)): e.rubric.text for e in config.edges
    }
    for pair, marker in markers.items():
        assert marker.lower() in texts[pair].lower(), f"рубрика {pair} не упоминает «{marker}»"
    # тексты попарно различны
    assert len(set(texts.values())) == len(texts)


def test_reconcile_creates_nine_edges_idempotently(tmp_path):
    db_path = tmp_path / "edges.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as session:
        config_manager.reconcile(session, config_manager.load_config())
        session.commit()
        pairs = {
            (str(e.source_role), str(e.target_role))
            for e in session.scalars(select(EdgeDef))
        }
        assert pairs == EXPECTED_EDGES
        assert len(list(session.scalars(select(Rubric)))) == 9

        summary = config_manager.reconcile(session, config_manager.load_config())
        session.commit()
        assert summary.edges_created == 0
        assert summary.rubrics_registered == 0
        assert len(list(session.scalars(select(Rubric)))) == 9  # append-only, без дублей
    engine.dispose()


# ── решение CEO 2026-07-30: мультифайловая сторона пары = список путей ──────


def test_multifile_side_sends_path_listing_to_llm(tmp_path):
    """Рёбра к коду (plan→code, code→tests): вместо «представителя» (первый файл
    по алфавиту, ложные break) LLM получает список путей всех файлов связки —
    рубрика «след задачи: модуль/файл по смыслу» работает по именам."""
    import asyncio
    from datetime import datetime

    from app import store
    from app.models import GitHost, SnapshotStatus, SyncTrigger
    from app.models.artifact_def import ArtifactDef
    from app.models.lesson import Lesson
    from app.services.coherence_analyzer import ensure_verdict
    from app.services.sync_orchestrator import PendingPair

    db_path = tmp_path / "multi_llm.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as s:
        lesson8 = Lesson(number=8, title="План", date=datetime(2026, 7, 9).date())
        lesson9 = Lesson(number=9, title="Код", date=datetime(2026, 7, 14).date())
        s.add_all([lesson8, lesson9])
        s.flush()
        adef_plan = ArtifactDef(lesson_id=lesson8.id, role="plan", expected_pattern="plan.md")
        adef_code = ArtifactDef(lesson_id=lesson9.id, role="code", expected_pattern="lib/**/*.dart")
        s.add_all([adef_plan, adef_code])
        rubric = store.register_rubric(s, type="edge", version="1.0", text="план → код")
        s.flush()
        edge = store.config_create_edge_def(
            s, source_role="plan", target_role="code", rubric_id=rubric.id
        )
        repo = store.register_repository(
            s, repo_url="https://github.com/u/r", git_host=GitHost.GitHub
        )
        run = store.register_sync_run(s, triggered_by=SyncTrigger.manual)
        s.flush()
        snap_plan = store.register_snapshot(
            s, sync_run_id=run.id, repository_id=repo.id, artifact_def_id=adef_plan.id,
            status=SnapshotStatus.found, content_hash="a" * 64,
            file_path="plan.md", source_commit_sha="c" * 40,
        )
        snap_code = store.register_snapshot(
            s, sync_run_id=run.id, repository_id=repo.id, artifact_def_id=adef_code.id,
            status=SnapshotStatus.found, content_hash="b" * 64,
            file_path="lib/a_first.dart", source_commit_sha="c" * 40,  # «представитель»
        )
        s.flush()
        pair = PendingPair(
            edge_def_id=edge.id, repository_id=repo.id,
            source_snapshot_id=snap_plan.id, target_snapshot_id=snap_code.id,
            source_content_hash="a" * 64, target_content_hash="b" * 64,
            rubric_id=rubric.id, llm_model="deepseek-v4-flash",
        )

        class TreeGit:
            async def get_tree(self, repo_url, git_host, ref="main"):
                return ["plan.md", "lib/a_first.dart", "lib/z_payments.dart"]

            async def get_file_content(self, repo_url, git_host, file_path, ref="main"):
                assert file_path == "plan.md"  # контент качается только для одиночной стороны
                return "# план: T-1 оплата"

        captured = {}

        class SpyLLM:
            async def check_coherence(self, source_text, target_text, rubric_text, model=None):
                captured["source"], captured["target"] = source_text, target_text
                return {
                    "verdict": "ok", "confidence": "high",
                    "entities_checked": 1, "entities_found": 1,
                    "entities_excluded": 0, "entities_lost": 0,
                    "points": [], "notes": "",
                }

        verdict = asyncio.run(ensure_verdict(s, TreeGit(), SpyLLM(), pair))
        assert verdict is not None and str(verdict.verdict) == "ok"
        assert captured["source"] == "# план: T-1 оплата"
        # мультифайловая сторона — список путей связки, а не контент представителя
        assert "lib/a_first.dart" in captured["target"]
        assert "lib/z_payments.dart" in captured["target"]
        assert "plan.md" not in captured["target"]  # чужие файлы в связку не попадают
    engine.dispose()
