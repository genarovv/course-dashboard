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
