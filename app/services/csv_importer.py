"""S6 (#8), FR-1: импорт CSV «ФИО,repo_url» → Repository.

ФИО не сохраняется — именных данных в модели нет (рамка CEO, data-model §1, BR-6):
реестр «ФИО ↔ адрес» живёт вне системы. Из строки берётся только repo_url.
Дубликаты (после нормализации, И6) отсеиваются; недоступный репозиторий
регистрируется тоже — доступность лишь считается в сводке (слепая зона — FR-6).
"""

import csv
import io

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import store
from app.clients.git_client import GitClient, GitClientError
from app.models import GitHost


class ImportSummary(BaseModel):
    """Сводка FR-1: N доступно / M недоступно / K дубликатов."""

    available: int = 0
    unavailable: int = 0
    duplicates: int = 0


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
    summary = ImportSummary()
    for repo_url in _extract_repo_urls(csv_text):
        if store.find_repository_by_normalized_url(session, repo_url):
            summary.duplicates += 1
            continue
        git_host = _detect_git_host(repo_url)
        repo = store.register_repository(session, repo_url=repo_url, git_host=git_host)
        session.flush()
        try:
            await git_client.get_tree(repo_url, git_host.value, repo.default_branch)
            summary.available += 1
        except GitClientError:
            summary.unavailable += 1
    return summary
