"""AIL.3C migration: real upgrade/downgrade on disposable SQLite databases.

Covers what create_all()-built test databases cannot: the CHECK constraint on
learning_evidence.ref_type, the frozen-version FK, and the partial unique
index, all as produced by the actual Alembic revision.
"""

from pathlib import Path
import logging.config

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from alembic import command
from app.config import settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BASE = "ail3b_experiment_execution"
HEAD = "ail3c_experiment_conclusion"
NOW = "2026-01-01T00:00:00"


def _cfg() -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    return cfg


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "ail3c-migration.db"
    monkeypatch.setattr(settings, "database_path", path)
    return path


def _engine(path: Path):
    return create_engine(f"sqlite:///{path.as_posix()}")


def _seed_learner(conn) -> None:
    conn.execute(text(f"INSERT INTO organizations (name, id, created_at) VALUES ('Org', 'org-1', '{NOW}')"))
    conn.execute(text(
        f"INSERT INTO users (org_id, email, role, id, created_at) VALUES ('org-1', 'a@example.com', 'member', 'user-1', '{NOW}')"
    ))
    conn.execute(text(
        f"INSERT INTO concepts (slug, name, level, kind, is_core, id, created_at) "
        f"VALUES ('tokens', 'Tokens', 'foundational', 'definitional', 0, 'concept-1', '{NOW}')"
    ))
    conn.execute(text(
        f"INSERT INTO concept_versions (concept_id, version, plain_definition, content_origin, status, created_at, id) "
        f"VALUES ('concept-1', 1, 'def', 'ai_drafted_unreviewed', 'active', '{NOW}', 'cv-1')"
    ))


def _insert_evidence(conn, evidence_id: str, ref_type: str, ref_id: str) -> None:
    conn.execute(text(
        "INSERT INTO learning_evidence (user_id, concept_id, concept_version_id, evidence_type, grader, "
        "on_demo_data, ref_type, ref_id, created_at, id) "
        f"VALUES ('user-1', 'concept-1', 'cv-1', 'lab', 'deterministic', 0, '{ref_type}', '{ref_id}', '{NOW}', '{evidence_id}')"
    ))


def _pragma_clean(conn) -> None:
    assert conn.execute(text("PRAGMA integrity_check")).scalar() == "ok"
    assert conn.execute(text("PRAGMA foreign_key_check")).fetchall() == []


def test_single_alembic_head():
    assert ScriptDirectory.from_config(_cfg()).get_heads() == [HEAD]


def test_alembic_migration_preserves_hosted_failure_logger(tmp_path, monkeypatch):
    """Alembic logging setup must not hide hosted startup migration errors."""
    from alembic import command

    db_path = tmp_path / "hosted-migration-logging.db"
    monkeypatch.setattr(settings, "database_path", db_path)
    calls = []
    real_file_config = logging.config.fileConfig

    def observed_file_config(*args, **kwargs):
        calls.append(kwargs.copy())
        return real_file_config(*args, **kwargs)

    monkeypatch.setattr(logging.config, "fileConfig", observed_file_config)
    hosted_logger = logging.getLogger("scripts.hosted_entrypoint")
    hosted_logger.disabled = False

    command.upgrade(_cfg(), "head")

    assert calls
    assert all(call.get("disable_existing_loggers") is False for call in calls)
    assert hosted_logger.disabled is False


def test_populated_upgrade_downgrade_reupgrade(db_path):
    command.upgrade(_cfg(), BASE)
    engine = _engine(db_path)
    with engine.begin() as conn:
        _seed_learner(conn)
        _insert_evidence(conn, "pre-existing", "agent_run", "run-1")
    engine.dispose()

    command.upgrade(_cfg(), "head")

    engine = _engine(db_path)
    with engine.begin() as conn:
        # Existing evidence survived the CHECK rebuild.
        assert conn.execute(text("SELECT COUNT(*) FROM learning_evidence WHERE id = 'pre-existing'")).scalar() == 1
        # The widened CHECK accepts experiment-backed evidence.
        _insert_evidence(conn, "exp-ev-1", "experiment", "experiment-1")
        # frozen-version FK exists and targets concept_versions.id
        fks = conn.execute(text("PRAGMA foreign_key_list(experiments)")).fetchall()
        assert any(fk[2] == "concept_versions" and fk[3] == "concept_version_id" and fk[4] == "id" for fk in fks)
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(experiments)"))}
        assert {"concept_version_id", "conclusion_type", "conclusion_text", "concluded_at"} <= columns
        _pragma_clean(conn)
    engine.dispose()

    # Downgrade is refused while experiment-backed evidence exists, changing nothing.
    with pytest.raises(RuntimeError, match="experiment-backed"):
        command.downgrade(_cfg(), "-1")
    engine = _engine(db_path)
    with engine.begin() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM learning_evidence WHERE ref_type = 'experiment'")).scalar() == 1
        conn.execute(text("DELETE FROM learning_evidence WHERE id = 'exp-ev-1'"))
    engine.dispose()

    command.downgrade(_cfg(), "-1")
    engine = _engine(db_path)
    with engine.begin() as conn:
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(experiments)"))}
        assert not ({"concept_version_id", "conclusion_type", "conclusion_text", "concluded_at"} & columns)
        assert conn.execute(text("SELECT COUNT(*) FROM learning_evidence WHERE id = 'pre-existing'")).scalar() == 1
        assert conn.execute(text(
            "SELECT COUNT(*) FROM sqlite_master WHERE name = 'uq_learning_evidence_experiment_ref'"
        )).scalar() == 0
        _pragma_clean(conn)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        _insert_evidence(conn, "exp-ev-2", "experiment", "experiment-1")
    engine.dispose()

    command.upgrade(_cfg(), "head")
    engine = _engine(db_path)
    with engine.begin() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM learning_evidence WHERE id = 'pre-existing'")).scalar() == 1
        _insert_evidence(conn, "exp-ev-3", "experiment", "experiment-1")
        _pragma_clean(conn)
    engine.dispose()


def test_partial_unique_index_is_enforced_only_for_experiment_refs(db_path):
    command.upgrade(_cfg(), "head")
    engine = _engine(db_path)
    with engine.begin() as conn:
        _seed_learner(conn)
        _insert_evidence(conn, "e1", "experiment", "experiment-1")
        # Same ref_id under a different ref_type is not constrained...
        _insert_evidence(conn, "e2", "agent_run", "experiment-1")
        _insert_evidence(conn, "e3", "agent_run", "experiment-1")
    with pytest.raises(IntegrityError), engine.begin() as conn:
        _insert_evidence(conn, "e4", "experiment", "experiment-1")
    engine.dispose()
