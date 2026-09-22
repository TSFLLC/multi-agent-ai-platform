"""MA7.3a migration (a3c9e17b5d42): approvals.resolution_note + the partial
unique index on workflow-node approvals.

DISPOSABLE DATABASES ONLY. Every test migrates a fresh ``tmp_path`` SQLite
file; the ``disposable_db`` fixture redirects alembic/env.py (which reads
``settings.database_url``) away from data/multi_agent_platform.db -- that file
holds real UAT data and must never be migrated, downgraded or otherwise touched
by a test -- and fails the test outright if the settings still point at it.
"""

from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from alembic import command
from app.config import settings
from app.db.base import Base

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PREVIOUS_HEAD = "bd27cdb01c15"
REVISION = "a3c9e17b5d42"
INDEX = "uq_approvals_workflow_node_run_operation"
REAL_DB = PROJECT_ROOT / "data" / "multi_agent_platform.db"


def _cfg() -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    return cfg


@pytest.fixture()
def disposable_db(tmp_path, monkeypatch):
    db_path = tmp_path / "ma73a_migration.db"
    monkeypatch.setattr(settings, "database_path", db_path)
    assert Path(settings.database_path).resolve() != REAL_DB.resolve()
    assert "multi_agent_platform" not in settings.database_url
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    yield db_path, engine
    engine.dispose()


def _revision(engine) -> str:
    with engine.connect() as conn:
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()


def _insert_approval(conn, row_id, scope="workflow_node_run", ref="ref-1", op="op", status="pending"):
    conn.execute(
        text(
            "INSERT INTO approvals (id, scope, scope_ref_id, operation_type, action_fingerprint, status, "
            "requested_at) VALUES (:id, :scope, :ref, :op, :fp, :status, CURRENT_TIMESTAMP)"
        ),
        {"id": row_id, "scope": scope, "ref": ref, "op": op, "fp": "f" * 64, "status": status},
    )


def test_revision_chain_places_this_migration_directly_after_the_previous_head():
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(_cfg())
    # MA7.5A: this revision is no longer the head -- later slices add
    # migrations after it. What this test pins is its own place in the chain:
    # still directly after PREVIOUS_HEAD, and still an ancestor of the head.
    assert REVISION in {revision.revision for revision in script.walk_revisions()}
    assert script.get_revision(REVISION).down_revision == PREVIOUS_HEAD


def test_upgrade_adds_nullable_resolution_note_and_the_partial_unique_index(disposable_db):
    _, engine = disposable_db
    command.upgrade(_cfg(), PREVIOUS_HEAD)
    insp = inspect(engine)
    assert "resolution_note" not in {c["name"] for c in insp.get_columns("approvals")}
    assert insp.get_indexes("approvals") == []

    command.upgrade(_cfg(), REVISION)
    assert _revision(engine) == REVISION

    insp = inspect(engine)
    columns = {c["name"]: c for c in insp.get_columns("approvals")}
    assert columns["resolution_note"]["nullable"] is True
    assert str(columns["resolution_note"]["type"]).upper() == "TEXT"

    indexes = {i["name"]: i for i in insp.get_indexes("approvals")}
    assert indexes[INDEX]["unique"] == 1
    assert indexes[INDEX]["column_names"] == ["scope", "scope_ref_id", "operation_type"]
    with engine.connect() as conn:
        index_sql = conn.execute(text("SELECT sql FROM sqlite_master WHERE name = :n"), {"n": INDEX}).scalar()
    assert "WHERE scope = 'workflow_node_run'" in index_sql


def test_upgrade_preserves_existing_approval_rows_and_check_constraints(disposable_db):
    _, engine = disposable_db
    command.upgrade(_cfg(), PREVIOUS_HEAD)
    with engine.begin() as conn:
        _insert_approval(conn, "pre-existing", scope="task_run", ref="t-1", status="approved")

    command.upgrade(_cfg(), REVISION)

    with engine.connect() as conn:
        row = conn.execute(text("SELECT id, status, resolution_note FROM approvals")).one()
    assert tuple(row) == ("pre-existing", "approved", None)

    # The scope/status CHECK constraints were not disturbed by the ADD COLUMN.
    names = {c["name"] for c in inspect(engine).get_check_constraints("approvals")}
    assert names == {"ck_approvals_approvalscope", "ck_approvals_approvalstatus"}
    with pytest.raises(IntegrityError), engine.begin() as conn:
        _insert_approval(conn, "bad-status", status="bogus")


def test_index_forbids_duplicate_workflow_node_approvals_but_only_for_that_scope(disposable_db):
    _, engine = disposable_db
    command.upgrade(_cfg(), REVISION)

    with engine.begin() as conn:
        _insert_approval(conn, "w1", scope="workflow_node_run", ref="node-run-1", op="gate")
        # Same node run, different operation: allowed.
        _insert_approval(conn, "w2", scope="workflow_node_run", ref="node-run-1", op="other-op")
        # Same reference/operation under other scopes: allowed (partial index).
        _insert_approval(conn, "t1", scope="task_run", ref="same", op="gate")
        _insert_approval(conn, "t2", scope="task_run", ref="same", op="gate")
        _insert_approval(conn, "a1", scope="agent_run", ref="same", op="gate")

    with pytest.raises(IntegrityError), engine.begin() as conn:
        _insert_approval(conn, "w-dup", scope="workflow_node_run", ref="node-run-1", op="gate")

    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM approvals")).scalar() == 5


def test_downgrade_one_step_removes_column_and_index_and_keeps_rows(disposable_db):
    _, engine = disposable_db
    command.upgrade(_cfg(), REVISION)
    with engine.begin() as conn:
        _insert_approval(conn, "keep-me", ref="node-run-9")
        conn.execute(text("UPDATE approvals SET resolution_note = 'note'"))

    command.downgrade(_cfg(), PREVIOUS_HEAD)

    assert _revision(engine) == PREVIOUS_HEAD
    insp = inspect(engine)
    assert "resolution_note" not in {c["name"] for c in insp.get_columns("approvals")}
    assert INDEX not in {i["name"] for i in insp.get_indexes("approvals")}
    assert {c["name"] for c in insp.get_check_constraints("approvals")} == {
        "ck_approvals_approvalscope",
        "ck_approvals_approvalstatus",
    }
    with engine.connect() as conn:
        assert conn.execute(text("SELECT id FROM approvals")).scalar() == "keep-me"


def test_upgrade_downgrade_upgrade_round_trip_is_stable(disposable_db):
    _, engine = disposable_db
    cfg = _cfg()
    command.upgrade(cfg, REVISION)
    command.downgrade(cfg, PREVIOUS_HEAD)
    command.upgrade(cfg, REVISION)

    assert _revision(engine) == REVISION
    insp = inspect(engine)
    assert INDEX in {i["name"] for i in insp.get_indexes("approvals")}
    assert "resolution_note" in {c["name"] for c in insp.get_columns("approvals")}


def test_full_chain_downgrade_to_base_and_back_on_a_disposable_db(disposable_db):
    _, engine = disposable_db
    cfg = _cfg()
    command.upgrade(cfg, REVISION)
    command.downgrade(cfg, "base")
    assert set(inspect(engine).get_table_names()) - {"alembic_version"} == set()
    command.upgrade(cfg, REVISION)
    assert _revision(engine) == REVISION


def test_migrated_schema_matches_the_model_for_approvals(disposable_db, tmp_path):
    """create_all (what the test suite uses) and the migration chain (what
    production uses) must agree on the approvals table."""
    _, migrated = disposable_db
    command.upgrade(_cfg(), REVISION)

    from app import models  # noqa: F401 -- populates Base.metadata

    created = create_engine(f"sqlite:///{(tmp_path / 'create_all.db').as_posix()}")
    try:
        Base.metadata.create_all(created)
        for engine in (migrated, created):
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        m_insp, c_insp = inspect(migrated), inspect(created)

        assert {c["name"] for c in m_insp.get_columns("approvals")} == {
            c["name"] for c in c_insp.get_columns("approvals")
        }
        assert {i["name"]: (i["unique"], i["column_names"]) for i in m_insp.get_indexes("approvals")} == {
            i["name"]: (i["unique"], i["column_names"]) for i in c_insp.get_indexes("approvals")
        }
        with created.connect() as conn:
            model_index_sql = conn.execute(
                text("SELECT sql FROM sqlite_master WHERE name = :n"), {"n": INDEX}
            ).scalar()
        assert "WHERE scope = 'workflow_node_run'" in model_index_sql
    finally:
        created.dispose()
