"""Alembic migrations — upgrade from empty DB, downgrade, schema/model parity."""

from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command
from app.config import settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _alembic_config() -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    return cfg


def _point_settings_at(monkeypatch, db_path: Path) -> None:
    # alembic/env.py reads app.config.settings.database_url at run time; it
    # is the same module singleton this test patches, so mutating it here
    # is visible to the env.py that command.upgrade/downgrade executes,
    # without ever touching the real data/multi_agent_platform.db.
    monkeypatch.setattr(settings, "database_path", db_path)


def test_upgrade_from_empty_db_creates_all_tables(tmp_path, monkeypatch):
    db_path = tmp_path / "migrate_test.db"
    _point_settings_at(monkeypatch, db_path)
    command.upgrade(_alembic_config(), "head")

    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    tables = set(inspect(engine).get_table_names())
    assert "agents" in tables
    assert "task_runs" in tables
    assert "execution_events" in tables
    assert "job_queue" in tables
    assert "alembic_version" in tables
    engine.dispose()


def test_downgrade_removes_all_tables(tmp_path, monkeypatch):
    db_path = tmp_path / "migrate_down_test.db"
    _point_settings_at(monkeypatch, db_path)
    cfg = _alembic_config()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")

    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    tables = set(inspect(engine).get_table_names()) - {"alembic_version"}
    assert tables == set()
    engine.dispose()


def test_upgrade_is_reapplicable_after_downgrade(tmp_path, monkeypatch):
    db_path = tmp_path / "migrate_cycle_test.db"
    _point_settings_at(monkeypatch, db_path)
    cfg = _alembic_config()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    with engine.connect() as conn:
        count = conn.execute(text("SELECT count(*) FROM agents")).scalar()
    assert count == 0
    engine.dispose()
