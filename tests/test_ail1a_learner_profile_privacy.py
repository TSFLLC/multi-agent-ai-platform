import pytest

from app.db.enums import ConceptLevel, EvidenceType, GradingMode, LearnerDepth
from app.errors import ConflictError, NotFoundError
from app.services.learner_profile_service import LearnerProfileService
from app.services.learning_evidence_service import LearningEvidenceService
from tests.ail1a_factories import make_concept, make_published_version, make_user


def test_get_or_create_profile_is_idempotent(db):
    user = make_user(db)
    service = LearnerProfileService(db)
    first = service.get_or_create_profile(user.id)
    second = service.get_or_create_profile(user.id)
    assert first.id == second.id


def test_update_profile_only_touches_explicit_fields(db):
    user = make_user(db)
    service = LearnerProfileService(db)
    service.update_profile(user.id, level=ConceptLevel.PRACTITIONER, weekly_minutes=180)
    updated = service.update_profile(user.id, goal_text="Get good at agentic AI")

    assert updated.level == ConceptLevel.PRACTITIONER
    assert updated.weekly_minutes == 180
    assert updated.goal_text == "Get good at agentic AI"


def test_set_interest_requires_exactly_one_target(db):
    user = make_user(db)
    service = LearnerProfileService(db)
    with pytest.raises(ConflictError):
        service.set_interest(user.id)


def test_interests_are_only_ever_explicit(db):
    """spec Sec 30: interests come from an explicit call with a concept_id
    or term_id supplied by the caller -- nothing in this service reads
    task content/code/prompts/artifacts to infer one."""
    user = make_user(db)
    concept = make_concept(db)
    service = LearnerProfileService(db)
    interest = service.set_interest(user.id, concept_id=concept.id, watch=True)
    assert interest.concept_id == concept.id
    assert interest.watch is True


def test_export_learner_data_includes_every_owned_table(db):
    user = make_user(db)
    version = make_published_version(db)
    profile_service = LearnerProfileService(db)
    profile_service.update_profile(user.id, depth=LearnerDepth.WORKING)
    profile_service.set_interest(user.id, concept_id=version.concept_id)
    LearningEvidenceService(db).record_evidence(
        user_id=user.id,
        concept_id=version.concept_id,
        concept_version_id=version.id,
        evidence_type=EvidenceType.LESSON_COMPLETED,
        grader=GradingMode.DETERMINISTIC,
    )

    export = profile_service.export_learner_data(user.id)
    assert export["profile"]["depth"] == "working"
    assert len(export["interests"]) == 1
    assert len(export["evidence"]) == 1


def test_delete_learner_data_removes_everything(db):
    user = make_user(db)
    version = make_published_version(db)
    profile_service = LearnerProfileService(db)
    profile_service.update_profile(user.id, depth=LearnerDepth.WORKING)
    profile_service.set_interest(user.id, concept_id=version.concept_id)
    LearningEvidenceService(db).record_evidence(
        user_id=user.id,
        concept_id=version.concept_id,
        concept_version_id=version.id,
        evidence_type=EvidenceType.LESSON_COMPLETED,
        grader=GradingMode.DETERMINISTIC,
    )

    profile_service.delete_learner_data(user.id)

    assert profile_service.get_profile(user.id) is None
    assert profile_service.list_interests(user.id) == []
    export = profile_service.export_learner_data(user.id)
    assert export["evidence"] == []


def test_delete_learner_data_never_touches_the_concept_itself(db):
    """Deletion is scoped to the learner's own rows -- the shared Concept
    Graph (owned platform-wide, not per-user) must survive."""
    user = make_user(db)
    version = make_published_version(db)
    LearnerProfileService(db).set_interest(user.id, concept_id=version.concept_id)

    from app.services.concept_graph_service import ConceptGraphService

    LearnerProfileService(db).delete_learner_data(user.id)
    assert ConceptGraphService(db).get_concept(version.concept_id) is not None


def test_delete_learner_data_unknown_user_raises_not_found(db):
    with pytest.raises(NotFoundError):
        LearnerProfileService(db).delete_learner_data("00000000-0000-0000-0000-000000000000")
