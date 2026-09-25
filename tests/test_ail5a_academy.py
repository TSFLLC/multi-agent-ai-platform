from datetime import datetime, timezone

import pytest

from app.db.enums import (
    AcademyEnrollmentStatus,
    AcademyProgramItemKind,
    ConceptKind,
    ConceptLevel,
    EvidenceType,
    GradingMode,
    PlanItemOrigin,
    PlanItemState,
)
from app.errors import ConflictError
from app.models.academy import AcademyProgramItem
from app.models.learner import LearningPlanItem
from app.services.academy_service import AcademyService
from app.services.concept_graph_service import ConceptGraphService
from app.services.learning_evidence_service import LearningEvidenceService
from tests.ail1a_factories import make_published_version, make_user


def _user_and_concepts(db):
    user = make_user(db)
    graph = ConceptGraphService(db)
    first = graph.create_concept(slug="tokens", name="Tokens", level=ConceptLevel.FOUNDATIONAL, kind=ConceptKind.DEFINITIONAL)
    second = graph.create_concept(slug="context-windows", name="Context Windows", level=ConceptLevel.FOUNDATIONAL, kind=ConceptKind.DEFINITIONAL)
    first_version = make_published_version(db, first)
    second_version = make_published_version(db, second)
    from app.db.enums import ConceptRelationType
    graph.add_relation(from_concept_id=first.id, to_concept_id=second.id, relation_type=ConceptRelationType.PREREQUISITE)
    return user, first, second, first_version, second_version


def test_program_version_publication_validates_references_and_prerequisites(db):
    user, first, second, _, _ = _user_and_concepts(db)
    service = AcademyService(db)
    program = service.create_program(user, slug="foundations", title="Foundations")
    version = service.create_version(user, program.id, duration_days=30)
    service.add_item(user, version.id, week=1, day=1, module_key="week-1", position=0, item_kind=AcademyProgramItemKind.CONCEPT, concept_id=second.id)
    service.add_item(user, version.id, week=1, day=2, module_key="week-1", position=0, item_kind=AcademyProgramItemKind.CONCEPT, concept_id=first.id)
    with pytest.raises(ConflictError, match="scheduled after"):
        service.publish_version(user, version.id)


def test_enrollment_is_pinned_and_projects_program_origin_plan(db):
    user, first, second, _, _ = _user_and_concepts(db)
    service = AcademyService(db)
    program = service.create_program(user, slug="foundations", title="Foundations")
    version = service.create_version(user, program.id, duration_days=30)
    service.add_item(user, version.id, week=1, day=1, module_key="week-1", position=0, item_kind=AcademyProgramItemKind.CONCEPT, concept_id=first.id)
    service.add_item(user, version.id, week=1, day=2, module_key="week-1", position=0, item_kind=AcademyProgramItemKind.CONCEPT, concept_id=second.id)
    published = service.publish_version(user, version.id)
    enrollment = service.enroll(user, published.id)
    assert enrollment.status == AcademyEnrollmentStatus.ACTIVE
    assert enrollment.program_version_id == published.id
    plan = db.query(LearningPlanItem).filter_by(user_id=user.id).all()
    assert {row.origin for row in plan} == {PlanItemOrigin.PROGRAM}
    assert {row.state for row in plan} == {PlanItemState.PLANNED}


def test_progress_is_evidence_derived_and_page_views_do_not_advance_it(db):
    user, first, _, first_version, _ = _user_and_concepts(db)
    service = AcademyService(db)
    program = service.create_program(user, slug="evidence", title="Evidence")
    version = service.create_version(user, program.id, duration_days=30)
    item = service.add_item(user, version.id, week=1, day=1, module_key="week-1", position=0, item_kind=AcademyProgramItemKind.CONCEPT, concept_id=first.id)
    published = service.publish_version(user, version.id)
    enrollment = service.enroll(user, published.id)
    before = service.progress(user.id, enrollment)
    assert before.completed_items == 0
    assert before.exposed == 0
    LearningEvidenceService(db).record_evidence(
        user_id=user.id, concept_id=first.id, concept_version_id=first_version.id,
        evidence_type=EvidenceType.LAB, grader=GradingMode.DETERMINISTIC, passed=True,
    )
    after = service.progress(user.id, enrollment)
    assert after.completed_items == 1
    assert after.practiced_or_better == 1
    assert after.complete is True


def test_enrollment_isolation_and_lifecycle(db):
    user, first, _, _, _ = _user_and_concepts(db)
    other = make_user(db, email="other@example.com")
    service = AcademyService(db)
    program = service.create_program(user, slug="private", title="Private")
    version = service.create_version(user, program.id, duration_days=5)
    service.add_item(user, version.id, week=1, day=1, module_key="week-1", position=0, item_kind=AcademyProgramItemKind.CONCEPT, concept_id=first.id)
    published = service.publish_version(user, version.id)
    enrollment = service.enroll(user, published.id)
    with pytest.raises(Exception):
        service.progress(other.id, service._own_enrollment(other.id, enrollment.id))
    service.change_status(user, enrollment.id, AcademyEnrollmentStatus.PAUSED)
    service.change_status(user, enrollment.id, AcademyEnrollmentStatus.ACTIVE)
    service.change_status(user, enrollment.id, AcademyEnrollmentStatus.WITHDRAWN)
    assert service._own_enrollment(user.id, enrollment.id).status == AcademyEnrollmentStatus.WITHDRAWN
