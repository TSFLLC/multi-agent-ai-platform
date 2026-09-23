"""Cross-user isolation — AIL architecture reconciliation, frozen contract
#10: learner data is user-scoped, and every repository/service query is
bound to an explicit ``user_id``, never an implicit "current project" the
way most of the rest of the platform is scoped. There is no separate AIL
authorization primitive to test here: every AIL.1A service method simply
takes ``user_id`` as an explicit, mandatory argument and filters on it --
this file proves that filter is actually applied, for every AIL.1A
service, not just asserted in a docstring.

``app.auth.get_current_user`` (unmodified by this slice) already resolves
to exactly one local Owner user in this V1, single-user installation; the
two distinct ``User`` rows constructed below exist only to prove the
repository-layer scoping holds even though the API layer cannot exercise
it in single-user V1.
"""

import pytest

from app.db.enums import ConceptLevel, EvidenceType, GradingMode
from app.errors import NotFoundError
from app.services.learner_profile_service import LearnerProfileService
from app.services.learner_state_service import NOT_STARTED, LearnerStateService
from app.services.learning_evidence_service import LearningEvidenceService
from app.services.learning_plan_service import LearningPlanService
from tests.ail1a_factories import make_concept, make_published_version, make_user


def test_learner_profiles_are_isolated_per_user(db):
    alice = make_user(db, email="alice@example.com")
    bob = make_user(db, email="bob@example.com")
    service = LearnerProfileService(db)

    service.update_profile(alice.id, weekly_minutes=60)
    service.update_profile(bob.id, weekly_minutes=600)

    assert service.get_profile(alice.id).weekly_minutes == 60
    assert service.get_profile(bob.id).weekly_minutes == 600


def test_learner_interests_are_isolated_per_user(db):
    alice = make_user(db, email="alice@example.com")
    bob = make_user(db, email="bob@example.com")
    concept = make_concept(db)
    service = LearnerProfileService(db)

    service.set_interest(alice.id, concept_id=concept.id, watch=True)

    assert len(service.list_interests(alice.id)) == 1
    assert service.list_interests(bob.id) == []


def test_learning_evidence_is_isolated_per_user(db):
    alice = make_user(db, email="alice@example.com")
    bob = make_user(db, email="bob@example.com")
    version = make_published_version(db)
    evidence_service = LearningEvidenceService(db)

    evidence_service.record_evidence(
        user_id=alice.id,
        concept_id=version.concept_id,
        concept_version_id=version.id,
        evidence_type=EvidenceType.LESSON_COMPLETED,
        grader=GradingMode.DETERMINISTIC,
    )

    assert len(evidence_service.list_evidence(alice.id)) == 1
    assert evidence_service.list_evidence(bob.id) == []


def test_learner_state_is_computed_per_user_not_globally(db):
    """Alice demonstrating a concept must never make it look demonstrated
    for Bob."""
    alice = make_user(db, email="alice@example.com")
    bob = make_user(db, email="bob@example.com")
    version = make_published_version(db)
    LearningEvidenceService(db).record_evidence(
        user_id=alice.id,
        concept_id=version.concept_id,
        concept_version_id=version.id,
        evidence_type=EvidenceType.LESSON_COMPLETED,
        grader=GradingMode.DETERMINISTIC,
    )

    state_service = LearnerStateService(db)
    assert state_service.state(alice.id, version.concept_id).ladder != NOT_STARTED
    assert state_service.state(bob.id, version.concept_id).ladder == NOT_STARTED


def test_learning_plan_items_are_isolated_per_user(db):
    alice = make_user(db, email="alice@example.com")
    bob = make_user(db, email="bob@example.com")
    concept = make_concept(db, level=ConceptLevel.FOUNDATIONAL)
    LearnerProfileService(db).set_interest(alice.id, concept_id=concept.id)

    plan_service = LearningPlanService(db)
    diff = plan_service.replan(alice.id)
    plan_service.accept_item(alice.id, diff.new_items[0].id)

    assert len(plan_service.get_plan(alice.id)) == 1
    assert plan_service.get_plan(bob.id) == []


def test_cannot_accept_another_users_plan_item(db):
    alice = make_user(db, email="alice@example.com")
    bob = make_user(db, email="bob@example.com")
    concept = make_concept(db, level=ConceptLevel.FOUNDATIONAL)
    LearnerProfileService(db).set_interest(alice.id, concept_id=concept.id)

    plan_service = LearningPlanService(db)
    diff = plan_service.replan(alice.id)
    alice_item_id = diff.new_items[0].id

    with pytest.raises(NotFoundError):
        plan_service.accept_item(bob.id, alice_item_id)


def test_cannot_remove_another_users_interest(db):
    alice = make_user(db, email="alice@example.com")
    bob = make_user(db, email="bob@example.com")
    concept = make_concept(db)
    service = LearnerProfileService(db)
    interest = service.set_interest(alice.id, concept_id=concept.id)

    with pytest.raises(NotFoundError):
        service.remove_interest(bob.id, interest.id)


def test_deleting_one_users_data_does_not_touch_another_users(db):
    alice = make_user(db, email="alice@example.com")
    bob = make_user(db, email="bob@example.com")
    version = make_published_version(db)
    profile_service = LearnerProfileService(db)
    profile_service.set_interest(alice.id, concept_id=version.concept_id)
    profile_service.set_interest(bob.id, concept_id=version.concept_id)

    profile_service.delete_learner_data(alice.id)

    assert profile_service.list_interests(alice.id) == []
    assert len(profile_service.list_interests(bob.id)) == 1
