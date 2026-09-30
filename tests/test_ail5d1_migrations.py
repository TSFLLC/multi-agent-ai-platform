"""AIL.5D.1 migration ``academy_step_progress`` on every path a database can arrive by.

* the graph: one head, on top of ``academy_lineage_enforcement``; the deployed ``ail5d_learning_item_lineage``
  revision (the historical, misleadingly named Learning Item lineage work) is untouched;
* Path A: an already-migrated (staging-shaped) database runs ONLY the new revision and loses nothing;
* Path B: a fresh database runs the whole chain;
* the migrated table equals the ORM model; the database itself enforces the row invariants;
* downgrade follows docs/deployment/migration-rollback-policy.md: reversible, and it refuses (changing nothing)
  while learner progress exists.

Disposable temp databases only.
"""

import hashlib
import logging
import re
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from alembic import command
from app import models  # noqa: F401
from app.config import settings
from app.db.base import Base
from app.db.session import build_engine

ROOT = Path(__file__).resolve().parent.parent
PREVIOUS = "academy_lineage_enforcement"
NEW = "academy_step_progress"
HEAD = "academy_step_response"  # AIL.5D.3, the current head
DEPLOYED = "ail5d_learning_item_lineage"
TABLE = "academy_step_progress"
NOW = "2026-01-01T00:00:00"


def _cfg() -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    return cfg


class _Steps(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.INFO)
        self.steps = []

    def emit(self, record):
        message = record.getMessage()
        if message.startswith(("Running upgrade", "Running downgrade")):
            self.steps.append(message.split(",")[0])


def _run(action, target):
    import logging.config

    logger = logging.getLogger("alembic.runtime.migration")
    handler, old_level, old_file_config = _Steps(), logger.level, logging.config.fileConfig
    logging.config.fileConfig = lambda *a, **k: None
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        getattr(command, action)(_cfg(), target)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
        logging.config.fileConfig = old_file_config
    return handler.steps


@pytest.fixture()
def path(tmp_path, monkeypatch):
    p = tmp_path / "ail5d1.db"
    monkeypatch.setattr(settings, "database_path", p)
    return p


def _engine(path):
    return build_engine(f"sqlite:///{path.as_posix()}")


def _seed(engine, items=5):
    """Learning items (lineage-enforced shape), evidence and a learner: the data that must survive."""
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO organizations (id, name, created_at) VALUES ('org-a', 'Org', :n)"), {"n": NOW})
        for uid in ("u-a", "u-b"):
            conn.execute(text("INSERT INTO users (id, org_id, email, role, created_at) VALUES (:u, 'org-a', :e, 'member', :n)"), {"u": uid, "e": f"{uid}@example.com", "n": NOW})
        conn.execute(text("INSERT INTO concepts (id, slug, name, level, kind, is_core, created_at) VALUES ('c-a', 'c-a', 'C', 'foundational', 'mechanism', 0, :n)"), {"n": NOW})
        conn.execute(text("INSERT INTO concept_versions (id, concept_id, version, plain_definition, content_origin, status, created_at) VALUES ('cv-a', 'c-a', 1, 'd', 'ai_drafted_unreviewed', 'active', :n)"), {"n": NOW})
        for i in range(items):
            conn.execute(
                text("INSERT INTO learning_items (id, concept_id, item_type, title, body_md, spec_json, reviewed, lineage_id, version, created_at) "
                     "VALUES (:id, 'c-a', 'resource', :t, 'body', '{\"academy_key\": \"k\"}', 1, :lin, 1, :n)"),
                {"id": f"item-{i}", "t": f"Item {i}", "lin": f"00000000-0000-4000-8000-00000000000{i}", "n": NOW},
            )
        conn.execute(text("INSERT INTO learning_evidence (id, user_id, concept_id, concept_version_id, evidence_type, learning_item_id, passed, grader, on_demo_data, ref_type, created_at) "
                          "VALUES ('ev-0', 'u-a', 'c-a', 'cv-a', 'lesson_completed', 'item-0', 1, 'self', 0, 'none', :n)"), {"n": NOW})


def _content_hash(engine):
    with engine.begin() as conn:
        rows = conn.execute(text("SELECT * FROM learning_items ORDER BY id")).fetchall()
        evidence = conn.execute(text("SELECT * FROM learning_evidence ORDER BY id")).fetchall()
        users = conn.execute(text("SELECT id, email FROM users ORDER BY id")).fetchall()
    return hashlib.sha256(repr((rows, evidence, users)).encode()).hexdigest()


def _snapshot(engine):
    with engine.begin() as conn:
        objects = conn.execute(text("SELECT type, name, sql FROM sqlite_master ORDER BY type, name")).fetchall()
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    return hashlib.sha256(repr((objects, version, _content_hash(engine))).encode()).hexdigest()


def _health(engine):
    with engine.begin() as conn:
        assert conn.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
        assert conn.execute(text("PRAGMA foreign_key_check")).fetchall() == []
        assert conn.execute(text("SELECT name FROM sqlite_master WHERE name LIKE '_alembic_tmp_%'")).fetchall() == []


def _version(engine):
    with engine.begin() as conn:
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()


def _tables(engine):
    return set(sa.inspect(engine).get_table_names())


def _progress_row(conn, **over):
    row = {"id": "p-1", "u": "u-a", "i": "item-0", "lin": "00000000-0000-4000-8000-000000000000", "k": "what-is-ai", "fp": "f" * 64,
           "st": "opened", "oc": 1, "done": None, "skip": None, "n": NOW}
    row.update(over)
    conn.execute(text(
        f"INSERT INTO {TABLE} (id, user_id, learning_item_id, lineage_id, step_key, step_fingerprint, status, open_count, first_opened_at, "
        "last_opened_at, completed_at, skipped_at, created_at) VALUES (:id, :u, :i, :lin, :k, :fp, :st, :oc, :n, :n, :done, :skip, :n)"), row)


# -- the migration graph -------------------------------------------------------------------------------------------------------


def test_there_is_one_head_and_it_sits_on_the_forward_lineage_enforcement_revision():
    script = ScriptDirectory.from_config(_cfg())
    assert script.get_heads() == [HEAD]  # later slices sit above this revision; this one is exactly one step below the head
    assert script.get_revision(HEAD).down_revision == NEW
    revision = script.get_revision(NEW)
    assert revision.down_revision == PREVIOUS
    assert script.get_revision(PREVIOUS).down_revision == DEPLOYED
    # the misleading historical name is not reused for this work
    assert not NEW.startswith("ail5d_") and DEPLOYED != NEW


def test_the_deployed_lineage_revision_is_exactly_as_it_shipped():
    body = (ROOT / "alembic" / "versions" / "ail5d_learning_item_lineage.py").read_bytes().replace(b"\r\n", b"\n")
    assert b'revision: str = "ail5d_learning_item_lineage"' in body
    assert b'down_revision: Union[str, None] = "ail5c_grader_agent_role"' in body
    assert hashlib.sha256(body).hexdigest() == "0e1790a47fb2079708c8320074c59db9a75f88c73a90f2956a469da7271ff385"  # deployed history: never edited
    # every revision in the chain still resolves under its original id (a rename breaks deploys with "Can't locate revision")
    script = ScriptDirectory.from_config(_cfg())
    for rid in ("ail5c_grader_agent_role", DEPLOYED, PREVIOUS, NEW):
        assert script.get_revision(rid) is not None


# -- Path A: an already-migrated database ----------------------------------------------------------------------------------------


def test_upgrading_from_the_current_head_runs_only_the_new_revision_and_loses_nothing(path):
    command.upgrade(_cfg(), PREVIOUS)
    engine = _engine(path)
    _seed(engine)
    before = _content_hash(engine)
    assert TABLE not in _tables(engine)
    engine.dispose()
    steps = _run("upgrade", NEW)
    assert steps == [f"Running upgrade {PREVIOUS} -> {NEW}"]
    engine = _engine(path)
    assert _version(engine) == NEW and TABLE in _tables(engine)
    assert _content_hash(engine) == before
    with engine.begin() as conn:
        assert conn.execute(text(f"SELECT COUNT(*) FROM {TABLE}")).scalar_one() == 0
    _health(engine)
    engine.dispose()


def test_the_new_revision_changes_no_existing_table(path):
    command.upgrade(_cfg(), PREVIOUS)
    engine = _engine(path)
    with engine.begin() as conn:
        before = {r[0]: r[1] for r in conn.execute(text("SELECT name, sql FROM sqlite_master WHERE tbl_name <> 'alembic_version' ORDER BY name"))}
    engine.dispose()
    command.upgrade(_cfg(), NEW)
    engine = _engine(path)
    with engine.begin() as conn:
        after = {r[0]: r[1] for r in conn.execute(text("SELECT name, sql FROM sqlite_master WHERE tbl_name <> 'alembic_version' ORDER BY name"))}
    engine.dispose()
    added = set(after) - set(before)
    assert {n for n in added if not n.startswith(("sqlite_autoindex_" + TABLE, "ix_academy_step_progress"))} == {TABLE}
    assert all(after[n] == before[n] for n in before)  # not one existing table, index or trigger was altered


# -- Path B: a fresh database ---------------------------------------------------------------------------------------------------


def test_the_whole_chain_on_a_fresh_database_reaches_the_new_head(path):
    steps = _run("upgrade", NEW)
    assert steps[0].startswith("Running upgrade  ->") and steps[-1] == f"Running upgrade {PREVIOUS} -> {NEW}"
    assert any(DEPLOYED in s for s in steps)
    engine = _engine(path)
    assert _version(engine) == NEW and TABLE in _tables(engine)
    _health(engine)
    engine.dispose()


def test_upgrade_is_idempotent_at_head_and_refuses_a_stray_preexisting_table(path):
    command.upgrade(_cfg(), NEW)
    assert _run("upgrade", NEW) == []
    command.downgrade(_cfg(), PREVIOUS)
    engine = _engine(path)
    with engine.begin() as conn:
        conn.execute(text(f"CREATE TABLE {TABLE} (x INTEGER)"))
    engine.dispose()
    with pytest.raises(RuntimeError, match="already exists"):
        command.upgrade(_cfg(), NEW)


# -- the schema equals the model, and the database enforces the invariants -------------------------------------------------------


def test_the_migrated_table_matches_the_orm_model(path):
    command.upgrade(_cfg(), NEW)
    engine = _engine(path)
    with engine.connect() as conn:
        diffs = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": False}), Base.metadata)
    engine.dispose()
    assert [d for d in diffs if TABLE in repr(d)] == []


def test_the_database_rejects_rows_that_break_the_step_progress_invariants(path):
    command.upgrade(_cfg(), NEW)
    engine = _engine(path)
    _seed(engine)
    with engine.begin() as conn:
        _progress_row(conn)
    bad = [
        dict(id="p-2", k="k2", oc=0),                                  # open_count >= 1
        dict(id="p-3", k="k3", st="completed"),                        # completed needs completed_at
        dict(id="p-4", k="k4", st="opened", done=NOW),                 # completed_at only when completed
        dict(id="p-5", k="k5", st="skipped"),                          # skipped needs skipped_at
        dict(id="p-6", k="k6", st="opened", skip=NOW),                 # skipped_at only when skipped
        dict(id="p-7", k="k7", st="forged"),                           # closed status vocabulary
        dict(id="p-8", k="what-is-ai"),                                # one row per user/item/step
        dict(id="p-9", k="k9", i="no-such-item"),                      # learning item must exist
        dict(id="p-10", k="k10", u="no-such-user"),                    # learner must exist
    ]
    for row in bad:
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                _progress_row(conn, **row)
    with engine.begin() as conn:
        _progress_row(conn, id="p-ok", k="k-ok", st="completed", done=NOW)
        _progress_row(conn, id="p-ok2", u="u-b", k="what-is-ai")  # another learner, same item and step: allowed
        assert conn.execute(text(f"SELECT COUNT(*) FROM {TABLE}")).scalar_one() == 3
    engine.dispose()


def test_learner_deletion_cascades_and_an_item_with_progress_cannot_be_deleted(path):
    command.upgrade(_cfg(), NEW)
    engine = _engine(path)
    _seed(engine)
    with engine.begin() as conn:
        _progress_row(conn)
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM learning_items WHERE id = 'item-0'"))
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM learning_evidence"))
        conn.execute(text("DELETE FROM users WHERE id = 'u-a'"))
        assert conn.execute(text(f"SELECT COUNT(*) FROM {TABLE}")).scalar_one() == 0
    engine.dispose()


# -- downgrade / rollback policy -----------------------------------------------------------------------------------------------


def test_downgrade_with_no_progress_removes_only_the_new_table_and_can_be_reapplied(path):
    command.upgrade(_cfg(), NEW)
    engine = _engine(path)
    _seed(engine)
    content = _content_hash(engine)
    engine.dispose()
    assert _run("downgrade", PREVIOUS) == [f"Running downgrade {NEW} -> {PREVIOUS}"]
    engine = _engine(path)
    assert TABLE not in _tables(engine) and _version(engine) == PREVIOUS
    assert _content_hash(engine) == content
    with engine.begin() as conn:  # the lineage enforcement triggers below this revision are still in force
        names = {r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'trigger'"))}
    assert {"trg_learning_items_lineage_insert", "trg_learning_items_lineage_update"} <= names
    _health(engine)
    engine.dispose()
    command.upgrade(_cfg(), NEW)
    engine = _engine(path)
    assert TABLE in _tables(engine) and _content_hash(engine) == content
    engine.dispose()


def test_downgrade_refuses_while_learner_progress_exists_and_changes_nothing(path):
    command.upgrade(_cfg(), NEW)
    engine = _engine(path)
    _seed(engine)
    with engine.begin() as conn:
        _progress_row(conn, st="completed", done=NOW)
    before = _snapshot(engine)
    engine.dispose()
    with pytest.raises(RuntimeError, match="learner step-progress row"):
        command.downgrade(_cfg(), PREVIOUS)
    engine = _engine(path)
    assert _snapshot(engine) == before and _version(engine) == NEW
    _health(engine)
    engine.dispose()


def test_the_new_revision_is_reversible_and_the_policy_document_says_so():
    from tests.test_migration_rollback_policy import EXPECTED_IRREVERSIBLE

    assert NEW not in EXPECTED_IRREVERSIBLE
    source = (ROOT / "alembic" / "versions" / "academy_step_progress.py").read_text(encoding="utf-8")
    assert "IRREVERSIBLE" not in source
    assert re.search(r"def downgrade\(\) -> None:\n(?:.*\n)*?.*op\.drop_table", source)
    policy = (ROOT / "docs" / "deployment" / "migration-rollback-policy.md").read_text(encoding="utf-8")
    assert NEW in policy and "restore of the pre-migration backup" in policy
