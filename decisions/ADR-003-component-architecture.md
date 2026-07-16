# ADR-003 — Компонентная архитектура

- **Статус:** Superseded ARCHITECTURE.md v2+ (детали слоёв живут там; актуальная версия — v3, 2026-07-09)
- **Дата:** 2026-07-07
- **Решили:** CEO (по итогам архитектурной сессии, занятие 7)

## Контекст

Выбран стек (Python + FastAPI + SQLite — ADR-002). Требуется разбиение на модули: границы ответственности, направление зависимостей, жизненный цикл компонентов. Дополнительно закрыты два открытых вопроса модели данных:

- **С1 (deferred — не вердикт):** исключён из уникальности четвёрки И3 — `deferred` техническая запись, не блокирует настоящий вердикт.
- **Config FR-2:** эталонный конфиг — `config.yaml` в корне проекта (версионируется в git). При старте приложения — синк в БД.

Драйверы, влияющие на разбиение:

| D | Значение |
|---|----------|
| D10 | Один пользователь — не нужен multi-tenant, RBAC, аудит действий |
| D18 | Append-only — repositories не имеют `update`/`delete` |
| D33 | LLM-вызовы async, ~200/нед — не нужен Celery, хватит in-memory задач |
| D39 | Код пишут агенты — минимум слоёв, плоская структура, явные контракты |

> **Superseded by ARCHITECTURE.md v2 (2026-07-07).** Ниже — решение v1; актуальная структура — в ARCHITECTURE.md §§3–4. Ключевые отличия v2: удалены `schemas/` и `repositories/` как отдельные слои (заменены `store.py` и Pydantic по месту использования); добавлен `routes/health.py` (мониторинг); Alpine.js удалён.

## Решение (v1, уточнено v2)

### 1. Разбиение на модули (слои)

Актуальная структура проекта — `ARCHITECTURE.md §3.1`. Вкратце:

```
app/
├── main.py, config.py
├── models/               # 12 сущностей + enums (StrEnum, единое место)
├── store.py              # 1 файл вместо 7 repositories/
├── services/             # sync_orchestrator, coherence_analyzer, matrix_builder, evidence_chain, csv_importer, config_manager
├── clients/              # git_client, llm_client (с validate_llm_response)
├── routes/               # auth, dashboard, admin, health (NEW)
├── templates/, static/
└── config.yaml
```

**Изменения относительно v1:**
- `schemas/` — упразднён. Pydantic — только в `clients/llm_client.py` и по месту в services.
- `repositories/` — упразднён. Заменён единым `store.py`.
- `routes/health.py` — добавлен. In-memory мониторинг LLM-задач (TaskTracker).
- Alpine.js — удалён.

### 2. Направление зависимостей

```
routes → services → store.py → models
                ↘ clients → (Git API / DeepSeek API)
```

- **routes** вызывают services, никогда не обращаются к store напрямую
- **services** вызывают store (только register/insert + select) и clients
- **store.py** — единая точка доступа к данным. Экспортирует только `register_*`/`insert_*` и `find_*`/`select_*` — никаких update/delete (И5)
- **clients** не знают о модели данных — работают с сырыми текстами и Pydantic
- **models** — pure SQLAlchemy declarative, без бизнес-логики. Enums — в `__init__.py`

### 3. Жизненный цикл ключевых сценариев

Актуальные data flow — в `ARCHITECTURE.md §5`. Ключевые изменения имён:

#### 3.1 Плановый обход (FR-8)
```
POST /sync → sync_orchestrator.run_sync()
  → register_sync_run(status=in_progress)           # INSERT (было: SyncRun creation)
  → loop over Repository (WHERE archived_at IS NULL):
    → git_client.get_tree() + get_file_content()
    → content_hash comparison → register_snapshot() # INSERT, не upsert
    → record_sync_outcome(outcome=…)                 # INSERT, не upsert
  → complete_sync_run(status=completed|partial)
  → for each changed edge pair:
    → task_tracker.start()
    → asyncio.create_task(coherence_analyzer.ensure_verdict(…))
```

#### 3.2 Проверка связности (FR-5)
```
coherence_analyzer.ensure_verdict(edge, snapshot_a, snapshot_b)
  → find_verdict_by_quadruple(...)                    # SELECT, не check
    → if exists AND not deferred: return
  → llm_client.check_coherence(...)
    → validate_llm_response( response )               # NEW: schema check + 3 retries
    → if invalid: register_verdict(verdict=deferred, reason=parse_error)
    → if LLM ok: register_verdict(CoherenceVerdict)
  → return verdict
```

#### 3.3 Отображение матрицы (FR-4)
```
GET / → matrix_builder.build_matrix()
  → last_snapshot_per_repo_and_artifact()
  → latest_verdict_per_edge_and_repo()
  → MatrixView → render template
```

### 4. Решения по открытым вопросам

| Вопрос | Решение |
|--------|---------|
| **C1 (deferred)** | `deferred` исключён из уникальности И3: уникальный индекс `(source_content_hash, target_content_hash, rubric_id, llm_model)` с условием `WHERE verdict != 'deferred'`. `deferred`-запись не блокирует запись настоящего вердикта. |
| **Config FR-2** | Эталонный конфиг — `config.yaml` в корне проекта (версионируется в git). При старте `config_manager.sync_to_db()` читает YAML, diff-ит с БД, создаёт новые версии рубрик (append-only). Правки конфига — через git (code review), не через дашборд. |
| **Фоновые задачи** | `asyncio.create_task` — in-memory, без persist-очереди. При рестарте сервера in-flight проверки теряются; следующий обход подхватит пропущенные пары (content_hash не изменился — вердикта нет → перепроверка). |
| **Структура проекта** | `app/` — корневой пакет, без дополнительного `src/`-уровня. |
| **Типизация** | Все enum-домены (11 шт.) — `StrEnum` / Python `enum` + SQLAlchemy `TypeDecorator`. Определены в `models/__init__.py`. |

## Последствия

### Плюсы (v1, уточнено v2)
- **Плоская структура:** 3 слоя + store.py вместо 6 директорий.
- **services вызывают store напрямую:** нет прослойки из 7 файлов-репозиториев.
- **store append-only по контракту:** экспортирует только register/insert/select — update/delete физически отсутствуют.
- **CLI отсутствует:** всё управление — через дашборд.
- **Enum — одно определение:** в `models/__init__.py` (StrEnum), без дублирования в Pydantic-слое.

### Минусы / цена (v1, уточнено v2)
- **In-memory задачи + TaskTracker:** при рестарте теряются in-flight LLM-запросы. TaskTracker добавляет наблюдаемость (GET /health), но не persistence.
- **Редактирование конфига — через git:** без изменений.
- **Enum определён один раз** (было "три места" — исправлено в ARCHITECTURE v2 §3.3).

### Что станет сложнее / новый техдолг
- **v2 (FR-11):** без изменений.
- **C1 решён:** partial unique index в DDL + `deferred` получил поле `reason` (llm_unavailable | parse_error).
- **Мониторинг фоновых задач:** добавлен in-memory TaskTracker + `GET /health` (ARCHITECTURE v2 §5.4).

## Связанные
- `ADR-002-stack-python-fastapi-sqlite.md` — стек
- `data-model.md` — логическая модель (DM §1, §2), С1 закрыт
- `ARCHITECTURE.md` — сводный документ архитектуры
- `product/prd.md` §5 — FR-0…FR-11
