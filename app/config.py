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
    static_dir: str = str(Path(__file__).parent / "static")

    model_config = {"env_prefix": "CD_", "env_file": ".env"}


settings = Settings()
