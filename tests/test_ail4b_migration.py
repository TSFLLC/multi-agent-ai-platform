"""AIL.4B migration (review_attempts + review_prompt_deliveries): real upgrade/downgrade on
DISPOSABLE SQLite databases only.

Every test points ``settings.database_path`` at a fresh file under pytest's
``tmp_path`` (and asserts it is not the persistent development database), so
nothing here can touch ``data/multi_agent_platform.db``.
"""

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from alembic import command
from app.config import settings
from app.db.base import Base
from app.db.session import build_engine

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BASE = "ail3c_experiment_conclusion"
HEAD = "ail4b_review_attempts"
NOW = "2026-01-01T00:00:00"
REAL_DB = PROJECT_ROOT / "data" / "multi_agent_platform.db"


def _cfg() -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    return cfg


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "ail4b-migration.db"
    monkeypatch.setattr(settings, "database_path", path)
    assert path.resolve() != REAL_DB.resolve() and "multi_agent_platform" not in settings.database_url
    return path


def _engine(path: Path):
    # The application's own engine builder, so foreign keys are enforced
    # exactly as they are in production (PRAGMA foreign_keys=ON).
    return build_engine(f"sqlite:///{path.as_posix()}")


def _seed(conn) -> None:
    conn.execute(text(f"INSERT INTO organizations (name, id, created_at) VALUES ('Org', 'org-1', '{NOW}')"))
    for user in ("user-1", "user-2"):
        conn.execute(text(
            f"INSERT INTO users (org_id, email, role, id, created_at) VALUES ('org-1', '{user}@example.com', 'member', '{user}', '{NOW}')"
        ))
    conn.execute(text(
        f"INSERT INTO concepts (slug, name, level, kind, is_core, id, created_at) "
        f"VALUES ('tokens', 'Tokens', 'foundational', 'definitional', 1, 'concept-1', '{NOW}')"
    ))
    conn.execute(text(
        f"INSERT INTO concept_versions (concept_id, version, plain_definition, content_origin, status, created_at, id) "
        f"VALUES ('concept-1', 1, 'def', 'ai_drafted_unreviewed', 'active', '{NOW}', 'cv-1')"
    ))
    conn.execute(text(
        "INSERT INTO learning_evidence (user_id, concept_id, concept_version_id, evidence_type, grader, "
        "on_demo_data, ref_type, created_at, id, passed) "
        f"VALUES ('user-1', 'concept-1', 'cv-1', 'knowledge_check', 'deterministic', 0, 'none', '{NOW}', 'ev-1', 1)"
    ))
    conn.execute(text(
        "INSERT INTO learning_evidence (user_id, concept_id, concept_version_id, evidence_type, grader, "
        "on_demo_data, ref_type, created_at, id, passed) "
        f"VALUES ('user-1', 'concept-1', 'cv-1', 'knowledge_check', 'deterministic', 0, 'none', '{NOW}', 'ev-2', 1)"
    ))


def _attempt(conn, attempt_id, *, status="started", user="user-1", completed=None, evidence=None):
    conn.execute(text(
        "INSERT INTO review_attempts (id, user_id, concept_id, concept_version_id, status, "
        "resulting_learning_evidence_id, started_at, completed_at) "
        f"VALUES ('{attempt_id}', '{user}', 'concept-1', 'cv-1', '{status}', "
        f"{'NULL' if evidence is None else repr(evidence)}, '{NOW}', "
        f"{'NULL' if completed is None else repr(completed)})"
    ))


def _delivery(conn, delivery_id, *, concept="concept-1", user="user-1", week="2026-09-14T00:00:00", slot=1):
    conn.execute(text(
        "INSERT INTO review_prompt_deliveries (id, user_id, concept_id, week_start, slot, prompt_kind, delivered_at) "
        f"VALUES ('{delivery_id}', '{user}', '{concept}', '{week}', {slot}, 'REVIEW_DUE', '{NOW}')"
    ))


def _clean(conn) -> None:
    assert conn.execute(text("PRAGMA integrity_check")).scalar() == "ok"
    assert conn.execute(text("PRAGMA foreign_key_check")).fetchall() == []


def _other_table_sql(conn) -> dict:
    return {
        name: sql
        for name, sql in conn.execute(text(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT IN ('review_attempts', 'review_prompt_deliveries', 'alembic_version')"
        )).fetchall()
    }


def test_single_linear_head_above_ail3c():
    script = ScriptDirectory.from_config(_cfg())
    assert script.get_heads() == ["ail4c_professor_agent_role"]
    assert script.get_revision(HEAD).down_revision == BASE
    assert script.get_revision("ail4c_professor_agent_role").down_revision == HEAD


def test_upgrade_is_purely_additive_and_keeps_existing_data(db_path):
    command.upgrade(_cfg(), BASE)
    engine = _engine(db_path)
    with engine.begin() as conn:
        _seed(conn)
        before_tables = _other_table_sql(conn)
        assert conn.execute(text("SELECT COUNT(*) FROM sqlite_master WHERE name = 'review_attempts'")).scalar() == 0
    engine.dispose()

    command.upgrade(_cfg(), HEAD)

    engine = _engine(db_path)
    with engine.begin() as conn:
        assert _other_table_sql(conn) == before_tables  # no existing table's definition was touched
        assert conn.execute(text("SELECT COUNT(*) FROM learning_evidence")).scalar() == 2
        columns = {row[1]: row for row in conn.execute(text("PRAGMA table_info(review_attempts)"))}
        assert set(columns) == {
            "id", "user_id", "concept_id", "concept_version_id", "learning_item_id", "status",
            "resulting_learning_evidence_id", "started_at", "completed_at",
        }
        assert columns["learning_item_id"][3] == 0 and columns["resulting_learning_evidence_id"][3] == 0  # nullable
        assert columns["concept_version_id"][3] == 1 and columns["status"][3] == 1  # required
        fks = {(fk[3], fk[2]) for fk in conn.execute(text("PRAGMA foreign_key_list(review_attempts)"))}
        assert fks == {
            ("user_id", "users"), ("concept_id", "concepts"), ("concept_version_id", "concept_versions"),
            ("learning_item_id", "learning_items"), ("resulting_learning_evidence_id", "learning_evidence"),
        }
        indexes = {row[1] for row in conn.execute(text("PRAGMA index_list(review_attempts)"))}
        assert {
            "uq_review_attempts_active", "uq_review_attempts_resulting_evidence", "ix_review_attempts_user_concept_started",
        } <= indexes

        delivery_columns = {row[1]: row for row in conn.execute(text("PRAGMA table_info(review_prompt_deliveries)"))}
        assert set(delivery_columns) == {"id", "user_id", "concept_id", "week_start", "slot", "prompt_kind", "delivered_at"}
        assert all(row[3] == 1 for row in delivery_columns.values() if row[1] != "id")  # every column required
        delivery_fks = {(fk[3], fk[2]) for fk in conn.execute(text("PRAGMA foreign_key_list(review_prompt_deliveries)"))}
        assert delivery_fks == {("user_id", "users"), ("concept_id", "concepts")}
        _clean(conn)
    engine.dispose()


def test_the_migrated_table_enforces_the_lifecycle(db_path):
    command.upgrade(_cfg(), HEAD)
    engine = _engine(db_path)
    with engine.begin() as conn:
        _seed(conn)
        _attempt(conn, "started-1")
        _attempt(conn, "passed-1", status="passed", completed=NOW, evidence="ev-1")
        _attempt(conn, "failed-1", status="failed", completed=NOW)
        _attempt(conn, "other-user", user="user-2")  # another learner's own in-progress review
        _clean(conn)

    refused = [
        ("passed without evidence", lambda c: _attempt(c, "x1", status="passed", completed=NOW)),
        ("passed without completion", lambda c: _attempt(c, "x2", status="passed", evidence="ev-2")),
        ("failed with evidence", lambda c: _attempt(c, "x3", status="failed", completed=NOW, evidence="ev-2")),
        ("started with completion", lambda c: _attempt(c, "x4", completed=NOW)),
        ("second started attempt", lambda c: _attempt(c, "x5")),
        ("evidence reused by another attempt", lambda c: _attempt(c, "x6", status="passed", completed=NOW, evidence="ev-1")),
        ("snoozed", lambda c: _attempt(c, "x7", status="snoozed", completed=NOW)),
        ("dismissed", lambda c: _attempt(c, "x8", status="dismissed", completed=NOW)),
        ("unknown evidence", lambda c: _attempt(c, "x9", status="passed", completed=NOW, evidence="nope")),
    ]
    for label, insert in refused:
        with pytest.raises(IntegrityError), engine.begin() as conn:
            insert(conn)
        assert label  # (the label makes a failing case identifiable in the report)
    with engine.begin() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM review_attempts")).scalar() == 4
        # A finished attempt frees the in-progress slot.
        conn.execute(text("UPDATE review_attempts SET status='failed', completed_at='2026-01-02T00:00:00' WHERE id='started-1'"))
        _attempt(conn, "started-2")
        _clean(conn)
    engine.dispose()


def test_the_migrated_delivery_table_enforces_two_prompts_per_user_per_week(db_path):
    command.upgrade(_cfg(), HEAD)
    engine = _engine(db_path)
    with engine.begin() as conn:
        _seed(conn)
        conn.execute(text(
            f"INSERT INTO concepts (slug, name, level, kind, is_core, id, created_at) "
            f"VALUES ('routing', 'Routing', 'foundational', 'operational', 1, 'concept-2', '{NOW}')"
        ))
        conn.execute(text(
            f"INSERT INTO concepts (slug, name, level, kind, is_core, id, created_at) "
            f"VALUES ('agents', 'Agents', 'foundational', 'architectural', 1, 'concept-3', '{NOW}')"
        ))
        _delivery(conn, "d1", concept="concept-1", slot=1)
        _delivery(conn, "d2", concept="concept-2", slot=2)
        _delivery(conn, "d-next-week", concept="concept-1", week="2026-09-21T00:00:00", slot=1)  # a new week is fresh
        _delivery(conn, "d-other-user", concept="concept-1", user="user-2", slot=1)  # another user has their own quota
        _clean(conn)

    refused = [
        ("a third delivery (slot 3)", lambda c: _delivery(c, "x1", concept="concept-3", slot=3)),
        ("slot zero", lambda c: _delivery(c, "x2", concept="concept-3", slot=0)),
        ("slot 1 reused in the same week", lambda c: _delivery(c, "x3", concept="concept-3", slot=1)),
        ("slot 2 reused in the same week", lambda c: _delivery(c, "x4", concept="concept-3", slot=2)),
        ("the same concept twice in one week", lambda c: _delivery(c, "x5", concept="concept-1", week="2026-09-21T00:00:00", slot=2)),
        ("an unknown concept", lambda c: _delivery(c, "x6", concept="nope", week="2026-09-28T00:00:00", slot=1)),
        ("an unknown user", lambda c: _delivery(c, "x7", user="nobody", week="2026-09-28T00:00:00", slot=1)),
    ]
    for label, insert in refused:
        with pytest.raises(IntegrityError), engine.begin() as conn:
            insert(conn)
        assert label
    with engine.begin() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM review_prompt_deliveries")).scalar() == 4
        _clean(conn)
    engine.dispose()


def test_downgrade_refuses_while_attempts_exist_then_reverses_cleanly_and_reupgrades(db_path):
    command.upgrade(_cfg(), HEAD)
    engine = _engine(db_path)
    with engine.begin() as conn:
        _seed(conn)
        _attempt(conn, "started-1")
    engine.dispose()

    with pytest.raises(RuntimeError, match="review attempt"):
        command.downgrade(_cfg(), BASE)
    engine = _engine(db_path)
    with engine.begin() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM review_attempts")).scalar() == 1  # changed nothing
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == HEAD
        conn.execute(text("DELETE FROM review_attempts"))
        _delivery(conn, "delivery-1")
    engine.dispose()

    with pytest.raises(RuntimeError, match="prompt delivery"):  # deliveries alone also block a downgrade
        command.downgrade(_cfg(), BASE)
    engine = _engine(db_path)
    with engine.begin() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM review_prompt_deliveries")).scalar() == 1  # changed nothing
        assert conn.execute(text("SELECT COUNT(*) FROM sqlite_master WHERE name = 'review_attempts'")).scalar() == 1
        conn.execute(text("DELETE FROM review_prompt_deliveries"))
    engine.dispose()

    command.downgrade(_cfg(), BASE)
    engine = _engine(db_path)
    with engine.begin() as conn:
        assert conn.execute(text(
            "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE '%review_attempts%' OR name LIKE '%review_prompt_deliveries%'"
        )).scalar() == 0
        assert conn.execute(text("SELECT COUNT(*) FROM learning_evidence")).scalar() == 2  # learner evidence untouched
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == BASE
        _clean(conn)
    engine.dispose()

    command.upgrade(_cfg(), HEAD)
    engine = _engine(db_path)
    with engine.begin() as conn:
        _attempt(conn, "again")
        _clean(conn)
    engine.dispose()


def test_the_orm_models_and_the_migration_describe_the_same_tables(db_path):
    command.upgrade(_cfg(), HEAD)
    engine = _engine(db_path)
    for name in ("review_attempts", "review_prompt_deliveries"):
        table = Base.metadata.tables[name]
        with engine.begin() as conn:
            info = {row[1]: row for row in conn.execute(text(f"PRAGMA table_info({name})"))}
            indexes = {row[1] for row in conn.execute(text(f"PRAGMA index_list({name})"))}
            unique = {
                tuple(col[2] for col in conn.execute(text(f"PRAGMA index_info({row[1]})")))
                for row in conn.execute(text(f"PRAGMA index_list({name})"))
                if row[2]
            }
        assert set(info) == {c.name for c in table.columns}, name
        assert {n: bool(info[n][3]) for n in info} == {c.name: not c.nullable for c in table.columns}, name
        assert {i.name for i in table.indexes} <= indexes, name
        for constraint in table.constraints:
            if constraint.__class__.__name__ == "UniqueConstraint":
                assert tuple(c.name for c in constraint.columns) in unique, (name, constraint.name)
    engine.dispose()


def test_full_chain_round_trips_on_a_disposable_database(db_path):
    """Upgrade from empty to head and back to base — on tmp_path only."""
    command.upgrade(_cfg(), "head")
    command.downgrade(_cfg(), "base")
    engine = _engine(db_path)
    with engine.begin() as conn:
        tables = {r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))} - {"alembic_version"}
    engine.dispose()
    assert tables == set()
    command.upgrade(_cfg(), "head")
