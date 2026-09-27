from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.db.enums import ConceptKind, ConceptLevel, GradingMode, LearningItemType
from app.errors import ConflictError
from app.models.concepts import Concept, LearningItem
from app.services.concept_graph_service import ConceptGraphService


def _concept(db):
    concept = Concept(
        slug="versioned-concept",
        name="Versioned Concept",
        level=ConceptLevel.FOUNDATIONAL,
        kind=ConceptKind.MECHANISM,
    )
    db.add(concept)
    db.commit()
    return concept


def _item(db, concept, *, title="Original"):
    return ConceptGraphService(db).create_learning_item(
        concept_id=concept.id,
        item_type=LearningItemType.CHECK_QUESTION,
        title=title,
        body_md="Original body",
        spec={"kind": "choice", "options": ["A"], "answer_key": [0], "multiple": False},
        grading_mode=GradingMode.DETERMINISTIC,
        reviewed=True,
    )


def test_learning_item_v2_is_append_only_and_latest_resolution_is_unique(db):
    concept = _concept(db)
    v1 = _item(db, concept)
    original = (v1.title, v1.body_md, v1.spec, v1.version, v1.lineage_id)
    service = ConceptGraphService(db)

    v2 = service.create_learning_item_version(
        lineage_id=v1.lineage_id,
        expected_current_version=1,
        item_type=LearningItemType.CHECK_QUESTION,
        title="Executable revision",
        body_md="New body",
        spec={"kind": "choice", "options": ["A", "B"], "answer_key": [1], "multiple": False},
        grading_mode=GradingMode.DETERMINISTIC,
        reviewed=True,
    )

    db.expire_all()
    preserved = db.get(LearningItem, v1.id)
    assert (preserved.title, preserved.body_md, preserved.spec, preserved.version, preserved.lineage_id) == original
    assert v2.id != v1.id and v2.version == 2 and v2.lineage_id == v1.lineage_id
    assert service.list_current_learning_items(concept.id) == [v2]


def test_identical_revision_is_idempotent_and_conflicting_expected_revision_fails(db):
    concept = _concept(db)
    v1 = _item(db, concept)
    service = ConceptGraphService(db)
    kwargs = dict(
        lineage_id=v1.lineage_id,
        expected_current_version=1,
        item_type=LearningItemType.CHECK_QUESTION,
        title="Executable revision",
        body_md="New body",
        spec={"kind": "choice", "options": ["A", "B"], "answer_key": [1], "multiple": False},
        grading_mode=GradingMode.DETERMINISTIC,
        reviewed=True,
    )
    v2 = service.create_learning_item_version(**kwargs)
    assert service.create_learning_item_version(**kwargs).id == v2.id
    with pytest.raises(ConflictError, match="expected 1"):
        service.create_learning_item_version(**{**kwargs, "title": "Conflicting revision"})
    assert db.execute(select(LearningItem).where(LearningItem.lineage_id == v1.lineage_id)).scalars().all().__len__() == 2


def test_database_rejects_nonpositive_version_and_duplicate_lineage_version(db):
    concept = _concept(db)
    v1 = _item(db, concept)
    duplicate = LearningItem(
        concept_id=concept.id,
        item_type=LearningItemType.CHECK_QUESTION,
        title="Duplicate",
        lineage_id=v1.lineage_id,
        version=1,
    )
    db.add(duplicate)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    invalid = LearningItem(
        concept_id=concept.id,
        item_type=LearningItemType.CHECK_QUESTION,
        title="Invalid",
        version=0,
    )
    db.add(invalid)
    with pytest.raises(IntegrityError):
        db.commit()


def test_lineage_migration_backfills_without_changing_existing_rows(tmp_path, monkeypatch):
    from app.config import settings

    project_root = Path(__file__).resolve().parent.parent
    db_path = tmp_path / "ail5d-migration.db"
    monkeypatch.setattr(settings, "database_path", db_path)
    cfg = Config(str(project_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(project_root / "alembic"))
    command.upgrade(cfg, "ail5c_grader_agent_role")

    from app.db.session import build_engine

    engine = build_engine(f"sqlite:///{db_path.as_posix()}")
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO concepts (id, slug, name, level, kind, is_core, created_at) VALUES ('c-1', 'c-1', 'C', 'foundational', 'mechanism', 0, '2026-01-01')"))
        conn.execute(text("INSERT INTO learning_items (id, concept_id, item_type, title, body_md, spec_json, grading_mode, reviewed, version, created_at) VALUES ('li-1', 'c-1', 'check_question', 'One', 'Body', '{}', 'deterministic', 1, 1, '2026-01-01')"))
        conn.execute(text("INSERT INTO learning_items (id, concept_id, item_type, title, body_md, spec_json, grading_mode, reviewed, version, created_at) VALUES ('li-2', 'c-1', 'scenario', 'Two', 'Body 2', '{}', 'deterministic', 1, 1, '2026-01-01')"))
        before = conn.execute(text("SELECT id, title, body_md, version FROM learning_items ORDER BY id")).fetchall()

    command.upgrade(cfg, "ail5d_learning_item_lineage")
    with engine.begin() as conn:
        after = conn.execute(text("SELECT id, title, body_md, version FROM learning_items ORDER BY id")).fetchall()
        assert after == before
        rows = conn.execute(text("SELECT id, lineage_id, version FROM learning_items ORDER BY id")).fetchall()
        assert len({row[1] for row in rows}) == 2
        assert all(row[2] == 1 for row in rows)
        assert conn.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
        assert conn.execute(text("PRAGMA foreign_key_check")).fetchall() == []
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO learning_items (id, concept_id, item_type, title, body_md, spec_json, grading_mode, reviewed, lineage_id, version, created_at) VALUES ('li-3', 'c-1', 'check_question', 'One V2', 'New', '{}', 'deterministic', 1, :lineage, 2, '2026-01-01')"), {"lineage": rows[0][1]})
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(text("INSERT INTO learning_items (id, concept_id, item_type, title, reviewed, lineage_id, version, created_at) VALUES ('li-4', 'c-1', 'check_question', 'Duplicate', 1, :lineage, 2, '2026-01-01')"), {"lineage": rows[0][1]})
    engine.dispose()


def test_populated_lineage_migration_preserves_fk_references(tmp_path, monkeypatch):
    from app.config import settings
    from app.db.enums import EvidenceRefType, EvidenceType
    from app.models.learner import LearningEvidence
    from app.db.enums import GradingMode

    project_root = Path(__file__).resolve().parent.parent
    db_path = tmp_path / "ail5d-populated-failure.db"
    monkeypatch.setattr(settings, "database_path", db_path)
    cfg = Config(str(project_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(project_root / "alembic"))
    command.upgrade(cfg, "ail5c_grader_agent_role")
    from app.db.session import build_engine
    engine = build_engine(f"sqlite:///{db_path.as_posix()}")
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO organizations (id, name, created_at) VALUES ('org-fk', 'Org', '2026-01-01')"))
        conn.execute(text("INSERT INTO users (id, org_id, email, role, created_at) VALUES ('u-fk', 'org-fk', 'fk@example.com', 'owner', '2026-01-01')"))
        conn.execute(text("INSERT INTO concepts (id, slug, name, level, kind, is_core, created_at) VALUES ('c-fk', 'c-fk', 'C', 'foundational', 'mechanism', 0, '2026-01-01')"))
        conn.execute(text("INSERT INTO concept_versions (id, concept_id, version, plain_definition, content_origin, status, created_at) VALUES ('cv-fk', 'c-fk', 1, 'd', 'ai_drafted_unreviewed', 'active', '2026-01-01')"))
        conn.execute(text("INSERT INTO learning_items (id, concept_id, item_type, title, body_md, spec_json, grading_mode, reviewed, version, created_at) VALUES ('li-fk', 'c-fk', 'check_question', 'Referenced', 'Body', '{}', 'deterministic', 1, 1, '2026-01-01')"))
    with engine.begin() as conn:
        session = __import__('sqlalchemy.orm', fromlist=['Session']).Session(bind=conn)
        session.add(LearningEvidence(user_id='u-fk', concept_id='c-fk', concept_version_id='cv-fk', learning_item_id='li-fk', evidence_type=EvidenceType.LESSON_COMPLETED, grader=GradingMode.SELF, on_demo_data=False, ref_type=EvidenceRefType.NONE, passed=True))
        session.flush()
        # Model the SQLite batch-rebuild residue left when the old migration
        # copied rows and then failed while dropping the referenced table.
        conn.execute(text("CREATE TABLE _alembic_tmp_learning_items (id VARCHAR(36) PRIMARY KEY)"))
    command.upgrade(cfg, "ail5d_learning_item_lineage")
    with engine.begin() as conn:
        assert conn.execute(text("SELECT name FROM sqlite_master WHERE name = '_alembic_tmp_learning_items'")).first() is None
        assert conn.execute(text("SELECT id FROM learning_items WHERE id = 'li-fk'")).scalar_one() == "li-fk"
        assert conn.execute(text("SELECT learning_item_id FROM learning_evidence WHERE id = (SELECT id FROM learning_evidence LIMIT 1)")).scalar_one() == "li-fk"
        assert conn.execute(text("SELECT lineage_id, version FROM learning_items WHERE id = 'li-fk'")).first()[1] == 1
        assert conn.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
        assert conn.execute(text("PRAGMA foreign_key_check")).fetchall() == []
    engine.dispose()


def _migration_config(project_root):
    cfg = Config(str(project_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(project_root / "alembic"))
    return cfg


def test_lineage_migration_resumes_exact_interrupted_add_column_state(tmp_path, monkeypatch):
    from app.config import settings
    from app.db.session import build_engine

    project_root = Path(__file__).resolve().parent.parent
    db_path = tmp_path / "ail5d-interrupted.db"
    monkeypatch.setattr(settings, "database_path", db_path)
    cfg = _migration_config(project_root)
    command.upgrade(cfg, "ail5c_grader_agent_role")
    engine = build_engine(f"sqlite:///{db_path.as_posix()}")
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO concepts (id, slug, name, level, kind, is_core, created_at) VALUES ('c-i', 'c-i', 'C', 'foundational', 'mechanism', 0, '2026-01-01')"))
        conn.execute(text("INSERT INTO learning_items (id, concept_id, item_type, title, reviewed, version, created_at) VALUES ('li-i', 'c-i', 'check_question', 'Interrupted', 1, 1, '2026-01-01')"))
        conn.execute(text("ALTER TABLE learning_items ADD COLUMN lineage_id VARCHAR(36)"))
    command.upgrade(cfg, "ail5d_learning_item_lineage")
    with engine.begin() as conn:
        assert conn.execute(text("SELECT lineage_id FROM learning_items WHERE id = 'li-i'")).scalar_one()
        assert conn.execute(text("SELECT name FROM sqlite_master WHERE name = 'uq_learning_items_lineage_version'")).first()
        assert conn.execute(text("PRAGMA foreign_key_check")).fetchall() == []
    engine.dispose()


def test_lineage_migration_resumes_preserving_valid_existing_lineage_ids(tmp_path, monkeypatch):
    from app.config import settings
    from app.db.session import build_engine

    project_root = Path(__file__).resolve().parent.parent
    db_path = tmp_path / "ail5d-partial-backfill.db"
    monkeypatch.setattr(settings, "database_path", db_path)
    cfg = _migration_config(project_root)
    command.upgrade(cfg, "ail5c_grader_agent_role")
    engine = build_engine(f"sqlite:///{db_path.as_posix()}")
    existing = "11111111-1111-4111-8111-111111111111"
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO concepts (id, slug, name, level, kind, is_core, created_at) VALUES ('c-p', 'c-p', 'C', 'foundational', 'mechanism', 0, '2026-01-01')"))
        conn.execute(text("INSERT INTO learning_items (id, concept_id, item_type, title, reviewed, version, created_at) VALUES ('li-p1', 'c-p', 'check_question', 'Populated', 1, 1, '2026-01-01')"))
        conn.execute(text("INSERT INTO learning_items (id, concept_id, item_type, title, reviewed, version, created_at) VALUES ('li-p2', 'c-p', 'scenario', 'Missing', 1, 1, '2026-01-01')"))
        conn.execute(text("ALTER TABLE learning_items ADD COLUMN lineage_id VARCHAR(36)"))
        conn.execute(text("UPDATE learning_items SET lineage_id = :lineage WHERE id = 'li-p1'"), {"lineage": existing})
    command.upgrade(cfg, "ail5d_learning_item_lineage")
    with engine.begin() as conn:
        assert conn.execute(text("SELECT lineage_id FROM learning_items WHERE id = 'li-p1'")).scalar_one() == existing
        assert conn.execute(text("SELECT lineage_id FROM learning_items WHERE id = 'li-p2'")).scalar_one() != existing
    engine.dispose()


def test_lineage_migration_fails_closed_for_incompatible_partial_schema(tmp_path, monkeypatch):
    from app.config import settings
    from app.db.session import build_engine

    project_root = Path(__file__).resolve().parent.parent
    db_path = tmp_path / "ail5d-incompatible.db"
    monkeypatch.setattr(settings, "database_path", db_path)
    cfg = _migration_config(project_root)
    command.upgrade(cfg, "ail5c_grader_agent_role")
    engine = build_engine(f"sqlite:///{db_path.as_posix()}")
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE learning_items ADD COLUMN lineage_id INTEGER"))
    with pytest.raises(RuntimeError, match="lineage_id must be VARCHAR\\(36\\)"):
        command.upgrade(cfg, "ail5d_learning_item_lineage")
    engine.dispose()


def test_lineage_migration_resume_is_idempotent_when_indexes_already_exist(tmp_path, monkeypatch):
    from app.config import settings
    from app.db.session import build_engine

    project_root = Path(__file__).resolve().parent.parent
    db_path = tmp_path / "ail5d-index-boundary.db"
    monkeypatch.setattr(settings, "database_path", db_path)
    cfg = _migration_config(project_root)
    command.upgrade(cfg, "ail5c_grader_agent_role")
    engine = build_engine(f"sqlite:///{db_path.as_posix()}")
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO concepts (id, slug, name, level, kind, is_core, created_at) VALUES ('c-x', 'c-x', 'C', 'foundational', 'mechanism', 0, '2026-01-01')"))
        conn.execute(text("INSERT INTO learning_items (id, concept_id, item_type, title, reviewed, version, created_at) VALUES ('li-x', 'c-x', 'check_question', 'Indexed', 1, 1, '2026-01-01')"))
        conn.execute(text("ALTER TABLE learning_items ADD COLUMN lineage_id VARCHAR(36)"))
        conn.execute(text("UPDATE learning_items SET lineage_id = '22222222-2222-4222-8222-222222222222' WHERE id = 'li-x'"))
        conn.execute(text("CREATE UNIQUE INDEX uq_learning_items_lineage_version ON learning_items (lineage_id, version)"))
        conn.execute(text("CREATE INDEX ix_learning_items_lineage_id ON learning_items (lineage_id)"))
    command.upgrade(cfg, "ail5d_learning_item_lineage")
    with engine.begin() as conn:
        names = [row[0] for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'learning_items'"))]
        assert names.count("uq_learning_items_lineage_version") == 1
        assert names.count("ix_learning_items_lineage_id") == 1
        assert conn.execute(text("PRAGMA foreign_key_check")).fetchall() == []
    engine.dispose()
