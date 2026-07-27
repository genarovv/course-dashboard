"""config_manager — config.yaml → конфиг-реконсиляция БД (S4, #6; FR-2).

Источник правды о конфигурации курса — config.yaml (§3.4); БД — его отражение.
Единственный модуль, которому разрешены функции конфиг-реконсиляции store.py
(категория 3 контракта §3.5, ADR-005). Rubric в категорию 3 не входит:
новая версия — только register_rubric (append-only, И5).
"""

from datetime import date as date_type
from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import store
from app.config import settings
from app.models import ArtifactRole, GitHost, RubricType


class ArtifactConfig(BaseModel):
    role: ArtifactRole
    expected_pattern: str
    template_relative_path: str | None = None


class LessonConfig(BaseModel):
    number: int
    title: str
    date: date_type
    artifacts: list[ArtifactConfig] = Field(default_factory=list)


class RubricConfig(BaseModel):
    version: str
    text: str
    items: dict | None = None


class EdgeConfig(BaseModel):
    source_role: ArtifactRole
    target_role: ArtifactRole
    rubric: RubricConfig


class TemplateRepoConfig(BaseModel):
    """PRD FR-4: адрес репозитория-шаблона задаётся в конфиге FR-2 (D35, детект заготовок)."""

    url: str
    git_host: GitHost = GitHost.GitHub
    branch: str = "main"


class ConfigYAML(BaseModel):
    lessons: list[LessonConfig]
    edges: list[EdgeConfig] = Field(default_factory=list)
    template_repo: TemplateRepoConfig | None = None


class ReloadSummary(BaseModel):
    lessons_created: int = 0
    lessons_updated: int = 0
    artifact_defs_created: int = 0
    artifact_defs_updated: int = 0
    edges_created: int = 0
    edges_repointed: int = 0
    rubrics_registered: int = 0
    # Смена рубрики = обязательный прогон golden set (железное правило CLAUDE.md)
    rubric_changes: list[str] = Field(default_factory=list)
    golden_set_required: bool = False


def parse_config(yaml_text: str) -> ConfigYAML:
    """YAML-текст → валидированный ConfigYAML (Pydantic, enum-домены из models)."""
    return ConfigYAML.model_validate(yaml.safe_load(yaml_text))


def load_config(path: str | Path | None = None) -> ConfigYAML:
    """Чтение эталонного config.yaml (по умолчанию — settings.config_yaml_path)."""
    return parse_config(Path(path or settings.config_yaml_path).read_text(encoding="utf-8"))


def reconcile(session: Session, config: ConfigYAML) -> ReloadSummary:
    """Идемпотентная реконсиляция БД под config.yaml (ADR-005).

    Lesson/ArtifactDef/EdgeDef.rubric_id — конфиг-мутации (категория 3);
    новая версия рубрики — append-only строка; старые вердикты не трогаются
    (привязаны к своим версиям через rubric_id в четвёрке И3).
    Удалений нет by design: ушедшие из YAML сущности остаются в БД.
    """
    summary = ReloadSummary()

    for lesson_cfg in config.lessons:
        lesson, outcome = store.config_upsert_lesson(
            session, number=lesson_cfg.number, title=lesson_cfg.title, date=lesson_cfg.date
        )
        _count(summary, "lessons", outcome)
        session.flush()
        for artifact_cfg in lesson_cfg.artifacts:
            _, adef_outcome = store.config_upsert_artifact_def(
                session,
                lesson_id=lesson.id,
                role=artifact_cfg.role,
                expected_pattern=artifact_cfg.expected_pattern,
                template_relative_path=artifact_cfg.template_relative_path,
            )
            _count(summary, "artifact_defs", adef_outcome)

    for edge_cfg in config.edges:
        edge = store.find_edge_def_by_roles(session, edge_cfg.source_role, edge_cfg.target_role)
        if edge is None:
            rubric = _register_rubric(session, edge_cfg, summary)
            store.config_create_edge_def(
                session,
                source_role=edge_cfg.source_role,
                target_role=edge_cfg.target_role,
                rubric_id=rubric.id,
            )
            summary.edges_created += 1
            continue
        current = session.get(store.Rubric, edge.rubric_id)
        if (current.version, current.text, current.items) != (
            edge_cfg.rubric.version, edge_cfg.rubric.text, edge_cfg.rubric.items
        ):
            rubric = _register_rubric(session, edge_cfg, summary)
            store.config_repoint_edge_rubric(session, edge_def_id=edge.id, rubric_id=rubric.id)
            summary.edges_repointed += 1
            summary.rubric_changes.append(f"{edge_cfg.source_role}→{edge_cfg.target_role}")
            summary.golden_set_required = True

    session.flush()
    return summary


def _register_rubric(session: Session, edge_cfg: EdgeConfig, summary: ReloadSummary):
    rubric = store.register_rubric(
        session,
        type=RubricType.edge,
        version=edge_cfg.rubric.version,
        text=edge_cfg.rubric.text,
        items=edge_cfg.rubric.items,
    )
    session.flush()
    summary.rubrics_registered += 1
    return rubric


def _count(summary: ReloadSummary, prefix: str, outcome: str) -> None:
    if outcome != "unchanged":
        setattr(summary, f"{prefix}_{outcome}", getattr(summary, f"{prefix}_{outcome}") + 1)
