"""AIL.5C migrations R1/R2/R3 — disposable temp databases ONLY.

Never touches data/multi_agent_platform.db, staging or production: every test
points ``settings.database_path`` at a ``tmp_path`` file.
"""

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from alembic import command
from app import models  # noqa: F401
from app.config import settings
from app.db.base import Base
from app.db.session import build_engine

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PREVIOUS_HEAD = "c31b7f4a1a5e"
R1, R2, R3 = "ail5c_assessment_tables", "ail5c_evidence_enum_ext", "ail5c_grader_agent_role"
R4 = "ail5d_learning_item_lineage"  # deployed history: keep this exact id
R5 = "academy_lineage_enforcement"  # forward revision that adds the stronger enforcement
R6 = "academy_step_progress"  # AIL.5D.1 structured learning foundation
R7 = "academy_step_response"  # AIL.5D.3 learner step responses
R8 = "academy_professor_help"  # AIL.5D.4 step Professor help tracking: the current head
NOW = "2026-01-01T00:00:00"
NEW_TABLES = {
    "assessment_definitions",
    "assessment_definition_concepts",
    "assessment_attempts",
    "assessment_results",
    "assessment_reviews",
}


def _cfg() -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    return cfg


def _engine(path: Path):
    return build_engine(f"sqlite:///{path.as_posix()}")


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "ail5c-migration.db"
    monkeypatch.setattr(settings, "database_path", path)
    return path


def _clean(conn):
    assert conn.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
    assert conn.execute(text("PRAGMA foreign_key_check")).fetchall() == []


def _seed(conn):
    conn.execute(text(f"INSERT INTO organizations (id, name, created_at) VALUES ('org-1', 'Org', '{NOW}')"))
    conn.execute(
        text(
            f"INSERT INTO users (id, org_id, email, role, created_at) VALUES ('u-1', 'org-1', 'a@x.io', 'owner', '{NOW}')"
        )
    )
    conn.execute(
        text(
            f"INSERT INTO concepts (id, slug, name, level, kind, is_core, created_at) VALUES ('c-1', 's', 'C', 'foundational', 'mechanism', 0, '{NOW}')"
        )
    )
    conn.execute(
        text(
            "INSERT INTO concept_versions (id, concept_id, version, plain_definition, content_origin, status, created_at) "
            f"VALUES ('cv-1', 'c-1', 1, 'd', 'ai_drafted_unreviewed', 'active', '{NOW}')"
        )
    )
    conn.execute(
        text(
            f"INSERT INTO projects (id, org_id, name, kind, ail_evidence_opt_in, created_at) VALUES ('p-1', 'org-1', 'AIL', 'system_ail', 0, '{NOW}')"
        )
    )
    conn.execute(
        text(
            f"INSERT INTO agents (id, project_id, name, role, current_status, created_at) VALUES ('a-1', 'p-1', 'A', 'professor', 'active', '{NOW}')"
        )
    )
    conn.execute(
        text(
            f"INSERT INTO agent_versions (id, agent_id, version, name, role, default_model_strategy, status, created_at) VALUES ('av-1', 'a-1', 1, 'A', 'professor', 'manual_required', 'active', '{NOW}')"
        )
    )
    conn.execute(
        text(
            f"INSERT INTO tasks (id, project_id, title, execution_mode, status, created_at) VALUES ('t-1', 'p-1', 'T', 'single_agent', 'ready', '{NOW}')"
        )
    )
    conn.execute(
        text(
            f"INSERT INTO task_runs (id, task_id, status, timeout_seconds, created_at) VALUES ('tr-1', 't-1', 'created', 60, '{NOW}')"
        )
    )
    for i, role in enumerate(("primary", "reviewer", "repair", "evaluator", "professor")):
        conn.execute(
            text(
                "INSERT INTO agent_runs (id, task_run_id, agent_version_id, status, role, timeout_seconds, fencing_token, created_at) "
                f"VALUES ('run-{i}', 'tr-1', 'av-1', 'created', '{role}', 60, 0, '{NOW}')"
            )
        )
    for i, (etype, ref) in enumerate(
        [
            ("knowledge_check", "none"),
            ("lab", "experiment"),
            ("interpretation", "human"),
            ("self_report", "none"),
        ]
    ):
        conn.execute(
            text(
                "INSERT INTO learning_evidence (id, user_id, concept_id, concept_version_id, evidence_type, grader, "
                f"on_demo_data, ref_type, ref_id, created_at) VALUES ('le-{i}', 'u-1', 'c-1', 'cv-1', '{etype}', "
                f"'deterministic', 0, '{ref}', {'NULL' if ref == 'none' else repr('ref-' + str(i))}, '{NOW}')"
            )
        )


def _evidence_insert(conn, ident, etype, ref, ref_id):
    conn.execute(
        text(
            "INSERT INTO learning_evidence (id, user_id, concept_id, concept_version_id, evidence_type, grader, "
            f"on_demo_data, ref_type, ref_id, created_at) VALUES ('{ident}', 'u-1', 'c-1', 'cv-1', '{etype}', "
            f"'deterministic', 0, '{ref}', '{ref_id}', '{NOW}')"
        )
    )


def _agent_run_insert(conn, ident, role):
    conn.execute(
        text(
            "INSERT INTO agent_runs (id, task_run_id, agent_version_id, status, role, timeout_seconds, fencing_token, created_at) "
            f"VALUES ('{ident}', 'tr-1', 'av-1', 'created', '{role}', 60, 0, '{NOW}')"
        )
    )


def _checks(engine, table):
    return {c["name"]: c["sqltext"] for c in sa.inspect(engine).get_check_constraints(table)}


def test_r1_is_additive_and_leaves_existing_tables_untouched(db_path):
    command.upgrade(_cfg(), PREVIOUS_HEAD)
    engine = _engine(db_path)
    with engine.begin() as conn:
        _seed(conn)
    before = {t: _checks(engine, t) for t in ("learning_evidence", "agent_runs")}
    assert not (NEW_TABLES & set(sa.inspect(engine).get_table_names()))
    engine.dispose()

    command.upgrade(_cfg(), R1)
    engine = _engine(db_path)
    assert NEW_TABLES <= set(sa.inspect(engine).get_table_names())
    assert before == {t: _checks(engine, t) for t in ("learning_evidence", "agent_runs")}
    with engine.begin() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM learning_evidence")).scalar_one() == 4
        _clean(conn)
    engine.dispose()


def test_r2_extends_evidence_enums_preserving_every_row_and_constraint(db_path):
    command.upgrade(_cfg(), R1)
    engine = _engine(db_path)
    with engine.begin() as conn:
        _seed(conn)
        old_rows = conn.execute(text("SELECT * FROM learning_evidence ORDER BY id")).fetchall()
    old_checks = _checks(engine, "learning_evidence")
    engine.dispose()

    command.upgrade(_cfg(), R2)
    engine = _engine(db_path)
    with engine.begin() as conn:
        assert conn.execute(text("SELECT * FROM learning_evidence ORDER BY id")).fetchall() == old_rows
        for i, etype in enumerate(
            ("explain_back", "modification", "reproduction", "debugging", "project_assessment")
        ):
            _evidence_insert(conn, f"new-{i}", etype, "assessment_result", f"res-{i}")
        _clean(conn)
    new_checks = _checks(engine, "learning_evidence")
    # exactly the two enum CHECKs changed; every other CHECK is preserved verbatim
    changed = {n for n in old_checks if old_checks[n] != new_checks.get(n)}
    assert changed == {"ck_learning_evidence_evidencetype", "ck_learning_evidence_evidencereftype"}
    assert set(new_checks) == set(old_checks), "no CHECK may be duplicated or lost by the rebuild"
    index_names = {i["name"] for i in sa.inspect(engine).get_indexes("learning_evidence")}
    assert {"uq_learning_evidence_assessment_ref", "uq_learning_evidence_experiment_ref"} <= index_names
    engine.dispose()


def test_r2_database_enforces_assessment_evidence_idempotency(db_path):
    command.upgrade(_cfg(), R2)
    engine = _engine(db_path)
    with engine.begin() as conn:
        _seed(conn)
        _evidence_insert(conn, "e-1", "modification", "assessment_result", "res-1")
    with pytest.raises(IntegrityError), engine.begin() as conn:
        _evidence_insert(conn, "e-2", "modification", "assessment_result", "res-1")
    with engine.begin() as conn:  # same result, different concept would be a different row
        conn.execute(
            text(
                f"INSERT INTO concepts (id, slug, name, level, kind, is_core, created_at) VALUES ('c-2', 's2', 'C2', 'foundational', 'mechanism', 0, '{NOW}')"
            )
        )
        conn.execute(
            text(
                f"INSERT INTO concept_versions (id, concept_id, version, plain_definition, content_origin, status, created_at) VALUES ('cv-2', 'c-2', 1, 'd', 'ai_drafted_unreviewed', 'active', '{NOW}')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO learning_evidence (id, user_id, concept_id, concept_version_id, evidence_type, grader, "
                f"on_demo_data, ref_type, ref_id, created_at) VALUES ('e-3', 'u-1', 'c-2', 'cv-2', 'modification', "
                f"'deterministic', 0, 'assessment_result', 'res-1', '{NOW}')"
            )
        )
    with pytest.raises(IntegrityError):  # a value outside the extended enum is still rejected
        with engine.begin() as conn:
            _evidence_insert(conn, "e-4", "mastery", "none", "x")
    engine.dispose()


def test_r3_adds_grader_role_preserving_agent_runs(db_path):
    command.upgrade(_cfg(), R2)
    engine = _engine(db_path)
    with engine.begin() as conn:
        _seed(conn)
    engine.dispose()

    command.upgrade(_cfg(), R3)
    engine = _engine(db_path)
    with engine.begin() as conn:
        _agent_run_insert(conn, "run-grader", "grader")
        roles = {r[0] for r in conn.execute(text("SELECT role FROM agent_runs"))}
        assert roles == {"primary", "reviewer", "repair", "evaluator", "professor", "grader"}
        _clean(conn)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        _agent_run_insert(conn, "run-bad", "teacher")
    engine.dispose()


def test_downgrades_refuse_while_assessment_data_exists_and_change_nothing(db_path):
    command.upgrade(_cfg(), R3)
    engine = _engine(db_path)
    with engine.begin() as conn:
        _seed(conn)
        _agent_run_insert(conn, "run-grader", "grader")
        _evidence_insert(conn, "e-1", "explain_back", "assessment_result", "res-1")
    engine.dispose()

    with pytest.raises(RuntimeError, match="Grader"):
        command.downgrade(_cfg(), R2)
    engine = _engine(db_path)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM agent_runs WHERE id = 'run-grader'"))
    engine.dispose()

    command.downgrade(_cfg(), R2)  # R3 -> R2 now fine
    with pytest.raises(RuntimeError, match="assessment-derived"):
        command.downgrade(_cfg(), R1)
    engine = _engine(db_path)
    with engine.begin() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM learning_evidence WHERE id = 'e-1'")).scalar_one() == 1
        conn.execute(text("DELETE FROM learning_evidence WHERE id = 'e-1'"))
    engine.dispose()

    command.downgrade(_cfg(), R1)  # R2 -> R1
    engine = _engine(db_path)
    assert "explain_back" not in _checks(engine, "learning_evidence")["ck_learning_evidence_evidencetype"]
    assert "uq_learning_evidence_assessment_ref" not in {
        i["name"] for i in sa.inspect(engine).get_indexes("learning_evidence")
    }
    with engine.begin() as conn:
        _clean(conn)
    engine.dispose()

    # R1 downgrade refuses only while an attempt exists
    command.upgrade(_cfg(), R1)
    engine = _engine(db_path)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO assessment_definitions (id, definition_key, version, status, assessment_kind, title, "
                "instructions_md, produces_evidence_type, criteria_json, independence_policy_json, grading_policy_json, "
                f"allowed_resources_json, author_user_id, created_at) VALUES ('d-1', 'k', 1, 'draft', 'knowledge_check', 't', 'i', "
                f"'knowledge_check', '[]', '{{}}', '{{}}', '[]', 'u-1', '{NOW}')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO assessment_attempts (id, user_id, definition_id, origin, status, pinned_versions_json, "
                f"fresh_required, grading_round, started_at, created_at) VALUES ('at-1', 'u-1', 'd-1', 'learner_started', 'draft', '{{}}', 0, 0, '{NOW}', '{NOW}')"
            )
        )
    engine.dispose()
    with pytest.raises(RuntimeError, match="assessment attempt"):
        command.downgrade(_cfg(), PREVIOUS_HEAD)


def test_full_chain_on_a_fresh_disposable_database(db_path):
    command.upgrade(_cfg(), "head")
    engine = _engine(db_path)
    assert NEW_TABLES <= set(sa.inspect(engine).get_table_names())
    with engine.begin() as conn:
        _clean(conn)
    assert command.current  # alembic api present
    heads = {row[0] for row in engine.connect().execute(text("SELECT version_num FROM alembic_version"))}
    assert heads == {R8}
    engine.dispose()


def test_migrated_assessment_schema_matches_the_models(db_path):
    """The five new tables and the two learning_evidence indexes must be exactly
    what the ORM models declare (create_all-built test databases rely on it)."""
    command.upgrade(_cfg(), "head")
    engine = _engine(db_path)
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": False})
        diffs = compare_metadata(ctx, Base.metadata)
    engine.dispose()

    def about_ours(d):
        blob = repr(d)
        return "assessment_" in blob and "assessment_ready_submissions" not in blob

    relevant = [d for d in diffs if about_ours(d)]
    assert relevant == [], relevant
