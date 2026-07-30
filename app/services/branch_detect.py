"""Детект default-ветки (ADR-006): общий шаг импорта (S6) и обхода (G2) — #50.

До #50 блок дублировался в csv_importer и sync_orchestrator и мутировал
repo.default_branch напрямую мимо store (нарушение контракта §3.5).
"""

import logging

from sqlalchemy.orm import Session

from app import store
from app.clients.git_client import GitClientError
from app.models.repository import Repository

logger = logging.getLogger(__name__)


async def refresh_default_branch(session: Session, git_client, repo: Repository) -> None:
    """Сверить default_branch репозитория с API хостинга.

    Ошибка API — молча пропускаем (репозиторий уйдёт в свой исход при чтении дерева);
    None у пустого проекта (#48) в NOT NULL не пишем. Мутация — только через store (§3.5).
    """
    try:
        actual = await git_client.fetch_default_branch(repo.repo_url, repo.git_host)
    except GitClientError:
        return
    if actual and actual != repo.default_branch:
        store.update_repository_default_branch(session, repo.id, actual)
