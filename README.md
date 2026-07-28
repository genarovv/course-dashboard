# Course Dashboard

Дашборд преподавателя курса «Управление агентной разработкой»: автоматически читает `.md`-артефакты из 25 публичных репозиториев студентов, строит матрицу «кто что сдал» и LLM-агентом подсвечивает разрывы связности между артефактами. Проект строится live на занятиях курса как эталон агентной разработки.

## Стек

| Слой | Технология |
|---|---|
| Backend | Python ≥ 3.12, FastAPI, Uvicorn |
| ORM / миграции | SQLAlchemy 2.0 (Mapped), Alembic |
| БД | SQLite (WAL) |
| Шаблоны | Jinja2 + HTMX |
| Auth | bcrypt + signed cookies (single-user) |
| LLM | DeepSeek API (`deepseek-v4-flash`) |
| HTTP-клиент | httpx (async) |
| Тесты | pytest |
| Линтер | ruff |

## Запуск с нуля

```bash
# 1. Клонирование
git clone <url> && cd course-dashboard

# 2. Виртуальное окружение
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 3. Зависимости
pip install -e ".[dev]"

# 4. Миграции (создаст course_dashboard.db + seed-админа)
alembic upgrade head

# 5. Сервер (port 8000)
uvicorn app.main:app --reload

# 6. Тесты
pytest

# 7. Линтер
ruff check .
```

> **Минимум для запуска:** переменные `CD_SECRET_KEY` и `CD_ADMIN_PASSWORD` заданы в окружении. Остальное имеет дефолты (см. `app/config.py`).

## Структура каталогов

```
course-dashboard/
├── app/                  # Код приложения (FastAPI)
│   ├── main.py           #   App-фабрика, роутеры, middleware
│   ├── config.py         #   Настройки из env (pydantic-settings)
│   ├── config.yaml       #   Конфигурация курса (уроки, артефакты, связи)
│   ├── store.py          #   Единая точка доступа к данным
│   ├── models/           #   SQLAlchemy ORM (12 сущностей)
│   ├── services/         #   Бизнес-логика (matrix_builder, csv_importer, …)
│   ├── clients/          #   Внешние API (git_client, llm_client)
│   ├── routes/           #   HTTP-эндпоинты (auth, dashboard, admin, health)
│   ├── templates/        #   Jinja2-шаблоны
│   └── static/           #   CSS
├── alembic/              # Миграции БД
├── tests/                # pytest-тесты (7 модулей)
├── evals/                # Golden-set для регрессии LLM-ядра
├── product/              # PRD, user stories, интервью, персоны
├── decisions/            # ADR (архитектурные решения)
├── reviews/              # Состязательные ревью
├── roles/                # Описания ролей агентной команды
├── plans/                # План разработки (16 задач)
├── memory/               # MEMORY.md — трекер состояния проекта
└── archive/              # Архивные версии документов
```

## Документация

| Документ | Ссылка | Описание |
|---|---|---|
| PRD | [`product/prd.md`](product/prd.md) | Канонический PRD v2.4 со словарём терминов |
| Архитектура | [`ARCHITECTURE.md`](ARCHITECTURE.md) | Стек, компоненты, инварианты, потоки данных |
| Модель данных | [`data-model.md`](data-model.md) | 12 сущностей, ER-диаграмма, DDL-инварианты |
| ADR | [`decisions/`](decisions/) | Ключевые архитектурные решения |
| Навигация | [`INDEX.md`](INDEX.md) | Карта контекста проекта (начинать здесь) |
| Конституция | [`CLAUDE.md`](CLAUDE.md) | Правила разработки, команды, железные правила |

## Статус

Первая итерация кода в работе (занятия 9–13): модели + миграции, store, аутентификация, CSV-импорт, git_client, config_manager (FR-2), обход репозиториев с детектом заготовок (FR-8/FR-4/BR-3), свод-реконсиляция LLM-пар (без ядра), матрица (FR-4), карточка студента (FR-9), Override UI (FR-10), /health. Ядро FR-5 (`coherence_analyzer`) — за гейтом Фазы 0 (PRD §13): свод идентифицирует пары, воркер вердиктов подключается после мини-эвала ADR-004. Трекер: [GitHub Issues](https://github.com/genarovv/course-dashboard/issues), карта тестов: [tests/MAP.md](tests/MAP.md).

## Деплой (VPS, ARCHITECTURE §5.5)

Обход по расписанию запускает системный cron (носитель FR-8 — ОС, не приложение):

```cron
# обход 2 раза в сутки; SYNC_TOKEN = значение CD_SYNC_TOKEN из окружения приложения
0 7,19 * * * curl -s -X POST http://localhost:8000/sync -H "X-Sync-Token: $SYNC_TOKEN"
```

Чек-лист деплоя: env-переменные (`CD_ADMIN_PASSWORD` — до миграции, сид админа читает её; `CD_SECRET_KEY`; `CD_SYNC_TOKEN`; `CD_GITHUB_TOKEN`/`CD_GITLAB_TOKEN` — read-only, NFR-3) → `alembic upgrade head` → `uvicorn app.main:app` → загрузить реестр репозиториев (файл версионируется в репо; импорт идемпотентен — дубликаты отсеиваются по нормализованному URL):

```bash
curl -X POST http://localhost:8000/import-csv --data-binary @data/student-repos.csv -b cookies.txt
```

→ `POST /sync` (первый обход) → **проверить `crontab -l`** → `GET /health` показывает время последнего обхода (если cron сломан — отметка «актуально на» устаревает видимо, §5.5).

> Все интервью в основе требований — синтетические (ИИ играл персону); требования — приоритизированные гипотезы. Это учебная честность курса, зафиксированная в PRD §0.
