"""sync_orchestrator — цикл обхода репозиториев (G2, #9; FR-8, FR-4; ARCHITECTURE §5.1).

Обход последователен (единый writer SQLite). Ошибка одного репозитория не валит
обход (NFR-2): она превращается в исход SyncRunRepository. Снапшот пишется только
при изменении наблюдения (D28 — инкрементальность); факт «проверено, без изменений»
хранится исходом ok_unchanged.
"""

import hashlib
from pathlib import PurePosixPath

from sqlalchemy.orm import Session

from app import store
from app.clients.git_client import (
    GitAuthFailedError,
    GitClientError,
    GitRateLimitedError,
)
from app.models import SnapshotStatus, SyncOutcome, SyncStatus, SyncTrigger
from app.models.artifact_def import ArtifactDef
from app.models.repository import Repository
from app.models.sync_run import SyncRun

_OK_OUTCOMES = {SyncOutcome.ok_changed, SyncOutcome.ok_unchanged}


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _match_artifact(artifact_def: ArtifactDef, tree: list[str]) -> str | None:
    """Первый (лексикографически) путь дерева, подходящий под expected_pattern."""
    matches = [
        path for path in tree
        if PurePosixPath(path).full_match(artifact_def.expected_pattern)
    ]
    return min(matches) if matches else None


def _classify(
    content_hash: str | None, template_hashes: frozenset[str]
) -> tuple[SnapshotStatus, list[str] | None]:
    """Классификация наблюдения (AC 2). Детект заготовки — D35/BR-3 (G3, #10)."""
    if content_hash is None:
        return SnapshotStatus.not_found, None
    if content_hash in template_hashes:
        return SnapshotStatus.partial, ["template_copy"]
    return SnapshotStatus.found, None


def _outcome_for_error(exc: GitClientError) -> SyncOutcome:
    if isinstance(exc, GitAuthFailedError):
        return SyncOutcome.auth_failed
    if isinstance(exc, GitRateLimitedError):
        return SyncOutcome.skipped_rate_limit
    return SyncOutcome.repo_unavailable  # GitRepoUnavailableError и прочие GitClientError


async def _observe_artifact(
    session: Session,
    git_client,
    sync_run_id: str,
    repo: Repository,
    artifact_def: ArtifactDef,
    tree: list[str],
    template_hashes: frozenset[str],
) -> bool:
    """Наблюдение одного артефакта; True — наблюдение изменилось (записан новый снапшот)."""
    file_path = _match_artifact(artifact_def, tree)
    content_hash = None
    if file_path is not None:
        content = await git_client.get_file_content(
            repo.repo_url, repo.git_host, file_path, ref=repo.default_branch
        )
        content_hash = _content_hash(content)
    status, partial_reason = _classify(content_hash, template_hashes)
    if status == SnapshotStatus.not_found:
        file_path = None  # И8: у not_found нет file_path/sha

    last = store.find_last_snapshot(session, repo.id, artifact_def.id)
    if last is not None and (last.status, last.content_hash) == (status, content_hash):
        return False  # D28: наблюдение не изменилось — снапшот не пишем

    fields = dict(
        sync_run_id=sync_run_id,
        repository_id=repo.id,
        artifact_def_id=artifact_def.id,
        status=status,
        file_path=file_path,
        content_hash=content_hash,
    )
    if partial_reason is not None:
        # явный None в JSON-колонке стал бы json-'null' и нарушил бы CHECK И8
        fields["partial_reason"] = partial_reason
    store.register_snapshot(session, **fields)
    return True


async def _sync_one_repo(
    session: Session,
    git_client,
    sync_run_id: str,
    repo: Repository,
    artifact_defs: list[ArtifactDef],
    template_hashes: frozenset[str],
) -> SyncOutcome:
    try:
        tree = await git_client.get_tree(repo.repo_url, repo.git_host, ref=repo.default_branch)
        changed = False
        for artifact_def in artifact_defs:
            changed |= await _observe_artifact(
                session, git_client, sync_run_id, repo, artifact_def, tree, template_hashes
            )
        return SyncOutcome.ok_changed if changed else SyncOutcome.ok_unchanged
    except GitClientError as exc:  # NFR-2: ошибка репозитория — исход, не крах обхода
        return _outcome_for_error(exc)


def _final_status(outcomes: list[SyncOutcome]) -> SyncStatus:
    """AC 4: completed — все ок; partial — часть ок; failed — ни одного ок при непустом списке."""
    if not outcomes or all(o in _OK_OUTCOMES for o in outcomes):
        return SyncStatus.completed
    if any(o in _OK_OUTCOMES for o in outcomes):
        return SyncStatus.partial
    return SyncStatus.failed


async def run_sync(
    session: Session,
    git_client,
    *,
    triggered_by: SyncTrigger = SyncTrigger.schedule,
) -> SyncRun:
    """Полный обход активных репозиториев (§5.1). Возвращает SyncRun с финальным статусом."""
    run = store.register_sync_run(session, triggered_by=triggered_by)
    session.flush()

    artifact_defs = [
        adef
        for lesson in store.find_all_lessons(session)
        for adef in store.find_artifact_defs_by_lesson(session, lesson.id)
    ]
    template_hashes: frozenset[str] = frozenset()  # заполняется детектом заготовок (G3, #10)

    outcomes: list[SyncOutcome] = []
    for repo in store.find_active_repositories(session):
        outcome = await _sync_one_repo(
            session, git_client, run.id, repo, artifact_defs, template_hashes
        )
        store.register_sync_outcome(
            session, sync_run_id=run.id, repository_id=repo.id, outcome=outcome
        )
        outcomes.append(outcome)

    store.update_sync_run_status(session, run.id, _final_status(outcomes))
    session.flush()
    return run
