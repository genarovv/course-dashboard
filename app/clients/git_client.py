"""G1 (#4): чтение деревьев и файлов через GitLab/GitHub API (FR-3, NFR-3, NFR-4).

Клиент не знает о моделях данных (ARCHITECTURE §3.2): работает с сырыми текстами
и типизированными исключениями; маппинг на SyncOutcome — задача sync_orchestrator.
Токены — read-only, только из env (NFR-3, решение CEO 2026-07-09).
"""

import asyncio
from urllib.parse import quote, urlparse

import httpx

from app.config import settings

_RATE_LIMIT_RETRIES = 2
_RATE_LIMIT_PAUSE_SEC = 30.0


class GitClientError(Exception):
    """База: ошибка одного репозитория не валит остальные — ловится по-репозиторному."""


class GitAuthFailedError(GitClientError):
    """Токен протух/невалиден → исход auth_failed (FR-3)."""


class GitRepoUnavailableError(GitClientError):
    """Репозиторий недоступен (404/сеть) → исход repo_unavailable (NFR-2)."""


class GitRateLimitedError(GitClientError):
    """Лимит API не отпустил после пауз и повторов → исход skipped_rate_limit (NFR-4)."""


def _parse_repo(repo_url: str) -> tuple[str, str]:
    """URL → (host, 'owner/repo'). Понимает GitHub и GitLab (включая подгруппы)."""
    parsed = urlparse(repo_url)
    path = parsed.path.strip("/").removesuffix(".git")
    if not parsed.hostname or "/" not in path:
        raise GitRepoUnavailableError(f"не удалось разобрать URL репозитория: {repo_url}")
    return parsed.hostname, path


class GitClient:
    """Read-only доступ к деревьям и файлам студенческих репозиториев."""

    def __init__(self, http: httpx.AsyncClient | None = None):
        self._http = http or httpx.AsyncClient(timeout=30)

    async def get_tree(self, repo_url: str, git_host: str, ref: str = "main") -> list[str]:
        """Список путей файлов репозитория (рекурсивно)."""
        host, path = _parse_repo(repo_url)
        if git_host == "GitHub":
            data = await self._request_json(
                f"https://api.github.com/repos/{path}/git/trees/{quote(ref)}?recursive=1",
                self._github_headers(),
            )
            return [e["path"] for e in data.get("tree", []) if e.get("type") == "blob"]
        data = await self._request_json(
            f"https://{host}/api/v4/projects/{quote(path, safe='')}/repository/tree"
            f"?recursive=true&per_page=100&ref={quote(ref)}",
            self._gitlab_headers(),
        )
        return [e["path"] for e in data if e.get("type") == "blob"]

    async def get_file_content(self, repo_url: str, git_host: str, file_path: str, ref: str = "main") -> str:
        """Сырое содержимое файла."""
        host, path = _parse_repo(repo_url)
        if git_host == "GitHub":
            url = f"https://api.github.com/repos/{path}/contents/{quote(file_path)}?ref={quote(ref)}"
            headers = self._github_headers() | {"Accept": "application/vnd.github.raw+json"}
        else:
            url = (
                f"https://{host}/api/v4/projects/{quote(path, safe='')}"
                f"/repository/files/{quote(file_path, safe='')}/raw?ref={quote(ref)}"
            )
            headers = self._gitlab_headers()
        response = await self._request(url, headers)
        return response.text

    # ── внутреннее ───────────────────────────────────────────────────────

    def _github_headers(self) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json"}
        if settings.github_token:
            headers["Authorization"] = f"Bearer {settings.github_token}"
        return headers

    def _gitlab_headers(self) -> dict[str, str]:
        return {"PRIVATE-TOKEN": settings.gitlab_token} if settings.gitlab_token else {}

    async def _request(self, url: str, headers: dict[str, str]) -> httpx.Response:
        """GET с обработкой auth/rate-limit/недоступности (NFR-4: пауза и повтор)."""
        for attempt in range(_RATE_LIMIT_RETRIES + 1):
            try:
                response = await self._http.get(url, headers=headers)
            except httpx.HTTPError as exc:
                raise GitRepoUnavailableError(f"сетевая ошибка: {exc.__class__.__name__}") from exc
            if response.status_code == 401:
                raise GitAuthFailedError("401: токен невалиден или протух")
            if self._is_rate_limited(response):
                if attempt < _RATE_LIMIT_RETRIES:
                    await asyncio.sleep(self._retry_after(response))
                    continue
                raise GitRateLimitedError("лимит API не отпустил после повторов")
            if response.status_code == 403:
                raise GitAuthFailedError("403: доступ запрещён")
            if response.status_code >= 400:
                raise GitRepoUnavailableError(f"HTTP {response.status_code}")
            return response
        raise GitRateLimitedError("лимит API не отпустил после повторов")  # pragma: no cover

    async def _request_json(self, url: str, headers: dict[str, str]):
        return (await self._request(url, headers)).json()

    @staticmethod
    def _is_rate_limited(response: httpx.Response) -> bool:
        if response.status_code == 429:
            return True
        # GitHub отдаёт rate-limit как 403 с обнулённым X-RateLimit-Remaining
        return response.status_code == 403 and response.headers.get("X-RateLimit-Remaining") == "0"

    @staticmethod
    def _retry_after(response: httpx.Response) -> float:
        value = response.headers.get("Retry-After")
        try:
            return min(float(value), 120.0) if value else _RATE_LIMIT_PAUSE_SEC
        except ValueError:
            return _RATE_LIMIT_PAUSE_SEC
