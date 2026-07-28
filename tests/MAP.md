# MAP — Тесты и покрытие course-dashboard

**Дата:** 2026-07-28 (обновлено в MR тикетов #31–#33, #18)
**Стек:** Python 3.13 · FastAPI · SQLAlchemy 2.x (Mapped) · SQLite (WAL) · Alembic · Jinja2+HTMX · bcrypt
**Тестов:** 147, все ✅ · **Покрытие общее:** 98% (`pytest-cov`)

---

## 1. Тестовый модуль → тип и что проверяет

| Тестовый файл | Тип | Что проверяет |
|---|---|---|
| `test_app_starts.py` | интеграционный (TestClient) | Скелет приложения: `/health` отвечает 200, `/login` отдаёт форму, `/` без авторизации → 303 на `/login` |
| `test_auth.py` | интеграционный (TestClient + реальная БД) | FR-0: логин/выход/блокировка — сессия создаётся, после 5 ошибок lockout 15 мин, logout чистит сессию |
| `test_config_manager.py` | модульный + интеграционный (session fixture, TestClient) | FR-2 (S4 #6, ADR-005): создание Lesson/ArtifactDef/EdgeDef/Rubric из YAML, идемпотентность reload, repoint рубрики со старыми вердиктами нетронутыми, флаг golden set, ограничитель «config_* вызывает только config_manager», роут /admin/reload-config, чтение конфига на старте |
| `test_csv_import.py` | интеграционный (TestClient + MockTransport) | FR-1: CSV-импорт создаёт репозитории, дубликаты (И6) отсеиваются, reimport не теряет старые, без авторизации → 401 |
| `test_health.py` | интеграционный (TestClient + реальная БД) | FR-8 (I2 #13): /health без аутентификации — время последнего обхода, пары без вердикта, deferred по причинам, нули на пустой БД |
| `test_evidence_chain.py` | модульный (session fixture) + интеграционный (TestClient) | FR-9 (D4 #14): хронология по observed_at (force-push), рёбра done/pending/no_data, вердикт+уверенность+≤5 точек, override-флаг, GET /students/{id} (200/303/404) |
| `test_git_client.py` | модульный (MockTransport, без сети) | FR-3/NFR-4: GitHub и GitLab API — деревья + файлы + head SHA (FR-9), 401→GitAuthFailedError, 429→пауза+ретрай, исчерпание лимита, изоляция ошибок между репо |
| `test_matrix_builder.py` | модульный (session fixture + alembic) | FR-4 (D1 #12): матрица «репо × занятие» — статусы ячеек, partial_reason, последний снапшот побеждает, «актуально на», пустая БД |
| `test_dashboard_matrix.py` | интеграционный (TestClient + реальная БД) | FR-4 (D1 #12): GET / рендерит матрицу — строка репозитория, колонка занятия, partial_reason, «Актуально на», редирект без сессии |
| `test_migrations.py` | интеграционный (alembic upgrade + raw SQL) | DDL: все 12 таблиц созданы, сид system_user, downgrade без ошибок, И1 (XOR), И3 (quad unique), И4 (one active override), И5 (append-only триггеры), И6 (norm URL unique), И8 (snapshot CHECK), И9+И11 (уникальность тройки/пары), И10 (reference uniqueness) |
| `test_sync_orchestrator.py` | модульный (FakeGitClient) + интеграционный (TestClient) | FR-8/FR-4 (G2 #9): классификация found/not_found, sha256 (в т.ч. мульти-совпадение паттерна и `**`-глоб), source_commit_sha (FR-9), инкрементальность D28, исходы всех 5 видов + detail, статусы SyncRun, архивные репо пропущены, POST /sync (сессия / X-Sync-Token / 401) |
| `test_override_ui.py` | интеграционный (TestClient + реальная БД) | FR-10 (O2 #16): toggle создаёт/снимает Override (revoked_at, история строк), auth, 404, кнопка «ложный разрыв» в матрице и карточке, гашение подсветки, новая четвёрка не наследует отметку |
| `test_reconcile.py` | модульный (session fixture + фейк-воркер) | FR-5/FR-8 (G4 #11): идентификация пар без валидного вердикта, create_task через инжектированный воркер (ядро FR-5 — за гейтом Фазы 0), D25 «не мигаем», deferred-ретрай, идемпотентность свода, свод в конце run_sync |
| `test_store.py` | модульный (session fixture) | Контракт store.py: ровно 4 `update_*`, нет `delete_*`, все `register_*` на месте, `normalize_url()`, CRUD-флоу репозиториев/runs/credentials/overrides, `find_verdict_by_quadruple` |

---

## 2. Модуль приложения → покрытие

| Модуль | Stmts | Покрыт | % | Чем покрыт |
|---|---|---|---|---|
| `app/main.py` | 15 | ✅ | **100%** | test_app_starts, test_auth, test_csv_import |
| `app/config.py` | 14 | ✅ | **100%** | транзитивно через все тесты |
| `app/store.py` | 88 | 5 miss | **94%** | test_store (contract + CRUD), test_auth (lockout), test_csv_import (register_repository) |
| `app/models/__init__.py` | 67 | ✅ | **100%** | test_migrations (DDL + enums + TypeDecorator) |
| `app/models/*.py` (11 файлов) | 154 | ✅ | **100%** | test_migrations, test_store |
| `app/clients/git_client.py` | 81 | 8 miss | **90%** | test_git_client (MockTransport) |
| `app/clients/llm_client.py` | 0 | — | **пустой** | — (заглушка, Фаза 0 gate) |
| `app/services/csv_importer.py` | 38 | 1 miss | **97%** | test_csv_import |
| `app/services/config_manager.py` | 75 | ✅ | **100%** | test_config_manager |
| `app/services/sync_orchestrator.py` | 161 | 2 miss | **99%** | test_sync_orchestrator (G2/G3), test_reconcile (G4) |
| `app/routes/auth.py` | 41 | 2 miss | **95%** | test_auth |
| `app/routes/admin.py` | 34 | 1 miss | **97%** | test_csv_import, test_config_manager, test_sync_orchestrator |
| `app/services/matrix_builder.py` | 33 | 1 miss | **97%** | test_matrix_builder, test_dashboard_matrix, test_override_ui (breaks) |
| `app/services/evidence_chain.py` | 31 | ✅ | **100%** | test_evidence_chain, test_override_ui |
| `app/routes/dashboard.py` | 33 | ✅ | **100%** | test_app_starts, test_dashboard_matrix, test_evidence_chain, test_override_ui |
| `app/routes/health.py` | 15 | ✅ | **100%** | test_health |
| `app/routes/__init__.py` | 8 | 3 miss | **62%** | все тесты через dependency override → сид сессии |

---

## 3. Модули БЕЗ тестов

| Модуль | Статус файла | Причина |
|---|---|---|
| `services/coherence_analyzer.py` | **пустой** (0 строк) | ⛔ Фаза 0 gate (PRD §13) — железное правило CLAUDE.md |
| `clients/llm_client.py` | **пустой** (0 строк) | Тикет C1 — не начат (после Фаза 0) |

---

## 4. Вопросы по непокрытым модулям

**Пустые сервисы (6 модулей):**

1. **coherence_analyzer.py** — ⛔ Фаза 0 gate, но: дыра или сознательно не тестируем?
2. **llm_client.py** — дыра или сознательно не тестируем?

**Пропуски в покрытых модулях:**

7. **git_client.py** (8 stmts miss: 403 handler, git_host валидация, default_branch fallback) — дыра или сознательно не тестируем?
8. **store.py** (5 miss: SQLite pragmas, find_last_snapshot query) — дыра или сознательно не тестируем?

---

## 5. Рекомендация: где продолжать test-first

**G2 — sync_orchestrator** (тикет [#9](https://github.com/genarovv/course-dashboard/issues/9)): конфиг готов (S4), git_client готов (G1) — обход можно тестировать на фейковом клиенте без сети; за ним стеком G3 (детект заготовок) и G4 (свод-реконсиляция).
