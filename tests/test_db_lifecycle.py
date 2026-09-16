"""Database lifecycle — Section D.

Creation, Alembic upgrade, schema validation, startup readiness — never
``create_all()`` as the production schema-management mechanism.
"""

from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine

from alembic import command
from app.config import settings
from app.db.lifecycle import get_current_revision, get_head_revision, is_schema_up_to_date

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _alembic_config() -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    return cfg


def test_fresh_unmigrated_db_is_not_ready(tmp_path):
    db_path = tmp_path / "unmigrated.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    with engine.connect():
        pass  # creates an empty file, no alembic_version table

    assert get_current_revision(engine) is None
    assert is_schema_up_to_date(engine) is False
    engine.dispose()


def test_migrated_db_is_ready(tmp_path, monkeypatch):
    db_path = tmp_path / "migrated.db"
    monkeypatch.setattr(settings, "database_path", db_path)
    command.upgrade(_alembic_config(), "head")

    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    assert get_current_revision(engine) == get_head_revision()
    assert is_schema_up_to_date(engine) is True
    engine.dispose()


def test_head_revision_is_stable_across_calls():
    assert get_head_revision() == get_head_revision()


def test_partially_migrated_db_is_not_up_to_date(tmp_path, monkeypatch):
    db_path = tmp_path / "partial.db"
    monkeypatch.setattr(settings, "database_path", db_path)
    cfg = _alembic_config()
    # Upgrade only to the first MA0 revision, not all the way to head.
    command.upgrade(cfg, "f85ec3fb33ae")

    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    current = get_current_revision(engine)
    assert current == "f85ec3fb33ae"
    assert current != get_head_revision()
    assert is_schema_up_to_date(engine) is False
    engine.dispose()
