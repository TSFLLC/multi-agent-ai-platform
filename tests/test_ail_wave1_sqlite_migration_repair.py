"""Regression coverage for the SQLite-safe Wave-1 projects migration."""

from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text

from alembic import command
from app.config import settings


PROJECT_ROOT = Path(__file__).resolve().parent.parent
OLD_COLUMNS = ("org_id", "name", "policy_settings_json", "id", "created_at")
NEW_COLUMNS = OLD_COLUMNS + ("kind", "ail_evidence_opt_in")


def _cfg() -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    return cfg


def _upgrade_to_ail2a(db_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "database_path", db_path)
    command.upgrade(_cfg(), "ail2a_radar_ledger")


def _seed_referenced_project(db_path: Path) -> None:
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO organizations (name, id, created_at) VALUES "
                "('Repair Org', 'org-1', '2026-01-01T00:00:00')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO projects (org_id, name, id, created_at) VALUES "
                "('org-1', 'Repair Project', 'project-1', '2026-01-01T00:00:00')"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE project_references ("
                "id VARCHAR(36) PRIMARY KEY, project_id VARCHAR(36) NOT NULL, "
                "FOREIGN KEY(project_id) REFERENCES projects(id))"
            )
        )
        conn.execute(
            text(
                "INSERT INTO project_references (id, project_id) "
                "VALUES ('project-reference-1', 'project-1')"
            )
        )
    engine.dispose()


def _create_known_temp_table(conn) -> None:
    conn.execute(
        text(
            """
            CREATE TABLE _alembic_tmp_projects (
                org_id VARCHAR(36) NOT NULL,
                name VARCHAR(255) NOT NULL,
                policy_settings_json JSON,
                id VARCHAR(36) NOT NULL,
                created_at DATETIME NOT NULL,
                kind VARCHAR(10) NOT NULL DEFAULT 'standard',
                ail_evidence_opt_in BOOLEAN NOT NULL DEFAULT 0,
                CONSTRAINT projectkind CHECK (kind IN ('standard', 'system_ail'))
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


def test_existing_referenced_project_upgrades_without_recreation(tmp_path, monkeypatch):
    db_path = tmp_path / "existing-project.db"
    _upgrade_to_ail2a(db_path, monkeypatch)
    _seed_referenced_project(db_path)

    command.upgrade(_cfg(), "ail_wave1_integration")

    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    with engine.connect() as conn:
        assert _columns(db_path, "projects") == NEW_COLUMNS
        assert conn.execute(text("SELECT count(*) FROM projects")).scalar() == 1
        assert conn.execute(text("SELECT count(*) FROM project_references")).scalar() == 1
        assert conn.execute(text("PRAGMA foreign_key_check")).all() == []
        assert conn.execute(
            text("SELECT kind, ail_evidence_opt_in FROM projects")
        ).one() == ("standard", 0)
    engine.dispose()


def test_known_zero_row_orphan_is_removed_and_wave1_completes(tmp_path, monkeypatch):
    db_path = tmp_path / "failed-retry.db"
    _upgrade_to_ail2a(db_path, monkeypatch)
    _seed_referenced_project(db_path)
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    with engine.begin() as conn:
        _create_known_temp_table(conn)
    engine.dispose()

    command.upgrade(_cfg(), "ail_wave1_integration")

    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    with engine.connect() as conn:
        assert conn.execute(
            text(
                "SELECT count(*) FROM sqlite_master WHERE name = "
                "'_alembic_tmp_projects'"
            )
        ).scalar() == 0
        assert conn.execute(text("SELECT count(*) FROM project_references")).scalar() == 1
    engine.dispose()


def test_nonempty_orphan_fails_closed_without_deleting_it(tmp_path, monkeypatch):
    db_path = tmp_path / "unsafe-retry.db"
    _upgrade_to_ail2a(db_path, monkeypatch)
    _seed_referenced_project(db_path)
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    with engine.begin() as conn:
        _create_known_temp_table(conn)
        conn.execute(
            text(
                "INSERT INTO _alembic_tmp_projects "
                "(org_id, name, id, created_at) VALUES "
                "('org-1', 'unsafe', 'unsafe-project', '2026-01-01T00:00:00')"
            )
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="contains 1 row"):
        command.upgrade(_cfg(), "ail_wave1_integration")

    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    with engine.connect() as conn:
        assert conn.execute(
            text(
                "SELECT count(*) FROM sqlite_master WHERE name = "
                "'_alembic_tmp_projects'"
            )
        ).scalar() == 1
        assert conn.execute(text("SELECT count(*) FROM projects")).scalar() == 1
        assert _columns(db_path, "projects") == OLD_COLUMNS
    engine.dispose()


def test_fresh_database_reaches_final_head(tmp_path, monkeypatch):
    db_path = tmp_path / "fresh.db"
    monkeypatch.setattr(settings, "database_path", db_path)
    command.upgrade(_cfg(), "head")
    assert _columns(db_path, "projects") == NEW_COLUMNS
