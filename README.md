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

Первая итерация в работе: ORM, store, git-клиент, CSV-импорт, матрица, auth — реализованы.
LLM-ядро (`coherence_analyzer.py`) заблокировано гейтом Фазы 0 (живая валидация на студентах).
