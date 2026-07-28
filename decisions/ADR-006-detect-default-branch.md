# ADR-006 — Детект дефолтной ветки репозитория при импорте

- **Статус:** Accepted
- **Дата:** 2026-07-28
- **Решил:** CEO (Виталий Генаров), по итогам дебага 404 на `mtb-knowledge-hub`

## Контекст

При первом прогоне обходчика (`POST /sync`) репозиторий `https://github.com/Intese-m9/mtb-knowledge-hub` получил исход `repo_unavailable` с detail `HTTP 404`. Остальные 8 репозиториев обошлись успешно.

Расследование (3 гипотезы):
1. **H₁ (токен не имеет доступа):** `curl` на `GET /repos/Intese-m9/mtb-knowledge-hub` с токеном из `.env` → `200` — опровергнута.
2. **H₂ (ветка не `main`, а `master`):** `GET /repos/…/git/trees/main` → `404`, `GET /repos/…/git/trees/master` → `200` — **подтверждена**.
3. **H₃ (кривой парсинг URL):** `_parse_repo(...)` → `('github.com', 'Intese-m9/mtb-knowledge-hub')` — опровергнута.

**Причина:** код жёстко шьёт `default_branch="main"`:
- Модель `Repository.default_branch` (default `"main"`)
- `csv_importer.import_csv` не передаёт параметр → `register_repository` с дефолтом
- `sync_orchestrator._sync_one_repo` читает `repo.default_branch` как есть

Репозитории, созданные до перехода GitHub на `main` (октябрь 2020), и репозитории с ручным переименованием ветки используют `master`.

## Решение

Два механизма:

### 1. `GitClient.fetch_default_branch` — новый метод API-клиента

`app/clients/git_client.py`:

```python
async def fetch_default_branch(self, repo_url: str, git_host: str) -> str:
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
```

GitHub: `GET /repos/{owner}/{name}` → `default_branch`.  
GitLab: `GET /projects/{id}` → `default_branch`.

### 2. Детект при импорте (`csv_importer.py`)

Сразу после `register_repository` — `fetch_default_branch` в try/except:
- Успех → `repo.default_branch = actual_branch`
- Недоступность → fallback `"main"` (импорт не валится)

### 3. Детект при обходе (`sync_orchestrator.run_sync`)

Перед циклом репозиториев — переопределить `default_branch` для ВСЕХ активных репо (включая добавленные до фикса):
- Успех + ветка изменилась → `repo.default_branch = actual_branch`
- Недоступность → пропускаем (живём со старым значением)

## Последствия

**Плюсы:**
- Репозитории с `master` (и любым нестандартным именем ветки) корректно обходятся
- Фикс работает как для новых импортов, так и для уже добавленных репо
- Недоступность API не валит ни импорт, ни обход (NFR-2)

**Минусы:**
- Один extra GET на репозиторий при импорте и один при каждом обходе
- При недоступности API на этапе обхода используем старую ветку — репо получит `repo_unavailable` (корректная деградация)

**Не затронуты:** тесты (120 → 121, все зелёные); `ARCHITECTURE.md` (изменение в рамках существующего контракта GitClient); модель данных (новых полей не введено).
