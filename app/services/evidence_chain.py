"""evidence_chain — карточка студента: хронология + разрывы (D4 min, #14; FR-9).

Хронология строится по observed_at, не по git-истории (D27: force-push
неуязвимость): запись наблюдения переживает переписывание истории студентом.
Контент документов не хранится (C4) — доказательная цепочка = source_commit_sha
+ content_hash + observed_at.
"""

from datetime import datetime, time

from sqlalchemy.orm import Session

from app import store
from app.config import settings
from app.models import SNAPSHOT_STATUS_RANK, SnapshotStatus, SyncOutcome, VerdictValue
from app.timeutil import to_display, utcnow

MAX_POINTS = 5  # AC 2 / PRD §5.1: не более 5 подсвеченных точек на разрыв


def _latest_snapshot_for_role(session: Session, repository_id: str, role):
    """Снапшот-представитель роли среди её альтернативных артефактов.

    Пакет «12 артефактов» (fix по ревью T3): сперва лучший статус (found важнее
    partial-заготовки — иначе вердикт на защите считался бы по свежей копии
    шаблона), при равном статусе — последний по observed_at.
    """
    candidates = [
        snap
        for adef in store.find_artifact_defs_by_role(session, role)
        if (snap := store.find_last_snapshot(session, repository_id, adef.id)) is not None
        and snap.content_hash
    ]
    return min(
        candidates,
        key=lambda snap: (SNAPSHOT_STATUS_RANK[snap.status], -snap.observed_at.timestamp()),
        default=None,
    )


def _edge_card(session: Session, repository_id: str, edge, llm_model: str) -> dict:
    card = {
        "edge_def_id": edge.id,
        "source_role": edge.source_role,
        "target_role": edge.target_role,
        "state": "no_data",
        "verdict": None,
        "confidence": None,
        "points": [],
        "notes": None,
        "verdict_id": None,
        "override_active": False,
        "override_at": None,
        "deferred_reason": None,
        "computed_at": None,
        "source_file": None,
        "source_sha": None,
        "target_file": None,
        "target_sha": None,
    }
    snap_a = _latest_snapshot_for_role(session, repository_id, edge.source_role)
    snap_b = _latest_snapshot_for_role(session, repository_id, edge.target_role)
    if snap_a is None or snap_b is None:
        return card  # нет наблюдений обеих сторон — нет данных
    # D18 (#62): доказательство на защите — файлы и коммиты обеих сторон вердикта
    card.update(
        source_file=snap_a.file_path, source_sha=snap_a.source_commit_sha,
        target_file=snap_b.file_path, target_sha=snap_b.source_commit_sha,
    )

    verdict = store.find_latest_verdict_for_quadruple(
        session,
        source_content_hash=snap_a.content_hash,
        target_content_hash=snap_b.content_hash,
        rubric_id=edge.rubric_id,
        llm_model=llm_model,
    )
    if verdict is None:
        card["state"] = "pending"  # В13: «проверяется» — вычислимое состояние
        return card
    if verdict.verdict == VerdictValue.deferred:
        # D6 (#37): «отложено» отличимо от «проверяется» — с причиной (§5.2)
        card["state"] = "deferred"
        card["deferred_reason"] = str(verdict.deferred_reason) if verdict.deferred_reason else None
        return card

    override = store.find_active_override_for_verdict(session, verdict.id)
    card.update(
        state="done",
        verdict=verdict.verdict,
        confidence=verdict.confidence,
        points=(verdict.points or [])[:MAX_POINTS],
        notes=verdict.notes,
        verdict_id=verdict.id,
        override_active=override is not None,
        # D19 (#65): дата отметки — «разрыв снят преподавателем ДД.ММ» в деле защиты
        override_at=override.created_at if override is not None else None,
        computed_at=verdict.computed_at,  # D16 (#60): метка «новое с прошлого обхода»
    )
    return card


def edge_states(session: Session, repository_id: str, llm_model: str) -> list[dict]:
    """Состояния всех рёбер конвейера для репозитория (карточка + разрывы матрицы, O2)."""
    return [
        _edge_card(session, repository_id, edge, llm_model)
        for edge in store.find_all_edge_defs(session)
    ]


def build_student_card(
    session: Session, repository_id: str, *, llm_model: str | None = None
) -> dict | None:
    """Карточка студента: хронология наблюдений + состояние рёбер конвейера.

    Возвращает None, если репозиторий не найден (роут отвечает 404).
    """
    repo = store.find_repository_by_id(session, repository_id)
    if repo is None:
        return None
    llm_model = llm_model or settings.deepseek_model

    role_by_adef = {
        adef.id: adef.role
        for lesson in store.find_all_lessons(session)
        for adef in store.find_artifact_defs_by_lesson(session, lesson.id)
    }
    timeline = [
        {
            "observed_at": snap.observed_at.isoformat(),
            "role": role_by_adef.get(snap.artifact_def_id, "?"),
            "status": snap.status,
            "partial_reason": snap.partial_reason,
            "file_path": snap.file_path,
            "content_hash": snap.content_hash,
            "source_commit_sha": snap.source_commit_sha,
        }
        for snap in store.find_snapshots_by_repository(session, repository_id)
    ]
    edges = edge_states(session, repository_id, llm_model)
    # FR-12 (#41): MR последнего наблюдения — процесс сдачи рядом с артефактами
    mrs = [
        {
            "number": row.mr_number,
            "title": row.title,
            "source_branch": row.source_branch,
            "state": row.state,
            "reviewer_approved": row.reviewer_approved,
            "ready_for_merge": row.state == "opened" and row.reviewer_approved,
            "markers": row.markers or {},
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
        for row in store.find_latest_mr_observations(session, repository_id)
    ]
    # T2 (#44): признаки проб содержимого — по последним снапшотам артефактов с пробами;
    # статус ячеек не меняют (BR-3), это отдельный сигнал «объявленное требование не найдено»
    probe_rows = []
    for lesson in store.find_all_lessons(session):
        for adef in store.find_artifact_defs_by_lesson(session, lesson.id):
            if not adef.content_probes:
                continue
            snap = store.find_last_snapshot(session, repository_id, adef.id)
            for finding in (snap.probe_findings or []) if snap else []:
                probe_rows.append({
                    "role": str(adef.role),
                    "file_path": snap.file_path,
                    "key": finding.get("key"),
                    "label": finding.get("label", finding.get("key", "?")),
                })

    return {
        "repository": {"id": repo.id, "repo_url": repo.repo_url, "git_host": repo.git_host},
        "timeline": timeline,
        "edges": edges,
        "mrs": mrs,
        "probe_findings": probe_rows,
    }


def merged_no_review_count(rows) -> int:
    """FR-12 (порядок 2026-07-29): merged без вердикта ревьюера = «мимо ревью».

    Один факт — в одном месте: правило используют процесс матрицы занятий,
    сводка строки артефактной матрицы (D20) и шапка-резюме дела (D36).
    Переехало из matrix_builder (итерация 5) — evidence_chain ниже по слою.
    """
    return sum(1 for r in rows if r.state == "merged" and not r.reviewer_approved)


def repo_progress(session: Session, repository_id: str) -> dict:
    """D20/D36: «X/M» — сдано ролей из ожидаемых файлами (found+partial = сдано,
    BR-3); роли, ожидаемые через MR и не найденные файлами, — вне знаменателя."""
    mr_lessons = {
        lesson.id
        for lesson in store.find_all_lessons(session)
        if lesson.submission_channel == "mr"
    }
    defs_by_role: dict = {}
    for lesson in store.find_all_lessons(session):
        for adef in store.find_artifact_defs_by_lesson(session, lesson.id):
            defs_by_role.setdefault(adef.role, []).append(adef)
    x = m = 0
    for defs in defs_by_role.values():
        candidates = [
            snap
            for adef in defs
            if (snap := store.find_last_snapshot(session, repository_id, adef.id)) is not None
        ]
        best = min(
            candidates,
            key=lambda snap: (SNAPSHOT_STATUS_RANK[snap.status], -snap.observed_at.timestamp()),
            default=None,
        )
        mr_channel = (
            (best is None or best.status == SnapshotStatus.not_found)
            and any(adef.lesson_id in mr_lessons for adef in defs)
        )
        if mr_channel:
            continue
        m += 1
        if best is not None and best.status in (SnapshotStatus.found, SnapshotStatus.partial):
            x += 1
    return {"x": x, "m": m}


def _defense_events(session: Session, repository_id: str) -> list[dict]:
    """D19 (#65): хронология защиты — переходы состояний + вехи занятий.

    Снапшот пишется только при изменении наблюдения (D28), но смена одного
    контента при том же статусе — не переход; переходы: появился / статус
    изменился / исчез. Первый снапшот not_found перехода не рождает.
    Дата перехода — дата коммита; без неё — дата наблюдения с пометкой
    «зафиксировано обходом». Поток ведётся по artifact_def (альтернативные
    пути роли не смешиваются), подпись — роль.
    D24 (итерация 5): пара «исчез»+«появился» одной роли в одном обходе —
    перенос файла, схлопывается в «перемещён: старый → новый»; MR-события
    с датами хостинга встроены в общую ленту (независимые даты).
    """
    role_by_adef = {
        adef.id: adef.role
        for lesson in store.find_all_lessons(session)
        for adef in store.find_artifact_defs_by_lesson(session, lesson.id)
    }
    events: list[dict] = [
        {
            "kind": "lesson",
            "number": lesson.number,
            "title": lesson.title,
            "when": datetime.combine(lesson.date, time.min),
        }
        for lesson in store.find_all_lessons(session)
    ]
    last_status: dict[str, SnapshotStatus] = {}
    last_path: dict[str, str | None] = {}
    transitions: list[dict] = []
    for snap in store.find_snapshots_by_repository(session, repository_id):
        prev = last_status.get(snap.artifact_def_id)
        prev_path = last_path.get(snap.artifact_def_id)
        last_status[snap.artifact_def_id] = snap.status
        if snap.file_path:
            last_path[snap.artifact_def_id] = snap.file_path
        if prev == snap.status:
            continue  # смена контента без смены статуса — не переход
        if prev is None and snap.status == SnapshotStatus.not_found:
            continue  # ничего не было и нет — нечему появляться
        if snap.status == SnapshotStatus.not_found:
            label = "исчез"
        elif prev is None or prev == SnapshotStatus.not_found:
            label = "появился"
        else:
            label = "статус изменился"
        transitions.append({
            "kind": "transition",
            "role": role_by_adef.get(snap.artifact_def_id, "?"),
            "label": label,
            "from_status": prev,
            "to_status": snap.status,
            # #32 + ревью итерации 4 (находка 1): в БД UTC, показ и сортировка —
            # в местном времени, иначе даты съезжают рядом с местными вехами занятий
            "when": to_display(snap.source_commit_date or snap.observed_at),
            "by_observation": snap.source_commit_date is None,
            "file_path": snap.file_path,
            "from_path": prev_path,
            "sha": snap.source_commit_sha,
            "sync_run_id": snap.sync_run_id,
        })

    # D24: «исчез» + «появился» одной роли в одном обходе = перенос файла;
    # ревью итерации 5, находка 7: пара схлопывается независимо от порядка сканирования
    collapsed: list[dict] = []
    for tr in transitions:
        pair_label = {"появился": "исчез", "исчез": "появился"}.get(tr["label"])
        mate = next(
            (
                c for c in collapsed
                if pair_label is not None and c["label"] == pair_label
                and c["role"] == tr["role"] and c["sync_run_id"] == tr["sync_run_id"]
            ),
            None,
        )
        if mate is not None:
            appeared = tr if tr["label"] == "появился" else mate
            gone = mate if tr["label"] == "появился" else tr
            collapsed.remove(mate)
            tr = {**appeared, "label": "перемещён", "from_path": gone["from_path"]}
        collapsed.append(tr)
    events.extend(collapsed)

    # D24: независимые даты хостинга — MR-события в общей ленте (без даты — не рисуем)
    events.extend(
        {
            "kind": "mr",
            "number": row.mr_number,
            "source_branch": row.source_branch,
            "state": row.state,
            "when": to_display(row.updated_at),
        }
        for row in store.find_latest_mr_observations(session, repository_id)
        if row.updated_at is not None
    )
    events.sort(key=lambda e: e["when"])
    return events


def build_defense_card(
    session: Session, repository_id: str, *, llm_model: str | None = None
) -> dict | None:
    """US-C2 (D18, #62; v2 — D19, #65): «дело» одного студента для защиты.

    Поверх карточки: показываются только разрывы с уверенностью «высокая»
    и не погашенные отметкой (митигация риска PRD §11); остальные разрывы
    отсутствуют в выдаче. Связные рёбра — доказательная база «в плюс».
    v2: хронология переходов с вехами занятий, погашенные разрывы и
    MR-история свёрнутыми — история решений легитимизирует оценку.
    Слепая зона не роняет режим — честная пометка.
    """
    card = build_student_card(session, repository_id, llm_model=llm_model)
    if card is None:
        return None
    edges = card["edges"]
    sure_breaks = [
        e for e in edges
        if e["state"] == "done" and e["verdict"] == VerdictValue.break_
        and str(e["confidence"]) == "high" and not e["override_active"]
    ]
    ok_edges = [
        e for e in edges if e["state"] == "done" and e["verdict"] == VerdictValue.ok
    ]
    muted_breaks = [
        e for e in edges
        if e["state"] == "done" and e["verdict"] == VerdictValue.break_
        and e["override_active"]
    ]
    last = store.find_last_outcome_row(session, repository_id)
    blind = last is not None and last.outcome == SyncOutcome.repo_unavailable

    # D36: шапка-резюме — комиссия за 10 секунд; период работы по независимым
    # датам хостинга, без MR — по датам коммитов/наблюдений, без данных — None
    mr_rows = store.find_latest_mr_observations(session, repository_id)
    dates = sorted(row.updated_at for row in mr_rows if row.updated_at is not None)
    if not dates:
        dates = sorted(
            snap.source_commit_date or snap.observed_at
            for snap in store.find_snapshots_by_repository(session, repository_id)
        )
    case_summary = {
        "submitted": repo_progress(session, repository_id),
        # ревью итерации 5, находка 6: «разрывов: N» = числу разрывов НА ЭКРАНЕ
        # (high, не погашенные) — цифры резюме и секции не расходятся перед комиссией
        "breaks": len(sure_breaks),
        "mrs_total": len(card["mrs"]),
        "no_review": merged_no_review_count(mr_rows),
        # находка 1 (серьёзное): период — в местном времени (#32), как вся хронология
        "work_from": to_display(dates[0]) if dates else None,
        "work_to": to_display(dates[-1]) if dates else None,
    }
    # D43 (#69): работа вне основной ветки. На защите это обязано быть видно:
    # иначе комиссия читает пустое дело как «студент ничего не сделал», хотя
    # работа есть, а слить её в основную ветку он не может (main защищена).
    branch_hints = [
        {
            "branch_name": h.branch_name,
            "artifacts_found": h.artifacts_found,
            "artifacts_in_default": h.artifacts_in_default,
            "head_date": to_display(h.head_date) if h.head_date else None,
        }
        for h in store.find_latest_branch_hints(session, repository_id)
    ]
    return {
        **card,
        "branch_hints": branch_hints,
        "sure_breaks": sure_breaks,
        "ok_edges": ok_edges,
        "muted_breaks": muted_breaks,
        "case_summary": case_summary,
        "events": _defense_events(session, repository_id),
        "opened_at": to_display(utcnow()),
        "blind": blind,
        "blind_detail": (last.detail or "") if blind else None,
    }
