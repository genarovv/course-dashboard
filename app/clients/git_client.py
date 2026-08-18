"""G1 (#4): чтение деревьев и файлов через GitLab/GitHub API (FR-3, NFR-3, NFR-4).

Клиент не знает о моделях данных (ARCHITECTURE §3.2): работает с сырыми текстами
и типизированными исключениями; маппинг на SyncOutcome — задача sync_orchestrator.
Токены — read-only, только из env (NFR-3, решение CEO 2026-07-09).
"""

import asyncio
from dataclasses import dataclass
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


@dataclass(frozen=True)
class NoteInfo:
    """Нота обсуждения MR: текст + время (нужно правилу устаревания вердикта, ADR-007 сц. 5)."""

    body: str
    created_at: str  # ISO-строка хостинга


@dataclass(frozen=True)
class BranchInfo:
    """Ветка репозитория: имя и голова (D43, #69). Дата — сырая ISO-строка хостинга."""

    name: str
    head_sha: str
    committed_date: str | None


@dataclass(frozen=True)
class MrCommit:
    """Коммит в выдаче хостинга (FR-14 этап 1, #80).

    position — хронологический индекс внутри MR (0 — самый ранний): проверке
    tests-first важен именно порядок «красная фаза раньше кода». Для головы
    ветки (list_commits) порядок хостинга — свежие первыми, position следует ему.
    """

    sha: str
    message: str
    position: int


@dataclass(frozen=True)
class MrInfo:
    """Нормализованный MR/PR (FR-12, #38). Клиент не знает о моделях данных (§3.2)."""

    number: int              # iid (GitLab) / number (GitHub)
    title: str
    source_branch: str
    state: str               # opened | merged | closed
    head_sha: str
    updated_at: str          # ISO-строка хостинга
    description: str         # тело MR; None хостинга нормализуется в ""


class GitClient:
    """Read-only доступ к деревьям и файлам студенческих репозиториев."""

    def __init__(self, http: httpx.AsyncClient | None = None):
        self._http = http or httpx.AsyncClient(timeout=30)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get_tree(self, repo_url: str, git_host: str, ref: str = "main") -> list[str]:
        """Список путей файлов репозитория (рекурсивно)."""
        host, path = _parse_repo(repo_url)
        if git_host == "GitHub":
            # #73: правило #51 действует и здесь — слэши веток вида feature/T-005
            # в путевом сегменте кодируются (%2F), иначе GitHub отвечает 404
            data = await self._request_json(
                f"https://api.github.com/repos/{path}/git/trees/{quote(ref, safe='')}?recursive=1",
                self._github_headers(),
            )
            return [e["path"] for e in data.get("tree", []) if e.get("type") == "blob"]
        paths: list[str] = []
        page = "1"
        while page:
            response = await self._request(
                f"https://{host}/api/v4/projects/{quote(path, safe='')}/repository/tree"
                f"?recursive=true&per_page=100&ref={quote(ref)}&page={page}",
                self._gitlab_headers(),
            )
            paths += [e["path"] for e in response.json() if e.get("type") == "blob"]
            page = response.headers.get("x-next-page", "")
        return paths

    async def get_head_sha(self, repo_url: str, git_host: str, ref: str = "main") -> str:
        """SHA головного коммита ветки — свидетельство доказательной цепочки FR-9."""
        host, path = _parse_repo(repo_url)
        if git_host == "GitHub":
            data = await self._request_json(
                f"https://api.github.com/repos/{path}/commits/{quote(ref, safe='')}",  # #73: см. get_tree
                self._github_headers(),
            )
            return data["sha"]
        # #51: ref — путевой сегмент, слэши веток вида feat/x обязаны кодироваться (%2F),
        # иначе GitLab отвечает 404; в query-параметрах (?ref=) слэш допустим
        data = await self._request_json(
            f"https://{host}/api/v4/projects/{quote(path, safe='')}"
            f"/repository/commits/{quote(ref, safe='')}",
            self._gitlab_headers(),
        )
        return data["id"]

    async def get_head_commit_date(
        self, repo_url: str, git_host: str, ref: str = "main"
    ) -> str | None:
        """D19 (#65): дата головного коммита ветки (сырая ISO-строка хостинга).

        Отдельный вызов того же эндпоинта, что get_head_sha: контракт get_head_sha
        не меняется (его используют фейки существующих тестов) — цена одного
        лишнего запроса на репозиторий за обход. API не вернул дату — None.
        """
        host, path = _parse_repo(repo_url)
        if git_host == "GitHub":
            data = await self._request_json(
                f"https://api.github.com/repos/{path}/commits/{quote(ref, safe='')}",  # #73: см. get_tree
                self._github_headers(),
            )
            # "committer": null у коммитов без учётки GitHub — None, не AttributeError
            return ((data.get("commit") or {}).get("committer") or {}).get("date")
        data = await self._request_json(
            f"https://{host}/api/v4/projects/{quote(path, safe='')}"
            f"/repository/commits/{quote(ref, safe='')}",
            self._gitlab_headers(),
        )
        return data.get("committed_date")

    async def fetch_default_branch(self, repo_url: str, git_host: str) -> str:
        """Определить дефолтную ветку репозитория через API (ADR-006)."""
        host, path = _parse_repo(repo_url)
        if git_host == "GitHub":
            data = await self._request_json(
                f"https://api.github.com/repos/{path}",
                self._github_headers(),
            )
            return data["default_branch"]
        data = await self._request_json(
            f"https://{host}/api/v4/projects/{quote(path, safe='')}",
            self._gitlab_headers(),
        )
        return data["default_branch"]

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

    async def list_branches(self, repo_url: str, git_host: str) -> list[BranchInfo]:
        """D43 (#69): все ветки репозитория с датой головного коммита.

        Нужно, чтобы отличать «студент ничего не сделал» от «сделал не в той
        ветке»: в форках ЦУ `main` защищена, слить туда работу студент не может.
        """
        host, path = _parse_repo(repo_url)
        if git_host == "GitHub":
            data = await self._request_json(
                f"https://api.github.com/repos/{path}/branches?per_page=100",
                self._github_headers(),
            )
            # GitHub в списке веток даёт только sha — дата коммита тянется отдельно
            # там, где она нужна (кандидатов немного, см. бюджет запросов D43)
            return [
                BranchInfo(name=b["name"], head_sha=(b.get("commit") or {}).get("sha") or "",
                           committed_date=None)
                for b in data
            ]
        branches: list[BranchInfo] = []
        page = "1"
        while page:
            response = await self._request(
                f"https://{host}/api/v4/projects/{quote(path, safe='')}/repository/branches"
                f"?per_page=100&page={page}",
                self._gitlab_headers(),
            )
            branches += [
                BranchInfo(
                    name=b["name"],
                    head_sha=(b.get("commit") or {}).get("id") or "",
                    committed_date=(b.get("commit") or {}).get("committed_date"),
                )
                for b in response.json()
            ]
            page = response.headers.get("x-next-page", "")
        return branches

    async def list_merge_requests(self, repo_url: str, git_host: str) -> list[MrInfo]:
        """FR-12 (#38): все MR/PR репозитория (открытые, смерженные, закрытые)."""
        host, path = _parse_repo(repo_url)
        if git_host == "GitHub":
            data = await self._request_json(
                f"https://api.github.com/repos/{path}/pulls?state=all&per_page=100",
                self._github_headers(),
            )
            return [
                MrInfo(
                    number=pr["number"],
                    title=pr.get("title") or "",
                    source_branch=pr.get("head", {}).get("ref") or "",
                    state="merged" if pr.get("merged_at")
                          else ("opened" if pr.get("state") == "open" else "closed"),
                    head_sha=pr.get("head", {}).get("sha") or "",
                    updated_at=pr.get("updated_at") or "",
                    description=pr.get("body") or "",
                )
                for pr in data
            ]
        mrs: list[MrInfo] = []
        page = "1"
        while page:
            response = await self._request(
                f"https://{host}/api/v4/projects/{quote(path, safe='')}/merge_requests"
                f"?state=all&per_page=100&page={page}",
                self._gitlab_headers(),
            )
            mrs += [
                MrInfo(
                    number=mr["iid"],
                    title=mr.get("title") or "",
                    source_branch=mr.get("source_branch") or "",
                    state=mr.get("state") or "closed",
                    head_sha=mr.get("sha") or "",
                    updated_at=mr.get("updated_at") or "",
                    description=mr.get("description") or "",
                )
                for mr in response.json()
            ]
            page = response.headers.get("x-next-page", "")
        return mrs

    async def list_mr_commits(self, repo_url: str, git_host: str, mr_number: int) -> list[MrCommit]:
        """FR-14 (#80): коммиты MR в хронологическом порядке (проверка tests-first).

        GitHub отдаёт хронологию как есть; GitLab — новейший первым, поэтому
        разворачиваем: position обязан значить одно и то же на обоих хостах.
        """
        host, path = _parse_repo(repo_url)
        if git_host == "GitHub":
            data = await self._request_json(
                f"https://api.github.com/repos/{path}/pulls/{mr_number}/commits?per_page=100",
                self._github_headers(),
            )
            raw = [((c.get("commit") or {}).get("message") or "", c.get("sha") or "") for c in data]
        else:
            data = await self._request_json(
                f"https://{host}/api/v4/projects/{quote(path, safe='')}"
                f"/merge_requests/{mr_number}/commits?per_page=100",
                self._gitlab_headers(),
            )
            raw = [
                (c.get("message") or c.get("title") or "", c.get("id") or "")
                for c in reversed(data)
            ]
        return [MrCommit(sha=sha, message=message, position=i) for i, (message, sha) in enumerate(raw)]

    async def list_commits(
        self, repo_url: str, git_host: str, ref: str = "main", limit: int = 100
    ) -> list[MrCommit]:
        """FR-14 (#80): коммиты головы ветки (доля сообщений с ID тикета).

        ref уходит в query-параметр — слэш там легален, кодирование путевого
        сегмента (правило #73) не требуется. Порядок хостинга: свежие первыми.
        """
        host, path = _parse_repo(repo_url)
        if git_host == "GitHub":
            data = await self._request_json(
                f"https://api.github.com/repos/{path}/commits"
                f"?sha={quote(ref)}&per_page={limit}",
                self._github_headers(),
            )
            raw = [((c.get("commit") or {}).get("message") or "", c.get("sha") or "") for c in data]
        else:
            data = await self._request_json(
                f"https://{host}/api/v4/projects/{quote(path, safe='')}/repository/commits"
                f"?ref_name={quote(ref)}&per_page={limit}",
                self._gitlab_headers(),
            )
            raw = [(c.get("message") or c.get("title") or "", c.get("id") or "") for c in data]
        return [MrCommit(sha=sha, message=message, position=i) for i, (message, sha) in enumerate(raw)]

    async def list_mr_changes(self, repo_url: str, git_host: str, mr_number: int) -> list[str]:
        """FR-14 (#80): пути изменённых файлов MR (проверка «код и доки одним MR»).

        GitLab — эндпоинт diffs (API v4); `changes` устарел и не используется.
        """
        host, path = _parse_repo(repo_url)
        if git_host == "GitHub":
            data = await self._request_json(
                f"https://api.github.com/repos/{path}/pulls/{mr_number}/files?per_page=100",
                self._github_headers(),
            )
            return [f.get("filename") or "" for f in data]
        data = await self._request_json(
            f"https://{host}/api/v4/projects/{quote(path, safe='')}"
            f"/merge_requests/{mr_number}/diffs?per_page=100",
            self._gitlab_headers(),
        )
        return [d.get("new_path") or d.get("old_path") or "" for d in data]

    async def list_mr_notes(self, repo_url: str, git_host: str, number: int) -> list[NoteInfo]:
        """FR-12 (#38): обсуждение MR/PR — тексты с временем (вердикт ревьюера + устаревание)."""
        host, path = _parse_repo(repo_url)
        if git_host == "GitHub":
            data = await self._request_json(
                f"https://api.github.com/repos/{path}/issues/{number}/comments?per_page=100",
                self._github_headers(),
            )
        else:
            data = await self._request_json(
                f"https://{host}/api/v4/projects/{quote(path, safe='')}"
                f"/merge_requests/{number}/notes?per_page=100",
                self._gitlab_headers(),
            )
        return [
            NoteInfo(body=note.get("body") or "", created_at=note.get("created_at") or "")
            for note in data
        ]

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
