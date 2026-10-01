"""AIL.5D.5 migration ``academy_practice_instances`` on every path a database can arrive by.

One head on top of ``academy_professor_help``; an already-migrated database runs ONLY the new revision and loses nothing; a
fresh chain reaches the head; the tables equal the models; the database enforces the row invariants; downgrade follows
docs/deployment/migration-rollback-policy.md (reversible, refuses while practice rows exist). Disposable temp databases only.
"""

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from alembic import command
from app.db.base import Base
from tests.test_ail5d1_migrations import (  # noqa: F401  (the path fixture is shared)
    DEPLOYED, NOW, _cfg, _content_hash, _engine, _health, _run, _seed, _snapshot, _tables, _version, path,
)

PREVIOUS = "academy_professor_help"
NEW = "academy_practice_instances"
TABLES = {"academy_practice_instances", "academy_practice_runs"}


def _instance(conn, **over):
    row = {"id": "pi-1", "u": "u-a", "i": "item-0", "lin": "00000000-0000-4000-8000-000000000000", "k": "lab-run", "mode": "guided",
           "kit": "day5-grounding-lab", "kv": 1, "sc": "grounded-vs-ungrounded", "sha": "f" * 64, "j": "{}", "st": "created", "att": 1, "mx": 6, "n": NOW}
    row.update(over)
    conn.execute(text(
        "INSERT INTO academy_practice_instances (id, user_id, learning_item_id, lineage_id, step_key, mode, kit_key, kit_version, scenario_key, "
        "scenario_sha256, scenario_json, status, attempt_no, max_runs, created_at) VALUES (:id, :u, :i, :lin, :k, :mode, :kit, :kv, :sc, :sha, :j, :st, :att, :mx, :n)"), row)


def test_there_is_one_head_above_the_professor_help_revision():
    script = ScriptDirectory.from_config(_cfg())
    assert script.get_heads() == [NEW]
    assert script.get_revision(NEW).down_revision == PREVIOUS
    assert script.get_revision(PREVIOUS).down_revision == "academy_step_response"
    assert not NEW.startswith("ail5d_") and script.get_revision(DEPLOYED) is not None


def test_upgrading_from_the_previous_head_runs_only_the_new_revision_and_loses_nothing(path):
    command.upgrade(_cfg(), PREVIOUS)
    engine = _engine(path)
    _seed(engine)
    before = _content_hash(engine)
    engine.dispose()
    assert _run("upgrade", "head") == [f"Running upgrade {PREVIOUS} -> {NEW}"]
    engine = _engine(path)
    assert _version(engine) == NEW and TABLES <= _tables(engine) and _content_hash(engine) == before
    with engine.begin() as conn:
        for table in TABLES:
            assert conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one() == 0
    _health(engine)
    engine.dispose()


def test_the_whole_chain_on_a_fresh_database_reaches_the_head(path):
    steps = _run("upgrade", "head")
    assert steps[-1] == f"Running upgrade {PREVIOUS} -> {NEW}" and any(DEPLOYED in s for s in steps)
    engine = _engine(path)
    assert _version(engine) == NEW and TABLES | {"academy_professor_help", "academy_step_responses"} <= _tables(engine)
    _health(engine)
    engine.dispose()


def test_the_new_revision_changes_no_existing_table(path):
    command.upgrade(_cfg(), PREVIOUS)
    engine = _engine(path)
    with engine.begin() as conn:
        before = {r[0]: r[1] for r in conn.execute(text("SELECT name, sql FROM sqlite_master WHERE tbl_name <> 'alembic_version'"))}
    engine.dispose()
    command.upgrade(_cfg(), NEW)
    engine = _engine(path)
    with engine.begin() as conn:
        after = {r[0]: r[1] for r in conn.execute(text("SELECT name, sql FROM sqlite_master WHERE tbl_name <> 'alembic_version'"))}
    engine.dispose()
    added = {n for n in set(after) - set(before) if not n.startswith(("sqlite_autoindex_academy_practice", "ix_academy_practice"))}
    assert added == TABLES and all(after[n] == before[n] for n in before)


def test_the_migrated_tables_match_the_orm_models(path):
    command.upgrade(_cfg(), NEW)
    engine = _engine(path)
    with engine.connect() as conn:
        diffs = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": False}), Base.metadata)
    engine.dispose()
    assert [d for d in diffs if "academy_practice" in repr(d)] == []


def test_the_database_rejects_rows_that_break_the_practice_invariants(path):
    command.upgrade(_cfg(), NEW)
    engine = _engine(path)
    _seed(engine)
    with engine.begin() as conn:
        _instance(conn)
    bad = [
        dict(id="pi-2", mode="exam"),            # closed mode vocabulary
        dict(id="pi-3", st="graded"),            # closed lifecycle vocabulary
        dict(id="pi-4", mx=0),                   # a practice always allows at least one run
        dict(id="pi-5", att=0),
        dict(id="pi-6", i="no-such-item"),
        dict(id="pi-7", u="no-such-user"),
    ]
    for row in bad:
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                _instance(conn, **row)
    with pytest.raises(IntegrityError):            # a run must belong to a real instance and a real task run
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO academy_practice_runs (id, instance_id, user_id, seq, variables_json, prompt_sha256, task_run_id, agent_run_id, created_at) "
                "VALUES ('r-1', 'pi-none', 'u-a', 1, '{}', :h, 'tr-none', 'ar-none', :n)"), {"h": "0" * 64, "n": NOW})
    engine.dispose()


def test_an_item_with_practice_cannot_be_deleted_and_learner_deletion_cascades(path):
    command.upgrade(_cfg(), NEW)
    engine = _engine(path)
    _seed(engine)
    with engine.begin() as conn:
        _instance(conn)
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM learning_items WHERE id = 'item-0'"))
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM learning_evidence"))
        conn.execute(text("DELETE FROM users WHERE id = 'u-a'"))
        assert conn.execute(text("SELECT COUNT(*) FROM academy_practice_instances")).scalar_one() == 0
    engine.dispose()


def test_downgrade_with_no_practice_removes_only_the_new_tables_and_can_be_reapplied(path):
    command.upgrade(_cfg(), NEW)
    engine = _engine(path)
    _seed(engine)
    content = _content_hash(engine)
    engine.dispose()
    assert _run("downgrade", PREVIOUS) == [f"Running downgrade {NEW} -> {PREVIOUS}"]
    engine = _engine(path)
    assert not (TABLES & _tables(engine)) and "academy_professor_help" in _tables(engine) and _version(engine) == PREVIOUS
    assert _content_hash(engine) == content
    _health(engine)
    engine.dispose()
    command.upgrade(_cfg(), NEW)
    engine = _engine(path)
    assert TABLES <= _tables(engine) and _content_hash(engine) == content
    engine.dispose()


def test_downgrade_refuses_while_practice_rows_exist_and_changes_nothing(path):
    command.upgrade(_cfg(), NEW)
    engine = _engine(path)
    _seed(engine)
    with engine.begin() as conn:
        _instance(conn)
    before = _snapshot(engine)
    engine.dispose()
    with pytest.raises(RuntimeError, match="practice row"):
        command.downgrade(_cfg(), PREVIOUS)
    engine = _engine(path)
    assert _snapshot(engine) == before and _version(engine) == NEW
    _health(engine)
    engine.dispose()


def test_the_new_revision_is_reversible_and_documented():
    from pathlib import Path

    from tests.test_migration_rollback_policy import EXPECTED_IRREVERSIBLE

    root = Path(__file__).resolve().parent.parent
    assert NEW not in EXPECTED_IRREVERSIBLE
    assert "IRREVERSIBLE" not in (root / "alembic" / "versions" / "academy_practice_instances.py").read_text(encoding="utf-8")
    assert NEW in (root / "docs" / "deployment" / "migration-rollback-policy.md").read_text(encoding="utf-8")
