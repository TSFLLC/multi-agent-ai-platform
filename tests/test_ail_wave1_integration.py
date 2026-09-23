from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from app.config import settings
from app.db.session import build_engine
from app.db.enums import ConceptKind, ConceptLevel
from app.models.concepts import Concept
from app.models.radar import Development


PROJECT_ROOT = Path(__file__).resolve().parent.parent
REAL_DB = PROJECT_ROOT / "data" / "multi_agent_platform.db"


def _cfg() -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    return cfg


@pytest.fixture()
def migration_db(tmp_path, monkeypatch):
    db_path = tmp_path / "ail_wave1.db"
    monkeypatch.setattr(settings, "database_path", db_path)
    assert db_path.resolve() != REAL_DB.resolve()
    assert "multi_agent_platform" not in settings.database_url
    engine = build_engine(f"sqlite:///{db_path.as_posix()}")
    yield engine
    engine.dispose()


def test_final_chain_is_linear_and_has_one_head():
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(_cfg())
    assert [head.revision for head in script.get_revisions("heads")] == [
        "ail2b_radar_intelligence"
    ]
    assert script.get_revision("8c3f6b2e9d14").down_revision == "5e1d8a4c7b30"
    assert script.get_revision("ef873dac62a1").down_revision == "8c3f6b2e9d14"
    assert script.get_revision("0f87701fabff").down_revision == "ef873dac62a1"
    assert script.get_revision("ail2a_radar_ledger").down_revision == "0f87701fabff"
    assert script.get_revision("ail_wave1_integration").down_revision == "ail2a_radar_ledger"
    assert script.get_revision("ail2b_radar_intelligence").down_revision == "ail_wave1_integration"


def test_fresh_and_ma8_2_upgrade_create_wave1_contracts(migration_db):
    command.upgrade(_cfg(), "head")
    inspector = inspect(migration_db)
    project_columns = {column["name"] for column in inspector.get_columns("projects")}
    assert {"kind", "ail_evidence_opt_in"} <= project_columns
    assert inspector.has_table("development_concepts")

    with migration_db.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM development_concepts")).scalar() == 0

    command.downgrade(_cfg(), "ail2a_radar_ledger")
    assert not inspect(migration_db).has_table("development_concepts")
    project_columns = {column["name"] for column in inspect(migration_db).get_columns("projects")}
    assert "kind" not in project_columns
    assert "ail_evidence_opt_in" not in project_columns


def test_wave1_upgrade_creates_ail2b_intelligence_tables(migration_db):
    command.upgrade(_cfg(), "ail_wave1_integration")
    assert not inspect(migration_db).has_table("attention_samples")
    assert not inspect(migration_db).has_table("triage_decisions")
    command.upgrade(_cfg(), "head")
    inspector = inspect(migration_db)
    assert {"development_terms", "attention_samples", "triage_decisions"} <= set(inspector.get_table_names())

    command.upgrade(_cfg(), "8c3f6b2e9d14")
    command.upgrade(_cfg(), "head")
    assert inspect(migration_db).has_table("development_concepts")


def test_project_defaults_and_concept_link_review_api(client, db, auth_headers, bootstrap):
    concept = Concept(
        slug="model-routing",
        name="Model Routing",
        level=ConceptLevel.FOUNDATIONAL,
        kind=ConceptKind.MECHANISM,
    )
    development = Development(
        title="A new routing capability",
        development_type="capability_change",
        candidate_key="routing-capability-1",
    )
    db.add_all([concept, development])
    db.commit()

    project_response = client.post(
        "/projects",
        headers=auth_headers,
        json={"org_id": bootstrap.organization.id, "name": "Standard"},
    )
    assert project_response.status_code == 201
    assert project_response.json()["kind"] == "standard"
    assert project_response.json()["ail_evidence_opt_in"] is False

    path = f"/radar/developments/{development.id}/concepts"
    proposed = client.post(
        path,
        headers=auth_headers,
        json={"concept_id": concept.id},
    )
    assert proposed.status_code == 201
    assert proposed.json()["state"] == "proposed"
    assert proposed.json()["proposed_by"] == "user"

    listed = client.get(path, headers=auth_headers)
    assert listed.status_code == 200
    assert len(listed.json()) == 1

    confirmed = client.post(
        f"{path}/{concept.id}/confirm",
        headers=auth_headers,
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["state"] == "confirmed"

    rejected = client.post(
        f"{path}/{concept.id}/reject",
        headers=auth_headers,
    )
    assert rejected.status_code == 409
