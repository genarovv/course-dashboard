# ARCHITECTURE — Course Dashboard (v3, упрощённая по ревью занятия 7)

> **Статус:** принято. Supersedes v2 (2026-07-07) и предыдущую ARCHITECTURE.md.
> **Дата:** 2026-07-09
> **Основание v3:** ревью архитектуры (`reviews/08-ревью-архитектуры-занятие-7.md`, 14 находок) → митинг упрощения (`../digital-twin/staging/митинг-упрощение-архитектуры-2026-07-09.md`) → 5 решений CEO 2026-07-09.
> **Ключевые изменения относительно v2 (линза упрощения — вычёркивание, не добавление):**
> - Вычеркнуты 9 механизмов: TaskTracker + сущность-очередь; кеш-колонки Repository; append-only-триггеры на всех таблицах (остались на 2 доказательных); рудимент `processing=0`/SKIP LOCKED; И10-CHECK с подзапросом; `encrypted_token` + крипто в БД; APScheduler; 3 ретрая с эскалацией промпта; `step_quality_card.py` в v1-коде. К каждому — запись «почему не строим» по месту.
> - LLM-проверки: дельта-принцип «только changed» заменён идемпотентным сводом-реконсиляцией (§5.1)
> - Носитель FR-8 — системный cron/systemd timer (§5.5)
> - Раздел «журнал vs состояние» — честный контракт store.py: `register_*` + 4 узких `update_*` (§3.5)
> - Градация инвариантов DDL/код (§4)
> - Модель ядра: `deepseek-v4-flash` — ADR-004 (D38), цена по расчёту
>
> Изменения v2 (слои 6→3, удаление Alpine.js, `register_*` вместо `upsert`, закрытие C1–C4) — унаследованы.
> **Правка 2026-07-16 (S4):** контракт store.py §3.5 расширен третьей категорией «конфиг-реконсиляция из config.yaml» — решение CEO по развилке S4 (ADR-005, протокол `decisions/meetings/2026-07-16-S4-rubric-repoint.md`); устранён рассинхрон с data-model §3.2.
> Драйверы D1–D39 — вход для выбора стека и архитектуры. Источники: `product/prd.md` (v2.4), `data-model.md` (итоговый, 2026-07-07, правки 2026-07-09).

## 1. Архитектурные драйверы (D1–D39)

Перенесены из v1/v2; в v3 уточнены D23, D35, D38.

### 1.1 Масштаб и объёмы данных

| # | Драйвер | Значение | Источник |
|---|---------|----------|----------|
| D1 | Студентов в потоке (v1) | 25 | PRD §0 |
| D2 | Потоков (v1) | 1 | PRD §3 |
| D3 | Артефактов на студента | ~18 | PRD §0 |
| D4 | Всего документов под наблюдением | ~450 (25×18) | PRD §1 |
| D5 | Репозиториев для обхода | 25 | PRD §0 |
| D6 | Хостингов git | 2 (GitLab, GitHub) | PRD §0; DM §1.2 |
| D7 | Ролей артефакта | 9 (enum) | DM §1.4 |
| D8 | Рёбер связности (макс.) | 8 (полный конвейер курса) | PRD §12; DM §1.5 |
| D9 | Проверок LLM/неделя | ~200 пар | PRD §11 |
| D10 | Преподавателей (пользователей v1) | 1 | PRD §3; DM §1.1 |
| D11 | Занятий в курсе | 20 | overview.md §3 |
| D12 | Типов рубрик | 2 (edge, step) | DM §1.6 |
| D13 | Статусов снапшота | 3 (found, partial, not_found) | DM §1.9 |
| D14 | Исходов проверки репозитория | 5 (ok_changed, ok_unchanged, repo_unavailable, auth_failed, skipped_rate_limit) | DM §2 |
| D15 | Значений вердикта | 3 (ok, break, deferred) | DM §1.10 |
| D16 | Уровней уверенности | 3 (high, medium, low) | DM §1.10 |
| D17 | Длина четвёрки идентичности вердикта | 4 поля: source_content_hash + target_content_hash + rubric_id + llm_model | DM §1.10, Б1 |
| D18 | Модель хранения | append-only история (сущности не удаляются и не правятся) | DM §3.1 |
| D19 | Периметр персональных данных | repo_url — не ПДн (решение CEO); именного реестра в системе нет | DM §1 (рамка CEO), PRD §7 |
| D20 | Рост к v2+ | до 75 студентов (2–3 потока) | PRD §1 |

### 1.2 Критичные нефункциональные требования

| # | Драйвер | Описание | Источник |
|---|---------|----------|----------|
| D21 | Время обхода | Полный обход 25 репозиториев ≤2 мин; LLM-проверки асинхронны, вне этого тайминга | PRD §5 NFR-1 |
| D22 | Устойчивость к сбоям | Недоступный репозиторий не валит обход; итог — partial | PRD §5 NFR-2; DM §1.13 |
| D23 | Безопасность токена | Только чтение; секрет не в БД, не в коде, не в логах — env-var / файл chmod 600 (интерпретация NFR-3, решение CEO 2026-07-09; см. §8) | PRD §5 NFR-3; DM §1.7 |
| D24 | Rate-limit Git API | Паузы, повторы, деградация до partial; честное «не проверялось» | PRD §5 NFR-4; DM §1.8 |
| D25 | Не-мигание вердикта | Вердикт привязан к четвёрке (2 хеша + рубрика + модель); пересчёт только при её изменении | PRD §5 FR-5, §11; DM §1.10, И3 |
| D26 | LLM-недоступность | deferred-статус не валит дашборд; матрица работает | PRD §5 FR-5; DM §1.10 |
| D27 | Force-push неуязвимость | Хронология по observed_at, а не git-истории; source_commit_sha — свидетельство | PRD §5 FR-9; DM §3.1 |
| D28 | Инкрементальность | Перечитываются только изменения (content_hash); факт «проверено, без изменений» хранится | PRD §5 FR-8; DM §1.9, §1.13 |
| D29 | 152-ФЗ периметр | Именной реестр не уходит в LLM; максимум данных у преподавателя | PRD §7 R-PD3; DM §1 |
| D30 | 152-ФЗ хранение | Именные данные — 30 дней после защиты; обезличенная статистика — дольше | PRD §7 R-PD4 |

### 1.3 Интеграции с внешним миром

| # | Система | Операция | Механизм | Привязка к требованиям |
|---|---------|----------|----------|------------------------|
| D31 | GitLab API | Чтение файлов артефактов, дерево репозитория | HTTPS, read-only токен | FR-3, NFR-3, NFR-4; DM §1.7 |
| D32 | GitHub API | Чтение файлов артефактов, дерево репозитория | HTTPS, read-only токен | FR-3, NFR-3, NFR-4; DM §1.7 |
| D33 | DeepSeek API | Проверка связности (FR-5) + качество шагов (FR-11, v2) | HTTP API, асинхронно; ~200 запросов/нед | FR-5, FR-11, NFR-1; DM §1.10 |
| D34 | CSV (Яндекс.Форма) | Импорт списка repo_url студентов | CSV-файл, ручная загрузка | FR-1, BR-6; вопрос №7 |
| D35 | Репозиторий-шаблон | Эталон для детекта заготовок (FR-4 «частично») | Сравнение `content_hash` файла студента с хешами файлов шаблона — **шаг классификации в `sync_orchestrator`** (`status=partial`, `partial_reason=template_copy`); хеши шаблона тянутся раз за обход. Почему не строим отдельный компонент: это одно сравнение хешей внутри уже существующего шага классификации снапшота. | FR-4, BR-3; DM §1.4 |

### 1.4 Ограничения

| # | Драйвер | Значение | Комментарий |
|---|---------|----------|-------------|
| D36 | Бюджет на всё | ~$20/мес | VPS + LLM API + домен/SSL |
| D37 | Хостинг | Дешёвый VPS в РФ (1 vCPU, 1–2 GB RAM) | Timeweb, Beget, FirstVDS, Selectel — под 152-ФЗ |
| D38 | LLM-провайдер и модель | DeepSeek API, модель **`deepseek-v4-flash`** (ADR-004) | ≈**$1.3–1.5/мес** по расчёту: ~900 пар/мес × (~10K токенов вход + ~1K выход); V4-Flash $0.14/M вход (cache-miss), $0.28/M выход, cache-hit $0.0028/M — закреплённая рубрика кешируется. Тариф: <https://api-docs.deepseek.com/quick_start/pricing/>. Имена `deepseek-chat`/`deepseek-reasoner` устаревают 2026-07-24 — не использовать. `deepseek-v4-pro` — fallback после пилота Фазы 0 (~$19/мес по стандартному тарифу — риск D36). Из РФ доступен без прокси |
| D39 | Разработка | Весь код пишут агенты | Обязателен spec-first подход; минимум конфигов; типизированный код |

**Баланс бюджета:** VPS ~$5–8 + DeepSeek API ~$1.3–1.5 (расчёт в D38) + домен/SSL ~$1–2 = **$7.5–11.5/мес** — запас до $20 (D36) двукратный.

---

## 2. Стек (упрощённый)

| Слой | Выбор | Обоснование |
|------|-------|-------------|
| Язык | Python 3.12+ | D39: максимальная предсказуемость для LLM-агента |
| Backend | FastAPI | Async из коробки (D33); Pydantic — валидация enum-доменов |
| СУБД | SQLite (WAL-mode) | D37: нулевой оверхед, нет отдельного процесса, constraints покрывают DDL-часть И1–И11 (§4) |
| ORM | SQLAlchemy 2.x (Mapped) | D18: журнальные сущности — insert + select; мутации — только по трёхчастному контракту §3.5 (4 узких `update_*` состояния + конфиг-реконсиляция для config_manager). Unit-of-work и change-tracking не используются — легковесный mapper: Mapped + session.add() / session.execute(select()) |
| Миграции | Alembic | Единственный стандарт для SQLAlchemy |
| Фронтенд | Jinja2 + HTMX | D37: серверный рендеринг, не нужен отдельный API; HTMX обновляет матрицу без JS-фреймворка |
| Аутентификация | bcrypt + sessions | D10: один пользователь — готовое решение тяжелее задачи |
| Фоновые задачи | `asyncio.create_task` + идемпотентный свод-реконсиляция (§5.1) | D9: ~200 запросов/нед — брокер, очередь и in-memory tracker избыточны |
| Планировщик | Системный cron / systemd timer (§5.5) | 2 запуска/сутки, 1 пользователь — планировщик даёт ОС |

**Отклонённые варианты (сокращённо):**
- **PostgreSQL** — не влезает в 512MB (D37).
- **Django** — синхронен, потребовал бы Celery + Redis для async (D33).
- **Next.js** — билд требует 2GB RAM (D37).
- **Alpine.js** (был в v1) — HTMX покрывает все сценарии дашборда (матрица, фильтры, подгрузка карточки); Alpine.js не имеет драйвера в D1–D39.
- **repositories/ слой** (был в v1) — для журнала (insert + select) каждый файл — boilerplate 20 строк; заменён прямой работой services с models через единый `store.py`.
- **TaskTracker (in-memory) + сущность-очередь** (были в v2) — почему не строим: всё состояние LLM-задач выводимо запросом к БД (свод §5.1); in-memory копия теряется при рестарте и требует отдельной синхронизации с реальностью.
- **APScheduler / встроенный планировщик** (обсуждался) — почему не строим: 2 запуска/сутки на 1 пользователя даёт cron; встроенный шедулер — лишний компонент и лишний фейл-мод внутри процесса (§5.5).

**SQLite-риски (осознанно):**

| Риск | Снижение |
|------|----------|
| SQLITE_BUSY при конкурентной записи | Единый writer: обход репозиториев последователен; LLM-задачи ставятся сводом последовательно |
| Нет enum-типов | StrEnum в Python + TypeDecorator в SQLAlchemy = одно определение enum, два converter-метода |
| Нет partial уникальности до 3.30.0 | Проверить версию SQLite на хостинге; fallback — триггер |

> Рудимент `UPDATE ... WHERE processing=0` / `FOR UPDATE SKIP LOCKED` (был в v2) вычеркнут: почему не строим — очереди задач в БД нет (находка 3 ревью, свод §5.1), защищаться от двойной обработки несуществующей очереди не от чего.

---

## 3. Компонентная архитектура (3 слоя)

### 3.1 Структура проекта

```
app/
├── main.py                  # App factory, lifespan, middleware
├── config.py                # Pydantic-settings (пути, ключи из env, DeepSeek endpoint)
│
├── models/                  # SQLAlchemy ORM (11 сущностей в v1-коде, DM §1)
│   ├── __init__.py          #   enums (StrEnum) + TypeDecorator — единственное место определения доменов
│   ├── system_user.py       #   SystemUser — FR-0
│   ├── repository.py        #   Repository — FR-1, FR-6; содержит archived_at (C2); БЕЗ кеш-колонок (§3.5)
│   ├── lesson.py            #   Lesson — FR-2
│   ├── artifact_def.py      #   ArtifactDef — FR-2
│   ├── edge_def.py          #   EdgeDef — FR-5
│   ├── rubric.py            #   Rubric — FR-2, версионирована append-only
│   ├── git_credential.py    #   GitCredential — FR-3; git_host + is_valid + checked_at, токен в env (§8)
│   ├── sync_run.py          #   SyncRun — FR-8
│   ├── sync_run_repository.py  # SyncRunRepository — FR-6/7/8
│   ├── artifact_snapshot.py #   ArtifactSnapshot — FR-4; partial_reason — JSON-массив (C3)
│   ├── coherence_verdict.py #   CoherenceVerdict — FR-5
│   └── override.py          #   Override — FR-10
│
├── store.py                 # Data access: register_* (журнал) + 4 узких update_* + конфиг-реконсиляция (§3.5) + select
│
├── services/
│   ├── sync_orchestrator.py #   Цикл обхода FR-8 + свод-реконсиляция LLM-пар (§5.1) + детект заготовки (D35)
│   ├── coherence_analyzer.py#   Ядро FR-5: LLM-агент по рубрикам. ⛔ FR-5 core — НЕ кодить до прохождения Фазы 0 (PRD §13, железное правило CLAUDE.md)
│   ├── matrix_builder.py    #   Проекция матрицы FR-4/6/7
│   ├── evidence_chain.py    #   Хронология FR-9
│   ├── csv_importer.py      #   Импорт CSV → Repository (FR-1)
│   └── config_manager.py    #   config.yaml → синк в БД (FR-2)
│
├── clients/
│   ├── git_client.py        #   GitLab + GitHub API: read-only, rate-limit, retry
│   └── llm_client.py        #   DeepSeek API (deepseek-v4-flash): async + validate_llm_response (§5.2)
│
├── routes/                  # FastAPI HTML-роуты (Jinja2)
│   ├── auth.py              #   GET/POST /login, GET /logout (FR-0)
│   ├── dashboard.py         #   GET / — матрица, слепая зона, хроники, карточка студента
│   ├── health.py            #   GET /health — счётчики из БД (вычислимый запрос, без in-memory состояния)
│   └── admin.py             #   POST /sync, POST /import-csv, POST /credential (FR-1,3,8)
│
├── templates/               # Jinja2
│   ├── base.html
│   ├── login.html
│   ├── dashboard/matrix.html, blind_spots.html, chronics.html, student_card.html
│   └── admin/sync.html, csv_import.html, credentials.html
│
├── static/
│   └── css/app.css
│
└── config.yaml              # Эталон: уроки, артефакты, рёбра, рубрики (FR-2)
```

**Пометки v3:**
- `step_quality_card.py` в v1-коде **не создаётся** (решение CEO 2026-07-09): FR-11 за-гейчен v2, файл модели без потребителя — мёртвый код. Сущность остаётся в `data-model.md` (§1.11) как будущее v2 — почему не строим сейчас: scaffolding без реализации FR-11 никого не обслуживает, а миграция добавит таблицу за один шаг, когда v2 откроется.
- `coherence_analyzer.py` стоит в структуре как место ядра, но **не кодится до прохождения Фазы 0** (PRD §13).

**Что изменилось относительно v1 (история v2):**

| Было | Стало | Причина |
|------|-------|---------|
| `schemas/` (4 файла) | Pydantic-модели в `clients/llm_client.py`, `services/matrix_builder.py`, `services/config_manager.py` по месту использования | Слой из 4 файлов не оправдан для 3 мест использования |
| `repositories/` (7 файлов) | `store.py` (1 файл, ~150 строк) | 7 файлов с insert/select — boilerplate; журнальная семантика не требует DDD-слоя |
| Alpine.js | удалён | D10: один пользователь; HTMX достаточно |
| `archived_at` в Repository | добавлен | Закрывает C2 |
| `partial_reason` (одно поле) | JSON-массив | Закрывает C3 |

### 3.2 Направление зависимостей

```
routes → services → store.py → models
                ↘ clients (Git API / DeepSeek API)
```

- **routes** вызывают services, возвращают Jinja2-шаблоны. Никакой бизнес-логики.
- **services** вызывают `store.py` и clients. Могут вызывать другие services.
- **store.py** — единая точка доступа к данным. Экспортирует `register_*` / `find_*`, ровно 4 узких `update_*` и функции конфиг-реконсиляции (вызывает только config_manager) — §3.5. Для журнальных сущностей update/delete физически не экспортируются. Внутри — прямые SQLAlchemy-запросы.
- **clients** не знают о модели данных — работают с сырыми текстами и Pydantic-схемами.
- **models** — pure SQLAlchemy declarative, без бизнес-методов. Enums — в `models/__init__.py` (StrEnum), единое место определений.

### 3.3 Управление enum-доменами (одно определение)

Все 11 доменов из DM §2 определяются **один раз** в `models/__init__.py` как `StrEnum`:

```python
class ArtifactRole(StrEnum):
    interview = "interview"
    persona = "persona"
    # ...
```

Там же — единственный `TypeDecorator` для SQLAlchemy:

```python
class EnumColumn(types.TypeDecorator):
    impl = types.String
    def __init__(self, enum_type):
        self.enum_type = enum_type
        super().__init__()
    def process_bind_param(self, value, dialect):
        return value.value if isinstance(value, self.enum_type) else value
    def process_result_value(self, value, dialect):
        return self.enum_type(value) if value else None
```

Все модели используют `EnumColumn(ArtifactRole)` вместо дублирования. Pydantic-схемы на границах (LLM, CSV, config) импортируют тот же `ArtifactRole` из `models`.

**Правило:** изменение enum — в одном файле, `models/__init__.py`. Никакого тройного дублирования.

### 3.4 Config FR-2: YAML-файл как источник правды (без изменений)

Эталонный конфиг — `config.yaml` в корне проекта (версионируется в git). При старте `config_manager` читает YAML, приводит к `ConfigYAML` (Pydantic), diff-ит с БД, создаёт новые версии рубрик (append-only). Изменение YAML = перезапуск / `POST /admin/reload-config`. В дашборде нет форм редактирования рубрик.

### 3.5 Журнал vs состояние vs конфиг — честный контракт store.py (v3, три категории с 2026-07-16)

Контракт v2 «store.py — только insert/select» был фактически неверен: UPDATE нужен пяти местам (находка 2 ревью). Двухчастный контракт v3 «журнал + ровно 4 `update_*`» тоже оказался неполон: он противоречил data-model §3.2 (`EdgeDef.rubric_id` перенаправляется на новую версию рубрики) и AC тикета S4 — reload `config.yaml` обязан реконсилировать `Lesson`, `ArtifactDef` и `EdgeDef.rubric_id` (решение CEO 2026-07-16, ADR-005; протокол `decisions/meetings/2026-07-16-S4-rubric-repoint.md`). Вместо того чтобы прятать мутации, разделяем явно — **три категории**:

**1. Журнал (иммутабельно, только `register_*`):** `Rubric`, `ArtifactSnapshot`, `CoherenceVerdict`, `SyncRunRepository`, создание `Override`. Для них store.py не экспортирует update/delete — это и есть основной механизм И5 (плюс триггеры на 2 доказательных таблицах, §4).

**2. Рабочее состояние (ровно 4 узких `update_*`):**

| Функция | Мутация | Зачем |
|---------|---------|-------|
| `update_sync_run_status` | `SyncRun.status`: in_progress → completed / partial / failed | жизненный цикл обхода (FR-8, NFR-2) |
| `update_override_revoked` | `Override.revoked_at` | обратимость отметки (FR-10); снятие — не удаление |
| `update_user_lockout` | `SystemUser.failed_attempts`, `locked_until` | блокировка входа (FR-0) |
| `update_credential_validity` | `GitCredential.is_valid`, `checked_at` | сигнал «обнови токен» (FR-3) |

Других `update_*` в этой категории не появляется; `delete_*` нет вообще.

**3. Конфиг-реконсиляция из `config.yaml` (единственный вызывающий — `config_manager`):**

| Сущность | Мутации | Зачем |
|----------|---------|-------|
| `Lesson` | атрибуты занятия (title, date, …) | справочник-конфиг (FR-2); реконструкция прошлых конфигов не требуется (DM §3.1, В12) |
| `ArtifactDef` | атрибуты ожидаемого артефакта (expected_pattern, …) | то же |
| `EdgeDef.rubric_id` | repoint на новую версию рубрики | «действующая рубрика ребра» — первичный факт конфигурации; источник правды — `config.yaml` (§3.4), БД — его отражение |

Почему строим третью категорию, а не пятый `update_*` и не вычислимую «последнюю версию рубрики»: узкая заплатка не закрывала бы reload (Lesson/ArtifactDef мутируют так же), а вычислимая связь требовала бы миграции и ключа `rubric_key` без FR-драйвера и делала журнал `Rubric` источником правды о конфиге — инверсия §3.4 (разбор вариантов — ADR-005). `Rubric` в категорию 3 **не входит**: правка текста рубрики = новая строка через `register_rubric` (категория 1, И5).

Ограничители категории 3 (митигация риска «третья категория — лазейка»):
- **Единственный вызывающий:** функции конфиг-реконсиляции вызывает только `config_manager` — закреплено тестом на импорт («функции конфиг-реконсиляции не импортирует никто, кроме config_manager»). Появление второго вызывающего — триггер пересмотра решения (ADR-005).
- **Гарантия «проверка идёт по действующей рубрике» держится тестом, не конструкцией** (зафиксированная позиция тестировщика в протоколе): обязательный AC-тест «reload перенаправляет ребро на новую версию; старые вердикты не пересчитываются и не теряются» — старые вердикты остаются привязаны к своим версиям через `rubric_id` в четвёрке И3.
- **Любая смена рубрики = обязательный прогон golden set** (`evals/golden-set.md`) — железное правило CLAUDE.md; расхождение с эталонным вердиктом = регрессия.

**Кеш-колонки `Repository.availability` / `last_ok_sync_at` — вычеркнуты.** Доступность и время последнего успешного чтения считаются на лету из последней строки `SyncRunRepository` (маппинг — §5.3). Почему не строим денормализованный кеш: при 25 репозиториях это лишнее UPDATE-место и риск рассинхрона кеша с журналом при нулевом выигрыше в скорости.

---

## 4. Физическая карта инвариантов (градация DDL / код, v3)

В DM §3.3 инварианты И1–И11 объявлены текстом. Здесь каждый заземлён на физический механизм — с градацией соразмерности: **DDL там, где constraint дёшев и ловит реальную порчу; код/сид там, где DDL невозможен или несоразмерен** (single-user, single-writer).

| Инвариант | DM § | Физический механизм | Где |
|-----------|------|---------------------|-----|
| **И1** (XOR Override) | §1.12 | `CHECK ((coherence_verdict_id IS NOT NULL AND step_quality_card_id IS NULL) OR (coherence_verdict_id IS NULL AND step_quality_card_id IS NOT NULL))` | DDL модели |
| **И2** (согласованность вердикта) | §1.10 | **Приложение** — `coherence_analyzer` проверяет, что оба снапшота одного repository, а роли совпадают с ребром. DB-constraint невозможен (транзитивная логика). Нарушение = баг, ловится golden set. | service |
| **И3** (уникальность четвёрки) | §1.10 | `UNIQUE INDEX idx_quad ON coherence_verdict(source_content_hash, target_content_hash, rubric_id, llm_model) WHERE verdict != 'deferred'`. SQLite 3.30+ | DDL |
| **И4** (одна активная отметка) | §1.12 | `UNIQUE INDEX idx_active_ovr ON override(coherence_verdict_id) WHERE revoked_at IS NULL`. Второй индекс для step_quality_card_id. | DDL |
| **И5** (append-only) | §3.1 | **store.py** не экспортирует update/delete для журнальных сущностей (§3.5). Плюс `BEFORE UPDATE/DELETE`-триггеры `RAISE(ABORT)` **только на `artifact_snapshot` и `coherence_verdict`** — доказательное ядро FR-9 (решение CEO 2026-07-09). | код + DDL (2 таблицы) |
| **И6** (уникальность repo_url) | §1.2 | `UNIQUE INDEX idx_norm_url ON repository(normalized_repo_url)`. Колонка `normalized_repo_url` заполняется при insert через Python-функцию `normalize_url()` (strip .git, lowercase, strip trailing /). | DDL + код |
| **И7** (типизация рубрик) | §1.6 | **Приложение** — `config_manager` и `coherence_analyzer` проверяют тип. `CHECK` + подзапрос в SQLite невозможен. | service |
| **И8** (согласованность снапшота) | §1.9 | `CHECK` без подзапросов, все три ветки: `((status = 'partial') = (partial_reason IS NOT NULL AND partial_reason != '[]')) AND ((status IN ('found','partial')) = (content_hash IS NOT NULL)) AND (status != 'not_found' OR (file_path IS NULL AND source_commit_sha IS NULL))` | DDL |
| **И9** (единственность наблюдения) | §1.9 | `UNIQUE (sync_run_id, repository_id, artifact_def_id)` | DDL |
| **И10** (справочники) | §1.2–1.6 | `UNIQUE (number)` на Lesson; `UNIQUE (source_role, target_role)` на EdgeDef; `UNIQUE (git_host)` на GitCredential — DDL. **Single-user** — сид одной строки `system_user` при миграции + отсутствие роута создания пользователя. Почему не CHECK: `CHECK (SELECT COUNT(*) …)` в SQLite физически не собирается (v2-вариант был неисполним); сид + отсутствие пути записи дают ту же гарантию бесплатно. | DDL (UNIQUE) + сид/код (single-user) |
| **И11** (единственность охвата) | §1.13 | `UNIQUE (sync_run_id, repository_id)` | DDL |

**Итог градации:** DDL — И1, И3, И4, И6, И8, И9, И11 + UNIQUE-части И10. Код/сид — И2, И7, И10 (single-user). Триггеры — только 2 доказательных таблицы (И5). Почему не триггеры на всех журнальных таблицах: при single-writer/single-process дисциплины store.py достаточно; триггер на 1 из 6 таблиц (как было в v2) — полумера, учащая ложной защите, а полный комплект — шум. На двух таблицах, которыми продукт торгует на защите (FR-9), защита от будущего бага с UPDATE стоит ~4 строки DDL и педагогически показывает, где она критична.

---

## 5. Ключевые сценарии (исправленные data flow)

### 5.1 Плановый обход (FR-8) + свод-реконсиляция LLM-проверок

```
POST /sync  →  sync_orchestrator.run_sync()

  → register_sync_run(status=in_progress)        # INSERT SyncRun

  → for each Repository (where archived_at IS NULL):
      → git_client.get_tree() + get_file_content(default_branch)
      → content_hash = sha256(content)
      → классификация: found | partial | not_found
          # partial_reason=template_copy — сравнение с content_hash файлов шаблона (D35)
      → if content_hash != last_snapshot.content_hash:
          → register_snapshot(…)                  # INSERT ArtifactSnapshot (новое наблюдение)
      → record_sync_outcome(…)                    # INSERT SyncRunRepository (append-only)

  → update_sync_run_status(completed|partial)     # единственная мутация SyncRun (§3.5)

  → СВОД-РЕКОНСИЛЯЦИЯ (идемпотентный, в конце КАЖДОГО обхода):
      → найти все пары (EdgeDef × репозиторий с текущими снапшотами обеих ролей),
        у которых НЕТ валидного вердикта на их текущую четвёрку
        (source_content_hash, target_content_hash, rubric_id, llm_model)
      → asyncio.create_task(coherence_analyzer.ensure_verdict(…)) на каждую
```

**Почему свод, а не дельта «только changed» (v2):** дельта-постановка задач теряла deferred-пары и задачи, погибшие при рестарте, — они зависали до следующего изменения документа (находка 3 ревью). Идемпотентный свод закрывает всё одним механизмом:
- **deferred-пары ретраятся сами** — у них нет валидного вердикта, они попадут в следующий свод;
- **потерянные при рестарте задачи ретраятся сами** — тем же способом;
- **статус «проверяется» — вычислимое состояние** (есть текущая четвёрка, вердикта нет, обход жив), не хранимое и не in-memory (В13 в DM §6 — закрыт);
- цена — ~200 дешёвых SELECT за обход (D9), это дешевле сущности-очереди.

**Почему не строим TaskTracker и очередь задач в БД:** всё их содержимое выводимо запросом к журналу; in-memory трекер терялся бы при рестарте, а персистентная очередь потребовала бы `processing`-флагов и защиты от двойной обработки — механизм ради механизма при 200 парах/нед. При недоступной LLM все пары уйдут в deferred и повторятся следующим обходом (2 раза/сутки) — бэкофф не вводим, объём мал (D9).

### 5.2 Проверка связности (FR-5)

```
coherence_analyzer.ensure_verdict(edge, snapshot_a, snapshot_b)
  → find_verdict_by_quadruple(hash_a, hash_b, rubric_id, llm_model)
      → если найден И verdict != 'deferred': return (D25 — не мигаем)
  → try:
        llm_response = await llm_client.check_coherence(source_text, target_text, rubric_text)
        validated = validate_llm_response(llm_response)     # schema-check + 1 ретрай
        if not validated:
            → register_verdict(verdict='deferred', reason='parse_error')
            → return deferred                                # свод §5.1 перепроверит в следующий обход
        → register_verdict(CoherenceVerdict(verdict=ok|break, …))
        → return verdict
    except LLMUnavailable:
        → register_verdict(verdict='deferred', reason='llm_unavailable')
        → return deferred
```

**`validate_llm_response()`** — проверяет структурированный выход до записи в БД:
- JSON schema: все обязательные поля присутствуют (verdict, confidence, entities_checked, entities_found, entities_lost, points ≤5)
- Типы: verdict ∈ {ok, break}, confidence ∈ {high, medium, low}
- Целостность: entities_checked == entities_found + entities_excluded + entities_lost
- При провале — **1 повторный запрос**; при повторном провале — deferred(reason=parse_error): не теряем данные, не записываем мусор.

Почему не 3 ретрая с усиленным промптом (v2): эскалация промпта — золочение; deferred-пара и так автоматически перепроверится сводом в следующий обход, а «усиленный промпт» ещё и размывал бы привязку вердикта к рубрике.

### 5.3 Матрица (FR-4) и маппинг исходов sync → UI

```
GET /  →  matrix_builder.build_matrix()
  → last_snapshot_per_repo_and_artifact()   # проекция последних снапшотов
  → latest_verdict_per_edge_and_repo()      # проекция последних вердиктов (И3)
  → last_outcome_per_repo()                 # последняя строка SyncRunRepository на репозиторий
  → blind_spots (последний outcome = repo_unavailable ИЛИ archived_at IS NOT NULL)
  → chronics (ok_unchanged 2+ интервала lesson.date)
  → MatrixView → templates/dashboard/matrix.html
```

**Маппинг исходов `SyncRunRepository.outcome` на UI (закрывает С5-остаток из DM §6):**

| Исход | Отображение | Требование |
|-------|-------------|------------|
| `ok_changed` / `ok_unchanged` | норма; `ok_unchanged` — вход в «хроники» FR-7 | FR-7, FR-8 |
| `repo_unavailable` | **слепая зона** | FR-6 |
| `auth_failed` | баннер «обнови токен» — **не** слепая зона (проблема наша, не студента) | FR-3 |
| `skipped_rate_limit` | честное «не проверялось» | NFR-4 |

Доступность репозитория — вычислимое свойство последней строки `SyncRunRepository`, кеш-колонок нет (§3.5).

### 5.4 Наблюдаемость LLM-проверок

`GET /health` (если оставляем роут) считает счётчики запросом к БД: сколько пар без валидного вердикта, сколько deferred по причинам (`llm_unavailable` / `parse_error`), время последнего обхода. Без in-memory состояния — почему не строим TaskTracker, см. §5.1. Deferred-пары видны преподавателю в матрице как «проверяется/отложено».

### 5.5 Носитель FR-8 — системный cron (v3)

Обход по расписанию запускает **системный планировщик VPS** (cron или systemd timer), дёргающий `POST /sync`:

```cron
# crontab: обход 2 раза в сутки
0 7,19 * * * curl -s -X POST http://localhost:8000/sync -H "X-Sync-Token: $SYNC_TOKEN"
```

- Переживает рестарт приложения; наблюдаем через логи cron; нулевая зависимость в коде.
- **Устаревание видимо:** UI показывает «актуально на ЧЧ:ММ» из `SyncRun.started_at` (FR-8) — если cron сломан или не настроен, преподаватель видит устаревшую отметку, а не молча старые данные.
- **Ops-заметка:** при деплое проверить, что cron реально настроен (`crontab -l`); это пункт чек-листа деплоя, не код.

Почему не строим встроенный планировщик (APScheduler): 2 запуска/сутки для 1 пользователя — задача ОС; встроенный шедулер добавил бы зависимость, состояние в процессе и второй фейл-мод (умер тихо вместе с воркером) — cron падает громче и логируется отдельно.

---

## 6. Закрытые вопросы модели (C1–C5, В13)

| Вопрос | Статус | Решение |
|--------|--------|---------|
| **C1 (deferred)** | Закрыт | И3: partial unique index `WHERE verdict != 'deferred'` (ADR-003). Дополнено: deferred имеет поле `reason ∈ (llm_unavailable, parse_error)` — диагностика, почему не проверено. |
| **C2 (жизненный цикл Repository)** | Закрыт | Добавлен атрибут `archived_at` (datetime, null). При `archived_at IS NOT NULL` репозиторий исключается из обходов и матрицы, но его снапшоты и вердикты сохраняются (FR-9). Ошибочно импортированный URL архивируется, не удаляется. |
| **C3 (partial_reason)** | Закрыт | `partial_reason` — TEXT, хранящий JSON-массив: `["template_copy", "wrong_place"]`. Пустой массив `[]` = нет причин (не partial). Приоритет отображения в UI: template_copy > wrong_place > inexact_name (первая = самая диагностически важная). |
| **C4 (верифицируемость цитат)** | **Принято сознательно** | Контент документов **не хранится** — только content_hash. После force-push цитаты в points неверифицируемы. Доказательная цепочка FR-9: source_commit_sha + content_hash + observed_at доказывают, что *такая версия* существовала — точное содержание невосстановимо. Добавлено в "Чего в модели сознательно нет" в `data-model.md`. |
| **C5-остаток (маппинг исходов → UI)** | **Закрыт (v3)** | Таблица маппинга — §5.3 (было обещано в ADR-002, доехало сюда). |
| **В13 (статус «проверяется»)** | **Закрыт (v3)** | Вычислимое состояние, не хранимое: есть текущая четвёрка, вердикта нет, обход жив (§5.1). |

---

## 7. Тайминг-бюджет (уточнение NFR-1)

NFR-1 в PRD специфицирует ≤2 мин только для обхода репозиториев (walk + content_hash). LLM-проверки — асинхронны и имеют отдельный бюджет:

| Фаза | Тайминг | Зависимости |
|------|---------|-------------|
| Walk 25 репозиториев | ≤2 мин | Git API rate-limit (NFR-4), пропускная способность VPS |
| Свод-реконсиляция + постановка LLM-задач | < 30 сек | После walk; все пары без валидного вердикта на текущую четвёрку (§5.1), sequential |
| LLM-проверки | ~3–7 мин на полную группу | 200 запросов × 1–2 сек sequential; DeepSeek API latency |
| Обновление дашборда | Мгновенно (матрица) | Вердикты подгружаются HTMX по мере готовности |

**Ожидание пользователя:** преподаватель видит матрицу и слепую зону сразу после walk (≤2.5 мин). Разрывы связности появляются в течение следующих 3–7 мин. Если LLM API недоступен — deferred-ячейки матрицы не скрываются, а помечаются "проверяется" (вычислимое состояние, §5.1).

---

## 8. Что уже зафиксировано требованиями (не отменяется)

- Периметр ПД (рамка CEO): максимум данных у преподавателя; именной реестр не уходит в LLM (R-PD3)
- Модель данных — time-series, не снапшот (PRD §13)
- Надёжность: инкрементальный синк, rate-limit/backoff, частичный успех не валит обход (NFR-1..4)
- Периметр: токен read-only, **живёт в env-переменной / файле с правами chmod 600 — не в БД, не в коде, не в логах**; VPS в РФ (D37); 152-ФЗ (D29–D30)

**NFR-3 — интерпретация v3 (решение CEO 2026-07-09):** дух требования — «секрет не в БД, не в коде, не в логах». Почему не строим шифрование токена в БД: при single-user деплое на одном VPS ключ шифрования лежал бы на том же диске рядом с БД — Fernet-крипто добавило бы код и ложное чувство защиты, не убрав ни одного реального вектора; filesystem-права соразмерны read-only токену к публичным репозиториям. Колонка `encrypted_token` из `GitCredential` удалена (остались `git_host`, `is_valid`, `checked_at`). Буквальная формулировка NFR-3 в PRD («хранится зашифрованным») требует отдельной правки PRD — зафиксировано в `memory/MEMORY.md` как открытый вопрос.

---

## 9. Map решений

| Решение | Документ | Статус |
|---------|----------|--------|
| Ядро FR-5 — LLM-агент по рубрикам | `decisions/ADR-001-fr5a-llm-agent-po-pravilam.md` | Принято |
| Стек: Python + FastAPI + SQLite + Jinja2/HTMX | `decisions/ADR-002-stack-python-fastapi-sqlite.md` | **Частично superseded** — Alpine.js удалён; SQLAlchemy — только Mapped-стиль |
| Компонентная архитектура (v3): 3 слоя, store.py (журнал/состояние), карта инвариантов, свод-реконсиляция, cron | Настоящий документ (§3–5) | Принято (supersedes ADR-003) |
| C1–C5, В13, валидация LLM | Настоящий документ (§5–6) | Принято |
| LLM-модель ядра — `deepseek-v4-flash` | `decisions/ADR-004-llm-model-deepseek-v4-flash.md` | **Proposed** (Accepted после мини-эвала на golden set + 2 реальных репо) |
| S4: конфиг-реконсиляция — третья категория контракта store.py | `decisions/ADR-005-s4-config-rekonsiliaciya-store.md` (+ протокол `decisions/meetings/2026-07-16-S4-rubric-repoint.md`) | Принято (CEO, 2026-07-16) |
