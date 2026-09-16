"""Engine/session setup and SQLite connection-level configuration.

Per spec Section 10.5.2 (frozen Owner instruction), the following are
non-negotiable connection-level settings, not developer discipline:

- ``PRAGMA foreign_keys = ON`` on every connection (SQLite defaults this
  off per-connection).
- WAL journal mode, for non-blocking concurrent readers alongside a single
  writer.
- A configured ``busy_timeout`` so a transient writer/writer collision is
  retried by SQLite itself rather than immediately surfacing
  ``SQLITE_BUSY`` to application code.

These are applied via a ``connect`` event listener so they hold for every
connection the pool hands out, regardless of which code path opens it.

Transaction-scope discipline (never hold a transaction open across a model
provider call, tool/subprocess execution, or other long-running I/O) is a
service-layer responsibility for later phases (MA3+); MA0 only establishes
the engine/session primitives those services will use.
"""

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings


def _ensure_parent_dir_for(url: str) -> None:
    """Create the parent directory of a ``sqlite:///`` URL's file path.

    Deliberately derives the directory from the URL actually being opened
    (not from the global ``settings`` singleton) — a test or any other
    caller passing a custom URL must get *its* directory created, not the
    real app's ``data/`` tree.
    """
    db_file = url[len("sqlite:///") :]
    Path(db_file).parent.mkdir(parents=True, exist_ok=True)


def build_engine(database_url: Optional[str] = None, *, echo: bool = False) -> Engine:
    url = database_url or settings.database_url
    if url.startswith("sqlite:///") and url != "sqlite:///:memory:":
        _ensure_parent_dir_for(url)
        if url == settings.database_url:
            # The real application engine additionally owns the sibling
            # data/ subdirectories (artifacts/logs/workspaces/backups) —
            # a custom test URL has no business creating these.
            for d in (
                settings.artifacts_dir,
                settings.logs_dir,
                settings.workspaces_dir,
                settings.backups_dir,
            ):
                d.mkdir(parents=True, exist_ok=True)

    engine = create_engine(
        url,
        echo=echo,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={settings.sqlite_busy_timeout_ms}")
        cursor.close()

    return engine


engine = build_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one short-lived session per request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope(session_factory: Optional[sessionmaker] = None) -> Iterator[Session]:
    """Short-transaction helper: commit on clean exit, rollback on error.

    Intended usage is the claim/commit -> long-running work (no open
    transaction) -> write-result/commit pattern required by Section 10.5.2
    and the ``job_queue`` lease protocol (Section 24.4 #12) — callers open
    a new ``session_scope`` for each short transaction rather than holding
    one across external I/O.
    """
    factory = session_factory or SessionLocal
    db = factory()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
