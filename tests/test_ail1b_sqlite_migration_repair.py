"""Regression coverage for the SQLite-safe AIL.1B snapshot migration."""

from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text

from alembic import command
from app.config import settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OLD_COLUMNS = (
    "provider_model_id",
    "model_id",
    "provider_id",
    "pricing_input_per_mtok",
    "pricing_output_per_mtok",
    "currency",
    "context_window",
    "capability_snapshot_json",
    "snapshotted_at",
    "id",
)
NEW_COLUMNS = OLD_COLUMNS + ("source", "change_kinds_json")


def _cfg() -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    return cfg


def _upgrade_to_ef(db_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "database_path", db_path)
    command.upgrade(_cfg(), "ef873dac62a1")


def _seed_snapshot_with_reference(db_path: Path) -> None:
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO models "
                "(canonical_model_id, tool_calling_support, structured_output_support, "
                "vision_capability, status, id, created_at) "
                "VALUES ('repair-model', 'none', 0, 0, 'active', 'model-1', "
                "'2026-01-01T00:00:00')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO providers "
                "(type, name, health_status, id, created_at) "
                "VALUES ('openrouter', 'Repair Provider', 'up', 'provider-1', "
                "'2026-01-01T00:00:00')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO provider_models "
                "(model_id, provider_id, provider_model_id, currency, availability_status, "
                "last_refreshed_at, created_at, id) VALUES "
                "('model-1', 'provider-1', 'repair-model', 'USD', 'up', "
                "'2026-01-01T00:00:00', '2026-01-01T00:00:00', 'provider-model-1')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO provider_model_snapshots "
                "(provider_model_id, model_id, provider_id, currency, snapshotted_at, id) "
                "VALUES ('provider-model-1', 'model-1', 'provider-1', 'USD', "
                "'2026-01-01T00:00:00', 'snapshot-1')"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE snapshot_references ("
                "id VARCHAR(36) PRIMARY KEY, snapshot_id VARCHAR(36) NOT NULL, "
                "FOREIGN KEY(snapshot_id) REFERENCES provider_model_snapshots(id))"
            )
        )
        conn.execute(
            text(
                "INSERT INTO snapshot_references (id, snapshot_id) "
                "VALUES ('reference-1', 'snapshot-1')"
            )
        )
    engine.dispose()


def _create_known_temp_table(conn) -> None:
    conn.execute(
        text(
            """
            CREATE TABLE _alembic_tmp_provider_model_snapshots (
                provider_model_id VARCHAR(36) NOT NULL,
                model_id VARCHAR(36) NOT NULL,
                provider_id VARCHAR(36) NOT NULL,
                pricing_input_per_mtok NUMERIC(18, 8),
                pricing_output_per_mtok NUMERIC(18, 8),
                currency VARCHAR(10) NOT NULL,
                context_window INTEGER,
                capability_snapshot_json JSON,
                snapshotted_at DATETIME NOT NULL,
                id VARCHAR(36) NOT NULL,
                source VARCHAR(16) NOT NULL DEFAULT 'legacy_unknown',
                change_kinds_json JSON,
                CONSTRAINT snapshotsource CHECK
                    (source IN ('execution_freeze', 'catalog_refresh', 'legacy_unknown'))
            )
            """
        )
    )


def _columns(db_path: Path, table: str):
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    with engine.connect() as conn:
        result = tuple(
            row[1] for row in conn.execute(text(f'PRAGMA table_info("{table}")')).all()
        )
    engine.dispose()
    return result


def test_existing_rows_and_fk_references_upgrade_without_recreation(tmp_path, monkeypatch):
    db_path = tmp_path / "existing.db"
    _upgrade_to_ef(db_path, monkeypatch)
    _seed_snapshot_with_reference(db_path)

    command.upgrade(_cfg(), "0f87701fabff")

    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    with engine.connect() as conn:
        assert _columns(db_path, "provider_model_snapshots") == NEW_COLUMNS
        assert conn.execute(text("SELECT count(*) FROM provider_model_snapshots")).scalar() == 1
        assert conn.execute(text("SELECT count(*) FROM snapshot_references")).scalar() == 1
        assert conn.execute(text("PRAGMA foreign_key_check")).all() == []
        assert conn.execute(
            text(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND name = "
                "'ix_provider_model_snapshots_provider_model_id_source_snapshotted_at'"
            )
        ).scalar() is not None
    engine.dispose()


def test_known_zero_row_orphan_is_removed_and_migration_completes(tmp_path, monkeypatch):
    db_path = tmp_path / "failed-retry.db"
    _upgrade_to_ef(db_path, monkeypatch)
    _seed_snapshot_with_reference(db_path)
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    with engine.begin() as conn:
        _create_known_temp_table(conn)
    engine.dispose()

    command.upgrade(_cfg(), "0f87701fabff")

    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT count(*) FROM sqlite_master WHERE name = "
                 "'_alembic_tmp_provider_model_snapshots'")
        ).scalar() == 0
        assert conn.execute(text("SELECT count(*) FROM provider_model_snapshots")).scalar() == 1
        assert conn.execute(text("SELECT id FROM provider_model_snapshots")).scalar() == "snapshot-1"
    engine.dispose()


def test_nonempty_orphan_fails_closed_without_deleting_it(tmp_path, monkeypatch):
    db_path = tmp_path / "unsafe-retry.db"
    _upgrade_to_ef(db_path, monkeypatch)
    _seed_snapshot_with_reference(db_path)
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    with engine.begin() as conn:
        _create_known_temp_table(conn)
        conn.execute(
            text(
                "INSERT INTO _alembic_tmp_provider_model_snapshots "
                "(provider_model_id, model_id, provider_id, currency, snapshotted_at, id) "
                "VALUES ('provider-model-1', 'model-1', 'provider-1', 'USD', "
                "'2026-01-01T00:00:00', 'unsafe-row')"
            )
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="contains 1 row"):
        command.upgrade(_cfg(), "0f87701fabff")

    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT count(*) FROM sqlite_master WHERE name = "
                 "'_alembic_tmp_provider_model_snapshots'")
        ).scalar() == 1
        assert conn.execute(text("SELECT count(*) FROM provider_model_snapshots")).scalar() == 1
        assert _columns(db_path, "provider_model_snapshots") == OLD_COLUMNS
    engine.dispose()


def test_fresh_database_reaches_ail1b(tmp_path, monkeypatch):
    db_path = tmp_path / "fresh.db"
    monkeypatch.setattr(settings, "database_path", db_path)
    command.upgrade(_cfg(), "0f87701fabff")
    assert _columns(db_path, "provider_model_snapshots") == NEW_COLUMNS
