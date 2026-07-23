# MAP — Тесты и покрытие course-dashboard

**Дата:** 2026-07-21
**Стек:** Python 3.13 · FastAPI · SQLAlchemy 2.x (Mapped) · SQLite (WAL) · Alembic · Jinja2+HTMX · bcrypt
**Тестов:** 36, все ✅ · **Покрытие общее:** 96% (`pytest-cov`)

---

## 1. Тестовый модуль → тип и что проверяет

| Тестовый файл | Тип | Что проверяет |
|---|---|---|
| `test_app_starts.py` | интеграционный (TestClient) | Скелет приложения: `/health` отвечает 200, `/login` отдаёт форму, `/` без авторизации → 303 на `/login` |
| `test_auth.py` | интеграционный (TestClient + реальная БД) | FR-0: логин/выход/блокировка — сессия создаётся, после 5 ошибок lockout 15 мин, logout чистит сессию |
| `test_csv_import.py` | интеграционный (TestClient + MockTransport) | FR-1: CSV-импорт создаёт репозитории, дубликаты (И6) отсеиваются, reimport не теряет старые, без авторизации → 401 |
| `test_git_client.py` | модульный (MockTransport, без сети) | FR-3/NFR-4: GitHub и GitLab API — деревья + файлы, 401→GitAuthFailedError, 429→пауза+ретрай, исчерпание лимита, изоляция ошибок между репо |
| `test_migrations.py` | интеграционный (alembic upgrade + raw SQL) | DDL: все 12 таблиц созданы, сид system_user, downgrade без ошибок, И1 (XOR), И3 (quad unique), И4 (one active override), И5 (append-only триггеры), И6 (norm URL unique), И8 (snapshot CHECK), И9+И11 (уникальность тройки/пары), И10 (reference uniqueness) |
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
| `app/routes/auth.py` | 41 | 2 miss | **95%** | test_auth |
| `app/routes/admin.py` | 16 | 1 miss | **94%** | test_csv_import |
| `app/routes/dashboard.py` | 9 | ✅ | **100%** | test_app_starts |
| `app/routes/health.py` | 5 | ✅ | **100%** | test_app_starts |
| `app/routes/__init__.py` | 8 | 3 miss | **62%** | все тесты через dependency override → сид сессии |

---

## 3. Модули БЕЗ тестов

| Модуль | Статус файла | Причина |
|---|---|---|
| `services/sync_orchestrator.py` | **пустой** (0 строк) | Тикет G2 #9 — не начат |
| `services/coherence_analyzer.py` | **пустой** (0 строк) | ⛔ Фаза 0 gate (PRD §13) — железное правило CLAUDE.md |
| `services/matrix_builder.py` | **пустой** (0 строк) | Тикет D1 #12 — не начат |
| `services/evidence_chain.py` | **пустой** (0 строк) | Тикет D4 #14 — не начат |
| `services/config_manager.py` | **пустой** (0 строк) | Тикет S4 #6 — не начат |
| `clients/llm_client.py` | **пустой** (0 строк) | Тикет C1 — не начат (после Фаза 0) |

---

## 4. Вопросы по непокрытым модулям

**Пустые сервисы (6 модулей):**

1. **sync_orchestrator.py** — дыра или сознательно не тестируем?
2. **coherence_analyzer.py** — ⛔ Фаза 0 gate, но: дыра или сознательно не тестируем?
3. **matrix_builder.py** — дыра или сознательно не тестируем?
4. **evidence_chain.py** — дыра или сознательно не тестируем?
5. **config_manager.py** — дыра или сознательно не тестируем?
6. **llm_client.py** — дыра или сознательно не тестируем?

**Пропуски в покрытых модулях:**

7. **git_client.py** (8 stmts miss: 403 handler, git_host валидация, default_branch fallback) — дыра или сознательно не тестируем?
8. **store.py** (5 miss: SQLite pragmas, find_last_snapshot query) — дыра или сознательно не тестируем?

---

## 5. Рекомендация: где начать test-first

**S4 — config_manager** (тикет [#6](https://github.com/genarovv/course-dashboard/issues/6)).

Почему именно он:
- **Нет gate-ограничений** — в отличие от coherence_analyzer (⛔ Фаза 0)
- **Дешёвый модуль** — YAML → Pydantic → diff с БД → append-only register_rubric + update Lesson/ArtifactDef/EdgeDef (контракт §3.5, категория 3)
- **Закрывает реальную дыру** — единственный из Stage 2, кто не написан
- **Высокая тестуемость** — стопка YAML, БД с миграциями, всё локальное, без внешних вызовов
- **Открывает путь к G2** — sync_orchestrator зависит от конфига (рубрики, артефакты, рёбра)
