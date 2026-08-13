"""S76 (#76): журнал отвечает на вопросы «кто входил», «что сделало ядро», «куда мигрируем».

Находки разбора боевого журнала 2026-08-14 (SSH-аудит journald):
  * вход/неудача/блокировка не логируются вовсе — вопрос «кто входил 12.08
    с незнакомого IP» по журналу не решается;
  * успешный вердикт ядра тих — «тихий успех» неотличим от «тихого отказа»
    (класс FIX-I2), свод при подключённом ядре не сообщает даже числа пар;
  * httpx на INFO пишет полный URL каждого запроса к хостингу — сотни строк
    на обход и адреса репозиториев с именами студентов (ПД) в журнале;
  * alembic читает адрес базы только из alembic.ini — после переноса базы
    приложением через CD_DATABASE_URL миграция молча создала бы пустой файл
    по старому пути (класс инцидента 31.07);
  * потолок осмотра веток-кандидатов зашит константой 5 — у С-03 их 13,
    восемь никогда не осматриваются (постоянная слепая зона без ручки).

Логгеры перехватываются напрямую (monkeypatch), а не через caplog: фикстуры
прогоняют миграции, `fileConfig` алембика заменяет обработчики корневого
логгера, и записи до caplog не доходят (см. tests/test_branch_discovery.py).
"""

import logging
from datetime import datetime

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from alembic import command
from app import store
from app.clients.git_client import BranchInfo
from app.config import alembic_database_url, settings
from app.logging_config import configure_logging
from app.main import app
from app.models import GitHost, SyncTrigger
from app.models.artifact_def import ArtifactDef
from app.models.lesson import Lesson
from app.routes import auth as auth_module
from app.routes import get_session
from app.services import sync_orchestrator
from app.services.sync_orchestrator import reconcile_llm_pairs, run_sync

PASSWORD = "correct-horse"


def _capture(monkeypatch, module_logger):
    """Список отформатированных записей уровня INFO+ данного логгера."""
    records: list[tuple[str, str]] = []

    def grab(level):
        def hook(msg, *args, **kwargs):
            records.append((level, msg % args if args else msg))
        return hook

    monkeypatch.setattr(module_logger, "info", grab("INFO"))
    monkeypatch.setattr(module_logger, "warning", grab("WARNING"))
    return records


# ---------------------------------------------------------------- auth (G3)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Приложение поверх БД из миграции с известным паролем админа (как в test_auth)."""
    monkeypatch.setenv("CD_ADMIN_PASSWORD", PASSWORD)
    db_path = tmp_path / "test.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")

    def override_session():
        with Session(engine) as session:
            yield session
            session.commit()

    app.dependency_overrides[get_session] = override_session
    yield TestClient(app)
    app.dependency_overrides.clear()
    engine.dispose()


def _login(client, password, username="admin"):
    return client.post(
        "/login", data={"username": username, "password": password}, follow_redirects=False
    )


def test_login_success_is_logged_without_username(client, monkeypatch):
    """Успешный вход — INFO с меткой-хешем и IP; сырого имени в журнале нет (NFR-3)."""
    records = _capture(monkeypatch, auth_module.logger)
    assert _login(client, PASSWORD).status_code == 303
    lines = [m for level, m in records if level == "INFO" and "Вход выполнен" in m]
    assert len(lines) == 1
    assert "u_" in lines[0]  # метка оператора — user_marker, не идентификатор
    assert "ip=" in lines[0]
    assert "admin" not in lines[0]


def test_login_failure_is_logged_with_attempt_number(client, monkeypatch):
    records = _capture(monkeypatch, auth_module.logger)
    assert _login(client, "wrong").status_code == 401
    lines = [m for level, m in records if level == "WARNING" and "Неудачный вход" in m]
    assert len(lines) == 1
    assert "попытка=1" in lines[0]
    assert "wrong" not in lines[0]  # пароль в журнал не попадает


def test_unknown_username_is_not_written_to_log(client, monkeypatch):
    """В поле имени часто по ошибке вводят пароль — сырое значение не логируется."""
    records = _capture(monkeypatch, auth_module.logger)
    assert _login(client, "x", username="s3cr3t-typo").status_code == 401
    warnings = [m for level, m in records if level == "WARNING"]
    assert len(warnings) == 1
    assert "s3cr3t-typo" not in warnings[0]
    assert "имя не найдено" in warnings[0]


def test_lockout_trigger_and_rejected_attempt_are_logged(client, monkeypatch):
    records = _capture(monkeypatch, auth_module.logger)
    for _ in range(5):
        _login(client, "wrong")
    assert any("заблокирована" in m for level, m in records if level == "WARNING")
    records.clear()
    assert _login(client, PASSWORD).status_code == 429  # блокировка активна
    assert any("блокировк" in m for level, m in records if level == "WARNING")


# ------------------------------------------------- свод и ядро (G2, класс FIX-I2)


@pytest.fixture()
def session(tmp_path):
    db_path = tmp_path / "svod.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as s:
        yield s
    engine.dispose()


PRD = "# PRD\nПродукт делает X."


class FakeGitClient:
    """Фейк с деревьями по веткам (как в test_branch_discovery)."""

    def __init__(self, trees: dict, branches: list):
        self.trees = trees
        self.branches = branches
        self.tree_calls: list[str] = []

    async def get_tree(self, repo_url, git_host, ref="main"):
        self.tree_calls.append(ref)
        return list(self.trees.get(ref, {}).keys())

    async def get_file_content(self, repo_url, git_host, file_path, ref="main"):
        return self.trees[ref][file_path]

    async def fetch_default_branch(self, repo_url, git_host):
        return "main"

    async def get_head_sha(self, repo_url, git_host, ref="main"):
        return "f" * 40

    async def list_branches(self, repo_url, git_host):
        return self.branches


def _seed(session):
    lesson = Lesson(number=5, title="PRD", date=datetime(2026, 6, 30).date())
    session.add(lesson)
    session.flush()
    adef = ArtifactDef(lesson_id=lesson.id, role="prd", expected_pattern="product/prd.md")
    session.add(adef)
    repo = store.register_repository(
        session, repo_url="https://github.com/s1/r", git_host=GitHost.GitHub
    )
    session.flush()
    return adef, repo


@pytest.mark.anyio
async def test_reconcile_with_worker_logs_pair_count(session, monkeypatch):
    """Свод с подключённым ядром называет число пар — раньше молчал (тихий успех)."""
    _seed(session)
    records = _capture(monkeypatch, sync_orchestrator.logger)
    seen: list = []

    async def worker(pair):
        seen.append(pair)

    client = FakeGitClient(trees={"main": {"product/prd.md": PRD}}, branches=[])
    await run_sync(session, client, triggered_by=SyncTrigger.manual, verdict_worker=worker)
    svod = [m for level, m in records if "Свод" in m]
    assert len(svod) == 1
    # пар нет (одна роль без рёбер) — свод говорит это явно, а не молчит
    assert "нет" in svod[0]


@pytest.mark.anyio
async def test_reconcile_names_number_of_pairs_handed_to_worker(session, monkeypatch):
    records = _capture(monkeypatch, sync_orchestrator.logger)

    async def worker(pair):
        pass

    pairs = [object(), object()]
    monkeypatch.setattr(
        sync_orchestrator, "find_pairs_without_verdict", lambda *a, **k: pairs
    )
    await reconcile_llm_pairs(session, llm_model="m", verdict_worker=worker)
    svod = [m for level, m in records if "Свод" in m]
    assert len(svod) == 1
    assert "2" in svod[0]
    assert "ядру" in svod[0]


# ------------------------------------------------------- httpx (ПД и шум в журнале)


def test_httpx_logger_is_capped_at_warning():
    """httpx INFO — строка на каждый запрос к хостингу с полным URL (ПД в путях)."""
    configure_logging()
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


# ------------------------------------------------------ alembic и CD_DATABASE_URL


def test_env_var_overrides_ini_default(monkeypatch):
    monkeypatch.setenv("CD_DATABASE_URL", "sqlite:////srv/x/db.sqlite")
    assert alembic_database_url("sqlite:///./course_dashboard.db") == "sqlite:////srv/x/db.sqlite"


def test_explicit_url_beats_env_var(monkeypatch):
    """Тестовые фикстуры (set_main_option) и правленый ini сильнее переменной:
    иначе экспортированный CD_DATABASE_URL увёл бы тестовые миграции в боевую базу."""
    monkeypatch.setenv("CD_DATABASE_URL", "sqlite:////srv/x/db.sqlite")
    assert alembic_database_url("sqlite:///tmp/test.db") == "sqlite:///tmp/test.db"


def test_without_env_var_ini_value_is_used(monkeypatch):
    monkeypatch.delenv("CD_DATABASE_URL", raising=False)
    assert alembic_database_url("sqlite:///./course_dashboard.db") == "sqlite:///./course_dashboard.db"
    assert alembic_database_url(None) == "sqlite:///./course_dashboard.db"


# ------------------------------------------------------- потолок веток (слепая зона)


@pytest.mark.anyio
async def test_branch_scan_limit_is_configurable(session, monkeypatch):
    """CD_BRANCH_SCAN_LIMIT: у С-03 веток-кандидатов 13 — потолок 5 не должен быть зашит."""
    _seed(session)
    monkeypatch.setattr(settings, "branch_scan_limit", 2)
    names = [f"feat/{i}" for i in range(6)]
    trees = {"main": {"README.md": "x"}}
    trees.update({n: {"product/prd.md": PRD} for n in names})
    branches = [BranchInfo(name="main", head_sha="a" * 40, committed_date="2026-06-01T10:00:00Z")] + [
        BranchInfo(
            name=n, head_sha="b" * 40, committed_date=f"2026-08-1{i}T10:00:00Z"
        )
        for i, n in enumerate(names)
    ]
    client = FakeGitClient(trees=trees, branches=branches)

    run = await run_sync(session, client, triggered_by=SyncTrigger.manual)
    session.flush()

    assert len(client.tree_calls) == 1 + 2  # дефолтная + ровно две самые свежие
    assert len(store.find_branch_hints(session, run.id)) == 2
