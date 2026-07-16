"""FR-4/6/7/10: Проекция матрицы с фильтрацией override (ARCHITECTURE §5.3).

build_matrix(session) → MatrixView:
  1. Последние снапшоты на (repository × artifact_def)
  2. Вердикты для рёбер
  3. Фильтрация вердиктов с активным Override (FR-10) — не показываем как разрыв
  4. Последние исходы обхода (для слепых зон)
"""

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import store
from app.models import SyncOutcome
from app.models.artifact_def import ArtifactDef
from app.models.coherence_verdict import CoherenceVerdict
from app.models.edge_def import EdgeDef
from app.models.repository import Repository


@dataclass
class VerdictCell:
    """Одна ячейка матрицы: ребро × репозиторий."""
    edge: EdgeDef
    repository: Repository
    verdict: CoherenceVerdict | None = None
    is_overridden: bool = False
    override_reason: str | None = None


@dataclass
class MatrixView:
    """Итоговая матрица для шаблона."""
    repositories: list[Repository]
    artifact_defs: list[ArtifactDef]
    verdict_cells: list[VerdictCell] = field(default_factory=list)
    blind_spot_repo_ids: list[str] = field(default_factory=list)
    chronic_repo_ids: list[str] = field(default_factory=list)


def build_matrix(session: Session) -> MatrixView:
    """Собрать матрицу: снапшоты → вердикты → override-фильтрация (FR-10).

    Не кодит FR-5 ядро (coherence_analyzer) — только читает готовые вердикты.
    """
    repositories = list(store.find_active_repositories(session))
    repo_ids = {r.id for r in repositories}

    snapshots = store.find_last_snapshots(session)

    all_verdicts = store.find_all_verdicts(session)

    # Группируем вердикты по (edge_def_id, repository_id) — берём последний
    verdicts_by_edge_repo: dict[tuple[str, str], CoherenceVerdict] = {}
    for v in all_verdicts:
        source_snap = next(
            (s for s in snapshots if s.id == v.source_snapshot_id), None
        )
        if source_snap is None or source_snap.repository_id not in repo_ids:
            continue
        key = (v.edge_def_id, source_snap.repository_id)
        existing = verdicts_by_edge_repo.get(key)
        if existing is None or v.computed_at > existing.computed_at:
            verdicts_by_edge_repo[key] = v

    # Ячейки матрицы
    edge_defs = list(session.scalars(select(EdgeDef)))
    cells: list[VerdictCell] = []
    for edge in edge_defs:
        for repo in repositories:
            verdict = verdicts_by_edge_repo.get((edge.id, repo.id))
            has_override = (
                verdict is not None
                and store.find_active_override_for_verdict(
                    session, verdict.id
                )
                is not None
            )
            cells.append(
                VerdictCell(
                    edge=edge,
                    repository=repo,
                    verdict=verdict if not has_override else None,
                    is_overridden=has_override,
                    override_reason="override" if has_override else None,
                )
            )

    # Слепые зоны: последний outcome = repo_unavailable
    last_outcomes = store.find_last_outcomes(session)
    blind_spot_ids = [
        o.repository_id
        for o in last_outcomes
        if o.outcome == SyncOutcome.repo_unavailable
    ]

    return MatrixView(
        repositories=repositories,
        artifact_defs=list(session.scalars(select(ArtifactDef))),
        verdict_cells=cells,
        blind_spot_repo_ids=blind_spot_ids,
    )
