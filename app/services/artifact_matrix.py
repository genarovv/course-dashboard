"""artifact_matrix — проекция «репозиторий × артефакт» (D7; макет CEO 2026-07-30).

Колонки — роли артефактов из конфига (FR-2) в порядке первого появления в курсе.
Ячейка — лучший снапшот роли (best-wins между альтернативными путями, как в
карточке студента) плюс свод вердиктов рёбер FR-5, где роль участвует: разрыв
называет первую потерянную сущность. Детали ячейки (файлы, рёбра с цитатами,
заметки, FR-10) отдаёт build_cell_details — модальное окно в UI.
"""

from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from app import store, timeutil
from app.config import settings
from app.models import SNAPSHOT_STATUS_RANK, ArtifactRole, SnapshotStatus, VerdictValue
from app.services import evidence_chain
from app.services.labels import PARTIAL_LABELS, repo_short_name
from app.services.matrix_builder import blind_spots_and_signals

# D10 (#54), US-A3: обход старше этого срока — явный флаг устаревания на стикере
STALE_AFTER = timedelta(hours=48)

# D13 (#57): ранжирование уверенности FR-5; неизвестное значение деградирует до «низкой»
CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}

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

# PARTIAL_LABELS переехали в labels.py (D8) — общие для обеих матриц и модалки


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


def _cell(
    session: Session, repository_id: str, defs: list, edges: list[dict],
    mr_lesson_ids: set[str] | None = None,
    fresh_since: datetime | None = None,
) -> dict:
    """Ячейка: статус + усечённый результат анализа (summary) + счётчики рёбер."""
    snap = _best_snapshot(session, repository_id, defs)
    touching = _edges_touching(edges, defs[0].role)
    # D9 (#53), FR-12/US-B7: best-wins не нашёл артефакт, а роль ожидается
    # в MR-занятии — честная плашка вместо красного «нет»
    mr_channel = (
        (snap is None or snap.status == SnapshotStatus.not_found)
        and any(adef.lesson_id in (mr_lesson_ids or set()) for adef in defs)
    )
    breaks = [
        e for e in touching
        if e["state"] == "done" and e["verdict"] == VerdictValue.break_ and not e["override_active"]
    ]
    pending = [e for e in touching if e["state"] == "pending"]
    deferred = [e for e in touching if e["state"] == "deferred"]  # D6 (#37)
    # D11 (#55), BR-2: решение преподавателя (override) не маскируется под вердикт агента
    overridden = [
        e for e in touching
        if e["state"] == "done" and e["verdict"] == VerdictValue.break_ and e["override_active"]
    ]
    oks = [e for e in touching if e["state"] == "done" and e["verdict"] == VerdictValue.ok]

    # D13 (#57): структурированный чип разрыва — сущность, счётчик, наивысшая уверенность
    break_info = None
    if breaks:
        first_point = next((p for e in breaks for p in e["points"]), None)
        top_conf = min(
            (str(e["confidence"]) for e in breaks),
            key=lambda c: CONFIDENCE_RANK.get(c, 3),
        )
        break_info = {
            "count": len(breaks),
            "entity": first_point["entity"] if first_point else None,
            "confidence": top_conf if top_conf in CONFIDENCE_RANK else "low",
            # D16 (#60): вердикт впервые вычислен в последнем обходе (D25: перепрогон
            # той же четвёрки не создаёт новой записи — метка не «мигает»)
            "new": bool(
                fresh_since is not None
                and any(
                    e["computed_at"] and e["computed_at"] >= fresh_since for e in breaks
                )
            ),
        }

    def break_tail() -> str:
        lost = f": потеряна «{break_info['entity']}»" if break_info["entity"] else ""
        count = f" ×{len(breaks)}" if len(breaks) > 1 else ""
        return f"разрыв{count}{lost}"

    # Приоритет свода (спека D11): разрыв > проверяется > помечен ложным > ок > статус;
    # presence — та же строка без хвоста разрыва (хвост в UI живёт в чипе, D13)
    if mr_channel:
        presence = summary = "сдача через MR"
    elif snap is None:
        presence = summary = None
    elif snap.status == SnapshotStatus.not_found:
        presence = summary = "нет"
    elif snap.status == SnapshotStatus.partial:
        reasons = [PARTIAL_LABELS.get(r, r) for r in (snap.partial_reason or [])]
        presence = "частично · " + ", ".join(reasons) if reasons else "частично"
        summary = f"{presence} · {break_tail()}" if breaks else presence
    elif breaks:
        presence = "есть"
        summary = f"есть · {break_tail()}"
    elif deferred:
        # D6: LLM недоступна / ответ не распарсился — честнее «проверяется»
        presence = summary = "есть · проверка отложена"
    elif pending:
        presence = summary = "есть · связность проверяется"
    elif overridden:
        presence = summary = "есть · помечен ложным"
    elif oks:
        presence = summary = "есть · связность ок"
    else:
        presence = summary = "есть"

    return {
        "status": snap.status if snap else None,
        "partial_reason": snap.partial_reason if snap else None,
        "summary": summary,
        "presence": presence,
        "break": break_info,
        # D16 (#60): артефакт наблюдался заново в последнем обходе — точка свежести
        "fresh": bool(fresh_since is not None and snap and snap.observed_at >= fresh_since),
        "mr_channel": mr_channel,
        "break_count": len(breaks),
        "pending_count": len(pending),
        "deferred_count": len(deferred),
        "overridden_count": len(overridden),
        "ok_count": len(oks),
    }


def _mr_lesson_ids(session: Session) -> set[str]:
    """Занятия с каналом сдачи «запрос на слияние» (FR-12)."""
    return {
        lesson.id
        for lesson in store.find_all_lessons(session)
        if lesson.submission_channel == "mr"
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
    session: Session, llm_model: str | None = None,
    today: date | None = None, now: datetime | None = None,
    sort: str | None = None,
) -> dict:
    """Матрица «репозиторий × артефакт» для GET /artifacts.

    sort="breaks" — «сначала проблемные» (D15, #59): по числу уникальных
    рёбер-разрывов, стабильно относительно порядка реестра.
    """
    resolved_model = llm_model or settings.deepseek_model
    checked_ids = store.find_checked_repository_ids(session)
    active_repos = store.find_active_repositories(session)
    repos = [r for r in active_repos if r.id in checked_ids]
    defs_by_role = _defs_by_role_in_course_order(session)
    mr_lesson_ids = _mr_lesson_ids(session)
    # D16 (#60): порог свежести — начало последнего обхода; без предыдущего обхода
    # меток нет (первый обход пометил бы «новым» весь экран)
    last_run = store.find_last_sync_run(session)
    fresh_since = (
        last_run.started_at
        if last_run and store.find_previous_sync_run(session) is not None
        else None
    )

    cells: dict[str, dict[str, dict]] = {}
    row_breaks: dict[str, int] = {}
    for repo in repos:
        edges = evidence_chain.edge_states(session, repo.id, resolved_model)
        cells[repo.id] = {
            str(role): _cell(session, repo.id, defs, edges, mr_lesson_ids, fresh_since)
            for role, defs in defs_by_role.items()
        }
        # D15 (#59): уникальные рёбра-разрывы — ребро видно в двух ячейках, считается раз
        row_breaks[repo.id] = sum(
            1 for e in edges
            if e["state"] == "done" and e["verdict"] == VerdictValue.break_
            and not e["override_active"]
        )
    if sort == "breaks":
        repos = sorted(repos, key=lambda r: -row_breaks[r.id])  # sorted стабилен — реестр внутри

    # Макет: «Время и дата последнего анализа: День.Месяц ЧЧ:ММ» (местное время, #32)
    as_of = (
        f"{timeutil.to_display(last_run.started_at):%d.%m %H:%M} ({timeutil.offset_label()})"
        if last_run else "—"
    )
    resolved_now = now or timeutil.utcnow()
    stale = bool(last_run and resolved_now - last_run.started_at > STALE_AFTER)

    return {
        "stale": stale,
        # D10 (#54): те же сигналы FR-6/FR-7/FR-3, что в матрице занятий
        **blind_spots_and_signals(session, active_repos, today or resolved_now.date()),
        "roles": [
            {"key": str(role), "title": ROLE_TITLES.get(role, str(role))}
            for role in defs_by_role
        ],
        "repositories": [
            {"id": r.id, "repo_url": r.repo_url, "name": repo_short_name(r.repo_url)}
            for r in repos
        ],
        "cells": cells,
        "row_breaks": row_breaks,
        "sort": sort,
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
    best = _best_snapshot(session, repository_id, defs)
    mr_channel = (
        (best is None or best.status == SnapshotStatus.not_found)
        and any(adef.lesson_id in _mr_lesson_ids(session) for adef in defs)
    )
    return {
        "repository": {"id": repo.id, "repo_url": repo.repo_url},
        "role": str(role_enum),
        "title": ROLE_TITLES.get(role_enum, str(role_enum)),
        "files": files,
        "edges": edges,
        "mr_channel": mr_channel,
    }
