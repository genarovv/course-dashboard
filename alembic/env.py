from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from app.config import alembic_database_url
from app.models import (  # noqa: F401 — ensure models are registered
    Base,
    artifact_def,
    artifact_snapshot,
    branch_hint,
    coherence_verdict,
    edge_def,
    git_credential,
    lesson,
    override,
    repository,
    rubric,
    sync_run,
    sync_run_repository,
    system_user,
)

config = context.config
# S76 (#76): миграция и приложение обязаны смотреть в один файл — CD_DATABASE_URL
# перекрывает дефолт alembic.ini (инцидент 31.07 «мигрировали пустой файл»;
# явный set_main_option тестовых фикстур остаётся сильнее, см. app.config)
config.set_main_option(
    "sqlalchemy.url", alembic_database_url(config.get_main_option("sqlalchemy.url"))
)
if config.config_file_name is not None:
    # disable_existing_loggers=False: иначе fileConfig глушит логгеры приложения,
    # уже созданные к моменту миграции (в тестах alembic и app живут в одном процессе)
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def run_migrations_offline():
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
