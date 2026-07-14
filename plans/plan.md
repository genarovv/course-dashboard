# Plan — план разработки Course Dashboard до защиты

**Дата:** 2026-07-09
**Основание:** Протокол приоритизации первой итерации (`decisions/meetings/2026-07-09-приоритизация-первой-итерации.md`), решение CEO.
**Трекер:** https://github.com/genarovv/course-dashboard/issues

---

## 1. Вехи проекта до защиты

| Веха | Критерий | Блокирует | Когда |
|------|----------|-----------|-------|
| **Фаза 0 (core gate)** | Прогон рубрики FR-5 на 2–3 чужих репозиториях + мини-эвал `deepseek-v4-flash` на golden set. ADR-004 → Accepted. | Разработку ядра (C1, C2) | До разработки |
| **Первая итерация (v1 core)** | 16 задач — полный сценарий «настройка → обход → матрица → защита» с минимальным D4 (2 ребра). | Защиту | После Фазы 0 |
| **Pre-defense** | D4 — расширение до 8 рёбер. D5 — UI админки. D6 — Deferred/health UI. C1 — llm_client. C2 — coherence_analyzer. | Демо защиты | После v1 core |
| **Защита** | Продукт готов к демо: матрица, разрывы, хронология, override. | — | К защите |

---

## 2. Первая итерация — 16 задач в порядке выполнения

### Stage 1 — Foundation (параллельные потоки)

| Порядок | Задача | Тикет | FR | Зависит от |
|:-------:|--------|:-----:|:--:|:----------:|
| 1 | **S1 — Модели данных (11 ORM-сущностей)** | [#5](https://github.com/genarovv/course-dashboard/issues/5) | FR-0/1/2/3/4/5/8/10 | — |
| 2 | **S3 — Миграции + DDL-инварианты** | [#1](https://github.com/genarovv/course-dashboard/issues/1) | Все | S1 |
| 3 | **S2 — store.py (append-only data access)** | [#3](https://github.com/genarovv/course-dashboard/issues/3) | FR-0/1/3/8/10 | S1 |
| 4 | **I1 — Скелет приложения (main.py, base/login.html, CSS)** | [#2](https://github.com/genarovv/course-dashboard/issues/2) | — | — |
| 5 | **G1 — git_client (GitLab + GitHub API)** | [#4](https://github.com/genarovv/course-dashboard/issues/4) | FR-3, NFR-3/4 | — |

### Stage 2 — Настройка

| Порядок | Задача | Тикет | FR | Зависит от |
|:-------:|--------|:-----:|:--:|:----------:|
| 6 | **S4 — config.yaml + config_manager** | [#6](https://github.com/genarovv/course-dashboard/issues/6) | FR-2 | S2 |
| 7 | **S5 — Аутентификация** | [#7](https://github.com/genarovv/course-dashboard/issues/7) | FR-0 | S1, I1 |
| 8 | **S6 — CSV-импорт репозиториев** | [#8](https://github.com/genarovv/course-dashboard/issues/8) | FR-1 | S1, S2, I1 |

### Stage 3 — Ядро обхода

| Порядок | Задача | Тикет | FR | Зависит от |
|:-------:|--------|:-----:|:--:|:----------:|
| 9 | **G2 — sync_orchestrator** | [#9](https://github.com/genarovv/course-dashboard/issues/9) | FR-8, FR-4 | S1, S2, G1 |
| 10 | **G3 — Детект заготовок (template_copy)** | [#10](https://github.com/genarovv/course-dashboard/issues/10) | FR-4 (BR-3) | G2 |
| 11 | **G4 — Свод-реконсиляция** | [#11](https://github.com/genarovv/course-dashboard/issues/11) | FR-5, FR-8 | G2 |

### Stage 4 — Отображение

| Порядок | Задача | Тикет | FR | Зависит от |
|:-------:|--------|:-----:|:--:|:----------:|
| 12 | **D1 — matrix_builder + matrix.html** | [#12](https://github.com/genarovv/course-dashboard/issues/12) | FR-4 | S2, G2 |
| 13 | **I2 — cron + health endpoint** | [#13](https://github.com/genarovv/course-dashboard/issues/13) | FR-8 | G2 |

### Stage 5 — Override + карточка

| Порядок | Задача | Тикет | FR | Зависит от |
|:-------:|--------|:-----:|:--:|:----------:|
| 14 | **O1 — Override store (data layer)** | [#15](https://github.com/genarovv/course-dashboard/issues/15) | FR-10 | S2 |
| 15 | **D4 (min) — Карточка студента, 2 ребра** | [#14](https://github.com/genarovv/course-dashboard/issues/14) | FR-9 | S2, G4 |
| 16 | **O2 — Override UI (кнопка на точке разрыва)** | [#16](https://github.com/genarovv/course-dashboard/issues/16) | FR-10 | O1, D1, D4 |

---

## 3. Что осознанно отложено и почему

### В пределах v1 (после первой итерации, до защиты)

| Задача | FR | Когда | Почему |
|--------|:--:|-------|--------|
| C3 — Мини-эвал ADR-004 | FR-5 | Любое время | curl-скрипт, не код продукта |
| D4 — расширение до 8 рёбер | FR-9 | До защиты | P0, но минимальная версия (2 ребра) — сначала |
| D5 — UI админки | FR-1/3/8 | По необходимости | Не блокирует сценарий «настройка → обход → защита» |
| D6 — Deferred/health в UI | FR-5 | После C1 | Нет LLM — нет deferred для отображения |
| C1 — llm_client | FR-5 | После Фазы 0 | Решение CEO: ядро не кодить до гейта |
| C2 — coherence_analyzer | FR-5 | После C1 | Железное правило Фазы 0 (PRD §13) |

### В v1.1

| Задача | FR | Почему |
|--------|:--:|--------|
| D2 — Слепая зона | FR-6 | Не блокирует обход: преподаватель видит недоступные репо в матрице |
| D3 — Хроники («студенты в тишине») | FR-7 | P1, не входит в минимальный сценарий |

### В v2

| Задача | FR | Почему |
|--------|:--:|--------|
| FR-11 — Анализ качества артефактов | FR-11 | Требует US + рубрики шагов + пилот точности. Решение CEO 2026-07-09 |
