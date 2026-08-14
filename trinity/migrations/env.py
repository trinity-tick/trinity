import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from alembic import context

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None

# Support both SQLite (default) and PostgreSQL via environment variables.
#  - TRINITY_DB_TYPE = "sqlite" | "postgresql"
#  - TRINITY_DB_PATH = absolute path to SQLite file
#  - DATABASE_URL    = PostgreSQL connection string (only used when DB_TYPE=postgresql)
_DB_TYPE = os.getenv("TRINITY_DB_TYPE", "sqlite")
_DB_PATH = os.getenv("TRINITY_DB_PATH") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "trinity_store.db"
)
_PG_URL = os.getenv("DATABASE_URL", "")

if _DB_TYPE == "postgresql" and _PG_URL:
    config.set_main_option("sqlalchemy.url", _PG_URL)
else:
    config.set_main_option("sqlalchemy.url", f"sqlite:///{_DB_PATH}")


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
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
