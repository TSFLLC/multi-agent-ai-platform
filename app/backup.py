"""Local SQLite backup/snapshot — pre-MA3 mandatory checkpoint (Owner
requirement).

Uses SQLite's own ``VACUUM INTO`` rather than copying the ``.db`` file
directly: a plain file copy can capture the main database file mid-write
or miss data still sitting in the WAL file, producing a corrupt or
stale backup. ``VACUUM INTO`` is SQLite's own WAL-safe mechanism for
producing a single, consistent snapshot file regardless of outstanding
WAL state (SQLite docs; available since 3.27, we run 3.35+).

Retention is bounded and configurable (``settings.backup_retention_count``)
— old backups are deleted oldest-first once the count is exceeded.

Run standalone with::

    python -m app.backup
"""

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from sqlalchemy import Engine

from app.config import settings

logger = logging.getLogger("app.backup")

_FILENAME_PREFIX = "multi_agent_platform_"
_FILENAME_SUFFIX = ".db"


def _timestamp() -> str:
    # Microsecond precision alone is not sufficient: this system's clock
    # resolution can be coarser than one microsecond in a tight loop
    # (observed on Windows — datetime.now() returned the exact same value
    # for successive calls), so two backups requested back-to-back can
    # still collide on timestamp. The random suffix below is what actually
    # guarantees uniqueness; the timestamp is for human sorting/readability.
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def create_backup(
    engine: Engine, *, backups_dir: Optional[Path] = None, retention_count: Optional[int] = None
) -> Path:
    backups_dir = backups_dir or settings.backups_dir
    retention_count = retention_count if retention_count is not None else settings.backup_retention_count
    backups_dir.mkdir(parents=True, exist_ok=True)

    unique_suffix = uuid.uuid4().hex[:8]
    backup_path = backups_dir / f"{_FILENAME_PREFIX}{_timestamp()}_{unique_suffix}{_FILENAME_SUFFIX}"

    with engine.connect() as conn:
        # VACUUM INTO takes a string literal path, not a normal bind
        # parameter — quote/escape single quotes defensively since this
        # is a local, operator-controlled path, not untrusted input.
        escaped = str(backup_path).replace("'", "''")
        conn.exec_driver_sql(f"VACUUM INTO '{escaped}'")

    logger.info("backup_created path=%s", backup_path)
    _enforce_retention(backups_dir, retention_count)
    return backup_path


def list_backups(backups_dir: Optional[Path] = None) -> List[Path]:
    backups_dir = backups_dir or settings.backups_dir
    if not backups_dir.exists():
        return []
    return sorted(backups_dir.glob(f"{_FILENAME_PREFIX}*{_FILENAME_SUFFIX}"))


def _enforce_retention(backups_dir: Path, retention_count: int) -> None:
    backups = list_backups(backups_dir)
    excess = len(backups) - retention_count
    for old_backup in backups[: max(excess, 0)]:
        old_backup.unlink()
        logger.info("backup_pruned path=%s", old_backup)


def main() -> None:
    from app.db.session import engine
    from app.logging_config import configure_logging

    configure_logging()
    path = create_backup(engine)
    print(f"Backup written to: {path}")


if __name__ == "__main__":
    main()
