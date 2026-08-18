"""practice_checker — проверки применения приёмов курса (FR-14 этап 1, #80).

Отвечает на вопрос «применяет ли студент приёмы курса», а не «лежат ли файлы».
Источники: история коммитов MR, изменённые файлы MR, обсуждения MR (уже
наблюдаемые FR-12), дерево и содержимое основной ветки. Без LLM — механизмы
[История] и [Текст] каталога приёмов; LLM-рубрики — этап 2 (отдельное решение CEO).

Результат — только наблюдения с доказательствами (passed / failed / no_data +
цитата, sha, номер MR): вердикт об оценке выносит преподаватель (BR-2).
Ошибка одной проверки — no_data с warning, обход не валится (NFR-2).
"""

import logging
import re
from urllib.parse import urlparse

import httpx
from sqlalchemy.orm import Session

from app import store
from app.clients.git_client import GitClientError
from app.models import ArtifactRole, PracticeStatus, SnapshotStatus
from app.models.repository import Repository
from app.services import config_manager
from app.services.labels import PRACTICE_STATUS_LABELS, repo_short_name
from app.services.sync_orchestrator import _glob_regex, _is_approval
from app.timeutil import to_display

logger = logging.getLogger(__name__)

# Бюджет API (образец D43): не больше 3 MR на репозиторий за обход для проверок,
# читающих коммиты/изменения MR; урезание называется в логе поимённо.
_MAX_MRS_PER_CHECK = 3
_URL_TIMEOUT_SEC = 10.0
# Значение длиннее этого порога в .env.example считается настоящим, не образцом
_ENV_VALUE_LIMIT = 8


async def run_practice_checks(
    session: Session,
    git_client,
    sync_run_id: str,
    repo: Repository,
    checks,
    mr_observations,
    tree: list[str],
    *,
    http_get=None,
) -> None:
    """Прогнать все проверки конфига по репозиторию; каждая — строка журнала.

    mr_observations — последние наблюдения MR (FR-12): проверки не перечитывают
    список MR, а работают по уже собранному журналу. http_get — инъекция сети
    для readme_url (тесты подменяют, прод берёт httpx с таймаутом).
    """
    for check in checks:
        try:
            status, evidence = await _run_one(
                session, git_client, repo, check, mr_observations, tree, http_get
            )
        except GitClientError as exc:
            # NFR-2: недочитанная проверка не валит ни соседние проверки, ни обход
            logger.warning(
                "Проверка %s (%s) не выполнена: %s", check.key, repo.repo_url, exc
            )
            status = PracticeStatus.no_data
            evidence = [{"kind": "note", "quote": f"проверка не выполнена: {exc}"[:200]}]
        store.register_practice_observation(
            session,
            sync_run_id=sync_run_id,
            repository_id=repo.id,
            check_key=check.key,
            status=status,
            evidence=evidence or None,
        )


async def _run_one(session, git_client, repo, check, mr_observations, tree, http_get):
    if check.kind == "mr_commit_pattern":
        return await _check_mr_commit_pattern(session, git_client, repo, check, mr_observations)
    if check.kind == "mr_docs_sync":
        return await _check_mr_docs_sync(session, git_client, repo, check, mr_observations)
    if check.kind == "commit_id_share":
        return await _check_commit_id_share(git_client, repo, check)
    if check.kind == "review_round":
        return await _check_review_round(git_client, repo, mr_observations)
    if check.kind == "readme_url":
        return await _check_readme_url(session, git_client, repo, http_get)
    if check.kind == "tree_probe":
        return await _check_tree_probe(git_client, repo, check, tree)
    # конфиг прошёл Pydantic, но вид не реализован — честное no_data, не крах
    logger.warning("Проверка %s: неизвестный вид %s — пропущена", check.key, check.kind)
    return PracticeStatus.no_data, [{"kind": "note", "quote": f"вид {check.kind} не реализован"}]


# ── смерженные MR: бюджет и кэш ─────────────────────────────────────────────


def _merged_candidates(repo, check_key, mr_observations):
    """До 3 самых свежих смерженных MR; урезание — warning поимённо (образец D43)."""
    merged = sorted(
        (m for m in mr_observations if m.state == "merged"),
        key=lambda m: (m.updated_at is not None, m.updated_at),
        reverse=True,
    )
    if len(merged) > _MAX_MRS_PER_CHECK:
        dropped = [str(m.mr_number) for m in merged[_MAX_MRS_PER_CHECK:]]
        logger.warning(
            "%s: смерженных MR %d, проверка %s читает %d самых свежих; не проверены: %s",
            repo.repo_url, len(merged), check_key, _MAX_MRS_PER_CHECK, ", ".join(dropped),
        )
        merged = merged[:_MAX_MRS_PER_CHECK]
    return merged


def _cached_result(session, repo, check_key, candidates):
    """Экономия API: ни один кандидат не обновлялся после последней проверки —
    её статус и доказательства переносятся без чтения хостинга (упрощение по
    спеке: сравнивается updated_at MR c observed_at последней проверки)."""
    last = store.find_last_practice_observation(session, repo.id, check_key)
    if last is None:
        return None
    if any(m.updated_at is None or m.updated_at > last.observed_at for m in candidates):
        return None
    return last.status, last.evidence


# ── mr_commit_pattern: tests-first / bug-repro ──────────────────────────────


async def _check_mr_commit_pattern(session, git_client, repo, check, mr_observations):
    """Passed — в смерженном MR есть коммит по паттерну, стоящий НЕ последним:
    после него есть ≥1 коммит — след перехода красный→зелёный. Это след, не
    доказательство (порядок имитируем задним числом) — подпись есть в UI."""
    candidates = _merged_candidates(repo, check.key, mr_observations)
    if not candidates:
        return PracticeStatus.no_data, [{"kind": "note", "quote": "смерженных MR нет"}]
    cached = _cached_result(session, repo, check.key, candidates)
    if cached is not None:
        return cached
    regex = re.compile(check.pattern, re.IGNORECASE)
    tail_only = False
    for mr in candidates:
        commits = await git_client.list_mr_commits(repo.repo_url, repo.git_host, mr.mr_number)
        for commit in commits[:-1]:  # последний исключён: после красной фазы обязан быть код
            if regex.search(commit.message):
                return PracticeStatus.passed, [{
                    "kind": "mr_commit",
                    "mr_number": mr.mr_number,
                    "sha": commit.sha,
                    "quote": commit.message.splitlines()[0][:200],
                }]
        if commits and regex.search(commits[-1].message):
            tail_only = True
    detail = (
        "коммит по паттерну — последний в MR, перехода к коду после него нет"
        if tail_only else "коммита по паттерну нет"
    )
    return PracticeStatus.failed, [{
        "kind": "note",
        "quote": f"в {len(candidates)} смерженных MR {detail}",
    }]


# ── mr_docs_sync: код и доки одним MR ───────────────────────────────────────


def _has_waiver(mr, waiver_pattern):
    """Явный отказ «докам обновление не требуется, потому что…» ищется в цитатах
    маркеров наблюдения (описание MR в журнале не хранится — хранятся маркеры,
    и waiver ловит маркер docs_waiver из config.yaml)."""
    if not waiver_pattern:
        return False
    return any(
        marker.get("found") and re.search(waiver_pattern, marker.get("quote") or "", re.IGNORECASE)
        for marker in (mr.markers or {}).values()
    )


async def _check_mr_docs_sync(session, git_client, repo, check, mr_observations):
    candidates = _merged_candidates(repo, check.key, mr_observations)
    if not candidates:
        return PracticeStatus.no_data, [{"kind": "note", "quote": "смерженных MR нет"}]
    cached = _cached_result(session, repo, check.key, candidates)
    if cached is not None:
        return cached
    counted = 0
    for mr in candidates:
        paths = await git_client.list_mr_changes(repo.repo_url, repo.git_host, mr.mr_number)
        if not any(not p.endswith(".md") for p in paths):
            continue  # MR только с .md — не считается
        counted += 1
        if any(p.endswith(".md") for p in paths) or _has_waiver(mr, check.waiver_pattern):
            continue
        return PracticeStatus.failed, [{
            "kind": "mr",
            "mr_number": mr.mr_number,
            "quote": "код без документации и без «потому что»",
        }]
    if counted:
        return PracticeStatus.passed, [{
            "kind": "note", "quote": f"проверено смерженных MR с кодом: {counted}",
        }]
    return PracticeStatus.no_data, [{"kind": "note", "quote": "смерженных MR с кодом нет"}]


# ── commit_id_share: трассировка код→задача ─────────────────────────────────


async def _check_commit_id_share(git_client, repo, check):
    commits = await git_client.list_commits(
        repo.repo_url, repo.git_host, ref=repo.default_branch, limit=100
    )
    if not commits:
        return PracticeStatus.no_data, [{"kind": "note", "quote": "коммитов в основной ветке нет"}]
    # регистр значим: [A-Z]{1,6}-\d+ ловит ключи трекеров, а не случайные слова
    regex = re.compile(check.pattern)
    matched = [c for c in commits if regex.search(c.message)]
    share = len(matched) / len(commits)
    status = PracticeStatus.passed if share >= check.threshold else PracticeStatus.failed
    evidence = [{
        "kind": "note",
        "quote": f"{len(matched)}/{len(commits)} сообщений с ID тикета (порог {check.threshold:g})",
    }]
    evidence += [
        {"kind": "commit", "sha": c.sha, "quote": c.message.splitlines()[0][:120]}
        for c in matched[:3]
    ]
    return status, evidence


# ── review_round: обсуждение + вердикт «принято» ────────────────────────────


async def _check_review_round(git_client, repo, mr_observations):
    approved = sorted(
        (m for m in mr_observations if m.reviewer_approved),
        key=lambda m: (m.updated_at is not None, m.updated_at),
        reverse=True,
    )[:_MAX_MRS_PER_CHECK]
    if not approved:
        return PracticeStatus.no_data, [{"kind": "note", "quote": "MR с вердиктом «принято» нет"}]
    for mr in approved:
        notes = await git_client.list_mr_notes(repo.repo_url, repo.git_host, mr.mr_number)
        verdict_at = next((i for i, note in enumerate(notes) if _is_approval(note.body)), None)
        if verdict_at:  # 0 = вердикт первой же нотой, обсуждения не было
            first = notes[0]
            return PracticeStatus.passed, [{
                "kind": "mr",
                "mr_number": mr.mr_number,
                "quote": first.body.splitlines()[0][:200],
            }]
    return PracticeStatus.failed, [{"kind": "note", "quote": "вердикт без обсуждения"}]


# ── readme_url: публичный адрес прототипа ───────────────────────────────────

_URL_RE = re.compile(r"https://[^\s)\]>\"'`]+")
_HOSTING_HOSTS = ("github.com", "gitlab")


def _first_external_url(content: str) -> str | None:
    """Первый https-адрес вне git-хостингов — кандидат в адрес прототипа."""
    for match in _URL_RE.finditer(content):
        url = match.group().rstrip(".,;:!?»")
        host = urlparse(url).hostname or ""
        if any(marker in host for marker in _HOSTING_HOSTS):
            continue
        return url
    return None


async def _fetch_status(url: str) -> int:
    async with httpx.AsyncClient(timeout=_URL_TIMEOUT_SEC) as client:
        return (await client.get(url)).status_code


def _readme_snapshot(session, repo):
    """Последнее наблюдение README с реальным путём — включая найденные «не там» (D44)."""
    for adef in store.find_artifact_defs_by_role(session, ArtifactRole.readme):
        snap = store.find_last_snapshot(session, repo.id, adef.id)
        if snap is not None and snap.file_path and snap.status != SnapshotStatus.not_found:
            return snap
    return None


async def _check_readme_url(session, git_client, repo, http_get):
    snap = _readme_snapshot(session, repo)
    if snap is None:
        return PracticeStatus.no_data, [{"kind": "note", "quote": "README не найден"}]
    content = await git_client.get_file_content(
        repo.repo_url, repo.git_host, snap.file_path, ref=repo.default_branch
    )
    url = _first_external_url(content)
    if url is None:
        return PracticeStatus.no_data, [{
            "kind": "note", "quote": "публичного адреса в README нет",
        }]
    try:
        status_code = await (http_get or _fetch_status)(url)
    except httpx.HTTPError as exc:
        return PracticeStatus.failed, [{
            "kind": "url", "quote": f"{url} — не открывается ({exc.__class__.__name__})",
        }]
    if 200 <= status_code < 400:
        return PracticeStatus.passed, [{"kind": "url", "quote": url}]
    return PracticeStatus.failed, [{"kind": "url", "quote": f"{url} → HTTP {status_code}"}]


# ── tree_probe: файлы-паттерны в дереве ─────────────────────────────────────


def _env_example_verdict(path: str, content: str):
    """Образец переменных обязан объявлять имена, не значения: строка со значением
    длиннее порога — след настоящего секрета. Само значение в evidence не пишется
    (NFR-3: доказательство не должно стать утечкой)."""
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        if len(value.strip()) >= _ENV_VALUE_LIMIT:
            return PracticeStatus.failed, [{
                "kind": "path", "path": path,
                "quote": f"в образце значения: {name.strip()}=…",
            }]
    return PracticeStatus.passed, [{"kind": "path", "path": path}]


async def _check_tree_probe(git_client, repo, check, tree):
    regexes = [_glob_regex(pattern) for pattern in check.tree_patterns]
    matched = sorted(
        (p for p in tree if any(r.match(p) for r in regexes)),
        key=lambda p: (p.count("/"), p),  # ближайший к корню — детерминизм выбора
    )
    if not matched:
        return PracticeStatus.failed, [{
            "kind": "note", "quote": "не найдено: " + ", ".join(check.tree_patterns),
        }]
    if check.key == "env_example":
        content = await git_client.get_file_content(
            repo.repo_url, repo.git_host, matched[0], ref=repo.default_branch
        )
        return _env_example_verdict(matched[0], content)
    return PracticeStatus.passed, [{"kind": "path", "path": p} for p in matched[:3]]


# ── проекции для UI (GET /practices и дело защиты) ──────────────────────────

_STATUS_SYMBOLS = {
    PracticeStatus.passed: "✓",
    PracticeStatus.failed: "✗",
    PracticeStatus.no_data: "—",
}


def _evidence_lines(evidence) -> list[str]:
    """Доказательство — строкой для человека: MR, короткий sha, путь, цитата."""
    lines = []
    for item in evidence or []:
        parts = []
        if item.get("mr_number"):
            parts.append(f"MR !{item['mr_number']}")
        if item.get("sha"):
            parts.append(item["sha"][:8])
        if item.get("path"):
            parts.append(item["path"])
        if item.get("quote"):
            parts.append(f"«{item['quote']}»")
        if parts:
            lines.append(" · ".join(parts))
    return lines


def _cell(check, obs) -> dict:
    if obs is None:
        return {
            "key": check.key,
            "status": str(PracticeStatus.no_data),
            "symbol": _STATUS_SYMBOLS[PracticeStatus.no_data],
            "title": "нет данных: проверка ещё не выполнялась",
            "lines": [],
            "observed_at": None,
        }
    lines = _evidence_lines(obs.evidence)
    label = PRACTICE_STATUS_LABELS[obs.status]
    return {
        "key": check.key,
        "status": str(obs.status),
        "symbol": _STATUS_SYMBOLS[obs.status],
        "title": f"{label}: " + ("; ".join(lines) if lines else "без деталей"),
        "lines": lines,
        "observed_at": to_display(obs.observed_at),
    }


def build_practice_matrix(session: Session) -> dict:
    """Свод «репозиторий × проверка» для GET /practices (проекция журнала)."""
    checks = config_manager.load_config().practice_checks or []
    rows = []
    for repo in store.find_active_repositories(session):
        latest = {o.check_key: o for o in store.find_last_practice_observations(session, repo.id)}
        rows.append({
            "repository_id": repo.id,
            "name": repo_short_name(repo.repo_url),
            "repo_url": repo.repo_url,
            "cells": [_cell(check, latest.get(check.key)) for check in checks],
        })
    return {
        "checks": [{"key": c.key, "label": c.label, "lesson": c.lesson} for c in checks],
        "rows": rows,
    }


def practice_summary(session: Session, repository_id: str) -> dict:
    """Блок «Приёмы курса» для дела защиты: passed / failed / no_data с доказательствами."""
    checks = config_manager.load_config().practice_checks or []
    latest = {
        o.check_key: o for o in store.find_last_practice_observations(session, repository_id)
    }
    groups: dict[str, list] = {"passed": [], "failed": [], "no_data": []}
    for check in checks:
        obs = latest.get(check.key)
        if obs is None:
            groups["no_data"].append({
                "key": check.key, "label": check.label,
                "lines": ["проверка ещё не выполнялась"],
            })
            continue
        groups[str(obs.status)].append({
            "key": check.key, "label": check.label,
            "lines": _evidence_lines(obs.evidence),
        })
    groups["total"] = len(checks)
    return groups
