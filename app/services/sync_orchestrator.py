"""sync_orchestrator — цикл обхода репозиториев (G2, #9; FR-8, FR-4; ARCHITECTURE §5.1).

Обход последователен (единый writer SQLite). Ошибка одного репозитория не валит
обход (NFR-2): она превращается в исход SyncRunRepository. Снапшот пишется только
при изменении наблюдения (D28 — инкрементальность); факт «проверено, без изменений»
хранится исходом ok_unchanged.
"""

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app import store, timeutil
from app.clients.git_client import (
    GitAuthFailedError,
    GitClientError,
    GitRateLimitedError,
)
from app.config import settings
from app.models import SnapshotStatus, SyncOutcome, SyncStatus, SyncTrigger
from app.models.artifact_def import ArtifactDef
from app.models.repository import Repository
from app.models.sync_run import SyncRun
from app.services.branch_detect import refresh_default_branch

logger = logging.getLogger(__name__)

_OK_OUTCOMES = {SyncOutcome.ok_changed, SyncOutcome.ok_unchanged}
_pending_tasks: set[asyncio.Task] = set()  # GC-guard для fire-and-forget задач свода

# D42 (порог выбран CEO 2026-08-04): обход длится ~1,5 минуты на 9 репозиториев.
# Всё, что висит in_progress дольше 5 минут, — не идущий обход, а след упавшего
# процесса: статус в БД некому было закрыть. Без срока давности кнопка
# «обновить сейчас» залипла бы до ручной правки БД.
ASSUME_DEAD_AFTER = timedelta(minutes=5)


def is_sync_running(session: Session, now: datetime | None = None) -> bool:
    """Идёт ли обход прямо сейчас (D42, решение CEO 2026-08-04).

    Истина серверная, а не браузерная: погашенная кнопка в одной вкладке не
    мешает ни второй вкладке, ни cron. Повторный запуск даёт 500 `database is
    locked` — транзакция обхода держит единственного писателя SQLite.
    """
    last_run = store.find_last_sync_run(session)
    if last_run is None or last_run.status != SyncStatus.in_progress:
        return False
    return (now or timeutil.utcnow()) - last_run.started_at < ASSUME_DEAD_AFTER


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


def _run_probes(probes, content: str) -> list[dict]:
    """T2 (#44): пробы содержимого — узкая редакция.

    contains — finding, если regex НЕ найден (обязательный раздел отсутствует);
    not_contains — finding, если regex найден (маркер незаполненного шаблона).
    Битый regex пробы — warning, проба пропускается (конфиг не роняет обход, NFR-2).
    """
    findings: list[dict] = []
    for probe in probes or []:
        try:
            if probe.get("contains") and not re.search(probe["contains"], content, re.I | re.M):
                findings.append({"key": probe["key"], "label": probe.get("label", probe["key"])})
            elif probe.get("not_contains") and re.search(probe["not_contains"], content, re.I | re.M):
                findings.append({"key": probe["key"], "label": probe.get("label", probe["key"])})
        except re.error as exc:
            logger.warning("Проба %s: битый regex (%s) — пропущена", probe.get("key"), exc)
    return findings


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


async def _head_commit_date(git_client, repo: Repository):
    """D19 (#65): дата головного коммита — NULL при любом сбое (негативный AC).

    Клиент без метода (старые фейки в тестах) или ошибка API не валят обход:
    дата — свидетельство «в плюс», не обязательное условие наблюдения.
    """
    getter = getattr(git_client, "get_head_commit_date", None)
    if getter is None:
        return None
    try:
        raw = await getter(repo.repo_url, repo.git_host, ref=repo.default_branch)
    except GitClientError:
        return None
    return _parse_host_datetime(raw) if raw else None


async def _observe_artifact(
    session: Session,
    git_client,
    sync_run_id: str,
    repo: Repository,
    artifact_def: ArtifactDef,
    tree: list[str],
    head_sha: str | None,
    head_date,
    template_hashes: frozenset[str],
) -> bool:
    """Наблюдение одного артефакта; True — наблюдение изменилось (записан новый снапшот)."""
    matches = _match_artifact(artifact_def, tree)
    probe_findings = None  # T2 (#44): пробы применимы только там, где читается контент
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
        probe_findings = _run_probes(artifact_def.content_probes, content) or None
    elif matches:
        # мульти-файловое наблюдение: хеш связки, с шаблоном не сравнивается
        # by construction (см. issue #10); представитель — первый по алфавиту;
        # пробы не применяются (цельного контента нет)
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
        probe_findings = _run_probes(artifact_def.content_probes, content) or None
    else:
        status, partial_reason, file_path, content_hash = (
            SnapshotStatus.not_found, None, None, None  # И8: у not_found нет file_path/sha
        )

    last = store.find_last_snapshot(session, repo.id, artifact_def.id)
    observed = (status, content_hash, file_path, partial_reason, probe_findings)
    if last is not None and observed == (
        last.status, last.content_hash, last.file_path, last.partial_reason or None,
        last.probe_findings or None,
    ):
        return False  # D28: наблюдение (включая причины, место и пробы) не изменилось

    fields = dict(
        sync_run_id=sync_run_id,
        repository_id=repo.id,
        artifact_def_id=artifact_def.id,
        status=status,
        file_path=file_path,
        content_hash=content_hash,
        # FR-9: SHA головы ветки — свидетельство «такая версия существовала» (C4)
        source_commit_sha=head_sha if status != SnapshotStatus.not_found else None,
        # D19 (#65): дата коммита — «когда сделано» против «когда увидели»
        source_commit_date=head_date if status != SnapshotStatus.not_found else None,
    )
    if probe_findings is not None:
        # как и у partial_reason: явный None стал бы json-'null' вместо SQL NULL (D6)
        fields["probe_findings"] = probe_findings
    if partial_reason is not None:
        # явный None в JSON-колонке стал бы json-'null' и нарушил бы CHECK И8
        fields["partial_reason"] = partial_reason
    store.register_snapshot(session, **fields)
    return True


_MAX_CANDIDATE_BRANCHES = 5


def _parse_branch_date(raw):
    """Дата головы ветки: сырая ISO-строка хостинга → datetime, сбой → None."""
    if not raw:
        return None
    return _parse_host_datetime(raw)


async def _branch_head_date(git_client, repo: Repository, branch):
    """Дата головы ветки; GitHub в списке веток её не отдаёт — дочитываем точечно."""
    known = _parse_branch_date(branch.committed_date)
    if known is not None:
        return known
    getter = getattr(git_client, "get_head_commit_date", None)
    if getter is None:
        return None
    try:
        return _parse_branch_date(await getter(repo.repo_url, repo.git_host, ref=branch.name))
    except GitClientError:
        return None


def _count_artifacts(artifact_defs: list[ArtifactDef], tree: list[str]) -> int:
    """Сколько ролей курса представлено в дереве — грубая мера «здесь есть работа».

    Считаются роли, не файлы: несколько дефов одной роли — альтернативные пути
    одного артефакта (DM §1.4), иначе ветка со всеми альтернативами выглядела бы
    богаче, чем есть.
    """
    return len({adef.role for adef in artifact_defs if _match_artifact(adef, tree)})


async def _scan_branches(
    session: Session,
    git_client,
    sync_run_id: str,
    repo: Repository,
    artifact_defs: list[ArtifactDef],
    default_tree: list[str],
) -> None:
    """D43 (#69): найти работу, лежащую вне дефолтной ветки.

    Вердикты по этим веткам не считаются — канон связности остаётся за дефолтной
    веткой, и свод честно отвечает «в основной ветке пусто». Смысл шага в другом:
    у студентов в форках ЦУ `main` защищена (merge только Maintainer), слить туда
    работу они не могут физически, и пустая строка матрицы читалась как «не сделал».

    Любая ошибка здесь — предупреждение, а не исход репозитория: подсказка это
    удобство, она не вправе портить наблюдение артефактов (NFR-2).
    """
    lister = getattr(git_client, "list_branches", None)
    if lister is None:
        return
    try:
        branches = await lister(repo.repo_url, repo.git_host)
    except GitClientError as exc:
        logger.warning("Список веток %s недоступен (%s) — подсказки пропущены", repo.repo_url, exc)
        return

    in_default = _count_artifacts(artifact_defs, default_tree)
    default_date = None
    others = []
    for branch in branches:
        if branch.name == repo.default_branch:
            default_date = await _branch_head_date(git_client, repo, branch)
        else:
            others.append(branch)

    dated = []
    for branch in others:
        head_date = await _branch_head_date(git_client, repo, branch)
        # ветка старше дефолтной — это её прошлое, а не спрятанная работа
        if default_date is not None and head_date is not None and head_date <= default_date:
            continue
        dated.append((branch, head_date))

    dated.sort(key=lambda pair: (pair[1] is None, -(pair[1].timestamp() if pair[1] else 0)))
    if len(dated) > _MAX_CANDIDATE_BRANCHES:
        dropped = [b.name for b, _ in dated[_MAX_CANDIDATE_BRANCHES:]]
        logger.warning(
            "%s: веток-кандидатов %d, осмотрены %d самых свежих; не осмотрены: %s",
            repo.repo_url, len(dated), _MAX_CANDIDATE_BRANCHES, ", ".join(dropped),
        )
        dated = dated[:_MAX_CANDIDATE_BRANCHES]

    for branch, head_date in dated:
        try:
            tree = await git_client.get_tree(repo.repo_url, repo.git_host, ref=branch.name)
        except GitClientError as exc:
            logger.warning("Ветка %s (%s) не прочитана: %s", branch.name, repo.repo_url, exc)
            continue
        found = _count_artifacts(artifact_defs, tree)
        if found <= in_default:
            continue  # прироста нет — сигналить не о чем (AC 6, защита от шума)
        store.register_branch_hint(
            session,
            sync_run_id=sync_run_id,
            repository_id=repo.id,
            branch_name=branch.name,
            head_sha=branch.head_sha,
            head_date=head_date,
            artifacts_found=found,
            artifacts_in_default=in_default,
        )


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
        head_date = await _head_commit_date(git_client, repo)  # D19: NULL — не сбой
        changed = False
        for artifact_def in artifact_defs:
            changed |= await _observe_artifact(
                session, git_client, sync_run_id, repo, artifact_def, tree, head_sha,
                head_date, template_hashes,
            )
        # D43 (#69): подсказки по веткам — после наблюдения, отдельным шагом:
        # они не влияют ни на снапшоты, ни на исход репозитория
        await _scan_branches(session, git_client, sync_run_id, repo, artifact_defs, tree)
        return SyncOutcome.ok_changed if changed else SyncOutcome.ok_unchanged, None
    except GitClientError as exc:  # NFR-2: ошибка репозитория — исход, не крах обхода
        # Ошибка посреди цикла артефактов: уже записанные наблюдения остаются —
        # append-only журнал истинен, исход честно говорит «не дочитано» (§5.3)
        return _outcome_for_error(exc), str(exc)[:500]


@dataclass(frozen=True)
class PendingPair:
    """Пара «ребро × репозиторий» без валидного вердикта на текущую четвёрку (И3)."""

    edge_def_id: str
    repository_id: str
    source_snapshot_id: str
    target_snapshot_id: str
    source_content_hash: str
    target_content_hash: str
    rubric_id: str
    llm_model: str


def find_pairs_without_verdict(session: Session, llm_model: str) -> list[PendingPair]:
    """§5.1: все пары (EdgeDef × репозиторий с текущими снапшотами обеих ролей),
    у которых нет валидного (не deferred) вердикта на текущую четвёрку."""
    pairs: list[PendingPair] = []
    repos = store.find_active_repositories(session)
    for edge in store.find_all_edge_defs(session):
        source_defs = store.find_artifact_defs_by_role(session, edge.source_role)
        target_defs = store.find_artifact_defs_by_role(session, edge.target_role)
        for repo in repos:
            for source_def in source_defs:
                for target_def in target_defs:
                    snap_a = store.find_last_snapshot(session, repo.id, source_def.id)
                    snap_b = store.find_last_snapshot(session, repo.id, target_def.id)
                    if not (snap_a and snap_b and snap_a.content_hash and snap_b.content_hash):
                        continue  # нет текущих наблюдений обеих ролей — проверять нечего
                    if store.find_verdict_by_quadruple(
                        session,
                        source_content_hash=snap_a.content_hash,
                        target_content_hash=snap_b.content_hash,
                        rubric_id=edge.rubric_id,
                        llm_model=llm_model,
                    ):
                        continue  # D25: валидный вердикт есть — не мигаем
                    pairs.append(PendingPair(
                        edge_def_id=edge.id,
                        repository_id=repo.id,
                        source_snapshot_id=snap_a.id,
                        target_snapshot_id=snap_b.id,
                        source_content_hash=snap_a.content_hash,
                        target_content_hash=snap_b.content_hash,
                        rubric_id=edge.rubric_id,
                        llm_model=llm_model,
                    ))
    return pairs


async def reconcile_llm_pairs(
    session: Session, *, llm_model: str, verdict_worker=None
) -> tuple[list[PendingPair], list[asyncio.Task]]:
    """Идемпотентный свод-реконсиляция (§5.1): в конце КАЖДОГО обхода.

    Deferred-пары и потерянные при рестарте задачи перепроверяются сами:
    у них нет валидного вердикта, они попадут в следующий свод (без TaskTracker).
    verdict_worker — ядро FR-5; до прохождения Фазы 0 оно не кодится (PRD §13),
    поэтому воркер инъецируется, а без него пары только идентифицируются —
    состояние «проверяется» вычислимо (В13) и видно в /health.
    """
    pairs = find_pairs_without_verdict(session, llm_model)
    if verdict_worker is None:
        if pairs:
            logger.info(
                "Свод: %d пар без вердикта; ядро FR-5 не подключено (гейт Фазы 0)", len(pairs)
            )
        return pairs, []
    tasks = [asyncio.create_task(verdict_worker(pair)) for pair in pairs]
    for task in tasks:
        # event loop держит task слабой ссылкой — без guard задача может быть
        # собрана GC до завершения; это не TaskTracker (состояния не хранит)
        _pending_tasks.add(task)
        task.add_done_callback(_pending_tasks.discard)
    return pairs, tasks


def _match_marker(pattern: str, description: str) -> dict:
    """Маркер недели в описании MR: найден/нет + цитата строки (FR-12)."""
    for line in description.splitlines():
        if re.search(pattern, line):
            return {"found": True, "quote": line.strip()[:200]}
    return {"found": False, "quote": None}


def _is_approval(note: str) -> bool:
    """Вердикт «принято» ревьюера (ADR-007): строка начинается с «принято»/«approve».

    Отрицания («не принято», «пока не принято») не матчатся правилом начала строки.
    """
    for line in note.splitlines():
        stripped = line.strip().lower()
        if stripped.startswith("принято") or stripped.startswith("approve"):
            return True
    return False


def _parse_host_datetime(value: str):
    """ISO-строка хостинга → наивный UTC (контракт хранения, timeutil)."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


async def _observe_mrs(
    session: Session, git_client, sync_run_id: str, repo: Repository, markers
) -> None:
    """FR-12 (#40): наблюдение MR репозитория. Ошибка — warning, не крах обхода (NFR-2)."""
    try:
        mrs = await git_client.list_merge_requests(repo.repo_url, repo.git_host)
    except GitClientError as exc:
        logger.warning("MR-наблюдение недоступно для %s (%s)", repo.repo_url, exc)
        return
    seen_numbers: set[int] = set()  # дубль номера из сдвига offset-пагинации не роняет И12
    for mr in mrs:
        if mr.number in seen_numbers:
            continue
        seen_numbers.add(mr.number)
        found_markers = {m.key: _match_marker(m.pattern, mr.description) for m in markers}
        approved = False
        prev = store.find_previous_mr_observation(session, repo.id, mr.number)
        if prev is not None and prev.head_sha == mr.head_sha and prev.reviewer_approved:
            approved = True  # вердикт при неизменной голове не отзываем — экономия API
        elif mr.state in ("opened", "merged"):
            # порядок сдачи 2026-07-29: студенты мержат сами — вердикт нужен и у merged;
            # closed без слияния не читаем
            try:
                notes = await git_client.list_mr_notes(repo.repo_url, repo.git_host, mr.number)
                # ADR-007 сц. 5: «принято» устаревает вместе с головой ветки — после
                # force-push считаются только ноты, датированные позже того момента,
                # когда мы в последний раз видели прежнюю голову
                if prev is not None and prev.head_sha != mr.head_sha:
                    cutoff = prev.observed_at
                    approved = any(
                        _is_approval(note.body)
                        and (parsed := _parse_host_datetime(note.created_at)) is not None
                        and parsed > cutoff
                        for note in notes
                    )
                else:
                    approved = any(_is_approval(note.body) for note in notes)
            except GitClientError as exc:
                logger.warning("Обсуждение MR !%s (%s) недоступно: %s", mr.number, repo.repo_url, exc)
        store.register_mr_observation(
            session,
            sync_run_id=sync_run_id,
            repository_id=repo.id,
            mr_number=mr.number,
            title=mr.title,
            source_branch=mr.source_branch,
            state=mr.state,
            reviewer_approved=approved,
            markers=found_markers,
            head_sha=mr.head_sha,
            updated_at=_parse_host_datetime(mr.updated_at),
        )


def build_health_counters(session: Session, llm_model: str) -> dict:
    """§5.4 (I2, #13): счётчики /health — вычислимые запросы к БД, без in-memory состояния.

    deferred считается по текущим парам (последний вердикт четвёрки = deferred),
    а не по всему журналу: исторические deferred уже неактуальны.
    """
    pairs = find_pairs_without_verdict(session, llm_model)
    deferred = {"llm_unavailable": 0, "parse_error": 0}
    for pair in pairs:
        verdict = store.find_latest_verdict_for_quadruple(
            session,
            source_content_hash=pair.source_content_hash,
            target_content_hash=pair.target_content_hash,
            rubric_id=pair.rubric_id,
            llm_model=pair.llm_model,
        )
        if verdict is not None and verdict.deferred_reason is not None:
            reason = str(verdict.deferred_reason)
            deferred[reason] = deferred.get(reason, 0) + 1  # устойчиво к расширению C1
    return {"pairs_without_verdict": len(pairs), "deferred": deferred}


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
    llm_model: str | None = None,  # модель четвёрки И3; по умолчанию — settings.deepseek_model
    verdict_worker=None,  # ядро FR-5; None до прохождения Фазы 0 (PRD §13)
    process_markers=None,  # FR-12 (#40): маркеры недели из config.yaml; None — MR-шаг выключен
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

    # ADR-006: сверка default_branch для репо, созданных до фикса (#48/#50 — общий шаг)
    for repo in store.find_active_repositories(session):
        await refresh_default_branch(session, git_client, repo)
    session.flush()

    outcomes: list[SyncOutcome] = []
    for repo in store.find_active_repositories(session):
        outcome, detail = await _sync_one_repo(
            session, git_client, run.id, repo, artifact_defs, template_hashes
        )
        store.register_sync_outcome(
            session, sync_run_id=run.id, repository_id=repo.id, outcome=outcome, detail=detail
        )
        outcomes.append(outcome)
        # FR-12 (#40): наблюдение MR — после артефактов, деградация независимая (NFR-2)
        if process_markers is not None:
            await _observe_mrs(session, git_client, run.id, repo, process_markers)

    store.update_sync_run_status(session, run.id, _final_status(outcomes))
    # Коммит ДО свода, а не flush: воркеры ядра FR-5 работают в собственных
    # сессиях (coherence_analyzer.make_verdict_worker — «сессия на пару»), и
    # незакоммиченные снапшоты обхода им не видны. Без этого каждая пара со
    # свежим снапшотом падает в И2 «снапшоты пары не найдены» — обход рапортует
    # completed, а вердиктов ноль (боевой случай 2026-08-04, обход 731b4991).
    session.commit()

    # §5.1: свод-реконсиляция LLM-пар — в конце каждого обхода (G4, #11)
    await reconcile_llm_pairs(
        session,
        llm_model=llm_model or settings.deepseek_model,
        verdict_worker=verdict_worker,
    )
    return run
