"""The forward lineage-enforcement revision, on every path a database can arrive by.

``ail5d_learning_item_lineage`` is deployed history (applied on hosted staging) and is
never edited. The stronger enforcement ships as ``academy_lineage_enforcement`` on top
of it, so:

* Path A, an already-migrated (staging-shaped) database, runs ONLY the new revision;
* Path B, a pre-AIL5 database, runs the whole chain;
* Path C, a fresh database, runs the whole chain;

and all three must end with the same lineage contract. Disposable temp databases only.
"""

import hashlib
import logging
import uuid
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from alembic import command
from app.config import settings
from app.db.base import Base
from app.db.session import build_engine

ROOT = Path(__file__).resolve().parent.parent
PRE_AIL5 = "ail4c_professor_agent_role"
R3 = "ail5c_grader_agent_role"
DEPLOYED = "ail5d_learning_item_lineage"  # deployed history: exact id, exact body
FORWARD = "academy_lineage_enforcement"  # these tests target this revision explicitly; later revisions (academy_step_progress) sit above it
NS = uuid.UUID("5d9a3b42-8a61-4cf8-b3e6-0a89d0de5d5d")  # the deployed backfill's namespace
ITEMS = 94  # the live staging table size


def _cfg() -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    return cfg


class _Steps(logging.Handler):
    """Records which revisions Alembic actually ran."""

    def __init__(self):
        super().__init__(level=logging.INFO)
        self.steps = []

    def emit(self, record):
        message = record.getMessage()
        if message.startswith(("Running upgrade", "Running downgrade")):
            self.steps.append(message.split(",")[0])


def _run(cfg, action, target):
    """Run an Alembic command and return the revisions it actually ran.

    env.py calls ``logging.config.fileConfig`` on every run, which strips handlers from the
    child loggers, so it is neutralised for the duration of the call.
    """
    import logging.config

    logger = logging.getLogger("alembic.runtime.migration")
    handler = _Steps()
    old_level, old_file_config = logger.level, logging.config.fileConfig
    logging.config.fileConfig = lambda *args, **kwargs: None
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        getattr(command, action)(cfg, target)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
        logging.config.fileConfig = old_file_config
    return handler.steps


def _database(tmp_path, monkeypatch, name):
    path = tmp_path / f"{name}.db"
    monkeypatch.setattr(settings, "database_path", path)
    return path


def _engine(path):
    return build_engine(f"sqlite:///{path.as_posix()}")


def _seed_items(engine, count, *, with_evidence=True):
    """Pre-lineage Learning Items (no lineage_id column yet), some referenced by evidence."""
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO organizations (id, name, created_at) VALUES ('org-a', 'Org', '2026-01-01')")
        )
        conn.execute(
            text(
                "INSERT INTO users (id, org_id, email, role, created_at) VALUES ('u-a', 'org-a', 'a@example.com', 'owner', '2026-01-01')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO concepts (id, slug, name, level, kind, is_core, created_at) VALUES ('c-a', 'c-a', 'C', 'foundational', 'mechanism', 0, '2026-01-01')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO concept_versions (id, concept_id, version, plain_definition, content_origin, status, created_at) VALUES ('cv-a', 'c-a', 1, 'd', 'ai_drafted_unreviewed', 'active', '2026-01-01')"
            )
        )
        for i in range(count):
            conn.execute(
                text(
                    "INSERT INTO learning_items (id, concept_id, item_type, title, body_md, spec_json, grading_mode, "
                    "reviewed, version, created_at) VALUES (:id, 'c-a', 'check_question', :t, :b, '{}', 'deterministic', 1, 1, '2026-01-01')"
                ),
                {"id": f"item-{i:03d}", "t": f"Item {i}", "b": f"Body {i}"},
            )
        if with_evidence:
            for i in range(0, count, max(1, count // 5)):
                conn.execute(
                    text(
                        "INSERT INTO learning_evidence (id, user_id, concept_id, concept_version_id, evidence_type, "
                        "learning_item_id, passed, grader, on_demo_data, ref_type, created_at) VALUES "
                        "(:id, 'u-a', 'c-a', 'cv-a', 'lesson_completed', :item, 1, 'self', 0, 'none', '2026-01-01')"
                    ),
                    {"id": f"ev-{i:03d}", "item": f"item-{i:03d}"},
                )


def _items(engine):
    with engine.begin() as conn:
        return conn.execute(text("SELECT id, lineage_id, version FROM learning_items ORDER BY id")).fetchall()


def _content_hash(engine):
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT id, concept_id, item_type, title, body_md, spec_json, grading_mode, reviewed, version, created_at FROM learning_items ORDER BY id"
            )
        ).fetchall()
        evidence = conn.execute(
            text("SELECT id, learning_item_id FROM learning_evidence ORDER BY id")
        ).fetchall()
    return hashlib.sha256(repr((rows, evidence)).encode()).hexdigest()


def _health(engine):
    with engine.begin() as conn:
        assert conn.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
        assert conn.execute(text("PRAGMA foreign_key_check")).fetchall() == []
        assert (
            conn.execute(text("SELECT name FROM sqlite_master WHERE name LIKE '_alembic_tmp_%'")).fetchall()
            == []
        )


def _shape(engine):
    """The schema objects that make up the lineage contract."""
    with engine.begin() as conn:
        norm = lambda sql: " ".join((sql or "").split())
        table = conn.execute(text("SELECT sql FROM sqlite_master WHERE name = 'learning_items'")).scalar_one()
        indexes = {
            r[0]: norm(r[1])
            for r in conn.execute(
                text(
                    "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'learning_items' AND sql IS NOT NULL"
                )
            )
        }
        triggers = {
            r[0]: norm(r[1])
            for r in conn.execute(
                text(
                    "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'learning_items'"
                )
            )
        }
    return {"table": norm(table), "indexes": indexes, "triggers": triggers}


_PROBE = (
    "INSERT INTO learning_items (id, concept_id, item_type, title, reviewed, lineage_id, version, created_at) "
    "VALUES (:id, :c, 'check_question', 'probe', 1, :lineage, :version, '2026-01-01')"
)


def _behaviour(engine):
    """What the database accepts and rejects, probed with raw SQL that is always rolled back."""
    with engine.begin() as conn:
        concept = conn.execute(text("SELECT id FROM concepts LIMIT 1")).scalar_one_or_none()
        if concept is None:
            conn.execute(
                text(
                    "INSERT INTO concepts (id, slug, name, level, kind, is_core, created_at) VALUES ('c-probe', 'c-probe', 'C', 'foundational', 'mechanism', 0, '2026-01-01')"
                )
            )
            concept = "c-probe"
    existing = None
    with engine.begin() as conn:
        row = conn.execute(text("SELECT lineage_id FROM learning_items LIMIT 1")).first()
        existing = row[0] if row else None
    result = {}
    fresh = str(uuid.uuid4())
    cases = {
        "null_lineage": (None, 1),
        "version_zero": (fresh, 0),
        "version_negative": (fresh, -2),
        "valid_new_lineage": (fresh, 1),
    }
    if existing:
        cases["duplicate_lineage_version"] = (existing, 1)
    for name, (lineage, version) in cases.items():
        conn = engine.connect()
        trans = conn.begin()
        try:
            conn.execute(text(_PROBE), {"id": "probe", "c": concept, "lineage": lineage, "version": version})
            result[name] = "accepted"
        except IntegrityError:
            result[name] = "rejected"
        finally:
            trans.rollback()
            conn.close()
    return result


EXPECTED_BEHAVIOUR = {
    "null_lineage": "rejected",
    "version_zero": "rejected",
    "version_negative": "rejected",
    "valid_new_lineage": "accepted",
}


def _final_contract(engine):
    behaviour = _behaviour(engine)
    behaviour.pop("duplicate_lineage_version", None)  # only probed when a row exists; asserted separately
    return {**_shape(engine), "behaviour": behaviour}


def _assert_target_contract(engine):
    shape = _shape(engine)
    assert set(shape["triggers"]) == {
        "trg_learning_items_lineage_insert",
        "trg_learning_items_lineage_update",
    }
    assert {"uq_learning_items_lineage_version", "ix_learning_items_lineage_id"} <= set(shape["indexes"])
    assert _behaviour(engine)["null_lineage"] == "rejected"


# -- Path A: the deployed (live-staging) shape ------------------------------------------------------------------------------------


def _deployed_shape_database(tmp_path, monkeypatch):
    path = _database(tmp_path, monkeypatch, "path-a")
    _run(_cfg(), "upgrade", R3)
    engine = _engine(path)
    _seed_items(engine, ITEMS)
    _run(_cfg(), "upgrade", DEPLOYED)  # the historical revision, exactly as deployed
    return path, engine


def test_path_a_the_deployed_staging_shape_upgrades_by_running_only_the_forward_revision(
    tmp_path, monkeypatch
):
    _path, engine = _deployed_shape_database(tmp_path, monkeypatch)

    # what live staging evidence describes: revision at the deployed id, 94 items, none NULL, none duplicated
    shape = _shape(engine)
    assert shape["triggers"] == {}, "the deployed revision added no enforcement triggers"
    assert {"uq_learning_items_lineage_version", "ix_learning_items_lineage_id"} <= set(shape["indexes"])
    assert (
        "lineage_id VARCHAR(36)" in shape["table"] and "lineage_id VARCHAR(36) NOT NULL" not in shape["table"]
    )
    before_items, before_hash = _items(engine), _content_hash(engine)
    assert len(before_items) == ITEMS and all(r[1] for r in before_items)
    assert len({r[1] for r in before_items}) == ITEMS
    assert (
        _behaviour(engine)["null_lineage"] == "accepted"
    ), "the deployed shape does not enforce it (why a forward revision is needed)"

    steps = _run(_cfg(), "upgrade", FORWARD)

    assert steps == [f"Running upgrade {DEPLOYED} -> {FORWARD}"], "no historical migration is rerun"
    assert _items(engine) == before_items, "no lineage id or version changed, no row lost or added"
    assert _content_hash(engine) == before_hash, "no Learning Item or evidence data changed"
    _health(engine)
    _assert_target_contract(engine)
    with engine.begin() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == FORWARD
    assert _run(_cfg(), "upgrade", FORWARD) == [], "a second upgrade is a no-op"
    engine.dispose()


def test_path_a_the_forward_revision_refuses_invalid_data_and_changes_nothing(tmp_path, monkeypatch):
    _path, engine = _deployed_shape_database(tmp_path, monkeypatch)
    for statement, needle in (
        ("UPDATE learning_items SET lineage_id = NULL WHERE id = 'item-001'", "NULL lineage_id"),
        ("UPDATE learning_items SET version = 0 WHERE id = 'item-001'", "version < 1"),
    ):
        with engine.begin() as conn:
            conn.execute(text(statement))  # only possible because the deployed shape does not enforce it
        before = _shape(engine)
        with pytest.raises(RuntimeError, match=needle):
            _run(_cfg(), "upgrade", FORWARD)
        assert _shape(engine) == before and before["triggers"] == {}
        with engine.begin() as conn:
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == DEPLOYED
            conn.execute(
                text("UPDATE learning_items SET lineage_id = :l, version = 1 WHERE id = 'item-001'"),
                {"l": str(uuid.uuid5(NS, "learning-item:item-001"))},
            )
    _run(_cfg(), "upgrade", FORWARD)  # repaired data now upgrades
    _assert_target_contract(engine)
    engine.dispose()


def test_path_a_an_interrupted_forward_revision_resumes(tmp_path, monkeypatch):
    _path, engine = _deployed_shape_database(tmp_path, monkeypatch)
    with engine.begin() as conn:  # SQLite DDL is not transactional: one trigger may already exist
        conn.execute(
            text(
                "CREATE TRIGGER trg_learning_items_lineage_insert BEFORE INSERT ON learning_items WHEN NEW.lineage_id IS NULL BEGIN SELECT RAISE(ABORT, 'x'); END"
            )
        )
    _run(_cfg(), "upgrade", FORWARD)
    _assert_target_contract(engine)
    engine.dispose()


def test_the_forward_revision_is_reversible_and_removes_only_its_own_triggers(tmp_path, monkeypatch):
    _path, engine = _deployed_shape_database(tmp_path, monkeypatch)
    deployed_shape, before_items, before_hash = _shape(engine), _items(engine), _content_hash(engine)
    _run(_cfg(), "upgrade", FORWARD)
    assert _run(_cfg(), "downgrade", DEPLOYED) == [f"Running downgrade {FORWARD} -> {DEPLOYED}"]
    assert _shape(engine) == deployed_shape, "back to exactly the deployed shape"
    assert _items(engine) == before_items and _content_hash(engine) == before_hash
    engine.dispose()


# -- Path B: a pre-AIL5 database runs the whole chain --------------------------------------------------------------------------------


def test_path_b_a_pre_ail5_database_runs_the_whole_chain_to_the_same_contract(tmp_path, monkeypatch):
    path = _database(tmp_path, monkeypatch, "path-b")
    _run(_cfg(), "upgrade", PRE_AIL5)
    engine = _engine(path)
    _seed_items(engine, ITEMS)
    before_hash = _content_hash(engine)

    steps = _run(_cfg(), "upgrade", FORWARD)

    assert steps[-2:] == [f"Running upgrade {R3} -> {DEPLOYED}", f"Running upgrade {DEPLOYED} -> {FORWARD}"]
    assert len(steps) == 8  # 5A, 5B (x2), 5C (x3), the deployed lineage revision, the forward revision
    rows = _items(engine)
    assert len(rows) == ITEMS and all(
        r[1] == str(uuid.uuid5(NS, f"learning-item:{r[0]}")) and r[2] == 1 for r in rows
    )
    assert _content_hash(engine) == before_hash
    _health(engine)
    _assert_target_contract(engine)
    engine.dispose()


# -- Path C: a fresh database, and all three agree ------------------------------------------------------------------------------------


def test_paths_a_b_and_c_end_with_the_same_lineage_contract(tmp_path, monkeypatch):
    _path_a, engine_a = _deployed_shape_database(tmp_path, monkeypatch)
    _run(_cfg(), "upgrade", FORWARD)

    path_b = _database(tmp_path, monkeypatch, "path-b")
    _run(_cfg(), "upgrade", PRE_AIL5)
    engine_b = _engine(path_b)
    _seed_items(engine_b, 12)
    _run(_cfg(), "upgrade", FORWARD)

    path_c = _database(tmp_path, monkeypatch, "path-c")
    assert _run(_cfg(), "upgrade", FORWARD)[-1] == f"Running upgrade {DEPLOYED} -> {FORWARD}"
    engine_c = _engine(path_c)

    contracts = {name: _final_contract(e) for name, e in (("A", engine_a), ("B", engine_b), ("C", engine_c))}
    assert contracts["A"] == contracts["B"] == contracts["C"]
    assert contracts["C"]["behaviour"] == EXPECTED_BEHAVIOUR
    for engine in (engine_a, engine_b):  # a duplicate (lineage, version) is rejected wherever there is a row
        assert _behaviour(engine)["duplicate_lineage_version"] == "rejected"
    for engine in (engine_a, engine_b, engine_c):
        _health(engine)
        engine.dispose()


def test_the_orm_created_schema_enforces_the_same_behaviour(tmp_path, monkeypatch):
    path = _database(tmp_path, monkeypatch, "create-all")
    engine = _engine(path)
    Base.metadata.create_all(engine)
    assert _behaviour(engine) == EXPECTED_BEHAVIOUR
    engine.dispose()
