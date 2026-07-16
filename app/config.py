from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "sqlite+aiosqlite:///./course_dashboard.db"
    sync_token: str = ""
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-v4-flash"
    secret_key: str = "change-me"
    template_dir: str = str(Path(__file__).parent / "templates")
    static_dir: str = str(Path(__file__).parent / "static")

    model_config = {"env_prefix": "CD_", "env_file": ".env"}


settings = Settings()
