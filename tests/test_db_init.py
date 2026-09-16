"""SQLite database initialization — Section 10.5.2 requirements."""

from app.db.session import build_engine


def test_creates_data_directory_and_file(tmp_path):
    db_path = tmp_path / "nested" / "multi_agent_platform.db"
    engine = build_engine(f"sqlite:///{db_path.as_posix()}")
    with engine.connect():
        pass
    assert db_path.parent.exists()
    engine.dispose()


def test_foreign_keys_pragma_on_by_default(engine):
    with engine.connect() as conn:
        value = conn.exec_driver_sql("PRAGMA foreign_keys").scalar()
    assert value == 1


def test_wal_journal_mode_on_file_db(engine):
    with engine.connect() as conn:
        mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
    assert mode.lower() == "wal"


def test_busy_timeout_configured(engine):
    with engine.connect() as conn:
        timeout = conn.exec_driver_sql("PRAGMA busy_timeout").scalar()
    assert timeout > 0


def test_new_connection_also_gets_pragmas(engine):
    """The pragma listener must apply per-connection, not once globally —
    SQLite's foreign_keys pragma is off-by-default per new connection."""
    with engine.connect() as conn1:
        assert conn1.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
    with engine.connect() as conn2:
        assert conn2.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
