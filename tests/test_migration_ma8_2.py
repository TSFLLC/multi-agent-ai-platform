"""MA8.2 migration (8c3f6b2e9d14): evidence-query indexes + router policy v1.

DISPOSABLE DATABASES ONLY — the ``disposable_db`` fixture (same shape as the
MA8.1 migration tests') points ``settings.database_path`` at a ``tmp_path``
file and fails outright if it still names data/multi_agent_platform.db.
"""

import json
from pathlib import Path

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.orm import sessionmaker

from alembic import command
from app.config import settings
from app.db.session import build_engine
from app.model_resolution import record_routing_decision, route
from app.routing_evidence import DEFAULT_V1_CONFIG, RoutingContext, load_active_policy
from tests.test_migration_ma8_1 import REAL_DB, _cfg, _seed_decision_referenced_by_a_model_call


@pytest.fixture()
def disposable_db(tmp_path, monkeypatch):
    db_path = tmp_path / "ma82_migration.db"
    monkeypatch.setattr(settings, "database_path", db_path)
    assert Path(settings.database_path).resolve() != REAL_DB.resolve()
    assert "multi_agent_platform" not in settings.database_url
    engine = build_engine(f"sqlite:///{db_path.as_posix()}")
    yield engine
    engine.dispose()


PREVIOUS = "5e1d8a4c7b30"
REVISION = "8c3f6b2e9d14"
INDEXES = {
    "model_calls": "ix_model_calls_provider_model_id_started_at",
    "evaluation_runs": "ix_evaluation_runs_subject_agent_run_id",
}


def _index_names(engine, table):
    return {ix["name"] for ix in inspect(engine).get_indexes(table)}


def _policy_rows(engine):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT version, status, scoring_config_json FROM router_policy_versions")
        ).all()


def _add_wave1_orm_compat_columns(engine):
    """The historical MA8.2 fixture uses the current ORM before Wave-1.

    Add only the later Project columns needed by that ORM; the actual
    Wave-1 migration remains the owner of these columns in production.
    """
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE projects ADD COLUMN kind VARCHAR(20) NOT NULL DEFAULT 'standard'"))
        conn.execute(text("ALTER TABLE projects ADD COLUMN ail_evidence_opt_in BOOLEAN NOT NULL DEFAULT 0"))

def test_ma8_2_is_followed_by_ail1a_in_the_integrated_chain():
    """MA8.2 must be a direct, uncontested descendant of MA8.1 — no
    branching. This intentionally no longer asserts MA8.2 is the *global*
    chain head: AIL.1B (0f87701fabff) is a later, additive migration on top
    of it, which is expected and does not touch MA8.2's own schema."""
    script = ScriptDirectory.from_config(_cfg())
    revision = script.get_revision(REVISION)
    assert revision.down_revision == PREVIOUS
    assert script.get_revision(REVISION).nextrev == frozenset({"ef873dac62a1"})


def _set_status(engine, status):
    with engine.begin() as conn:
        conn.execute(text("UPDATE router_policy_versions SET status = :s WHERE version = 1"), {"s": status})


def test_upgrade_adds_indexes_and_seeds_v1_inactive_so_routing_stays_ma8_1(disposable_db):
    engine = disposable_db
    command.upgrade(_cfg(), PREVIOUS)
    _add_wave1_orm_compat_columns(engine)
    _decision_id, call_id = _seed_decision_referenced_by_a_model_call(engine)
    command.upgrade(_cfg(), REVISION)

    for table, name in INDEXES.items():
        assert name in _index_names(engine, table)
    ((version, status, config),) = _policy_rows(engine)
    assert (version, status) == (1, "inactive")
    assert (config if isinstance(config, dict) else json.loads(config)) == DEFAULT_V1_CONFIG
    with engine.connect() as conn:
        assert (
            conn.execute(text("SELECT count(*) FROM model_calls WHERE id = :id"), {"id": call_id}).scalar()
            == 1
        )

    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        assert load_active_policy(session) is None  # installed, not active
        decision = route(session, {"mode": "auto", "auto_policy": "any"}, context=RoutingContext("p", "r"))
        assert decision.evidence == {"status": "disabled"} and decision.router_policy_version_id is None

        _set_status(engine, "active")  # explicit operator activation (MA8.3/MA8.4 will own this)
        active = load_active_policy(session)
        assert active.version == 1 and active.config.min_observations == DEFAULT_V1_CONFIG["min_observations"]
    finally:
        session.close()


def test_downgrade_refuses_while_a_decision_references_v1_and_otherwise_reverts(disposable_db):
    engine = disposable_db
    command.upgrade(_cfg(), PREVIOUS)
    _add_wave1_orm_compat_columns(engine)
    _seed_decision_referenced_by_a_model_call(engine)
    command.upgrade(_cfg(), REVISION)

    # Once activated, an evidence-routed decision references v1.
    _set_status(engine, "active")
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        with engine.connect() as conn:
            agent_run_id = conn.execute(text("SELECT id FROM agent_runs LIMIT 1")).scalar()
        decision = route(session, {"mode": "auto", "auto_policy": "any"}, context=RoutingContext("p", "r"))
        row = record_routing_decision(session, agent_run_id=agent_run_id, decision=decision)
        session.commit()
        assert row.router_policy_version_id is not None
        referencing_id = row.id
    finally:
        session.close()

    with pytest.raises(RuntimeError, match="Refusing to downgrade"):
        command.downgrade(_cfg(), PREVIOUS)
    assert len(_policy_rows(engine)) == 1
    for table, name in INDEXES.items():
        assert name in _index_names(engine, table)

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM model_routing_decisions WHERE id = :id"), {"id": referencing_id})
    command.downgrade(_cfg(), PREVIOUS)
    assert _policy_rows(engine) == []
    for table, name in INDEXES.items():
        assert name not in _index_names(engine, table)
    command.upgrade(_cfg(), REVISION)  # re-applicable
    assert len(_policy_rows(engine)) == 1
