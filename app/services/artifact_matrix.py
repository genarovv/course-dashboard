"""artifact_matrix — проекция «репозиторий × артефакт» (D7; макет CEO 2026-07-30).

Колонки — роли артефактов из конфига (FR-2) в порядке первого появления в курсе.
Ячейка — лучший снапшот роли (best-wins между альтернативными путями, как в
карточке студента) плюс свод вердиктов рёбер FR-5, где роль участвует: разрыв
называет первую потерянную сущность. Детали ячейки (файлы, рёбра с цитатами,
заметки, FR-10) отдаёт build_cell_details — модальное окно в UI.
"""

from sqlalchemy.orm import Session

from app import store, timeutil
from app.config import settings
from app.models import SNAPSHOT_STATUS_RANK, ArtifactRole, SnapshotStatus, VerdictValue
from app.services import evidence_chain

ROLE_TITLES: dict[ArtifactRole, str] = {
    ArtifactRole.interview: "Интервью",
    ArtifactRole.persona: "Персоны",
    ArtifactRole.user_story: "User stories",
    ArtifactRole.prd: "PRD",
    ArtifactRole.data_model: "Схема данных",
    ArtifactRole.architecture: "Архитектура",
    ArtifactRole.plan: "План",
    ArtifactRole.code: "Код",
    ArtifactRole.tests: "Тесты",
    ArtifactRole.jtbd: "JTBD",
    ArtifactRole.readme: "README",
    ArtifactRole.changelog: "CHANGELOG",
    ArtifactRole.adr: "ADR",
    ArtifactRole.claude_md: "CLAUDE.md",
    ArtifactRole.roles_roster: "Ростер ролей",
}

PARTIAL_LABELS = {
    "template_copy": "заготовка из шаблона",
    "inexact_name": "неточное имя файла",
    "wrong_place": "не в ожидаемом месте",
}


def _best_snapshot(session: Session, repository_id: str, defs: list):
    """Лучший снапшот роли среди альтернативных путей (ранг статуса, затем свежесть).

    В отличие от карточки (evidence_chain) not_found не отфильтровывается:
    матрице нужно честное «нет», а не пустая ячейка.
    """
    candidates = [
        snap
        for adef in defs
        if (snap := store.find_last_snapshot(session, repository_id, adef.id)) is not None
    ]
    return min(
        candidates,
        key=lambda snap: (SNAPSHOT_STATUS_RANK[snap.status], -snap.observed_at.timestamp()),
        default=None,
    )


def _edges_touching(edges: list[dict], role: ArtifactRole) -> list[dict]:
    return [e for e in edges if role in (e["source_role"], e["target_role"])]


def _cell(session: Session, repository_id: str, defs: list, edges: list[dict]) -> dict:
    """Ячейка: статус + усечённый результат анализа (summary) + счётчики рёбер."""
    snap = _best_snapshot(session, repository_id, defs)
    touching = _edges_touching(edges, defs[0].role)
    breaks = [
        e for e in touching
        if e["state"] == "done" and e["verdict"] == VerdictValue.break_ and not e["override_active"]
    ]
    pending = [e for e in touching if e["state"] == "pending"]
    oks = [
        e for e in touching
        if e["state"] == "done" and (e["verdict"] == VerdictValue.ok or e["override_active"])
    ]

    if snap is None:
        summary = None
    elif snap.status == SnapshotStatus.not_found:
        summary = "нет"
    elif snap.status == SnapshotStatus.partial:
        reasons = [PARTIAL_LABELS.get(r, r) for r in (snap.partial_reason or [])]
        summary = "частично · " + ", ".join(reasons) if reasons else "частично"
    elif breaks:
        first_point = next((p for e in breaks for p in e["points"]), None)
        lost = f": потеряна «{first_point['entity']}»" if first_point else ""
        count = f" ×{len(breaks)}" if len(breaks) > 1 else ""
        summary = f"есть · разрыв{count}{lost}"
    elif pending:
        summary = "есть · связность проверяется"
    elif oks:
        summary = "есть · связность ок"
    else:
        summary = "есть"

    return {
        "status": snap.status if snap else None,
        "partial_reason": snap.partial_reason if snap else None,
        "summary": summary,
        "break_count": len(breaks),
        "pending_count": len(pending),
        "ok_count": len(oks),
    }


def _defs_by_role_in_course_order(session: Session) -> dict[ArtifactRole, list]:
    """Роли из конфига, упорядоченные по номеру первого занятия, где роль ожидается."""
    defs_by_role: dict[ArtifactRole, list] = {}
    first_lesson: dict[ArtifactRole, int] = {}
    for lesson in store.find_all_lessons(session):
        for adef in store.find_artifact_defs_by_lesson(session, lesson.id):
            defs_by_role.setdefault(adef.role, []).append(adef)
            first_lesson[adef.role] = min(
                first_lesson.get(adef.role, lesson.number), lesson.number
            )
    return dict(sorted(defs_by_role.items(), key=lambda item: (first_lesson[item[0]], item[0])))


def build_artifact_matrix(
    session: Session, llm_model: str | None = None
) -> dict:
    """Матрица «репозиторий × артефакт» для GET /artifacts."""
    resolved_model = llm_model or settings.deepseek_model
    checked_ids = store.find_checked_repository_ids(session)
    active_repos = store.find_active_repositories(session)
    repos = [r for r in active_repos if r.id in checked_ids]
    defs_by_role = _defs_by_role_in_course_order(session)

    cells: dict[str, dict[str, dict]] = {}
    for repo in repos:
        edges = evidence_chain.edge_states(session, repo.id, resolved_model)
        cells[repo.id] = {
            str(role): _cell(session, repo.id, defs, edges)
            for role, defs in defs_by_role.items()
        }

    last_run = store.find_last_sync_run(session)
    # Макет: «Время и дата последнего анализа: День.Месяц ЧЧ:ММ» (местное время, #32)
    as_of = (
        f"{timeutil.to_display(last_run.started_at):%d.%m %H:%M} ({timeutil.offset_label()})"
        if last_run else "—"
    )

    return {
        "roles": [
            {"key": str(role), "title": ROLE_TITLES.get(role, str(role))}
            for role in defs_by_role
        ],
        "repositories": [{"id": r.id, "repo_url": r.repo_url} for r in repos],
        "cells": cells,
        "as_of": as_of,
        "registry_count": len(active_repos),
    }


def build_cell_details(
    session: Session, repository_id: str, role: str, llm_model: str | None = None
) -> dict | None:
    """Детали ячейки для модального окна: файлы роли, рёбра с точками и заметками.

    Возвращает None при неизвестной роли или репозитории (роут отвечает 404).
    """
    try:
        role_enum = ArtifactRole(role)
    except ValueError:
        return None
    repo = store.find_repository_by_id(session, repository_id)
    defs = store.find_artifact_defs_by_role(session, role_enum)
    if repo is None or not defs:
        return None
    resolved_model = llm_model or settings.deepseek_model

    files = []
    for adef in defs:
        snap = store.find_last_snapshot(session, repository_id, adef.id)
        files.append({
            "expected_pattern": adef.expected_pattern,
            "file_path": snap.file_path if snap else None,
            "status": snap.status if snap else None,
            "partial_reason": [
                PARTIAL_LABELS.get(r, r) for r in ((snap.partial_reason or []) if snap else [])
            ],
            "observed_at": snap.observed_at.isoformat() if snap else None,
            "source_commit_sha": snap.source_commit_sha if snap else None,
        })

    edges = _edges_touching(
        evidence_chain.edge_states(session, repository_id, resolved_model), role_enum
    )
    return {
        "repository": {"id": repo.id, "repo_url": repo.repo_url},
        "role": str(role_enum),
        "title": ROLE_TITLES.get(role_enum, str(role_enum)),
        "files": files,
        "edges": edges,
    }
