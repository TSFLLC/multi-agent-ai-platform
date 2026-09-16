"""Database readiness/lifecycle helpers — Section D.

Used by the API's ``/ready`` endpoint and by startup to answer "is the
local SQLite file actually at the schema this code expects" rather than
just "did the file open." Alembic migrations remain the only way the
schema changes (Section 10.5.1) — this module never calls
``Base.metadata.create_all()`` as a substitute.
"""

from pathlib import Path
from typing import Optional

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _alembic_config() -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    return cfg


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
