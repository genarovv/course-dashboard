"""matrix_builder — проекция матрицы «репозиторий × занятие» (D1, #12; ARCHITECTURE §5.3).

Читает последние снапшоты из БД, группирует по (repository, lesson).
Возвращает dict, пригодный для рендеринга Jinja2/HTMX.
"""

from datetime import date

from sqlalchemy.orm import Session

from app import store, timeutil
from app.config import settings
from app.models import SnapshotStatus, SyncOutcome, VerdictValue
from app.services import evidence_chain


def _blind_spots_and_signals(session: Session, repos: list, today: date) -> dict:
    """§5.3 (#18): слепая зона, auth-баннер, «не проверялось», хроники.

    Слепая зона — последний исход repo_unavailable либо архив. auth_failed —
    проблема нашего токена, не студента: баннер, не слепая зона. Хроника —
    нет новых наблюдений 2 и более занятия подряд (по lesson.date, US-B4).
    """
    blind_spots, unchecked, chronics = [], [], []
    auth_banner = False
    lesson_dates = sorted(lesson.date for lesson in store.find_all_lessons(session))

    for repo in store.find_archived_repositories(session):
        blind_spots.append({"repo_url": repo.repo_url, "detail": "архивирован"})

    for repo in repos:
        last = store.find_last_outcome_row(session, repo.id)
        outcome = last.outcome if last else None
        if outcome == SyncOutcome.repo_unavailable:
            blind_spots.append({"repo_url": repo.repo_url, "detail": last.detail or ""})
            continue  # недоступный репо не судим за тишину
        if outcome == SyncOutcome.auth_failed:
            auth_banner = True
            continue
        if outcome == SyncOutcome.skipped_rate_limit:
            unchecked.append({"repo_url": repo.repo_url})
            continue
        # Известное ограничение (ревью #18, находка 5): хроника не отличает
        # «студент молчит» от «cron давно не запускался» — устаревание обхода
        # видно по «Актуально на» (§5.5); гейт по checked_at — кандидат v1.2
        last_change = store.find_last_observed_at(session, repo.id) or repo.added_at
        lessons_silent = sum(
            1 for d in lesson_dates if last_change.date() < d <= today
        )
        if lessons_silent >= 2:
            chronics.append({"repo_url": repo.repo_url, "lessons_silent": lessons_silent})

    return {
        "blind_spots": blind_spots,
        "auth_banner": auth_banner,
        "unchecked": unchecked,
        "chronics": chronics,
    }


_STATUS_RANK = {  # лучший статус для альтернатив внутри роли (пакет «12 артефактов»)
    SnapshotStatus.found: 0,
    SnapshotStatus.partial: 1,
    SnapshotStatus.not_found: 2,
}


def _aggregate_cell(session: Session, repository_id: str, artifact_defs: list) -> dict:
    """Статус ячейки по последним снапшотам всех артефактов занятия (FR-4).

    Двухуровневое правило (пакет «12 артефактов», решение CEO 2026-07-30):
    1. Внутри роли несколько дефов = АЛЬТЕРНАТИВНЫЕ пути одного артефакта
       (product/prd.md ИЛИ REQUIREMENTS.md) — побеждает лучший статус;
       partial_reason берётся только у представителя-победителя.
    2. Между ролями занятия — прежнее строгое правило D1: все found → found;
       все not_found → not_found; смесь или partial → partial.
    Нет ни одного снапшота → пустая ячейка.
    """
    best_by_role: dict = {}
    for adef in artifact_defs:
        snap = store.find_last_snapshot(session, repository_id, adef.id)
        if snap is None:
            continue
        current = best_by_role.get(adef.role)
        if current is None or _STATUS_RANK[snap.status] < _STATUS_RANK[current.status]:
            best_by_role[adef.role] = snap
    snaps = list(best_by_role.values())
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


def build_matrix(session: Session, llm_model: str | None = None, today: date | None = None) -> dict:
    """Построение матрицы «репозиторий × занятие» из последних снапшотов.

    Возвращает dict с ключами:
      - repositories: list[{id, repo_url, git_host}]
      - lessons: list[{id, number, title}]
      - cells: dict[repo_id][lesson_number] → {status, partial_reason, content_hash}
      - as_of: str — время последнего обхода в формате HH:MM
    """
    # Только репозитории, у которых есть хотя бы одна запись SyncRunRepository
    checked_ids = store.find_checked_repository_ids(session)
    active_repos = store.find_active_repositories(session)  # один запрос на весь рендер
    repos = [r for r in active_repos if r.id in checked_ids]
    lessons = store.find_all_lessons(session)
    defs_by_lesson = {
        lesson.id: store.find_artifact_defs_by_lesson(session, lesson.id) for lesson in lessons
    }

    cells: dict[str, dict[int, dict]] = {}
    for repo in repos:
        cells[repo.id] = {}
        for lesson in lessons:
            cell = _aggregate_cell(session, repo.id, defs_by_lesson[lesson.id])
            # FR-12 интерим (ADR-007): у MR-занятия пустая/not_found ячейка — не «нет»,
            # артефакт может жить в ветке несмерженного MR
            cell["mr_channel"] = (
                lesson.submission_channel == "mr"
                and cell["status"] in (None, SnapshotStatus.not_found)
            )
            cells[repo.id][lesson.number] = cell

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

    # FR-12 (#41): сводка процесса сдачи по последним MR-наблюдениям
    process = {}
    for repo in repos:
        rows = store.find_latest_mr_observations(session, repo.id)
        process[repo.id] = {
            "ready": sum(1 for r in rows if r.state == "opened" and r.reviewer_approved),
            "opened": sum(1 for r in rows if r.state == "opened"),
            "merged": sum(1 for r in rows if r.state == "merged"),
            # порядок 2026-07-29 (мержат сами): merged+вердикт = сдан; без вердикта — мимо ревью
            "accepted": sum(1 for r in rows if r.state == "merged" and r.reviewer_approved),
            "merged_no_review": sum(1 for r in rows if r.state == "merged" and not r.reviewer_approved),
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
        "process": process,
        "as_of": as_of,
        # #31: пустой реестр — видимое состояние, не молчаливо пустая матрица
        "registry_count": len(active_repos),
        # #18 (v1.1): слепая зона FR-6, хроники FR-7, auth-баннер, «не проверялось»
        **_blind_spots_and_signals(
            session, active_repos,
            today or timeutil.utcnow().date(),
        ),
    }
