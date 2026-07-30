"""#33: лишняя переменная в .env не роняет старт (extra_forbidden).

Найдено смоком 2026-07-28: строка CD_ADMIN_PASSWORD в .env валила приложение —
pydantic-settings запрещает лишние ключи из env-файла, хотя ту же переменную
из окружения молча игнорирует. CD_ADMIN_PASSWORD читает только миграция.
"""



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


def test_deepseek_vars_without_cd_prefix_are_accepted(tmp_path, monkeypatch):
    """C3: CEO задал DEEPSEEK_API_KEY / DEEPSEEK_API_BASE_URL без префикса CD_ —
    принимаем оба написания (alias), чтобы предусловие P2 не спотыкалось об имя."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DEEPSEEK_API_KEY=sk-test\nDEEPSEEK_API_BASE_URL=https://proxy.example/v1\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    from app.config import Settings

    settings = Settings(_env_file=str(env_file))
    assert settings.deepseek_api_key == "sk-test"
    assert settings.deepseek_base_url == "https://proxy.example/v1"


def test_deepseek_base_url_default(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("CD_SECRET_KEY=abc\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    from app.config import Settings

    settings = Settings(_env_file=str(env_file))
    assert settings.deepseek_base_url == "https://api.deepseek.com"
