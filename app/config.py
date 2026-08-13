import os
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "sqlite:///./course_dashboard.db"
    sync_token: str = ""
    # NFR-3: read-only токены Git API — только env (CD_GITHUB_TOKEN / CD_GITLAB_TOKEN),
    # не в БД, не в коде, не в логах (решение CEO 2026-07-09)
    github_token: str = ""
    gitlab_token: str = ""
    # C3 (#34): ключ и адрес DeepSeek принимаются и без префикса CD_ —
    # так их задал CEO в .env (P2); base_url переопределяем (доступ через посредника)
    deepseek_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("CD_DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY"),
    )
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com",
        validation_alias=AliasChoices(
            "CD_DEEPSEEK_BASE_URL", "DEEPSEEK_API_BASE_URL", "DEEPSEEK_BASE_URL"
        ),
    )
    deepseek_model: str = "deepseek-v4-flash"
    secret_key: str = "change-me"
    template_dir: str = str(Path(__file__).parent / "templates")
    # FR-2: эталонный конфиг курса (§3.4) — источник правды для Lesson/ArtifactDef/EdgeDef/Rubric;
    # там же — адрес репозитория-шаблона для детекта заготовок (PRD FR-4, D35)
    config_yaml_path: str = str(Path(__file__).parent / "config.yaml")
    static_dir: str = str(Path(__file__).parent / "static")

    # #32: смещение отображаемого времени в минутах (240 = UTC+4);
    # None — взять смещение сервера. Хранение в БД всегда в UTC.
    tz_offset_minutes: int | None = None

    # S76 (#76): сколько веток-кандидатов осматривает обход (D43). Потолок был
    # зашит константой 5 — у С-03 кандидатов 13, восемь не осматривались никогда
    # (постоянная слепая зона). Цена ветки — один запрос дерева (D43 AC).
    branch_scan_limit: int = 5

    # extra="ignore" (#33): лишние CD_-переменные в .env (например CD_ADMIN_PASSWORD,
    # который читает только миграция) не должны ронять старт приложения
    model_config = {"env_prefix": "CD_", "env_file": ".env", "extra": "ignore"}


settings = Settings()

#: Значение sqlalchemy.url, записанное в alembic.ini репозитория.
_ALEMBIC_INI_DEFAULT_URL = "sqlite:///./course_dashboard.db"


def alembic_database_url(ini_url: str | None) -> str:
    """Адрес базы для миграций — тот же файл, который читает приложение.

    Инцидент 31.07: alembic берёт адрес из alembic.ini, приложение — из
    CD_DATABASE_URL; расхождение = миграция пустого файла молча. После переноса
    боевой базы в data/ (2026-08-14) CD_DATABASE_URL перекрывает дефолт ini.

    Перекрывается ТОЛЬКО дефолт: явно заданный URL (тестовые фикстуры через
    set_main_option, сознательно правленый ini) остаётся в силе — иначе
    экспортированный CD_DATABASE_URL увёл бы тестовые миграции в боевую базу.
    """
    if ini_url and ini_url != _ALEMBIC_INI_DEFAULT_URL:
        return ini_url
    return os.environ.get("CD_DATABASE_URL") or ini_url or _ALEMBIC_INI_DEFAULT_URL
