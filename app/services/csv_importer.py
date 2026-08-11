"""S6 (#8), FR-1: импорт CSV «ФИО,repo_url» → Repository.

ФИО не сохраняется — именных данных в модели нет (рамка CEO, data-model §1, BR-6):
реестр «ФИО ↔ адрес» живёт вне системы. Из строки берётся только repo_url.
Дубликаты (после нормализации, И6) отсеиваются; недоступный репозиторий
регистрируется тоже — доступность лишь считается в сводке (слепая зона — FR-6).
"""

import csv
import io
import logging

from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import store
from app.clients.git_client import GitClient, GitClientError
from app.models import GitHost
from app.services.branch_detect import refresh_default_branch

logger = logging.getLogger(__name__)


class ImportSummary(BaseModel):
    """Сводка FR-1: N доступно / M недоступно / K дубликатов.

    D45 (#71): плюс числа реконсиляции реестра — архивировано и возвращено.
    Молча терять строки матрицы нельзя: исчезновение репозитория с экрана
    должно быть объяснимо числом в ответе и записью в логе.
    """

    available: int = 0
    unavailable: int = 0
    duplicates: int = 0
    archived: int = 0
    restored: int = 0


def _detect_git_host(repo_url: str) -> GitHost:
    return GitHost.GitHub if "github.com" in repo_url.lower() else GitHost.GitLab


def _extract_repo_urls(csv_text: str) -> list[str]:
    """Колонка repo_url — последняя; заголовок пропускается; ФИО отбрасывается."""
    urls = []
    for row in csv.reader(io.StringIO(csv_text)):
        if not row:
            continue
        candidate = row[-1].strip()
        if candidate.lower() == "repo_url" or not candidate.startswith("http"):
            continue
        urls.append(candidate)
    return urls


async def import_csv(session: Session, csv_text: str, git_client: GitClient) -> ImportSummary:
    """FR-1 + D45: импорт и реконсиляция реестра — CSV источник правды в обе стороны."""
    urls = _extract_repo_urls(csv_text)
    if not urls:
        # D45: пустой или битый файл не обнуляет реестр одним нажатием —
        # «архивировать всё» не должно быть достижимо случайно
        raise HTTPException(status_code=400, detail="в файле не распознан ни один адрес репозитория")

    summary = ImportSummary()
    seen = set()
    for repo_url in urls:
        existing = store.find_repository_by_normalized_url(session, repo_url)
        if existing is not None:
            seen.add(existing.id)
            if existing.archived_at is not None:
                # вернулся в реестр — снова активен, история наблюдений цела (FR-9)
                store.restore_repository(session, existing.id)
                summary.restored += 1
                logger.info("Реестр: %s вернулся в CSV — разархивирован", existing.repo_url)
            else:
                summary.duplicates += 1
            continue
        git_host = _detect_git_host(repo_url)
        repo = store.register_repository(session, repo_url=repo_url, git_host=git_host)
        session.flush()
        seen.add(repo.id)
        await refresh_default_branch(session, git_client, repo)  # ADR-006/#48/#50
        session.flush()
        try:
            await git_client.get_tree(repo_url, git_host.value, repo.default_branch)
            summary.available += 1
        except GitClientError:
            summary.unavailable += 1

    # D45: чего в файле нет — уходит в архив. Не удаляется: снапшоты и вердикты
    # сохраняются (FR-9), ошибочно убранный адрес возвращается следующим импортом.
    for repo in store.find_active_repositories(session):
        if repo.id in seen:
            continue
        store.archive_repository(session, repo.id)
        summary.archived += 1
        logger.info("Реестр: %s отсутствует в CSV — заархивирован", repo.repo_url)
    return summary
