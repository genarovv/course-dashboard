"""evidence_chain — карточка студента: хронология + разрывы (D4 min, #14; FR-9).

Хронология строится по observed_at, не по git-истории (D27: force-push
неуязвимость): запись наблюдения переживает переписывание истории студентом.
Контент документов не хранится (C4) — доказательная цепочка = source_commit_sha
+ content_hash + observed_at.
"""

from sqlalchemy.orm import Session

from app import store
from app.config import settings
from app.models import SNAPSHOT_STATUS_RANK, VerdictValue

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
        "deferred_reason": None,
    }
    snap_a = _latest_snapshot_for_role(session, repository_id, edge.source_role)
    snap_b = _latest_snapshot_for_role(session, repository_id, edge.target_role)
    if snap_a is None or snap_b is None:
        return card  # нет наблюдений обеих сторон — нет данных

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

    card.update(
        state="done",
        verdict=verdict.verdict,
        confidence=verdict.confidence,
        points=(verdict.points or [])[:MAX_POINTS],
        notes=verdict.notes,
        verdict_id=verdict.id,
        override_active=store.find_active_override_for_verdict(session, verdict.id) is not None,
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
