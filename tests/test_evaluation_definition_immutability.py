"""Evaluation Definition Version immutability — structural (DB-level)
guarantees, mirroring tests/test_agent_version_immutability.py exactly:
"editing" a published rubric version must always insert a new row with
version+1, never mutate the published row's content columns in place.
"""

import pytest
from sqlalchemy.exc import IntegrityError

from app.db.enums import VersionStatus
from app.models.evaluation_definitions import EvaluationCriterion, EvaluationDefinitionVersion
from tests.conftest import make_evaluation_definition, make_evaluation_definition_version


def test_duplicate_version_number_rejected(db):
    definition = make_evaluation_definition(db)
    make_evaluation_definition_version(db, definition=definition, version=1, status=VersionStatus.ACTIVE)

    dup = EvaluationDefinitionVersion(evaluation_definition_id=definition.id, version=1, status=VersionStatus.DRAFT)
    db.add(dup)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_duplicate_criterion_key_within_one_version_rejected(db):
    version = make_evaluation_definition_version(db)  # already has one "correctness" criterion
    dup = EvaluationCriterion(
        evaluation_definition_version_id=version.id, key="correctness", label="Duplicate", order_index=1
    )
    db.add(dup)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_same_criterion_key_is_allowed_across_different_versions(db):
    """The uniqueness constraint is scoped to one version, never global --
    the whole point of versioning is that "correctness" can appear again,
    unchanged or refined, in every version of the same rubric."""
    definition = make_evaluation_definition(db)
    v1 = make_evaluation_definition_version(db, definition=definition, version=1)
    v2 = EvaluationDefinitionVersion(evaluation_definition_id=definition.id, version=2, status=VersionStatus.DRAFT)
    db.add(v2)
    db.flush()
    db.add(
        EvaluationCriterion(evaluation_definition_version_id=v2.id, key="correctness", label="Correctness v2")
    )
    db.commit()  # must not raise -- different version, same key is fine

    assert v1.version == 1
    assert v2.version == 2


def test_editing_a_published_version_creates_a_new_row(db):
    definition = make_evaluation_definition(db)
    v1 = make_evaluation_definition_version(db, definition=definition, version=1, status=VersionStatus.ACTIVE)
    original_description = v1.description

    v2 = make_evaluation_definition_version(db, definition=definition, version=2, status=VersionStatus.DRAFT)

    db.refresh(v1)
    assert v1.description == original_description
    assert v1.status == VersionStatus.ACTIVE
    assert v2.id != v1.id
    assert v2.version == v1.version + 1


def test_historical_criteria_are_untouched_by_a_later_version(db):
    """Something that referenced v1's criteria (a future EvaluationRun,
    out of scope for Slice 1) must keep resolving to v1's exact content
    even after v2 exists -- the same reproducibility guarantee
    AgentVersion already gives Agent Runs."""
    definition = make_evaluation_definition(db)
    v1 = make_evaluation_definition_version(db, definition=definition, version=1, status=VersionStatus.ACTIVE)
    v1_criterion_id = v1.criteria[0].id

    make_evaluation_definition_version(db, definition=definition, version=2, status=VersionStatus.DRAFT)

    db.refresh(v1)
    assert len(v1.criteria) == 1
    assert v1.criteria[0].id == v1_criterion_id
    assert v1.criteria[0].key == "correctness"


def test_cascade_delete_removes_versions_and_criteria(db):
    """Deleting the parent Evaluation Definition cascades to its versions
    and their criteria -- same ON DELETE CASCADE discipline as Agent ->
    AgentVersion."""
    from app.models.evaluation_definitions import EvaluationDefinition

    definition = make_evaluation_definition(db)
    version = make_evaluation_definition_version(db, definition=definition)
    db.commit()
    version_id = version.id
    criterion_id = version.criteria[0].id

    db.delete(db.get(EvaluationDefinition, definition.id))
    db.commit()

    # Session.get() would return a stale identity-map hit here since
    # expire_on_commit=False — the DB-level cascade doesn't expire the
    # ORM's cached instances, so a real query is required (same pattern as
    # tests/test_foreign_keys.py::test_cascade_delete_removes_dependent_versions).
    assert db.query(EvaluationDefinitionVersion).filter_by(id=version_id).first() is None
    assert db.query(EvaluationCriterion).filter_by(id=criterion_id).first() is None
