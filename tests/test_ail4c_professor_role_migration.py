"""Disposable SQLite tests for the single AIL.4C AgentRun role migration."""

from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from alembic import command
from app.config import settings
from app.db.session import build_engine

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PREVIOUS_HEAD = "ail4b_review_attempts"
NEW_HEAD = "ail4c_professor_agent_role"
NOW = "2026-01-01T00:00:00"


def _cfg() -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    return cfg


def _engine(path: Path):
    return build_engine(f"sqlite:///{path.as_posix()}")


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "ail4c-professor-role.db"
    monkeypatch.setattr(settings, "database_path", path)
    return path


def _seed_agent_runs(conn):
    conn.execute(text(f"INSERT INTO organizations (id, name, created_at) VALUES ('org-1', 'Org', '{NOW}')"))
    conn.execute(text(f"INSERT INTO projects (id, org_id, name, kind, ail_evidence_opt_in, created_at) VALUES ('project-1', 'org-1', 'AIL', 'system_ail', 0, '{NOW}')"))
    conn.execute(text(f"INSERT INTO agents (id, project_id, name, role, current_status, created_at) VALUES ('agent-1', 'project-1', 'Agent', 'professor', 'active', '{NOW}')"))
    conn.execute(text(f"INSERT INTO agent_versions (id, agent_id, version, name, role, default_model_strategy, status, created_at) VALUES ('av-1', 'agent-1', 1, 'Agent', 'professor', 'manual_required', 'active', '{NOW}')"))
    conn.execute(text(f"INSERT INTO tasks (id, project_id, title, execution_mode, status, created_at) VALUES ('task-1', 'project-1', 'Task', 'single_agent', 'ready', '{NOW}')"))
    conn.execute(text(f"INSERT INTO task_runs (id, task_id, status, timeout_seconds, created_at) VALUES ('tr-1', 'task-1', 'created', 60, '{NOW}')"))
    for role in ("primary", "reviewer", "repair", "evaluator"):
        conn.execute(text(
            "INSERT INTO agent_runs (id, task_run_id, agent_version_id, status, role, timeout_seconds, fencing_token, created_at) "
            f"VALUES ('run-{role}', 'tr-1', 'av-1', 'created', '{role}', 60, 0, '{NOW}')"
        ))


def _checks(conn):
    return [row[3] for row in conn.execute(text("PRAGMA table_info(agent_runs)"))]


def _clean(conn):
    assert conn.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
    assert conn.execute(text("PRAGMA foreign_key_check")).fetchall() == []


def test_professor_role_upgrade_preserves_rows_and_integrity(db_path):
    command.upgrade(_cfg(), PREVIOUS_HEAD)
    engine = _engine(db_path)
    with engine.begin() as conn:
        _seed_agent_runs(conn)
    engine.dispose()

    command.upgrade(_cfg(), NEW_HEAD)
    engine = _engine(db_path)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO agent_runs (id, task_run_id, agent_version_id, status, role, timeout_seconds, fencing_token, created_at) "
            f"VALUES ('run-professor', 'tr-1', 'av-1', 'created', 'professor', 60, 0, '{NOW}')"
        ))
        roles = {row[0] for row in conn.execute(text("SELECT role FROM agent_runs"))}
        assert roles == {"primary", "reviewer", "repair", "evaluator", "professor"}
        _clean(conn)
    engine.dispose()


def test_upgrade_recovers_known_stale_sqlite_batch_table(db_path):
    command.upgrade(_cfg(), PREVIOUS_HEAD)
    engine = _engine(db_path)
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE _alembic_tmp_agent_runs (id VARCHAR(36) NOT NULL)")
    engine.dispose()

    command.upgrade(_cfg(), NEW_HEAD)
    engine = _engine(db_path)
    with engine.begin() as conn:
        assert conn.execute(text("SELECT name FROM sqlite_master WHERE name = '_alembic_tmp_agent_runs'")).scalar_one_or_none() is None
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == NEW_HEAD
        _clean(conn)
    engine.dispose()

def test_professor_role_downgrade_refuses_without_relabeling(db_path):
    command.upgrade(_cfg(), NEW_HEAD)
    engine = _engine(db_path)
    with engine.begin() as conn:
        _seed_agent_runs(conn)
        conn.execute(text(
            "INSERT INTO agent_runs (id, task_run_id, agent_version_id, status, role, timeout_seconds, fencing_token, created_at) "
            f"VALUES ('run-professor', 'tr-1', 'av-1', 'created', 'professor', 60, 0, '{NOW}')"
        ))
    engine.dispose()

    with pytest.raises(RuntimeError, match="Professor AgentRun"):
        command.downgrade(_cfg(), PREVIOUS_HEAD)
    engine = _engine(db_path)
    with engine.begin() as conn:
        assert conn.execute(text("SELECT role FROM agent_runs WHERE id = 'run-professor'")).scalar_one() == "professor"
        _clean(conn)
    engine.dispose()


def test_downgrade_after_professor_rows_removed_restores_old_constraint(db_path):
    command.upgrade(_cfg(), NEW_HEAD)
    engine = _engine(db_path)
    with engine.begin() as conn:
        _seed_agent_runs(conn)
        conn.execute(text("DELETE FROM agent_runs WHERE role = 'evaluator'"))
        _clean(conn)
    engine.dispose()

    # No Professor row exists, so downgrade is safe and does not relabel any row.
    command.downgrade(_cfg(), PREVIOUS_HEAD)
    engine = _engine(db_path)
    with engine.begin() as conn:
        with pytest.raises(IntegrityError):
            conn.execute(text(
                "INSERT INTO agent_runs (id, task_run_id, agent_version_id, status, role, timeout_seconds, fencing_token, created_at) "
                f"VALUES ('run-invalid-professor', 'tr-1', 'av-1', 'created', 'professor', 60, 0, '{NOW}')"
            ))
        _clean(conn)
    engine.dispose()
