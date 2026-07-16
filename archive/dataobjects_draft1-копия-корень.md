# Data Objects — v1 (FR-0..FR-5, FR-8, FR-9, FR-10)

Сущности и их атрибуты. Связи не указаны.

---

## SystemUser — учётная запись преподавателя (FR-0)

| Атрибут | Тип | Описание |
|---------|-----|----------|
| id | UUID | Первичный ключ |
| username | string | Логин для входа |
| password_hash | string | Хеш пароля (bcrypt/argon2) |
| failed_attempts | int | Счётчик неудачных попыток входа |
| locked_until | timestamp|null | Блокировка до (null = не заблокирован) |

---

## Student — студент курса (FR-1)

| Атрибут | Тип | Описание |
|---------|-----|----------|
| id | UUID | Первичный ключ |
| full_name | string | ФИО |
| repo_url | string | URL публичного git-репозитория |
| git_host | enum(GitLab, GitHub) | Хостинг репозитория |
| added_at | timestamp | Когда добавлен |
| is_active | bool | Активен (false = удалён/отчислен) |

---

## Lesson — занятие курса (FR-2)

| Атрибут | Тип | Описание |
|---------|-----|----------|
| id | UUID | Первичный ключ |
| number | int | Номер занятия (1..10) |
| title | string | Название занятия |

---

## ArtifactDef — определение ожидаемого артефакта (FR-2)

| Атрибут | Тип | Описание |
|---------|-----|----------|
| id | UUID | Первичный ключ |
| lesson_id | UUID | Занятие, к которому привязан артефакт |
| role | enum | Роль: interview, persona, user_story, prd, data_model, architecture, plan, code, tests |
| expected_pattern | string | Глоб/регекс для поиска файла |
| template_relative_path | string|null | Путь в шаблонном репозитории (для сравнения с заготовкой) |

---

## EdgeDef — ребро связности (FR-2)

| Атрибут | Тип | Описание |
|---------|-----|----------|
| id | UUID | Первичный ключ |
| source_role | enum | Роль источника (из ArtifactDef.role) |
| target_role | enum | Роль приёмника (из ArtifactDef.role) |
| rubric_id | UUID | Какая рубрика проверяет это ребро |

---

## Rubric — версионированная рубрика проверки (FR-2, FR-5)

| Атрибут | Тип | Описание |
|---------|-----|----------|
| id | UUID | Первичный ключ |
| type | enum(edge, step) | Для ребра (FR-5) или шага (FR-11, v2) |
| version | string | Семантическая версия рубрики |
| text | text | Текст правила для LLM-агента |
| created_at | timestamp | Дата создания/обновления |

---

## GitCredential — токен доступа к Git API (FR-3)

| Атрибут | Тип | Описание |
|---------|-----|----------|
| id | UUID | Первичный ключ |
| git_host | enum(GitLab, GitHub) | Какому хостингу принадлежит токен |
| encrypted_token | string | Токен, зашифрованный при хранении |

---

## SyncRun — сессия обхода репозиториев (FR-4, FR-5, FR-8, FR-9)

| Атрибут | Тип | Описание |
|---------|-----|----------|
| id | UUID | Первичный ключ |
| started_at | timestamp | Начало обхода |
| completed_at | timestamp|null | Завершение (null = ещё идёт) |
| status | enum(in_progress, completed, partial, failed) | Итог обхода |

---

## ArtifactSnapshot — результат проверки наличия артефакта (FR-4, FR-9)

| Атрибут | Тип | Описание |
|---------|-----|----------|
| id | UUID | Первичный ключ |
| sync_run_id | UUID | В рамках какого обхода получен |
| student_id | UUID | Чей репозиторий |
| artifact_def_id | UUID | Какой артефакт искали |
| status | enum(found, partial, not_found) | Найден / частично / не найден |
| file_path | string|null | Путь к файлу в репозитории (null = не найден) |
| source_commit_sha | string|null | Хеш коммита, в котором найден файл |
| content_hash | string|null | Хеш содержимого (для инкрементального обхода, FR-8) |
| is_template_copy | bool|null | true = файл не изменён относительно шаблона-заготовки |

---

## CoherenceVerdict — вердикт проверки связности ребра (FR-5, FR-9, FR-10)

| Атрибут | Тип | Описание |
|---------|-----|----------|
| id | UUID | Первичный ключ |
| sync_run_id | UUID | В рамках какого обхода получен |
| student_id | UUID | Чей репозиторий |
| edge_def_id | UUID | Какое ребро проверяли |
| source_snapshot_id | UUID | Версия документа-источника |
| target_snapshot_id | UUID | Версия документа-приёмника |
| rubric_version | string | Версия рубрики на момент проверки |
| llm_model | string | Модель LLM (e.g. gpt-4o) |
| verdict | enum(ok, break, deferred) | Связность цела / разрыв / отложено (LLM недоступна) |
| confidence | enum(high, medium, low) | Уверенность агента |
| entities_checked | int | Сколько сущностей проверено |
| entities_found | int | Отражено в приёмнике |
| entities_excluded | int | Явно исключено |
| entities_lost | int | Потеряно |
| points | JSON | ≤5 точек: [{entity, source_quote, what_searched_in_target}] |
| notes | text|null | Заметка преподавателю от агента |

---

## Override — отметка ложного разрыва преподавателем (FR-10)

| Атрибут | Тип | Описание |
|---------|-----|----------|
| id | UUID | Первичный ключ |
| coherence_verdict_id | UUID | Какой вердикт переопределён |
| created_at | timestamp | Когда поставлена отметка |
| reason | text | Причина (напр. «синоним», «переименование») |
