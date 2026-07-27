"""sync_orchestrator — цикл обхода репозиториев (G2, #9; FR-8, FR-4; ARCHITECTURE §5.1).

Обход последователен (единый writer SQLite). Ошибка одного репозитория не валит
обход (NFR-2): она превращается в исход SyncRunRepository. Снапшот пишется только
при изменении наблюдения (D28 — инкрементальность); факт «проверено, без изменений»
хранится исходом ok_unchanged.
"""

import hashlib
import logging
import re

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

logger = logging.getLogger(__name__)

_OK_OUTCOMES = {SyncOutcome.ok_changed, SyncOutcome.ok_unchanged}


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _glob_regex(pattern: str) -> re.Pattern:
    """Глоб → regex: `**` — любое число сегментов, `*` — внутри сегмента.

    Своя трансляция, а не PurePosixPath.full_match: full_match появился в 3.13,
    проект заявляет Python 3.12+ (pyproject/CLAUDE.md).
    """
    segments = pattern.split("/")
    parts = []
    for i, segment in enumerate(segments):
        last = i == len(segments) - 1
        if segment == "**":
            parts.append(".*" if last else "(?:[^/]+/)*")
            continue
        escaped = re.escape(segment).replace(r"\*", "[^/]*").replace(r"\?", "[^/]")
        parts.append(escaped if last else escaped + "/")
    return re.compile("^" + "".join(parts) + "$")


def _match_artifact(artifact_def: ArtifactDef, tree: list[str]) -> list[str]:
    """Все пути дерева под expected_pattern, отсортированы (детерминизм наблюдения)."""
    regex = _glob_regex(artifact_def.expected_pattern)
    return sorted(path for path in tree if regex.match(path))


def _find_displaced(pattern: str, tree: list[str]) -> str | None:
    """BR-3: файл с ожидаемым именем, но не в ожидаемой папке («частично/не там»).

    Только для конкретных имён (без глоб-символов в basename): для «*.md»
    поиск по имени вырождался бы в «любой md где угодно».
    """
    basename = pattern.rsplit("/", 1)[-1]
    if any(ch in basename for ch in "*?"):
        return None
    candidates = sorted(p for p in tree if p.rsplit("/", 1)[-1] == basename)
    return candidates[0] if candidates else None


async def _fetch_template_hashes(git_client, template_repo) -> frozenset[str]:
    """D35: content_hash всех файлов репозитория-шаблона — один раз за обход.

    Адрес шаблона — из config.yaml (PRD FR-4: «задаётся в конфиге FR-2»),
    сюда приходит как ConfigYAML.template_repo. Шаблон не настроен или
    недоступен — детект деградирует до «выключен» на этот обход с warning
    в логе, сам обход не валится (NFR-2).
    """
    if template_repo is None:
        return frozenset()
    try:
        tree = await git_client.get_tree(
            template_repo.url, template_repo.git_host, ref=template_repo.branch
        )
        hashes = set()
        for path in tree:
            content = await git_client.get_file_content(
                template_repo.url, template_repo.git_host, path, ref=template_repo.branch
            )
            hashes.add(_content_hash(content))
        return frozenset(hashes)
    except GitClientError as exc:
        logger.warning(
            "Репозиторий-шаблон недоступен (%s) — детект заготовок выключен на этот обход", exc
        )
        return frozenset()


def _outcome_for_error(exc: GitClientError) -> SyncOutcome:
    if isinstance(exc, GitAuthFailedError):
        return SyncOutcome.auth_failed
    if isinstance(exc, GitRateLimitedError):
        return SyncOutcome.skipped_rate_limit
    return SyncOutcome.repo_unavailable  # GitRepoUnavailableError и прочие GitClientError


async def _hash_matches(git_client, repo: Repository, matches: list[str]) -> str:
    """Хеш наблюдения артефакта по всем совпавшим файлам.

    Один файл — sha256 содержимого (совместимо с G3-сравнением против шаблона).
    Несколько — sha256 связки «путь + хеш файла» по отсортированным путям:
    изменение любого из файлов меняет наблюдение (fix по ревью G2, находка 1).
    """
    if len(matches) == 1:
        content = await git_client.get_file_content(
            repo.repo_url, repo.git_host, matches[0], ref=repo.default_branch
        )
        return _content_hash(content)
    parts = []
    for path in matches:
        content = await git_client.get_file_content(
            repo.repo_url, repo.git_host, path, ref=repo.default_branch
        )
        parts.append(f"{path}\0{_content_hash(content)}")
    return _content_hash("\0".join(parts))


async def _observe_artifact(
    session: Session,
    git_client,
    sync_run_id: str,
    repo: Repository,
    artifact_def: ArtifactDef,
    tree: list[str],
    head_sha: str | None,
    template_hashes: frozenset[str],
) -> bool:
    """Наблюдение одного артефакта; True — наблюдение изменилось (записан новый снапшот)."""
    matches = _match_artifact(artifact_def, tree)
    if matches and len(matches) == 1:
        file_path = matches[0]
        content = await git_client.get_file_content(
            repo.repo_url, repo.git_host, file_path, ref=repo.default_branch
        )
        content_hash = _content_hash(content)
        # BR-3: заготовка = пустой файл ИЛИ не изменён относительно шаблона (D35)
        if not content.strip() or content_hash in template_hashes:
            status, partial_reason = SnapshotStatus.partial, ["template_copy"]
        else:
            status, partial_reason = SnapshotStatus.found, None
    elif matches:
        # мульти-файловое наблюдение: хеш связки, с шаблоном не сравнивается
        # by construction (см. issue #10); представитель — первый по алфавиту
        file_path = matches[0]
        content_hash = await _hash_matches(git_client, repo, matches)
        status, partial_reason = SnapshotStatus.found, None
    elif displaced := _find_displaced(artifact_def.expected_pattern, tree):
        file_path = displaced
        content = await git_client.get_file_content(
            repo.repo_url, repo.git_host, displaced, ref=repo.default_branch
        )
        content_hash = _content_hash(content)
        # приоритет причин — C3: template_copy > wrong_place
        if not content.strip() or content_hash in template_hashes:
            partial_reason = ["template_copy", "wrong_place"]
        else:
            partial_reason = ["wrong_place"]
        status = SnapshotStatus.partial
    else:
        status, partial_reason, file_path, content_hash = (
            SnapshotStatus.not_found, None, None, None  # И8: у not_found нет file_path/sha
        )

    last = store.find_last_snapshot(session, repo.id, artifact_def.id)
    observed = (status, content_hash, file_path, partial_reason)
    if last is not None and observed == (
        last.status, last.content_hash, last.file_path, last.partial_reason or None
    ):
        return False  # D28: наблюдение (включая причины и место) не изменилось — снапшот не пишем

    fields = dict(
        sync_run_id=sync_run_id,
        repository_id=repo.id,
        artifact_def_id=artifact_def.id,
        status=status,
        file_path=file_path,
        content_hash=content_hash,
        # FR-9: SHA головы ветки — свидетельство «такая версия существовала» (C4)
        source_commit_sha=head_sha if status != SnapshotStatus.not_found else None,
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
) -> tuple[SyncOutcome, str | None]:
    try:
        tree = await git_client.get_tree(repo.repo_url, repo.git_host, ref=repo.default_branch)
        head_sha = await git_client.get_head_sha(
            repo.repo_url, repo.git_host, ref=repo.default_branch
        )
        changed = False
        for artifact_def in artifact_defs:
            changed |= await _observe_artifact(
                session, git_client, sync_run_id, repo, artifact_def, tree, head_sha,
                template_hashes,
            )
        return SyncOutcome.ok_changed if changed else SyncOutcome.ok_unchanged, None
    except GitClientError as exc:  # NFR-2: ошибка репозитория — исход, не крах обхода
        # Ошибка посреди цикла артефактов: уже записанные наблюдения остаются —
        # append-only журнал истинен, исход честно говорит «не дочитано» (§5.3)
        return _outcome_for_error(exc), str(exc)[:500]


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
    template_repo=None,  # ConfigYAML.template_repo (PRD FR-4); None — детект заготовок выключен
) -> SyncRun:
    """Полный обход активных репозиториев (§5.1). Возвращает SyncRun с финальным статусом."""
    run = store.register_sync_run(session, triggered_by=triggered_by)
    session.flush()

    artifact_defs = [
        adef
        for lesson in store.find_all_lessons(session)
        for adef in store.find_artifact_defs_by_lesson(session, lesson.id)
    ]
    template_hashes = await _fetch_template_hashes(git_client, template_repo)  # D35: раз за обход

    outcomes: list[SyncOutcome] = []
    for repo in store.find_active_repositories(session):
        outcome, detail = await _sync_one_repo(
            session, git_client, run.id, repo, artifact_defs, template_hashes
        )
        store.register_sync_outcome(
            session, sync_run_id=run.id, repository_id=repo.id, outcome=outcome, detail=detail
        )
        outcomes.append(outcome)

    store.update_sync_run_status(session, run.id, _final_status(outcomes))
    session.flush()
    return run
