"""Local SQLite backup — pre-MA3 mandatory checkpoint.

WAL-safe (VACUUM INTO, not a raw file copy), bounded/configurable
retention, and — since ``main.py``'s lifespan runs this automatically —
never fatal to startup on failure.
"""

import sqlite3

from app.backup import create_backup, list_backups
from app.models.identity import Organization


def test_backup_creates_a_file(engine, tmp_path):
    backups_dir = tmp_path / "backups"
    path = create_backup(engine, backups_dir=backups_dir, retention_count=10)
    assert path.exists()
    assert path.parent == backups_dir


def test_backup_is_a_valid_readable_sqlite_file_with_data(db, engine, tmp_path):
    db.add(Organization(name="Backup Test Org"))
    db.commit()

    backups_dir = tmp_path / "backups"
    path = create_backup(engine, backups_dir=backups_dir)

    conn = sqlite3.connect(str(path))
    try:
        count = conn.execute("SELECT count(*) FROM organizations").fetchone()[0]
        assert count == 1
    finally:
        conn.close()


def test_backup_captures_wal_pending_writes(db, engine, tmp_path):
    """VACUUM INTO must reflect data even if it hasn't been checkpointed
    out of the WAL file yet — the exact scenario a naive file copy of the
    main .db file would miss."""
    db.add(Organization(name="Still In WAL"))
    db.commit()  # committed to WAL, not necessarily checkpointed

    backups_dir = tmp_path / "backups"
    path = create_backup(engine, backups_dir=backups_dir)

    conn = sqlite3.connect(str(path))
    try:
        names = [r[0] for r in conn.execute("SELECT name FROM organizations").fetchall()]
        assert "Still In WAL" in names
    finally:
        conn.close()


def test_retention_prunes_oldest_first(engine, tmp_path):
    backups_dir = tmp_path / "backups"
    paths = [create_backup(engine, backups_dir=backups_dir, retention_count=3) for _ in range(5)]

    remaining = list_backups(backups_dir)
    assert len(remaining) == 3
    # the two oldest were pruned
    assert paths[0] not in remaining
    assert paths[1] not in remaining
    assert paths[-1] in remaining


def test_retention_is_configurable(engine, tmp_path):
    backups_dir = tmp_path / "backups"
    for _ in range(6):
        create_backup(engine, backups_dir=backups_dir, retention_count=2)
    assert len(list_backups(backups_dir)) == 2


def test_list_backups_empty_dir_returns_empty_list(tmp_path):
    assert list_backups(tmp_path / "does-not-exist-yet") == []
