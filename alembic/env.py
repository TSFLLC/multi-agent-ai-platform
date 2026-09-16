import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context

# Make the `app` package importable regardless of the cwd Alembic is
# invoked from.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import models  # noqa: F401  (populates Base.metadata)
from app.config import settings
from app.db.base import Base
from app.db.session import build_engine

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Single source of truth for the DB URL is app.config.settings, not
# alembic.ini, so there is exactly one place that decides where the local
# SQLite file lives.
config.set_main_option("sqlalchemy.url", settings.database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # Reuse the app's own engine builder so migrations run under the same
    # PRAGMA foreign_keys=ON / WAL / busy_timeout configuration as the
    # application itself (Section 10.5.2), not a bare default connection.
    engine = build_engine(settings.database_url)

    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite can't ALTER most column/constraint definitions in
            # place; batch mode recreates the table under the hood so
            # future (MA1+) migrations that alter columns still work.
            render_as_batch=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
