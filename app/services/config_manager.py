"""config_manager — config.yaml → конфиг-реконсиляция БД (S4, #6; FR-2).

Источник правды о конфигурации курса — config.yaml (§3.4); БД — его отражение.
Единственный модуль, которому разрешены функции конфиг-реконсиляции store.py
(категория 3 контракта §3.5, ADR-005). Rubric в категорию 3 не входит:
новая версия — только register_rubric (append-only, И5).
"""

from datetime import date as date_type
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from app import store
from app.config import settings
from app.models import ArtifactRole, GitHost, RubricType


class ProbeConfig(BaseModel):
    """T2 (#44): проба содержимого — только объявленные требования курса.

    contains — finding, если regex НЕ найден (обязательный раздел отсутствует);
    not_contains — finding, если regex найден (маркер незаполненного шаблона).
    Ровно одно из двух полей (fix по ревью T2: два поля давали неочевидную семантику).
    """

    key: str
    label: str
    contains: str | None = None
    not_contains: str | None = None

    @model_validator(mode="after")
    def _exactly_one_condition(self):
        if bool(self.contains) == bool(self.not_contains):
            raise ValueError(
                f"проба {self.key}: ровно одно из contains/not_contains (конфиг fail-fast)"
            )
        return self


class ArtifactConfig(BaseModel):
    role: ArtifactRole
    expected_pattern: str
    template_relative_path: str | None = None
    content_probes: list[ProbeConfig] | None = None


class LessonConfig(BaseModel):
    number: int
    title: str
    date: date_type
    # FR-12: канал сдачи занятия — с занятия 11 сдача идёт через MR (ADR-007)
    submission_channel: Literal["files", "mr"] = "files"
    artifacts: list[ArtifactConfig] = Field(default_factory=list)


class RubricConfig(BaseModel):
    version: str
    text: str
    items: dict | None = None


class EdgeConfig(BaseModel):
    source_role: ArtifactRole
    target_role: ArtifactRole
    rubric: RubricConfig


class ProcessMarkerConfig(BaseModel):
    """FR-12 (ADR-007): маркер недели — строка-шаблон в описании MR."""

    key: str
    pattern: str  # regex (обычно с (?im): начало строки, регистронезависимо)
    # привязка маркера к занятию отложена: у MR нет связи с занятием в v1 (ADR-007)


class TemplateRepoConfig(BaseModel):
    """PRD FR-4: адрес репозитория-шаблона задаётся в конфиге FR-2 (D35, детект заготовок)."""

    url: str
    git_host: GitHost = GitHost.GitHub
    branch: str = "main"


class ConfigYAML(BaseModel):
    lessons: list[LessonConfig]
    edges: list[EdgeConfig] = Field(default_factory=list)
    template_repo: TemplateRepoConfig | None = None
    # FR-12: None — MR-шаг обхода выключен; список (даже пустой) — включён
    process_markers: list[ProcessMarkerConfig] | None = None


class ReloadSummary(BaseModel):
    lessons_created: int = 0
    lessons_updated: int = 0
    artifact_defs_created: int = 0
    artifact_defs_updated: int = 0
    # D23 (итерация 5): удалённые дубли определений (след смены ключа реконсиляции)
    artifact_defs_deduped: int = 0
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
    # D23: сперва чистка дублей (см. store) — upsert ниже работает по чистому ключу
    summary.artifact_defs_deduped = store.config_dedupe_artifact_defs(session)

    for lesson_cfg in config.lessons:
        lesson, outcome = store.config_upsert_lesson(
            session, number=lesson_cfg.number, title=lesson_cfg.title, date=lesson_cfg.date,
            submission_channel=lesson_cfg.submission_channel,
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
                content_probes=(
                    [probe.model_dump() for probe in artifact_cfg.content_probes]
                    if artifact_cfg.content_probes else None
                ),
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
