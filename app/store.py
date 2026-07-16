"""store.py — единая точка доступа к данным (S2, тикет #3; ARCHITECTURE §3.1, §3.5).

Контракт «журнал vs состояние»:
- журнальные сущности (Rubric, ArtifactSnapshot, CoherenceVerdict, SyncRunRepository,
  создание Override) — только register_* (INSERT) и find_* (SELECT);
- состояние — ровно 4 узких update_* (см. §3.5); delete_* нет вообще;
- system_user не создаётся через store — И10: сид одной строки при миграции,
  путь записи отсутствует by design.

Здесь же живут engine и фабрика сессий (файла database.py в структуре §3.1 нет).
"""

from datetime import datetime

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.models import GitHost, SyncOutcome, SyncStatus, SyncTrigger, VerdictValue
from app.models.artifact_snapshot import ArtifactSnapshot
from app.models.coherence_verdict import CoherenceVerdict
from app.models.git_credential import GitCredential
from app.models.override import Override
from app.models.repository import Repository
from app.models.rubric import Rubric
from app.models.sync_run import SyncRun
from app.models.sync_run_repository import SyncRunRepository
from app.models.system_user import SystemUser

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


def register_override(session: Session, *, coherence_verdict_id: str, reason: str) -> Override:
    """FR-10: отметка ложного разрыва (И1/И4; step_quality_card_id — v2)."""
    override = Override(coherence_verdict_id=coherence_verdict_id, reason=reason)
    session.add(override)
    return override


# ── СОСТОЯНИЕ: ровно 4 узких update_* (ARCHITECTURE §3.5) ────────────────────


def update_sync_run_status(session: Session, sync_run_id: str, status: SyncStatus) -> None:
    """FR-8, NFR-2: жизненный цикл обхода in_progress → completed/partial/failed."""
    run = session.get(SyncRun, sync_run_id)
    run.status = status
    run.completed_at = datetime.utcnow()


def update_override_revoked(session: Session, override_id: str) -> None:
    """FR-10: снятие отметки — мягкое гашение, не удаление."""
    override = session.get(Override, override_id)
    override.revoked_at = datetime.utcnow()


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
    credential.checked_at = datetime.utcnow()


# ── SELECT: find_* ───────────────────────────────────────────────────────────


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
