"""MA8.1 migration (5e1d8a4c7b30): routing-decision audit columns.

DISPOSABLE DATABASES ONLY. Every test migrates a fresh ``tmp_path`` SQLite
file; the ``disposable_db`` fixture points ``settings.database_path`` (which
alembic/env.py reads) away from data/multi_agent_platform.db — that file
holds real UAT data and must never be migrated, downgraded or otherwise
touched by a test — and fails outright if the settings still point at it.

The table is a parent of ``model_calls.model_routing_decision_id``, so the
tests migrate a database holding a decision row that a model call
references, in both directions, and prove neither row is lost.
"""

import uuid
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.orm import sessionmaker

from alembic import command
from app.config import settings
from app.db.enums import ModelSelectionMode
from app.db.session import build_engine
from app.models.execution import ModelRoutingDecision
from tests.test_migration_ma7_5a import _populate

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PREVIOUS_HEAD = "c7f4a2d9e815"  # also where the persistent UAT DB sits at MA8.1 start
REVISION = "5e1d8a4c7b30"
REAL_DB = PROJECT_ROOT / "data" / "multi_agent_platform.db"
NEW_COLUMNS = {"requested_policy", "routing_strategy", "decision_json"}


def _cfg() -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    return cfg


@pytest.fixture()
def disposable_db(tmp_path, monkeypatch):
    db_path = tmp_path / "ma81_migration.db"
    monkeypatch.setattr(settings, "database_path", db_path)
    assert Path(settings.database_path).resolve() != REAL_DB.resolve()
    assert "multi_agent_platform" not in settings.database_url
    engine = build_engine(f"sqlite:///{db_path.as_posix()}")
    yield engine
    engine.dispose()


def _columns(engine):
    return {c["name"] for c in inspect(engine).get_columns("model_routing_decisions")}


def _seed_decision_referenced_by_a_model_call(engine):
    """Pre-MA8.1-shaped decision row + a model call pointing at it."""
    _populate(engine, "m81")
    decision_id, call_id = str(uuid.uuid4()), str(uuid.uuid4())
    with engine.begin() as conn:
        agent_run_id = conn.execute(text("SELECT id FROM agent_runs LIMIT 1")).scalar()
        model_id, provider_id, pm_id = conn.execute(
            text("SELECT model_id, provider_id, id FROM provider_models LIMIT 1")
        ).one()
        conn.execute(
            text(
                "INSERT INTO model_routing_decisions (id, agent_run_id, selection_mode, rationale, created_at) "
                "VALUES (:id, :run, 'manual', 'seeded', CURRENT_TIMESTAMP)"
            ),
            {"id": decision_id, "run": agent_run_id},
        )
        conn.execute(
            text(
                "INSERT INTO model_calls (id, agent_run_id, model_id, provider_id, provider_model_id, "
                "model_routing_decision_id, cost_currency, cost_is_estimated, status, started_at) "
                "VALUES (:id, :run, :m, :p, :pm, :d, 'USD', 1, 'success', CURRENT_TIMESTAMP)"
            ),
            {
                "id": call_id,
                "run": agent_run_id,
                "m": model_id,
                "p": provider_id,
                "pm": pm_id,
                "d": decision_id,
            },
        )
    return decision_id, call_id


def _link(engine, call_id):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT model_routing_decision_id FROM model_calls WHERE id = :id"), {"id": call_id}
        ).scalar()


def _fk_violations(engine):
    with engine.connect() as conn:
        return conn.execute(text("PRAGMA foreign_key_check")).fetchall()


def test_revision_follows_ma7_5a_in_the_chain():
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(_cfg())
    assert script.get_revision(REVISION).down_revision == PREVIOUS_HEAD
    assert REVISION in {r.revision for r in script.walk_revisions()}


def test_upgrade_adds_nullable_columns_and_keeps_existing_rows(disposable_db):
    engine = disposable_db
    command.upgrade(_cfg(), PREVIOUS_HEAD)
    assert not (_columns(engine) & NEW_COLUMNS)
    decision_id, call_id = _seed_decision_referenced_by_a_model_call(engine)

    command.upgrade(_cfg(), REVISION)

    assert NEW_COLUMNS <= _columns(engine)
    assert _link(engine, call_id) == decision_id
    assert _fk_violations(engine) == []
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        old = session.get(ModelRoutingDecision, decision_id)
        assert old.rationale == "seeded" and old.details is None and old.requested_policy is None
        row = ModelRoutingDecision(
            agent_run_id=old.agent_run_id,
            selection_mode=ModelSelectionMode.AUTO,
            requested_policy="free_only",
            routing_strategy="ma8.1-deterministic-v1",
            details={"outcome": "failed"},
        )
        session.add(row)
        session.commit()
        assert session.get(ModelRoutingDecision, row.id).details == {"outcome": "failed"}
    finally:
        session.close()


def test_downgrade_drops_only_the_new_columns_without_losing_referenced_rows(disposable_db):
    engine = disposable_db
    command.upgrade(_cfg(), PREVIOUS_HEAD)
    decision_id, call_id = _seed_decision_referenced_by_a_model_call(engine)
    command.upgrade(_cfg(), REVISION)

    command.downgrade(_cfg(), PREVIOUS_HEAD)

    assert not (_columns(engine) & NEW_COLUMNS)
    assert _link(engine, call_id) == decision_id
    assert _fk_violations(engine) == []
    command.upgrade(_cfg(), REVISION)  # and re-applicable
    assert NEW_COLUMNS <= _columns(engine)
