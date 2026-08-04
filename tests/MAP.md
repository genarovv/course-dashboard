# MAP — Тесты и покрытие course-dashboard

**Дата:** 2026-08-04 (обновлено в MR D42: кнопка гаснет на время обхода)
**Стек:** Python 3.13 · FastAPI · SQLAlchemy 2.x (Mapped) · SQLite (WAL) · Alembic · Jinja2+HTMX · bcrypt
**Тестов:** 424, все ✅ · **Покрытие общее:** 98% (`pytest-cov`, гейт `fail_under = 90` в hooks/pre-push)

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
| `test_hooks.py` | интеграционный (tmp git-репо, subprocess) | H1 (#52): гейт commit-msg — правка/удаление существующих tests/ без «tests-change:» блокируется, с пометкой и для новых файлов проходит, не-тестовые правки свободны; coverage-гейт зафиксирован в pyproject |
| `test_llm_client.py` | модульный (MockTransport, без сети) | C1 (#35): check_coherence — валидный ответ, 1 ретрай, двойной провал → None, HTTP/сеть → LLMUnavailableError; промпт-контракт; schema-check §5.2 (регистр, счётчики, ≤5 точек, fence) |
| `test_coherence_analyzer.py` | модульный (фейки git/LLM) + интеграционный (TestClient) | C2 (#36): ensure_verdict — D25 «не мигаем», deferred пересчитывается, ok/break с полями §5.2, деградации (parse_error/llm_unavailable/git-сбой без записи), И2 (репо и роли), воркер в своей сессии + сериализация Lock, проводка /sync → verdict_worker |
| `test_config_12_artifacts.py` | модульный + интеграционный (alembic, реальный config.yaml) | T3/#43 «12 артефактов»: +6 ролей enum, альтернативные пути на роль, мультистековые code/tests, ребро prd→architecture; реконсиляция хранит несколько паттернов (ключ занятие+роль+паттерн), агрегация best-wins внутри роли, карточка предпочитает found заготовке шаблона |
| `test_edges_8.py` | модульный + интеграционный (реальный config.yaml, alembic) | R (#42): 9 рёбер конвейера, рубрики с версией/контрактом §5.2/спецификой, идемпотентная реконсиляция, мультифайловая сторона пары = список путей связки |
| `test_content_probes.py` | модульный + интеграционный (alembic, FakeGit) | T2 (#44): пробы contains/not_contains — парсинг/валидация «ровно одно условие», findings в снапшоте без смены статуса (BR-3), D28, признак в карточке, updated при смене конфига проб |
| `test_deferred_ui.py` | модульный + интеграционный (alembic, TestClient) | D6 (#37): состояния ребра pending/deferred(+причина)/done, рендер причины в карточке, клиент LLM на пару с aclose, валидация элементов points, SQL NULL у probe_findings |
| `test_fr11_pilot.py` | модульный (без сети) | FR-11-пилот (#45): рубрика качества README (5 критериев в тексте), промпт, валидация вердикта/критериев (домены, bool met), отчёт с моделью и версией рубрики, «README не найден» |
| `test_matrix_builder.py` | модульный (session fixture + alembic) | FR-4 (D1 #12): матрица «репо × занятие» — статусы ячеек, partial_reason, последний снапшот побеждает, «актуально на», пустая БД |
| `test_dashboard_matrix.py` | интеграционный (TestClient + реальная БД) | FR-4 (D1 #12): GET / рендерит матрицу — строка репозитория, колонка занятия, partial_reason, «Актуально на», редирект без сессии |
| `test_artifact_matrix.py` | модульный (session fixture) + интеграционный (TestClient) | D7 (макет CEO): матрица «репо × артефакт» — порядок ролей по занятиям, best-wins между альтернативными путями, усечённый свод ячейки (частично-причины, разрыв с первой потерянной сущностью, гашение override), модалка деталей (файлы, рёбра с цитатами и заметками, кнопка FR-10), auth-гварды, 404 |
| `test_artifact_mr_channel.py` | модульный + интеграционный (TestClient) | D9 #53 (FR-12/US-B7): плашка «сдача через MR» при пустой/not_found ячейке MR-занятия, best-wins→канал для роли в двух занятиях, реальный статус найденного, объяснение в модалке |
| `test_artifact_signals.py` | модульный + интеграционный (TestClient) | D10 #54 (FR-6/7/3): слепая зона/хроники/auth-баннер в артефактной матрице (общая blind_spots_and_signals), флаг stale >48ч, пустой реестр без блоков |
| `test_artifact_cell_honest.py` | модульный (session fixture) | D11 #55 (FR-5/FR-10/BR-2): свод называет разрыв и в «частично», override=«помечен ложным» (не «связность ок»), приоритет свода, новая четвёрка не наследует отметку |
| `test_artifact_inversion.py` | модульный + интеграционный (TestClient) | D13 #57 (риск §11): чип разрыва {сущность, счётчик, уверенность}, наивысшая уверенность при нескольких разрывах, разрыв без точек, подпись «уверенность низкая» до клика, статус словом |
| `test_artifact_legend.py` | интеграционный (TestClient) | D12 #56 (BR-3): легенда — все состояния ячейки, обе градации чипа, пояснение «частично» по наведению |
| `test_artifact_row_breaks.py` | модульный + интеграционный (TestClient) | D15 #59: уникальный счётчик рёбер-разрывов строки (без удвоения), сортировка ?sort=breaks стабильная, sticky-обёртка |
| `test_artifact_freshness.py` | модульный + интеграционный (TestClient) | D16 #60: «новое» по computed_at последнего обхода (D25 не мигает), точка свежести снапшота, первый обход без меток |
| `test_artifact_modal_polish.py` | интеграционный (TestClient) | D17 #61: бейдж уверенности conf-*, якорная ссылка в карточку (+id рёбер), фокусируемость модалки и скрипт фокуса |
| `test_defense_mode.py` | модульный + интеграционный (TestClient) | D18 #62 (FR-13/US-C2): фильтр high+не погашенные, даты и sha обеих сторон, «разрывов для показа нет», слепая зона, без FR-10-кнопок, auth/404, входы из матрицы и карточки |
| `test_migrations.py` | интеграционный (alembic upgrade + raw SQL) | DDL: все 12 таблиц созданы, сид system_user, downgrade без ошибок, И1 (XOR), И3 (quad unique), И4 (one active override), И5 (append-only триггеры), И6 (norm URL unique), И8 (snapshot CHECK), И9+И11 (уникальность тройки/пары), И10 (reference uniqueness) |
| `test_sync_orchestrator.py` | модульный (FakeGitClient) + интеграционный (TestClient) | FR-8/FR-4 (G2 #9): классификация found/not_found, sha256 (в т.ч. мульти-совпадение паттерна и `**`-глоб), source_commit_sha (FR-9), инкрементальность D28, исходы всех 5 видов + detail, статусы SyncRun, архивные репо пропущены, POST /sync (сессия / X-Sync-Token / 401) |
| `test_override_ui.py` | интеграционный (TestClient + реальная БД) | FR-10 (O2 #16): toggle создаёт/снимает Override (revoked_at, история строк), auth, 404, кнопка «ложный разрыв» в матрице и карточке, гашение подсветки, новая четвёрка не наследует отметку |
| `test_mr_channel_note.py` | модульный + интеграционный (TestClient) | FR-12 интерим (ADR-007): миграция submission_channel с downgrade, конфиг-реконсиляция канала, пометка «сдача через MR, не наблюдается» в ячейках (пустая/not_found/found), рендер со ссылкой на карточку |
| `test_mr_observation.py` | модульный (session fixture + alembic) | FR-12 (#39): миграция mr_observation с downgrade, И12 (unique triple), журнал по обходам, последние наблюдения не затираются обходом без MR-данных |
| `test_mr_sync.py` | модульный (FakeGit) | FR-12 (#40): MR-шаг обхода — маркеры с цитатой, вердикт «принято» (отрицание не считается), notes только у открытых, деградация NFR-2, выключение без конфига |
| `test_mr_ui.py` | модульный + интеграционный (TestClient) | FR-12 (#41): MR в карточке (ready_for_merge, closed не ready, дата, маркер «не найден»), колонка «Процесс» в матрице, рендер |
| `test_reconcile.py` | модульный (session fixture + фейк-воркер) | FR-5/FR-8 (G4 #11): идентификация пар без валидного вердикта, create_task через инжектированный воркер (ядро FR-5 — за гейтом Фазы 0), D25 «не мигаем», deferred-ретрай, идемпотентность свода, свод в конце run_sync |
| `test_sync_commit_before_reconcile.py` | интеграционный (файловая БД, две сессии) | FIX-I2: обход коммитит наблюдения до свода — чужая сессия видит снапшоты обхода, вердикт считается в том же обходе, в котором артефакт впервые наблюдён (репро боевого дефекта 2026-08-04: completed без вердиктов) |
| `test_sync_busy_button.py` | модульный + интеграционный (TestClient) | D42: серверная истина `is_sync_running` (идёт / завершён / протухший in_progress), повторный POST /sync → 409 без второго SyncRun, кнопка погашена в обеих матрицах и активна при отсутствии обхода |
| `test_store.py` | модульный (session fixture) | Контракт store.py: ровно 5 `update_*` (#50), нет `delete_*`, все `register_*` на месте, ограничитель «сервисы не присваивают default_branch напрямую», `normalize_url()`, CRUD-флоу репозиториев/runs/credentials/overrides, `find_verdict_by_quadruple` |

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
| `app/clients/llm_client.py` | — | ✅ | покрыт | test_llm_client (C1/#35) |
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
| — | — | Пустых модулей не осталось: гейт Фазы 0 снят 2026-07-30, ядро реализовано (C1 `llm_client` + C2 `coherence_analyzer`), оба покрыты |

---

## 4. Вопросы по непокрытым модулям

**Пропуски в покрытых модулях:**

7. **git_client.py** (8 stmts miss: 403 handler, git_host валидация, default_branch fallback) — дыра или сознательно не тестируем?
8. **store.py** (5 miss: SQLite pragmas, find_last_snapshot query) — дыра или сознательно не тестируем?

---

## 5. Рекомендация: где продолжать test-first

**G2 — sync_orchestrator** (тикет [#9](https://github.com/genarovv/course-dashboard/issues/9)): конфиг готов (S4), git_client готов (G1) — обход можно тестировать на фейковом клиенте без сети; за ним стеком G3 (детект заготовок) и G4 (свод-реконсиляция).
