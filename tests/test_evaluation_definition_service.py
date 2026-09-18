"""EvaluationDefinitionService — MA6 Slice 1.

Mirrors tests/test_agent_registry_service.py's shape, applied to the
Evaluation Definition Registry (rubric family -> versioned criteria list,
DRAFT -> ACTIVE -> DEPRECATED -> RETIRED, immutable once published).
"""

import pytest

from app.db.enums import VersionStatus
from app.errors import ConflictError, InvalidStateTransitionError, NotFoundError
from app.services.evaluation_definition_service import EvaluationDefinitionService
from tests.conftest import make_evaluation_definition, make_evaluation_definition_version, make_project


def _criteria(*keys):
    return [{"key": k, "label": k.title()} for k in keys]


# -- Evaluation Definitions ---------------------------------------------------


def test_create_definition(db):
    project = make_project(db)
    db.commit()
    svc = EvaluationDefinitionService(db)

    definition = svc.create_definition(project_id=project.id, name="Research Rubric", description="d")

    assert definition.id is not None
    assert definition.project_id == project.id
    assert definition.name == "Research Rubric"
    assert definition.current_status is None  # no version published yet


def test_list_definitions_scoped_to_project(db):
    project_a = make_project(db, name="A")
    project_b = make_project(db, name="B")
    db.commit()
    svc = EvaluationDefinitionService(db)
    svc.create_definition(project_id=project_a.id, name="A rubric")
    svc.create_definition(project_id=project_b.id, name="B rubric")

    result = svc.list_definitions(project_id=project_a.id)
    assert [d.name for d in result] == ["A rubric"]


def test_get_definition_not_found_returns_none(db):
    svc = EvaluationDefinitionService(db)
    assert svc.get_definition("does-not-exist") is None


# -- Evaluation Definition Versions -------------------------------------------


def test_create_version_with_ordered_criteria(db):
    definition = make_evaluation_definition(db)
    db.commit()
    svc = EvaluationDefinitionService(db)

    version = svc.create_version(
        evaluation_definition_id=definition.id,
        description="v1",
        criteria=[
            {"key": "correctness", "label": "Correctness", "description": "d1", "method_hint": "agent_judge"},
            {"key": "coverage", "label": "Requirement Coverage"},
        ],
    )

    assert version.version == 1
    assert version.status == VersionStatus.DRAFT
    assert [c.key for c in version.criteria] == ["correctness", "coverage"]  # stable order preserved
    assert version.criteria[0].order_index == 0
    assert version.criteria[1].order_index == 1
    assert version.criteria[0].method_hint == "agent_judge"


def test_create_version_auto_increments_version_number(db):
    definition = make_evaluation_definition(db)
    db.commit()
    svc = EvaluationDefinitionService(db)
    v1 = svc.create_version(evaluation_definition_id=definition.id, description=None, criteria=_criteria("a"))
    v2 = svc.create_version(evaluation_definition_id=definition.id, description=None, criteria=_criteria("a"))

    assert v1.version == 1
    assert v2.version == 2


def test_create_version_rejects_empty_criteria(db):
    definition = make_evaluation_definition(db)
    db.commit()
    svc = EvaluationDefinitionService(db)

    with pytest.raises(ConflictError):
        svc.create_version(evaluation_definition_id=definition.id, description=None, criteria=[])


def test_create_version_rejects_duplicate_criterion_keys(db):
    definition = make_evaluation_definition(db)
    db.commit()
    svc = EvaluationDefinitionService(db)

    with pytest.raises(ConflictError):
        svc.create_version(
            evaluation_definition_id=definition.id,
            description=None,
            criteria=[{"key": "x", "label": "X1"}, {"key": "x", "label": "X2"}],
        )


# -- Lifecycle transitions -----------------------------------------------------


def test_publish_version_transitions_draft_to_active_and_syncs_parent(db):
    version = make_evaluation_definition_version(db, status=VersionStatus.DRAFT)
    db.commit()
    svc = EvaluationDefinitionService(db)

    published = svc.publish_version(version.id)

    assert published.status == VersionStatus.ACTIVE
    assert published.published_at is not None
    db.refresh(published.evaluation_definition)
    assert published.evaluation_definition.current_status == VersionStatus.ACTIVE


def test_publish_already_active_version_rejected(db):
    version = make_evaluation_definition_version(db, status=VersionStatus.ACTIVE)
    db.commit()
    svc = EvaluationDefinitionService(db)

    with pytest.raises(InvalidStateTransitionError):
        svc.publish_version(version.id)


def test_deprecate_requires_active(db):
    draft_version = make_evaluation_definition_version(db, status=VersionStatus.DRAFT)
    db.commit()
    svc = EvaluationDefinitionService(db)

    with pytest.raises(InvalidStateTransitionError):
        svc.deprecate_version(draft_version.id)


def test_full_lifecycle_draft_active_deprecated_retired(db):
    version = make_evaluation_definition_version(db, status=VersionStatus.DRAFT)
    db.commit()
    svc = EvaluationDefinitionService(db)

    svc.publish_version(version.id)
    svc.deprecate_version(version.id)
    retired = svc.retire_version(version.id)

    assert retired.status == VersionStatus.RETIRED


def test_retire_allowed_from_active_or_deprecated(db):
    v_active = make_evaluation_definition_version(db, status=VersionStatus.ACTIVE)
    v_deprecated = make_evaluation_definition_version(
        db, definition=v_active.evaluation_definition, version=2, status=VersionStatus.DEPRECATED
    )
    db.commit()
    svc = EvaluationDefinitionService(db)

    assert svc.retire_version(v_active.id).status == VersionStatus.RETIRED
    assert svc.retire_version(v_deprecated.id).status == VersionStatus.RETIRED


def test_retire_from_draft_rejected(db):
    version = make_evaluation_definition_version(db, status=VersionStatus.DRAFT)
    db.commit()
    svc = EvaluationDefinitionService(db)

    with pytest.raises(InvalidStateTransitionError):
        svc.retire_version(version.id)


def test_publish_nonexistent_version_raises_not_found(db):
    svc = EvaluationDefinitionService(db)
    with pytest.raises(NotFoundError):
        svc.publish_version("does-not-exist")


# -- Immutability (Slice 1's core guarantee) ----------------------------------


def test_publishing_never_mutates_criteria_content(db):
    definition = make_evaluation_definition(db)
    db.commit()
    svc = EvaluationDefinitionService(db)
    version = svc.create_version(
        evaluation_definition_id=definition.id,
        description="original",
        criteria=[{"key": "correctness", "label": "Correctness", "description": "original desc"}],
    )
    original_criterion_id = version.criteria[0].id

    svc.publish_version(version.id)
    db.refresh(version)

    assert version.description == "original"
    assert len(version.criteria) == 1
    assert version.criteria[0].id == original_criterion_id
    assert version.criteria[0].description == "original desc"


def test_editing_a_published_rubric_means_a_new_version_not_a_mutation(db):
    definition = make_evaluation_definition(db)
    db.commit()
    svc = EvaluationDefinitionService(db)
    v1 = svc.create_version(
        evaluation_definition_id=definition.id, description=None, criteria=[{"key": "a", "label": "A"}]
    )
    svc.publish_version(v1.id)

    v2 = svc.create_version(
        evaluation_definition_id=definition.id,
        description=None,
        criteria=[{"key": "a", "label": "A"}, {"key": "b", "label": "B (added in v2)"}],
    )

    db.refresh(v1)
    assert v1.version == 1
    assert len(v1.criteria) == 1  # untouched by v2's extra criterion
    assert v2.version == 2
    assert len(v2.criteria) == 2
