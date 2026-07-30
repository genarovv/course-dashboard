"""store.py — единая точка доступа к данным (S2, тикет #3; ARCHITECTURE §3.1, §3.5).

Контракт «журнал vs состояние vs конфиг» — три категории (§3.5, ADR-005):
- журнальные сущности (Rubric, ArtifactSnapshot, CoherenceVerdict, SyncRunRepository,
  создание Override) — только register_* (INSERT) и find_* (SELECT);
- рабочее состояние — ровно 5 узких update_* (см. §3.5; 5-й — тикет #50); delete_* нет вообще;
- конфиг-реконсиляция из config.yaml (config_*: Lesson, ArtifactDef, EdgeDef.rubric_id) —
  единственный вызывающий config_manager, закреплено тестом на импорт;
- system_user не создаётся через store — И10: сид одной строки при миграции,
  путь записи отсутствует by design.

Здесь же живут engine и фабрика сессий (файла database.py в структуре §3.1 нет).
"""

from datetime import datetime

from sqlalchemy import create_engine, distinct, event, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.models import GitHost, SyncOutcome, SyncStatus, SyncTrigger, VerdictValue
from app.models.artifact_def import ArtifactDef
from app.models.artifact_snapshot import ArtifactSnapshot
from app.models.coherence_verdict import CoherenceVerdict
from app.models.edge_def import EdgeDef
from app.models.git_credential import GitCredential
from app.models.lesson import Lesson
from app.models.mr_observation import MrObservation
from app.models.override import Override
from app.models.repository import Repository
from app.models.rubric import Rubric
from app.models.sync_run import SyncRun
from app.models.sync_run_repository import SyncRunRepository
from app.models.system_user import SystemUser
from app.timeutil import utcnow

engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, _connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = sessionmaker(bind=engine)


# ── УТИЛИТЫ ──────────────────────────────────────────────────────────────────


def normalize_url(repo_url: str) -> str:
    """Нормализация URL для И6: strip .git, lowercase, strip trailing /."""
    url = repo_url.strip().lower().rstrip("/")
    return url.removesuffix(".git")


# ── ЖУРНАЛ: register_* (только INSERT) ───────────────────────────────────────


def register_repository(session: Session, *, repo_url: str, git_host: GitHost,
                         default_branch: str = "main") -> Repository:
    """FR-1: добавление репозитория (normalized_repo_url — И6)."""
    repo = Repository(
        repo_url=repo_url,
        normalized_repo_url=normalize_url(repo_url),
        git_host=git_host,
        default_branch=default_branch,
    )
    session.add(repo)
    return repo


def register_git_credential(session: Session, *, git_host: GitHost) -> GitCredential:
    """FR-3: запись о токене хостинга (сам токен — в env, не в БД)."""
    credential = GitCredential(git_host=git_host)
    session.add(credential)
    return credential


def register_rubric(session: Session, **fields) -> Rubric:
    """FR-2: новая версия рубрики (append-only — правка = новая строка)."""
    rubric = Rubric(**fields)
    session.add(rubric)
    return rubric


def register_sync_run(session: Session, *, triggered_by: SyncTrigger) -> SyncRun:
    """FR-8: начало обхода (status=in_progress по умолчанию модели)."""
    run = SyncRun(triggered_by=triggered_by)
    session.add(run)
    return run


def register_sync_outcome(session: Session, *, sync_run_id: str, repository_id: str,
                          outcome: SyncOutcome, detail: str | None = None) -> SyncRunRepository:
    """FR-6/7/8: пообходный охват (append-only, И11)."""
    row = SyncRunRepository(
        sync_run_id=sync_run_id, repository_id=repository_id, outcome=outcome, detail=detail
    )
    session.add(row)
    return row


def register_snapshot(session: Session, **fields) -> ArtifactSnapshot:
    """FR-4: наблюдение артефакта (append-only, И5/И8/И9)."""
    snapshot = ArtifactSnapshot(**fields)
    session.add(snapshot)
    return snapshot


def register_verdict(session: Session, **fields) -> CoherenceVerdict:
    """FR-5: вердикт связности (append-only, И3/И5)."""
    verdict = CoherenceVerdict(**fields)
    session.add(verdict)
    return verdict


def register_mr_observation(session: Session, **fields) -> "MrObservation":
    """FR-12: наблюдение MR за обход (append-only, И12; ADR-007)."""
    row = MrObservation(**fields)
    session.add(row)
    return row


def register_override(session: Session, *, coherence_verdict_id: str, reason: str) -> Override:
    """FR-10: отметка ложного разрыва (И1/И4; step_quality_card_id — v2)."""
    override = Override(coherence_verdict_id=coherence_verdict_id, reason=reason)
    session.add(override)
    return override


# ── СОСТОЯНИЕ: ровно 5 узких update_* (ARCHITECTURE §3.5; 5-й — тикет #50) ───


def update_repository_default_branch(session: Session, repository_id: str, branch: str) -> None:
    """ADR-006 (#50): смена наблюдаемой ветки — единственная легальная мутация Repository.

    5-й узкий update_* (§3.5): детект default-ветки при импорте и обходе менял поле
    напрямую в сервисах мимо store — узаконено здесь, прямые присваивания запрещены
    ограничителем в tests/test_store.py.
    """
    repo = session.get(Repository, repository_id)
    repo.default_branch = branch


def update_sync_run_status(session: Session, sync_run_id: str, status: SyncStatus) -> None:
    """FR-8, NFR-2: жизненный цикл обхода in_progress → completed/partial/failed."""
    run = session.get(SyncRun, sync_run_id)
    run.status = status
    run.completed_at = utcnow()


def update_override_revoked(session: Session, override_id: str) -> None:
    """FR-10: снятие отметки — мягкое гашение, не удаление."""
    override = session.get(Override, override_id)
    override.revoked_at = utcnow()


def update_user_lockout(session: Session, user_id: str, *, failed_attempts: int,
                        locked_until: datetime | None) -> None:
    """FR-0: блокировка входа после неудачных попыток."""
    user = session.get(SystemUser, user_id)
    user.failed_attempts = failed_attempts
    user.locked_until = locked_until


def update_credential_validity(session: Session, credential_id: str, *, is_valid: bool) -> None:
    """FR-3: сигнал «обнови токен»."""
    credential = session.get(GitCredential, credential_id)
    credential.is_valid = is_valid
    credential.checked_at = utcnow()


# ── КОНФИГ-РЕКОНСИЛЯЦИЯ из config.yaml (категория 3 §3.5, ADR-005) ──────────
# Единственный вызывающий — config_manager (ограничитель закреплён тестом на импорт).


def config_upsert_lesson(
    session: Session, *, number: int, title: str, date, submission_channel: str = "files"
) -> tuple[Lesson, str]:
    """FR-2: занятие из config.yaml. Возвращает (lesson, 'created'|'updated'|'unchanged')."""
    lesson = session.scalar(select(Lesson).where(Lesson.number == number))
    if lesson is None:
        lesson = Lesson(number=number, title=title, date=date, submission_channel=submission_channel)
        session.add(lesson)
        return lesson, "created"
    if (lesson.title, lesson.date, lesson.submission_channel) != (title, date, submission_channel):
        lesson.title = title
        lesson.date = date
        lesson.submission_channel = submission_channel
        return lesson, "updated"
    return lesson, "unchanged"


def config_upsert_artifact_def(
    session: Session, *, lesson_id: str, role, expected_pattern: str,
    template_relative_path: str | None = None,
) -> tuple[ArtifactDef, str]:
    """FR-2: ожидаемый артефакт занятия. Возвращает (artifact_def, 'created'|'updated'|'unchanged')."""
    adef = session.scalar(
        select(ArtifactDef).where(ArtifactDef.lesson_id == lesson_id, ArtifactDef.role == role)
    )
    if adef is None:
        adef = ArtifactDef(
            lesson_id=lesson_id, role=role, expected_pattern=expected_pattern,
            template_relative_path=template_relative_path,
        )
        session.add(adef)
        return adef, "created"
    if (adef.expected_pattern, adef.template_relative_path) != (expected_pattern, template_relative_path):
        adef.expected_pattern = expected_pattern
        adef.template_relative_path = template_relative_path
        return adef, "updated"
    return adef, "unchanged"


def config_create_edge_def(session: Session, *, source_role, target_role, rubric_id: str) -> "EdgeDef":
    """FR-5: новое ребро связности из config.yaml."""
    edge = EdgeDef(source_role=source_role, target_role=target_role, rubric_id=rubric_id)
    session.add(edge)
    return edge


def config_repoint_edge_rubric(session: Session, *, edge_def_id: str, rubric_id: str) -> None:
    """FR-2 (ADR-005): перенаправление ребра на новую версию рубрики.

    Старые вердикты не трогаются — они привязаны к своим версиям через rubric_id
    в четвёрке И3. Смена рубрики = обязательный прогон golden set (CLAUDE.md).
    """
    edge = session.get(EdgeDef, edge_def_id)
    edge.rubric_id = rubric_id


# ── SELECT: find_* ───────────────────────────────────────────────────────────


def find_all_edge_defs(session: Session) -> list[EdgeDef]:
    """FR-5: все рёбра связности (для свода-реконсиляции §5.1)."""
    return list(session.scalars(select(EdgeDef)))


def find_artifact_defs_by_role(session: Session, role) -> list[ArtifactDef]:
    """FR-5: артефакт-определения роли (стороны ребра)."""
    return list(session.scalars(select(ArtifactDef).where(ArtifactDef.role == role)))


def find_edge_def_by_roles(session: Session, source_role, target_role) -> "EdgeDef | None":
    """FR-2/FR-5: ребро по паре ролей (UNIQUE source_role+target_role, И10)."""
    return session.scalar(
        select(EdgeDef).where(
            EdgeDef.source_role == source_role, EdgeDef.target_role == target_role
        )
    )


def find_user_by_username(session: Session, username: str) -> SystemUser | None:
    """FR-0: вход по логину."""
    return session.scalar(select(SystemUser).where(SystemUser.username == username))


def find_repository_by_normalized_url(session: Session, repo_url: str) -> Repository | None:
    """FR-1: детект дубликата при импорте (И6)."""
    return session.scalar(
        select(Repository).where(Repository.normalized_repo_url == normalize_url(repo_url))
    )


def find_active_repositories(session: Session) -> list[Repository]:
    """FR-8: репозитории для обхода (archived_at IS NULL)."""
    return list(session.scalars(select(Repository).where(Repository.archived_at.is_(None))))


def find_archived_repositories(session: Session) -> list[Repository]:
    """FR-6: архивные репо — слепая зона по определению (§5.3)."""
    return list(session.scalars(select(Repository).where(Repository.archived_at.is_not(None))))


def find_last_outcome_row(session: Session, repository_id: str) -> SyncRunRepository | None:
    """FR-6/FR-7: последняя строка охвата репозитория — доступность вычислима из неё (§3.5)."""
    return session.scalar(
        select(SyncRunRepository)
        .where(SyncRunRepository.repository_id == repository_id)
        .order_by(SyncRunRepository.checked_at.desc())
        .limit(1)
    )


def find_last_observed_at(session: Session, repository_id: str) -> datetime | None:
    """FR-7: время последнего нового наблюдения (снапшота) репозитория."""
    return session.scalar(
        select(ArtifactSnapshot.observed_at)
        .where(ArtifactSnapshot.repository_id == repository_id)
        .order_by(ArtifactSnapshot.observed_at.desc())
        .limit(1)
    )


def find_checked_repository_ids(session: Session) -> set[str]:
    """FR-4: ID репозиториев, у которых есть хотя бы одна запись SyncRunRepository."""
    return set(session.scalars(select(distinct(SyncRunRepository.repository_id))))


def find_all_lessons(session: Session) -> list[Lesson]:
    """FR-2/FR-4: все занятия, упорядоченные по номеру."""
    return list(session.scalars(select(Lesson).order_by(Lesson.number)))


def find_artifact_defs_by_lesson(session: Session, lesson_id: str) -> list[ArtifactDef]:
    """FR-2/FR-4: артефакт-определения для заданного занятия."""
    return list(session.scalars(select(ArtifactDef).where(ArtifactDef.lesson_id == lesson_id)))


def find_last_sync_run(session: Session) -> SyncRun | None:
    """FR-8: последний обход (для метки «актуально на»)."""
    return session.scalar(select(SyncRun).order_by(SyncRun.started_at.desc()).limit(1))


def find_credential(session: Session, git_host: GitHost) -> GitCredential | None:
    """FR-3: запись валидности токена хостинга."""
    return session.scalar(select(GitCredential).where(GitCredential.git_host == git_host))


def find_last_snapshot(session: Session, repository_id: str, artifact_def_id: str) -> ArtifactSnapshot | None:
    """FR-8: последнее наблюдение — для инкрементальности (content_hash)."""
    return session.scalar(
        select(ArtifactSnapshot)
        .where(
            ArtifactSnapshot.repository_id == repository_id,
            ArtifactSnapshot.artifact_def_id == artifact_def_id,
        )
        .order_by(ArtifactSnapshot.observed_at.desc())
        .limit(1)
    )


def find_verdict_by_quadruple(session: Session, *, source_content_hash: str, target_content_hash: str,
                              rubric_id: str, llm_model: str) -> CoherenceVerdict | None:
    """FR-5, D25: валидный (не deferred) вердикт на четвёрку — не мигаем."""
    return session.scalar(
        select(CoherenceVerdict).where(
            CoherenceVerdict.source_content_hash == source_content_hash,
            CoherenceVerdict.target_content_hash == target_content_hash,
            CoherenceVerdict.rubric_id == rubric_id,
            CoherenceVerdict.llm_model == llm_model,
            CoherenceVerdict.verdict != VerdictValue.deferred,
        )
    )


def find_repository_by_id(session: Session, repository_id: str) -> Repository | None:
    """FR-9: репозиторий для карточки студента."""
    return session.get(Repository, repository_id)


def find_snapshots_by_repository(session: Session, repository_id: str) -> list[ArtifactSnapshot]:
    """FR-9: все наблюдения репозитория в порядке observed_at (хронология, D27)."""
    return list(session.scalars(
        select(ArtifactSnapshot)
        .where(ArtifactSnapshot.repository_id == repository_id)
        .order_by(ArtifactSnapshot.observed_at)
    ))


def find_latest_verdict_for_quadruple(
    session: Session, *, source_content_hash: str, target_content_hash: str,
    rubric_id: str, llm_model: str,
) -> CoherenceVerdict | None:
    """FR-8 (/health): последний вердикт четвёрки, ВКЛЮЧАЯ deferred — диагностика §5.4."""
    return session.scalar(
        select(CoherenceVerdict)
        .where(
            CoherenceVerdict.source_content_hash == source_content_hash,
            CoherenceVerdict.target_content_hash == target_content_hash,
            CoherenceVerdict.rubric_id == rubric_id,
            CoherenceVerdict.llm_model == llm_model,
        )
        .order_by(CoherenceVerdict.computed_at.desc())
        .limit(1)
    )


def find_mr_observations(session: Session, repository_id: str, sync_run_id: str) -> list["MrObservation"]:
    """FR-12: наблюдения MR репозитория в конкретном обходе."""
    return list(session.scalars(
        select(MrObservation)
        .where(
            MrObservation.repository_id == repository_id,
            MrObservation.sync_run_id == sync_run_id,
        )
        .order_by(MrObservation.mr_number.desc())
    ))


def find_previous_mr_observation(
    session: Session, repository_id: str, mr_number: int
) -> "MrObservation | None":
    """FR-12: последнее наблюдение конкретного MR — база правила устаревания вердикта (ADR-007 сц. 5)."""
    return session.scalar(
        select(MrObservation)
        .where(
            MrObservation.repository_id == repository_id,
            MrObservation.mr_number == mr_number,
        )
        .order_by(MrObservation.observed_at.desc())
        .limit(1)
    )


def find_latest_mr_observations(session: Session, repository_id: str) -> list["MrObservation"]:
    """FR-12: наблюдения последнего обхода, в котором у репозитория есть MR-данные.

    Обход без MR-шага (деградация NFR-2) не затирает картину прошлого обхода.
    """
    last_run_id = session.scalar(
        select(MrObservation.sync_run_id)
        .where(MrObservation.repository_id == repository_id)
        .order_by(MrObservation.observed_at.desc())
        .limit(1)
    )
    if last_run_id is None:
        return []
    return find_mr_observations(session, repository_id, last_run_id)


def find_verdict_by_id(session: Session, verdict_id: str) -> CoherenceVerdict | None:
    """FR-10: вердикт по ID (цель отметки «ложный разрыв»)."""
    return session.get(CoherenceVerdict, verdict_id)


# ── FR-10: Override find_* ─────────────────────────────────────────────────


def find_active_override_for_verdict(session: Session, verdict_id: str) -> Override | None:
    """FR-10: активная (revoked_at IS NULL) отметка на вердикт."""
    return session.scalar(
        select(Override).where(
            Override.coherence_verdict_id == verdict_id,
            Override.revoked_at.is_(None),
        )
    )


def find_override_by_id(session: Session, override_id: str) -> Override | None:
    """FR-10: оверрайд по ID (для UI — просмотр, снятие)."""
    return session.get(Override, override_id)


# ── C2 (#36): точечные find_* для ядра FR-5 ─────────────────────────────────


def find_snapshot_by_id(session: Session, snapshot_id: str) -> ArtifactSnapshot | None:
    return session.get(ArtifactSnapshot, snapshot_id)


def find_rubric_by_id(session: Session, rubric_id: str) -> Rubric | None:
    return session.get(Rubric, rubric_id)


def find_edge_def_by_id(session: Session, edge_def_id: str) -> EdgeDef | None:
    return session.get(EdgeDef, edge_def_id)


def find_artifact_def_by_id(session: Session, artifact_def_id: str) -> ArtifactDef | None:
    return session.get(ArtifactDef, artifact_def_id)
