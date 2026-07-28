"""matrix_builder — проекция матрицы «репозиторий × занятие» (D1, #12; ARCHITECTURE §5.3).

Читает последние снапшоты из БД, группирует по (repository, lesson).
Возвращает dict, пригодный для рендеринга Jinja2/HTMX.
"""

from sqlalchemy.orm import Session

from app import store, timeutil
from app.config import settings
from app.models import SnapshotStatus, VerdictValue
from app.services import evidence_chain


def _aggregate_cell(session: Session, repository_id: str, artifact_defs: list) -> dict:
    """Статус ячейки по последним снапшотам всех артефактов занятия (FR-4).

    Детерминированное правило: все found → found; все not_found → not_found;
    смесь или хотя бы один partial → partial. Причины partial — объединение
    по всем partial-артефактам, без дублей. Нет ни одного снапшота → пустая ячейка.
    """
    snaps = [
        snap
        for adef in artifact_defs
        if (snap := store.find_last_snapshot(session, repository_id, adef.id)) is not None
    ]
    if not snaps:
        return {"status": None, "partial_reason": None, "content_hash": None}

    statuses = {snap.status for snap in snaps}
    if statuses == {SnapshotStatus.found}:
        status = SnapshotStatus.found
    elif statuses == {SnapshotStatus.not_found}:
        status = SnapshotStatus.not_found
    else:
        status = SnapshotStatus.partial

    reasons = sorted({r for snap in snaps for r in (snap.partial_reason or [])})
    hashes = [snap.content_hash for snap in snaps if snap.content_hash]
    return {
        "status": status,
        "partial_reason": reasons or None,
        "content_hash": hashes[0] if len(hashes) == 1 else None,
    }


def build_matrix(session: Session, llm_model: str | None = None) -> dict:
    """Построение матрицы «репозиторий × занятие» из последних снапшотов.

    Возвращает dict с ключами:
      - repositories: list[{id, repo_url, git_host}]
      - lessons: list[{id, number, title}]
      - cells: dict[repo_id][lesson_number] → {status, partial_reason, content_hash}
      - as_of: str — время последнего обхода в формате HH:MM
    """
    # Только репозитории, у которых есть хотя бы одна запись SyncRunRepository
    checked_ids = store.find_checked_repository_ids(session)
    repos = [r for r in store.find_active_repositories(session) if r.id in checked_ids]
    lessons = store.find_all_lessons(session)
    defs_by_lesson = {
        lesson.id: store.find_artifact_defs_by_lesson(session, lesson.id) for lesson in lessons
    }

    cells: dict[str, dict[int, dict]] = {}
    for repo in repos:
        cells[repo.id] = {}
        for lesson in lessons:
            cells[repo.id][lesson.number] = _aggregate_cell(
                session, repo.id, defs_by_lesson[lesson.id]
            )

    # FR-10 (O2, #16): разрывы по рёбрам конвейера — точки для кнопки «ложный разрыв»
    resolved_model = llm_model or settings.deepseek_model
    breaks = {
        repo.id: [
            card
            for card in evidence_chain.edge_states(session, repo.id, resolved_model)
            if card["state"] == "done" and card["verdict"] == VerdictValue.break_
        ]
        for repo in repos
    }

    # Время последнего обхода
    last_run = store.find_last_sync_run(session)
    # #32: в БД наивный UTC, показываем местное время с меткой зоны
    as_of = (
        f"{timeutil.to_display(last_run.started_at):%H:%M} ({timeutil.offset_label()})"
        if last_run else "—:—"
    )

    return {
        "repositories": [
            {"id": r.id, "repo_url": r.repo_url, "git_host": r.git_host}
            for r in repos
        ],
        "lessons": [
            {"id": les.id, "number": les.number, "title": les.title}
            for les in lessons
        ],
        "cells": cells,
        "breaks": breaks,
        "as_of": as_of,
    }
