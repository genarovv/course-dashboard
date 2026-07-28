"""#33: лишняя переменная в .env не роняет старт (extra_forbidden).

Найдено смоком 2026-07-28: строка CD_ADMIN_PASSWORD в .env валила приложение —
pydantic-settings запрещает лишние ключи из env-файла, хотя ту же переменную
из окружения молча игнорирует. CD_ADMIN_PASSWORD читает только миграция.
"""

from pathlib import Path


def test_extra_env_file_variable_does_not_crash(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "CD_ADMIN_PASSWORD=secret\nCD_UNKNOWN_FUTURE_FLAG=1\nCD_SECRET_KEY=abc\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    from app.config import Settings

    settings = Settings(_env_file=str(env_file))  # не должно бросить ValidationError
    assert settings.secret_key == "abc"  # объявленные поля читаются
    assert not hasattr(settings, "admin_password")  # лишнее молча игнорируется


def test_declared_fields_still_read_from_env(monkeypatch):
    monkeypatch.setenv("CD_SYNC_TOKEN", "from-environ")
    from app.config import Settings

    assert Settings(_env_file=None).sync_token == "from-environ"
