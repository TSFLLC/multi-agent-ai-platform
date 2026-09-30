"""AIL.5D.4 migration ``academy_professor_help`` on every path a database can arrive by.

One head on top of ``academy_step_response``; an already-migrated database runs ONLY the new revision and loses nothing; a
fresh chain reaches the head; the table equals the model; the database enforces the row invariants; downgrade follows
docs/deployment/migration-rollback-policy.md (reversible, refuses while help rows exist). Disposable temp databases only.
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

PREVIOUS = "academy_step_response"
NEW = "academy_professor_help"
TABLE = "academy_professor_help"


def _row(conn, **over):
    row = {"id": "h-1", "u": "u-a", "i": "item-0", "lin": "00000000-0000-4000-8000-000000000000", "k": "think-traditional-or-ai", "fp": "f" * 64,
           "mode": "guided", "req": "hint", "lvl": "hint", "al": "h2", "rev": 0, "ix": "1" * 36, "h": "0" * 64, "n": NOW}
    row.update(over)
    conn.execute(text(
        f"INSERT INTO {TABLE} (id, user_id, learning_item_id, lineage_id, step_key, step_fingerprint, mode, request_kind, help_level, assistance_level, "
        "revealed_solution, interaction_id, context_sha256, created_at) VALUES (:id, :u, :i, :lin, :k, :fp, :mode, :req, :lvl, :al, :rev, :ix, :h, :n)"), row)


def test_there_is_one_head_above_the_step_response_revision():
    script = ScriptDirectory.from_config(_cfg())
    assert script.get_heads() == [NEW]
    assert script.get_revision(NEW).down_revision == PREVIOUS
    assert script.get_revision(PREVIOUS).down_revision == "academy_step_progress"
    assert not NEW.startswith("ail5d_") and script.get_revision(DEPLOYED) is not None


def test_upgrading_from_the_previous_head_runs_only_the_new_revision_and_loses_nothing(path):
    command.upgrade(_cfg(), PREVIOUS)
    engine = _engine(path)
    _seed(engine)
    before = _content_hash(engine)
    engine.dispose()
    assert _run("upgrade", "head") == [f"Running upgrade {PREVIOUS} -> {NEW}"]
    engine = _engine(path)
    assert _version(engine) == NEW and TABLE in _tables(engine) and _content_hash(engine) == before
    with engine.begin() as conn:
        assert conn.execute(text(f"SELECT COUNT(*) FROM {TABLE}")).scalar_one() == 0
    _health(engine)
    engine.dispose()


def test_the_whole_chain_on_a_fresh_database_reaches_the_head(path):
    steps = _run("upgrade", "head")
    assert steps[-1] == f"Running upgrade {PREVIOUS} -> {NEW}" and any(DEPLOYED in s for s in steps)
    engine = _engine(path)
    assert _version(engine) == NEW and {TABLE, "academy_step_responses", "academy_step_progress"} <= _tables(engine)
    _health(engine)
    engine.dispose()


def test_the_new_revision_changes_no_existing_table(path):
    command.upgrade(_cfg(), PREVIOUS)
    engine = _engine(path)
    with engine.begin() as conn:
        before = {r[0]: r[1] for r in conn.execute(text("SELECT name, sql FROM sqlite_master WHERE tbl_name <> 'alembic_version'"))}
    engine.dispose()
    command.upgrade(_cfg(), "head")
    engine = _engine(path)
    with engine.begin() as conn:
        after = {r[0]: r[1] for r in conn.execute(text("SELECT name, sql FROM sqlite_master WHERE tbl_name <> 'alembic_version'"))}
    engine.dispose()
    added = {n for n in set(after) - set(before) if not n.startswith(("sqlite_autoindex_" + TABLE, "ix_academy_professor_help"))}
    assert added == {TABLE} and all(after[n] == before[n] for n in before)


def test_the_migrated_table_matches_the_orm_model(path):
    command.upgrade(_cfg(), "head")
    engine = _engine(path)
    with engine.connect() as conn:
        diffs = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": False}), Base.metadata)
    engine.dispose()
    assert [d for d in diffs if TABLE in repr(d)] == []


def test_the_database_rejects_rows_that_break_the_help_invariants(path):
    command.upgrade(_cfg(), "head")
    engine = _engine(path)
    _seed(engine)
    with engine.begin() as conn:
        _row(conn)
    bad = [
        dict(id="h-2", ix="2" * 36, mode="assessment"),       # assessment is never a Professor mode
        dict(id="h-3", ix="3" * 36, req="solution"),           # closed request vocabulary
        dict(id="h-4", ix="4" * 36, lvl="answer"),             # closed ladder
        dict(id="h-5", ix="5" * 36, al="h9"),                  # existing H0-H5 scale
        dict(id="h-6", ix="1" * 36),                           # one row per delivered interaction
        dict(id="h-7", ix="7" * 36, i="no-such-item"),
        dict(id="h-8", ix="8" * 36, u="no-such-user"),
    ]
    for row in bad:
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                _row(conn, **row)
    with engine.begin() as conn:
        _row(conn, id="h-ok", ix="9" * 36, al=None, mode="teaching", lvl="explain", req="ask")   # teaching help carries no H-level
        assert conn.execute(text(f"SELECT COUNT(*) FROM {TABLE}")).scalar_one() == 2
    engine.dispose()


def test_learner_deletion_cascades_and_an_item_with_help_cannot_be_deleted(path):
    command.upgrade(_cfg(), "head")
    engine = _engine(path)
    _seed(engine)
    with engine.begin() as conn:
        _row(conn)
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM learning_items WHERE id = 'item-0'"))
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM learning_evidence"))
        conn.execute(text("DELETE FROM users WHERE id = 'u-a'"))
        assert conn.execute(text(f"SELECT COUNT(*) FROM {TABLE}")).scalar_one() == 0
    engine.dispose()


def test_downgrade_with_no_help_removes_only_the_new_table_and_can_be_reapplied(path):
    command.upgrade(_cfg(), "head")
    engine = _engine(path)
    _seed(engine)
    content = _content_hash(engine)
    engine.dispose()
    assert _run("downgrade", PREVIOUS) == [f"Running downgrade {NEW} -> {PREVIOUS}"]
    engine = _engine(path)
    assert TABLE not in _tables(engine) and "academy_step_responses" in _tables(engine) and _version(engine) == PREVIOUS
    assert _content_hash(engine) == content
    _health(engine)
    engine.dispose()
    command.upgrade(_cfg(), "head")
    engine = _engine(path)
    assert TABLE in _tables(engine) and _content_hash(engine) == content
    engine.dispose()


def test_downgrade_refuses_while_help_rows_exist_and_changes_nothing(path):
    command.upgrade(_cfg(), "head")
    engine = _engine(path)
    _seed(engine)
    with engine.begin() as conn:
        _row(conn)
    before = _snapshot(engine)
    engine.dispose()
    with pytest.raises(RuntimeError, match="Professor help row"):
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
    assert "IRREVERSIBLE" not in (root / "alembic" / "versions" / "academy_professor_help.py").read_text(encoding="utf-8")
    assert NEW in (root / "docs" / "deployment" / "migration-rollback-policy.md").read_text(encoding="utf-8")
