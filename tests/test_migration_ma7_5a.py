"""MA7.5A migration (c7f4a2d9e815): widen ``workflow_nodes.node_type``'s CHECK to
allow 'evaluation'.

DISPOSABLE DATABASES ONLY. Every test migrates a fresh ``tmp_path`` SQLite file;
the ``disposable_db`` fixture redirects alembic/env.py (which reads
``settings.database_url``) away from data/multi_agent_platform.db -- that file
holds real UAT data at bd27cdb01c15 and must never be migrated, downgraded or
otherwise touched by a test -- and fails the test outright if the settings still
point at it.

The interesting risk is not the CHECK text, it is the rebuild: ``workflow_nodes``
is a PARENT table (edges cascade from it, node runs reference it). So these
tests migrate POPULATED databases -- including the real UAT path, where
bd27cdb01c15 goes straight to the new head through a3c9e17b5d42 in one run --
and prove no child row is lost.
"""

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from alembic import command
from app.bootstrap import ensure_local_bootstrap
from app.config import settings
from app.db.base import Base
from app.db.session import build_engine
from app.services.workflow_execution_service import WorkflowExecutionService
from tests.ma7_3b_support import AGENT, TERMINAL
from tests.ma7_4b_support import build_graph

PROJECT_ROOT = Path(__file__).resolve().parent.parent
UAT_HEAD = "bd27cdb01c15"  # where the persistent product DB intentionally sits
PREVIOUS_HEAD = "a3c9e17b5d42"
REVISION = "c7f4a2d9e815"
REAL_DB = PROJECT_ROOT / "data" / "multi_agent_platform.db"
OLD_TYPES = (
    "agent",
    "parallel_group",
    "conditional",
    "repair_loop",
    "judge",
    "consensus",
    "human_approval",
    "terminal",
)


def _cfg() -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    return cfg


@pytest.fixture()
def disposable_db(tmp_path, monkeypatch):
    db_path = tmp_path / "ma75a_migration.db"
    monkeypatch.setattr(settings, "database_path", db_path)
    assert Path(settings.database_path).resolve() != REAL_DB.resolve()
    assert "multi_agent_platform" not in settings.database_url
    engine = build_engine(f"sqlite:///{db_path.as_posix()}")  # the app's own PRAGMA foreign_keys=ON engine
    yield db_path, engine
    engine.dispose()


def _revision(engine) -> str:
    with engine.connect() as conn:
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()


def _counts(engine):
    with engine.connect() as conn:
        return {
            table: conn.execute(text(f"SELECT count(*) FROM {table}")).scalar()
            for table in ("workflow_nodes", "workflow_edges", "workflow_node_runs", "agent_runs", "job_queue")
    }


def _add_wave1_orm_compat_columns(engine):
    """Historical MA7.5a fixtures use the current ORM before Wave-1."""
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE projects ADD COLUMN kind VARCHAR(20) NOT NULL DEFAULT 'standard'"))
        conn.execute(text("ALTER TABLE projects ADD COLUMN ail_evidence_opt_in BOOLEAN NOT NULL DEFAULT 0"))
        # AIL.3A's current ORM is also used by historical migration fixtures
        # that intentionally stop before AIL.3A. Keep those disposable
        # fixtures compatible without changing the historical schema owner.
        conn.execute(text("ALTER TABLE task_runs ADD COLUMN experiment_id VARCHAR(36)"))


def _populate(engine, label):
    """A real, started workflow (nodes, edges, node runs, an AgentRun and a job)
    written through the ORM into an already-migrated database."""
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        identities = ensure_local_bootstrap(session)
        session.commit()
        graph = build_graph(
            session,
            identities.project,
            [("a", AGENT), ("b", AGENT), ("c", AGENT), ("end", TERMINAL)],
            [("a", "b"), ("a", "c"), ("b", "end"), ("c", "end")],
            label=label,
        )
        run = WorkflowExecutionService(session).start_workflow_run(graph.built.published.id, graph.built.task_run.id)
        session.commit()
        return graph, run
    finally:
        session.close()


def _ddl(engine):
    with engine.connect() as conn:
        return conn.execute(text("SELECT sql FROM sqlite_master WHERE name = 'workflow_nodes'")).scalar()


def _insert_node(conn, node_id, node_type, version_id, max_iterations=None):
    conn.execute(
        text(
            "INSERT INTO workflow_nodes (id, workflow_version_id, node_key, node_type, max_iterations) "
            "VALUES (:id, :v, :k, :t, :m)"
        ),
        {"id": node_id, "v": version_id, "k": f"k-{node_id}", "t": node_type, "m": max_iterations},
    )


def _version_id(engine):
    with engine.connect() as conn:
        return conn.execute(text("SELECT workflow_version_id FROM workflow_nodes LIMIT 1")).scalar()


def test_revision_chain_places_this_migration_directly_after_a3c9e17b5d42():
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(_cfg())
    assert script.get_revision(REVISION).down_revision == PREVIOUS_HEAD
    assert REVISION in {revision.revision for revision in script.walk_revisions()}


def test_upgrade_widens_only_the_node_type_check_and_keeps_the_repair_loop_check(disposable_db):
    _, engine = disposable_db
    command.upgrade(_cfg(), PREVIOUS_HEAD)
    _add_wave1_orm_compat_columns(engine)
    graph, _run = _populate(engine, "chk")
    version_id = _version_id(engine)
    before = _ddl(engine)
    assert "'evaluation'" not in before

    command.upgrade(_cfg(), REVISION)

    ddl = _ddl(engine)
    for value in OLD_TYPES + ("evaluation",):
        assert f"'{value}'" in ddl  # every historical type survives, plus the new one
    assert ddl.count("ck_workflow_nodes_repair_loop_requires_max_iterations") == 1
    assert ddl.count("ck_workflow_nodes_workflownodetype") == 1
    assert "uq_workflow_nodes_version_id_node_key" in ddl
    assert "fk_workflow_nodes_workflow_version_id_workflow_versions" in ddl
    # no new column, no new table
    assert {c["name"] for c in inspect(engine).get_columns("workflow_nodes")} == {
        "id",
        "workflow_version_id",
        "node_key",
        "node_type",
        "config_json",
        "max_iterations",
        "timeout_seconds",
    }

    with engine.begin() as conn:
        _insert_node(conn, "ev-ok", "evaluation", version_id)
        _insert_node(conn, "loop-ok", "repair_loop", version_id, max_iterations=2)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        _insert_node(conn, "bogus", "not_a_node_type", version_id)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        _insert_node(conn, "loop-bad", "repair_loop", version_id, max_iterations=None)  # CHECK preserved
    assert graph is not None


def test_a_populated_database_keeps_every_node_edge_and_node_run(disposable_db):
    _, engine = disposable_db
    command.upgrade(_cfg(), PREVIOUS_HEAD)
    _add_wave1_orm_compat_columns(engine)
    _populate(engine, "pop")
    before = _counts(engine)
    assert before["workflow_edges"] == 4 and before["workflow_node_runs"] == 4  # a real graph, really started

    command.upgrade(_cfg(), REVISION)

    assert _counts(engine) == before  # nothing cascaded away, nothing orphaned
    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1  # the app's engine still enforces FKs
        types = {row[0] for row in conn.execute(text("SELECT node_type FROM workflow_nodes"))}
    assert types == {"agent", "terminal"}
    assert _revision(engine) == REVISION


def test_the_real_uat_path_bd27cdb01c15_straight_to_the_new_revision(disposable_db):
    """The persistent DB sits at bd27cdb01c15: its upgrade runs a3c9e17b5d42 AND this
    migration in ONE alembic run (so the PRAGMA must survive an earlier
    migration's open transaction). Populated, as a UAT database is."""
    _, engine = disposable_db
    command.upgrade(_cfg(), UAT_HEAD)
    _add_wave1_orm_compat_columns(engine)
    _populate(engine, "uat")
    before = _counts(engine)

    command.upgrade(_cfg(), REVISION)

    assert _revision(engine) == REVISION
    assert _counts(engine) == before
    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
    assert "'evaluation'" in _ddl(engine)


def test_downgrade_refuses_while_an_evaluation_node_exists_and_changes_nothing(disposable_db):
    _, engine = disposable_db
    command.upgrade(_cfg(), REVISION)
    _add_wave1_orm_compat_columns(engine)
    _populate(engine, "dg1")
    with engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=ON"))
        _insert_node(conn, "ev-1", "evaluation", _version_id(engine))
    before, ddl_before = _counts(engine), _ddl(engine)

    with pytest.raises(RuntimeError, match="EVALUATION workflow node"):
        command.downgrade(_cfg(), PREVIOUS_HEAD)

    assert _revision(engine) == REVISION  # still at the new revision
    assert _counts(engine) == before and _ddl(engine) == ddl_before  # nothing rebuilt, nothing deleted


def test_downgrade_without_evaluation_nodes_restores_the_narrow_check_and_keeps_data(disposable_db):
    _, engine = disposable_db
    command.upgrade(_cfg(), REVISION)
    _add_wave1_orm_compat_columns(engine)
    _populate(engine, "dg2")
    before = _counts(engine)

    command.downgrade(_cfg(), PREVIOUS_HEAD)

    assert _revision(engine) == PREVIOUS_HEAD
    assert _counts(engine) == before
    ddl = _ddl(engine)
    assert "'evaluation'" not in ddl and "ck_workflow_nodes_repair_loop_requires_max_iterations" in ddl
    with pytest.raises(IntegrityError), engine.begin() as conn:
        _insert_node(conn, "ev-x", "evaluation", _version_id(engine))
    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []


def test_upgrade_downgrade_upgrade_round_trip_is_stable(disposable_db):
    _, engine = disposable_db
    command.upgrade(_cfg(), REVISION)
    _add_wave1_orm_compat_columns(engine)
    _populate(engine, "rt")
    first_ddl, counts = _ddl(engine), _counts(engine)

    command.downgrade(_cfg(), PREVIOUS_HEAD)
    command.upgrade(_cfg(), REVISION)

    assert _revision(engine) == REVISION and _counts(engine) == counts
    assert sorted(_ddl(engine).split("\n")) == sorted(first_ddl.split("\n"))  # same constraints, any order


def test_full_chain_downgrade_to_base_and_back(disposable_db):
    _, engine = disposable_db
    command.upgrade(_cfg(), REVISION)
    command.downgrade(_cfg(), "base")
    assert set(inspect(engine).get_table_names()) - {"alembic_version"} == set()
    command.upgrade(_cfg(), REVISION)
    assert _revision(engine) == REVISION


def test_migrated_schema_matches_the_model(disposable_db, tmp_path):
    """create_all (what the test suite uses) and the migration chain (what
    production uses) must agree on workflow_nodes."""
    _, migrated = disposable_db
    command.upgrade(_cfg(), REVISION)

    from app import models  # noqa: F401 -- populates Base.metadata

    created = create_engine(f"sqlite:///{(tmp_path / 'create_all.db').as_posix()}")
    try:
        Base.metadata.create_all(created)
        checks = []
        for engine in (migrated, created):
            constraints = inspect(engine).get_check_constraints("workflow_nodes")
            checks.append({c["name"]: sorted(c["sqltext"].replace(" ", "").split("'")[1::2]) for c in constraints})
            assert {c["name"] for c in inspect(engine).get_columns("workflow_nodes")} == {
                "id",
                "workflow_version_id",
                "node_key",
                "node_type",
                "config_json",
                "max_iterations",
                "timeout_seconds",
            }
        assert checks[0]["ck_workflow_nodes_workflownodetype"] == checks[1]["ck_workflow_nodes_workflownodetype"]
        assert "evaluation" in checks[0]["ck_workflow_nodes_workflownodetype"]
        assert set(checks[0]) == set(checks[1])
    finally:
        created.dispose()


def test_negative_control_a_naive_rebuild_under_foreign_keys_on_is_destructive(disposable_db, tmp_path):
    """Why the migration pauses foreign keys: the SAME rebuild without that,
    on a populated database, either fails outright or destroys child rows. If
    this ever stops being true the migration's extra care is unnecessary --
    and this test is the alarm."""
    db_path, engine = disposable_db
    command.upgrade(_cfg(), PREVIOUS_HEAD)
    _add_wave1_orm_compat_columns(engine)
    _populate(engine, "naive")
    before = _counts(engine)
    engine.dispose()

    naive = build_engine(f"sqlite:///{db_path.as_posix()}")
    failed = False
    try:
        with naive.connect() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                from alembic import op

                try:
                    with op.batch_alter_table("workflow_nodes") as batch:
                        batch.alter_column(
                            "node_type",
                            existing_type=sa.Enum(
                                *OLD_TYPES, name="workflownodetype", native_enum=False, create_constraint=True
                            ),
                            type_=sa.Enum(
                                *OLD_TYPES,
                                "evaluation",
                                name="workflownodetype",
                                native_enum=False,
                                create_constraint=True,
                            ),
                            existing_nullable=False,
                        )
                    conn.commit()
                except IntegrityError:
                    failed = True
                    conn.rollback()
            after = {
                table: conn.execute(text(f"SELECT count(*) FROM {table}")).scalar()
                for table in ("workflow_nodes", "workflow_edges", "workflow_node_runs")
            }
    finally:
        naive.dispose()
    assert failed or after["workflow_edges"] < before["workflow_edges"] or after["workflow_node_runs"] < before[
        "workflow_node_runs"
    ]
