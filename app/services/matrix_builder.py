"""matrix_builder — проекция матрицы «репозиторий × занятие» (D1, #12; ARCHITECTURE §5.3).

Читает последние снапшоты из БД, группирует по (repository, lesson).
Возвращает dict, пригодный для рендеринга Jinja2/HTMX.
"""

from sqlalchemy.orm import Session

from app import store


def build_matrix(session: Session) -> dict:
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

    cells: dict[str, dict[int, dict]] = {}
    for repo in repos:
        cells[repo.id] = {}
        for lesson in lessons:
            artifact_defs = store.find_artifact_defs_by_lesson(session, lesson.id)
            # Берём последний снапшот по любому артефакту занятия
            latest_status = None
            latest_partial_reason = None
            latest_content_hash = None
            for adef in artifact_defs:
                snap = store.find_last_snapshot(session, repo.id, adef.id)
                if snap is not None:
                    latest_status = snap.status
                    latest_partial_reason = snap.partial_reason
                    latest_content_hash = snap.content_hash

            cells[repo.id][lesson.number] = {
                "status": latest_status,
                "partial_reason": latest_partial_reason,
                "content_hash": latest_content_hash,
            }

    # Время последнего обхода
    last_run = store.find_last_sync_run(session)
    as_of = last_run.started_at.strftime("%H:%M") if last_run else "—:—"

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
        "as_of": as_of,
    }
