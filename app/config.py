from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "sqlite:///./course_dashboard.db"
    sync_token: str = ""
    # NFR-3: read-only токены Git API — только env (CD_GITHUB_TOKEN / CD_GITLAB_TOKEN),
    # не в БД, не в коде, не в логах (решение CEO 2026-07-09)
    github_token: str = ""
    gitlab_token: str = ""
    deepseek_api_key: str = ""
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

    # extra="ignore" (#33): лишние CD_-переменные в .env (например CD_ADMIN_PASSWORD,
    # который читает только миграция) не должны ронять старт приложения
    model_config = {"env_prefix": "CD_", "env_file": ".env", "extra": "ignore"}


settings = Settings()
