"""Database readiness/lifecycle helpers — Section D.

Used by the API's ``/ready`` endpoint and by startup to answer "is the
local SQLite file actually at the schema this code expects" rather than
just "did the file open." Alembic migrations remain the only way the
schema changes (Section 10.5.1) — this module never calls
``Base.metadata.create_all()`` as a substitute.

MA7.7B: also the module the hosted deployment entrypoint
(scripts/hosted_entrypoint.py) calls to run "migrate, then confirm head"
before starting the Worker/Web processes, so that ordering logic and this
module's own readiness check share one implementation of "what does
`alembic upgrade head` mean here" rather than two.
"""

import logging
from pathlib import Path
from typing import Optional

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine

from alembic import command

logger = logging.getLogger("app.db.lifecycle")

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _alembic_config() -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    return cfg


def alembic_config() -> Config:
    """Public wrapper around ``_alembic_config`` — the same Alembic
    ``Config`` this module builds for its own head-revision lookups,
    exposed so other trusted local callers (tests, the hosted entrypoint)
    build it identically instead of duplicating the two-line setup."""
    return _alembic_config()


def get_head_revision() -> Optional[str]:
    script = ScriptDirectory.from_config(_alembic_config())
    return script.get_current_head()


def get_current_revision(engine: Engine) -> Optional[str]:
    with engine.connect() as conn:
        context = MigrationContext.configure(conn)
        return context.get_current_revision()


def is_schema_up_to_date(engine: Engine) -> bool:
    """True only if the DB has been migrated and sits exactly at head —
    not merely "some tables exist."""
    current = get_current_revision(engine)
    if current is None:
        return False
    return current == get_head_revision()


def upgrade_to_head(engine: Engine) -> str:
    """Runs ``alembic upgrade head`` against the database
    ``engine.url`` points at, then re-confirms the schema actually landed
    at head (belt-and-braces: Alembic itself raises on a failed
    migration, but a caller like the hosted entrypoint needs an
    unambiguous "did this really work" signal to decide whether it is
    safe to start the Worker/Web processes). Returns the resulting
    current revision.

    Deliberately takes an ``Engine`` (not "reads settings.database_url
    itself") for the same reason ``get_current_revision`` does — a caller
    that already resolved which database it means (e.g. a test pointed at
    a temp file) must not have that silently swapped out from under it by
    also going through the process-wide ``settings`` singleton here."""
    command.upgrade(_alembic_config(), "head")
    if not is_schema_up_to_date(engine):
        current = get_current_revision(engine)
        head = get_head_revision()
        raise RuntimeError(
            f"alembic upgrade head completed but the schema is not at head "
            f"(current={current!r}, head={head!r})."
        )
    revision = get_current_revision(engine)
    logger.info("database_migrated_to_head revision=%s", revision)
    return revision
