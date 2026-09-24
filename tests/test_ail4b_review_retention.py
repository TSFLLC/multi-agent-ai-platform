"""AIL.4B review & retention.

Derivation tests inject time (``NOW``); flow tests (start -> complete) run on
the real clock with evidence dated relative to ``T0``. Everything is built
through production paths (ConceptGraphService, LearningEvidenceService,
ReviewAttemptService); only chronology is seeded, by writing attempt/evidence
rows directly where a test needs a specific history."""

import itertools
import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import IntegrityError

from app.auth import get_current_user
from app.db.base import Base
from app.db.enums import (
    ChangeSeverity,
    ConceptKind,
    EvidenceType,
    GradingMode,
    LearningItemType,
    PlanItemOrigin,
    PlanItemState,
    ReviewAttemptStatus,
)
from app.main import app as fastapi_app
from app.models.learner import LearningEvidence, LearningPlanItem
from app.models.learning_review import ReviewAttempt, ReviewPromptDelivery
from app.services.concept_graph_service import ConceptGraphService
from app.services.learner_state_service import (
    CHANGED,
    DEMONSTRATED,
    UNDERSTOOD,
    LearnerStateService,
)
from app.services.learning_evidence_service import LearningEvidenceService
from app.services.review_attempt_service import (
    ReviewAttemptService,
    ReviewItemNotQualifyingError,
    ReviewNotAvailableError,
    ReviewSubmissionInvalidError,
    choice_spec,
    qualifies_as_review_item,
)
from app.services.review_prompt_service import ReviewPromptService
from app.services.review_retention_service import (
    BASE_INTERVAL_DAYS,
    MAX_INTERVAL_DAYS,
    RETRY_COOLDOWN,
    interval_days_for,
)
from app.services.stay_ahead_service import StayAheadService
from tests.ail1a_factories import make_concept, make_published_version, make_user

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
T0 = datetime.now(timezone.utc)
_counter = itertools.count(1)
_ONE_CHECK = {"requires_all": [{"evidence_type": "knowledge_check", "min_passed": 1, "allow_generated": False}]}


def ago(days, base=NOW):
    return base - timedelta(days=days)


def rago(days):
    return ago(days, base=T0)


# -- builders ------------------------------------------------------------------------


def _concept(db, *, kind=ConceptKind.DEFINITIONAL, core=True, name=None, freshness_days=None):
    n = next(_counter)
    concept = make_concept(db, slug=f"rv-{n}", name=name or f"Review Concept {n}", kind=kind, is_core=core)
    if freshness_days is not None:
        concept.freshness_days = freshness_days
        db.commit()
    version = make_published_version(db, concept, evidence_requirements=_ONE_CHECK)
    return concept, version


def _evidence(db, user, concept, version, *, at, passed=True, kind=EvidenceType.KNOWLEDGE_CHECK,
              grader=GradingMode.DETERMINISTIC, demo=False):
    row = LearningEvidenceService(db).record_evidence(
        user_id=user.id, concept_id=concept.id, concept_version_id=version.id, evidence_type=kind,
        grader=grader, passed=passed, on_demo_data=demo,
    )
    row.created_at = at
    db.commit()
    return row


def _item(db, concept, *, spec="default", reviewed=True, item_type=LearningItemType.CHECK_QUESTION,
          grading_mode=GradingMode.DETERMINISTIC, title="Which is right?", body="SECRET-QUESTION-BODY"):
    if spec == "default":
        spec = {"kind": "choice", "options": ["wrong", "right", "also wrong"], "answer_key": [1]}
    return ConceptGraphService(db).create_learning_item(
        concept_id=concept.id, item_type=item_type, title=title, body_md=body, spec=spec,
        grading_mode=grading_mode, reviewed=reviewed,
    )


def _plan(db, user, concept, state=PlanItemState.PLANNED):
    row = LearningPlanItem(user_id=user.id, concept_id=concept.id, position=next(_counter), state=state,
                           origin=PlanItemOrigin.USER)
    db.add(row)
    db.commit()
    return row


def _attempt(db, user, concept, version, *, status, started, completed=None, evidence=None, item=None):
    row = ReviewAttempt(
        user_id=user.id, concept_id=concept.id, concept_version_id=version.id,
        learning_item_id=item.id if item else None, status=status,
        resulting_learning_evidence_id=evidence.id if evidence else None, started_at=started, completed_at=completed,
    )
    db.add(row)
    db.commit()
    return row


def _passed_review(db, user, concept, version, *, at):
    evidence = _evidence(db, user, concept, version, at=at)
    return _attempt(db, user, concept, version, status=ReviewAttemptStatus.PASSED,
                    started=at - timedelta(minutes=5), completed=at, evidence=evidence)


def _failed_review(db, user, concept, version, *, at):
    return _attempt(db, user, concept, version, status=ReviewAttemptStatus.FAILED,
                    started=at - timedelta(minutes=5), completed=at)


def _publish(db, concept, *, at, severity, note=None, draft_only=False):
    service = ConceptGraphService(db)
    version = service.create_draft_version(concept_id=concept.id, plain_definition="Updated.",
                                           change_severity=severity, change_note=note,
                                           evidence_requirements=_ONE_CHECK)
    if draft_only:
        return version
    version = service.publish_version(version.id)
    version.published_at = at
    db.commit()
    return version


def _state(db, user, concept, now=NOW):
    return LearnerStateService(db).state(user.id, concept.id, now=now)


def _review(db, user, concept, now=NOW):
    return _state(db, user, concept, now).review


def _due_core(db, user, *, kind=ConceptKind.DEFINITIONAL, evidence_age_days=None, name=None):
    """A core, DEMONSTRATED concept whose interval has elapsed (real clock)."""
    concept, version = _concept(db, kind=kind, name=name)
    age = evidence_age_days if evidence_age_days is not None else BASE_INTERVAL_DAYS[kind] + 20
    _evidence(db, user, concept, version, at=rago(age))
    return concept, version


def _second_user(db, bootstrap, email="second@example.com"):
    user = make_user(db, org=bootstrap.organization, email=email)
    db.commit()  # never leave a write transaction open for other sessions (the API, other threads)
    return user


# =============================================================================
# REVIEW_DUE: eligibility and interval
# =============================================================================


def test_a_core_demonstrated_concept_past_its_interval_is_review_due_without_losing_its_ladder(db, bootstrap):
    concept, version = _concept(db)
    _evidence(db, bootstrap.user, concept, version, at=ago(181))
    state = _state(db, bootstrap.user, concept)
    assert state.ladder == DEMONSTRATED  # an overlay never downgrades the ladder
    assert "review_due" in state.overlays
    review = state.review
    assert review.due and review.eligible_via == ["core"]
    assert review.reason_codes == ["REVIEW_ELIGIBLE_CORE", "REVIEW_INTERVAL_ELAPSED"]
    assert review.due_at == ago(181) + timedelta(days=180)


def test_an_active_plan_makes_a_non_core_concept_eligible(db, bootstrap):
    concept, version = _concept(db, core=False)
    _evidence(db, bootstrap.user, concept, version, at=ago(200))
    assert "review_due" not in _state(db, bootstrap.user, concept).overlays  # neither core nor planned
    item = _plan(db, bootstrap.user, concept, state=PlanItemState.PROPOSED)
    assert not _review(db, bootstrap.user, concept).eligible  # a proposal is not an active plan
    item.state = PlanItemState.PLANNED
    db.commit()
    review = _review(db, bootstrap.user, concept)
    assert review.eligible_via == ["active_plan"] and review.due
    assert "REVIEW_ELIGIBLE_PLAN" in review.reason_codes
    item.state = PlanItemState.SKIPPED
    db.commit()
    assert not _review(db, bootstrap.user, concept).eligible


def test_another_users_plan_never_makes_a_concept_eligible(db, bootstrap):
    concept, version = _concept(db, core=False)
    _evidence(db, bootstrap.user, concept, version, at=ago(200))
    _plan(db, _second_user(db, bootstrap), concept)
    assert not _review(db, bootstrap.user, concept).eligible


def test_only_a_demonstrated_concept_is_reviewable(db, bootstrap):
    concept, version = _concept(db)
    concept.kind = ConceptKind.DEFINITIONAL
    db.commit()
    # Default definitional requirements need TWO checks; one only reaches UNDERSTOOD.
    two_checks = ConceptGraphService(db).create_draft_version(
        concept_id=concept.id, plain_definition="v2", evidence_requirements=None
    )
    ConceptGraphService(db).publish_version(two_checks.id)
    _evidence(db, bootstrap.user, concept, version, at=ago(400))
    state = _state(db, bootstrap.user, concept)
    assert state.ladder == UNDERSTOOD
    assert state.review.due is False and not state.review.eligible
    assert not ({"review_due", "review_failed"} & state.overlays)


@pytest.mark.parametrize("kind", list(BASE_INTERVAL_DAYS))
def test_intervals_by_kind_with_an_exact_boundary(db, bootstrap, kind):
    days = BASE_INTERVAL_DAYS[kind]
    assert days == {ConceptKind.DEFINITIONAL: 180, ConceptKind.MECHANISM: 120,
                    ConceptKind.OPERATIONAL: 90, ConceptKind.ARCHITECTURAL: 120}[kind]
    concept, version = _concept(db, kind=kind)
    _evidence(db, bootstrap.user, concept, version, at=ago(days - 1))
    assert not _review(db, bootstrap.user, concept).due
    assert _review(db, bootstrap.user, concept, now=NOW + timedelta(days=1) - timedelta(seconds=1)).due is False
    assert _review(db, bootstrap.user, concept, now=NOW + timedelta(days=1)).due is True  # exactly at the interval


def test_concept_freshness_days_is_not_the_review_interval(db, bootstrap):
    concept, version = _concept(db, freshness_days=1)
    _evidence(db, bootstrap.user, concept, version, at=ago(10))
    assert not _review(db, bootstrap.user, concept).due
    concept.freshness_days = 100000
    db.commit()
    _evidence(db, bootstrap.user, concept, version, at=ago(400))  # older evidence never matters: latest wins
    assert not _review(db, bootstrap.user, concept).due


# =============================================================================
# The retention clock
# =============================================================================


def test_new_qualifying_evidence_resets_the_clock(db, bootstrap):
    concept, version = _concept(db)
    _evidence(db, bootstrap.user, concept, version, at=ago(200))
    assert _review(db, bootstrap.user, concept).due
    _evidence(db, bootstrap.user, concept, version, at=ago(10), kind=EvidenceType.LAB)
    review = _review(db, bootstrap.user, concept)
    assert not review.due and review.baseline.recorded_at == ago(10)


def test_evidence_that_does_not_qualify_never_resets_the_clock(db, bootstrap):
    concept, version = _concept(db)
    _evidence(db, bootstrap.user, concept, version, at=ago(200))
    _evidence(db, bootstrap.user, concept, version, at=ago(5), passed=False)  # a failed check
    _evidence(db, bootstrap.user, concept, version, at=ago(5), kind=EvidenceType.SELF_REPORT)
    _evidence(db, bootstrap.user, concept, version, at=ago(5), demo=True)  # demo data
    _evidence(db, bootstrap.user, concept, version, at=ago(5), kind=EvidenceType.LESSON_COMPLETED, passed=None)
    replaced = _evidence(db, bootstrap.user, concept, version, at=ago(5))
    replaced.superseded_by_id = _evidence(db, bootstrap.user, concept, version, at=ago(400)).id  # disputed/regraded
    db.commit()
    review = _review(db, bootstrap.user, concept)
    assert review.due and review.baseline.recorded_at == ago(200)


def test_repeated_reads_at_the_same_time_are_identical(db, bootstrap):
    concept, version = _concept(db)
    _evidence(db, bootstrap.user, concept, version, at=ago(181))
    assert _review(db, bootstrap.user, concept) == _review(db, bootstrap.user, concept)


# =============================================================================
# Successful reviews: reset, extension, cap
# =============================================================================


def test_a_successful_review_resets_the_clock_and_extends_the_interval(db, bootstrap):
    concept, version = _concept(db)
    _evidence(db, bootstrap.user, concept, version, at=ago(200))
    assert _review(db, bootstrap.user, concept).due
    _passed_review(db, bootstrap.user, concept, version, at=ago(1))
    review = _review(db, bootstrap.user, concept)
    assert not review.due and review.successful_reviews_on_schedule == 1
    assert review.interval_days == 360 and review.base_interval_days == 180  # doubled once
    assert review.due_at == ago(1) + timedelta(days=360)


def test_the_interval_doubles_per_success_and_is_capped_at_365_days():
    assert [interval_days_for(ConceptKind.OPERATIONAL, k) for k in range(6)] == [90, 180, 360, 365, 365, 365]
    assert interval_days_for(ConceptKind.DEFINITIONAL, 1) == 360
    assert interval_days_for(ConceptKind.DEFINITIONAL, 2) == 365
    assert interval_days_for(ConceptKind.MECHANISM, 50) == MAX_INTERVAL_DAYS == 365
    assert interval_days_for(ConceptKind.ARCHITECTURAL, -3) == 120


def test_the_cap_holds_through_real_attempt_history(db, bootstrap):
    # Concept with operational (90 day) interval, which doubles to 180, 360, then caps at 365
    # Goal: test that 4 on-schedule reviews reach the cap
    concept, version = _concept(db, kind=ConceptKind.OPERATIONAL)
    _evidence(db, bootstrap.user, concept, version, at=ago(2000))  # baseline, due at ago(1910)
    # Review 1: at ago(1900), due ago(1910), is on-schedule. Next due: ago(1900 - 180) = ago(1720)
    _passed_review(db, bootstrap.user, concept, version, at=ago(1900))
    # Review 2: at ago(1700), due ago(1720), is on-schedule. Next due: ago(1700 - 360) = ago(1340)
    _passed_review(db, bootstrap.user, concept, version, at=ago(1700))
    # Review 3: at ago(1300), due ago(1340), is on-schedule. Next due: ago(1300 - 365) = ago(935)
    _passed_review(db, bootstrap.user, concept, version, at=ago(1300))
    # Review 4: at ago(900), due ago(935), is on-schedule. Interval caps at 365
    _passed_review(db, bootstrap.user, concept, version, at=ago(900))
    review = _review(db, bootstrap.user, concept)
    assert review.successful_reviews_on_schedule == 4 and review.interval_days == 365


def test_the_first_and_each_later_successful_review_extend_the_interval_again(db, bootstrap):
    concept, version = _concept(db, kind=ConceptKind.DEFINITIONAL)
    _evidence(db, bootstrap.user, concept, version, at=ago(900))
    assert _review(db, bootstrap.user, concept, now=ago(800)).interval_days == 180  # no review yet
    _passed_review(db, bootstrap.user, concept, version, at=ago(700))
    first = _review(db, bootstrap.user, concept, now=ago(690))
    assert (first.successful_reviews_on_schedule, first.interval_days) == (1, 360)  # first success extends it
    _passed_review(db, bootstrap.user, concept, version, at=ago(300))
    second = _review(db, bootstrap.user, concept, now=ago(290))
    assert (second.successful_reviews_on_schedule, second.interval_days) == (2, 365)  # a subsequent success extends again, capped


def test_a_failed_review_does_not_reset_accumulated_extension(db, bootstrap):
    concept, version = _concept(db, kind=ConceptKind.OPERATIONAL)  # 90 -> 180 -> 360
    _evidence(db, bootstrap.user, concept, version, at=ago(900))  # baseline, due ago(810)
    _passed_review(db, bootstrap.user, concept, version, at=ago(800))  # on-schedule (800 >= 810), due next at ago(710)
    _passed_review(db, bootstrap.user, concept, version, at=ago(700))  # on-schedule (700 >= 710)? Need to check... wait that's backwards
    # Let me recalculate: after first review at ago(800), next due = ago(800 - 180) = ago(620)
    _passed_review(db, bootstrap.user, concept, version, at=ago(610))  # on-schedule (610 >= 620), due next at ago(430)
    before = _review(db, bootstrap.user, concept, now=ago(420))
    assert (before.successful_reviews_on_schedule, before.interval_days) == (2, 360)

    _failed_review(db, bootstrap.user, concept, version, at=ago(300))  # this fails, doesn't affect anything
    after = _review(db, bootstrap.user, concept, now=ago(290))
    assert after.failed  # a failure may derive REVIEW_FAILED...
    assert (after.successful_reviews_on_schedule, after.interval_days) == (2, 360)  # ...but erases nothing
    assert after.baseline.recorded_at == ago(610)  # latest evidence from the last on-schedule review


def test_a_later_success_continues_from_the_prior_successful_review_history(db, bootstrap):
    concept, version = _concept(db, kind=ConceptKind.OPERATIONAL)
    _evidence(db, bootstrap.user, concept, version, at=ago(900))
    _passed_review(db, bootstrap.user, concept, version, at=ago(800))  # 90 -> 180
    _failed_review(db, bootstrap.user, concept, version, at=ago(700))
    resumed = _passed_review(db, bootstrap.user, concept, version, at=ago(600))  # continues: 2nd success -> 360
    review = _review(db, bootstrap.user, concept, now=ago(590))
    assert (review.successful_reviews_on_schedule, review.interval_days) == (2, 360)
    assert not review.failed  # the later success cleared REVIEW_FAILED
    assert review.due_at == resumed.completed_at + timedelta(days=360)
    _passed_review(db, bootstrap.user, concept, version, at=ago(100))
    assert _review(db, bootstrap.user, concept).interval_days == 365  # 720 capped


def test_only_qualifying_successes_count_toward_the_extension(db, bootstrap):
    concept, version = _concept(db)
    _evidence(db, bootstrap.user, concept, version, at=ago(300))  # baseline, due at ago(120)
    _passed_review(db, bootstrap.user, concept, version, at=ago(200))  # EARLY review (100 days early), doesn't count
    _passed_review(db, bootstrap.user, concept, version, at=ago(50))  # ON-SCHEDULE review (70 days after due), counts
    unusable = _evidence(db, bootstrap.user, concept, version, at=ago(40), passed=False)  # non-qualifying
    _attempt(db, bootstrap.user, concept, version, status=ReviewAttemptStatus.PASSED,
             started=ago(40) - timedelta(minutes=1), completed=ago(40), evidence=unusable)
    assert _review(db, bootstrap.user, concept).successful_reviews_on_schedule == 1  # only the on-schedule review counts


# =============================================================================
# A material Concept change
# =============================================================================


def test_a_material_concept_change_makes_a_review_due_even_inside_the_interval(db, bootstrap):
    concept, version = _concept(db)
    _evidence(db, bootstrap.user, concept, version, at=ago(10))
    _publish(db, concept, at=ago(3), severity=ChangeSeverity.MATERIAL, note="Definition rewritten.")
    review = _review(db, bootstrap.user, concept)
    assert review.due and not review.interval_elapsed
    assert review.due_reasons == ["REVIEW_CONCEPT_CHANGED"]
    assert [c["version"] for c in review.material_changes] == [2]
    assert review.material_changes[0]["change_note"] == "Definition rewritten."


def test_minor_changes_and_unpublished_drafts_do_not_make_a_review_due(db, bootstrap):
    concept, version = _concept(db)
    _evidence(db, bootstrap.user, concept, version, at=ago(10))
    _publish(db, concept, at=ago(3), severity=ChangeSeverity.MINOR)
    _publish(db, concept, at=ago(2), severity=ChangeSeverity.MATERIAL, draft_only=True)
    assert not _review(db, bootstrap.user, concept).due


def test_a_material_change_two_hops_back_is_still_found(db, bootstrap):
    concept, version = _concept(db)
    _evidence(db, bootstrap.user, concept, version, at=ago(20))
    _publish(db, concept, at=ago(10), severity=ChangeSeverity.MATERIAL)
    _publish(db, concept, at=ago(3), severity=ChangeSeverity.MINOR)
    review = _review(db, bootstrap.user, concept)
    assert review.due and [c["version"] for c in review.material_changes] == [2]
    assert CHANGED not in _state(db, bootstrap.user, concept).overlays  # the single-hop overlay misses it; review does not


def test_a_change_only_matters_for_an_eligible_concept(db, bootstrap):
    concept, version = _concept(db, core=False)
    _evidence(db, bootstrap.user, concept, version, at=ago(10))
    _publish(db, concept, at=ago(3), severity=ChangeSeverity.MATERIAL)
    assert not _review(db, bootstrap.user, concept).due


def test_a_review_of_the_new_version_clears_the_change(db, bootstrap):
    concept, version = _concept(db)
    _evidence(db, bootstrap.user, concept, version, at=ago(10))
    v2 = _publish(db, concept, at=ago(3), severity=ChangeSeverity.MATERIAL)
    assert _review(db, bootstrap.user, concept).due
    _passed_review(db, bootstrap.user, concept, v2, at=ago(1))
    review = _review(db, bootstrap.user, concept)
    assert not review.due and review.material_changes == []


# =============================================================================
# REVIEW_FAILED
# =============================================================================


def test_a_failed_review_derives_review_failed_and_preserves_everything(db, bootstrap):
    concept, version = _concept(db)
    _evidence(db, bootstrap.user, concept, version, at=ago(20))
    before = db.execute(select(func.count()).select_from(LearningEvidence)).scalar()
    _failed_review(db, bootstrap.user, concept, version, at=ago(1))
    state = _state(db, bootstrap.user, concept)
    assert state.ladder == DEMONSTRATED  # historical DEMONSTRATED is preserved
    assert "review_failed" in state.overlays
    assert state.review.reason_codes[-1] == "REVIEW_FAILED_LATEST"
    assert db.execute(select(func.count()).select_from(LearningEvidence)).scalar() == before  # nothing deleted or added


def test_a_later_success_clears_review_failed_and_review_due(db, bootstrap):
    concept, version = _concept(db)
    _evidence(db, bootstrap.user, concept, version, at=ago(200))
    _failed_review(db, bootstrap.user, concept, version, at=ago(3))
    state = _state(db, bootstrap.user, concept)
    assert {"review_failed", "review_due"} <= state.overlays
    _passed_review(db, bootstrap.user, concept, version, at=ago(1))
    state = _state(db, bootstrap.user, concept)
    assert not ({"review_failed", "review_due"} & state.overlays)
    assert state.ladder == DEMONSTRATED


def test_new_non_review_evidence_does_not_clear_review_failed(db, bootstrap):
    concept, version = _concept(db)
    _evidence(db, bootstrap.user, concept, version, at=ago(20))
    _failed_review(db, bootstrap.user, concept, version, at=ago(3))
    _evidence(db, bootstrap.user, concept, version, at=ago(1), kind=EvidenceType.LAB)
    assert "review_failed" in _state(db, bootstrap.user, concept).overlays  # only a later successful review clears it


def test_review_failed_needs_a_demonstrated_concept(db, bootstrap):
    concept, version = _concept(db)
    _failed_review(db, bootstrap.user, concept, version, at=ago(1))  # no evidence at all
    assert "review_failed" not in _state(db, bootstrap.user, concept).overlays


def test_a_passed_attempt_with_unusable_evidence_is_ignored_entirely(db, bootstrap):
    """PASSED alone means nothing: evidence must really exist and qualify."""
    concept, version = _concept(db)
    _evidence(db, bootstrap.user, concept, version, at=ago(200))
    _failed_review(db, bootstrap.user, concept, version, at=ago(10))
    bad_evidence = [
        _evidence(db, bootstrap.user, concept, version, at=ago(4), passed=False),
        _evidence(db, bootstrap.user, concept, version, at=ago(4), kind=EvidenceType.SELF_REPORT),
        _evidence(db, bootstrap.user, concept, version, at=ago(4), demo=True),
        _evidence(db, _second_user(db, bootstrap), concept, version, at=ago(4)),  # another learner's row
    ]
    for i, evidence in enumerate(bad_evidence):
        _attempt(db, bootstrap.user, concept, version, status=ReviewAttemptStatus.PASSED,
                 started=ago(4) - timedelta(minutes=i + 1), completed=ago(4) + timedelta(minutes=i),
                 evidence=evidence)
    review = _review(db, bootstrap.user, concept)
    assert review.failed  # not cleared by an attempt that merely says PASSED
    assert review.successful_reviews_on_schedule == 0  # nor extended
    assert review.baseline.recorded_at == ago(200)  # nor did it reset the clock


# =============================================================================
# The database enforces the lifecycle
# =============================================================================


def _raw_attempt(db, concept, user, version, **overrides):
    row = ReviewAttempt(
        user_id=user.id, concept_id=concept.id, concept_version_id=version.id,
        status=overrides.pop("status", ReviewAttemptStatus.STARTED), started_at=NOW, **overrides,
    )
    db.add(row)
    return row


def test_the_database_refuses_a_passed_attempt_without_evidence(db, bootstrap):
    concept, version = _concept(db)
    _raw_attempt(db, concept, bootstrap.user, version, status=ReviewAttemptStatus.PASSED, completed_at=NOW)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


@pytest.mark.parametrize("with_evidence,status,completed", [
    (True, ReviewAttemptStatus.FAILED, True),   # FAILED never carries evidence
    (False, ReviewAttemptStatus.STARTED, True),  # STARTED has no completion
    (True, ReviewAttemptStatus.STARTED, False),  # STARTED has no evidence
    (False, ReviewAttemptStatus.FAILED, False),  # FAILED needs a completion time
])
def test_the_database_refuses_inconsistent_lifecycle_rows(db, bootstrap, with_evidence, status, completed):
    concept, version = _concept(db)
    evidence = _evidence(db, bootstrap.user, concept, version, at=ago(1)) if with_evidence else None
    _raw_attempt(db, concept, bootstrap.user, version, status=status,
                 completed_at=NOW if completed else None,
                 resulting_learning_evidence_id=evidence.id if evidence else None)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_at_most_one_started_attempt_and_one_attempt_per_evidence_row(db, bootstrap):
    concept, version = _concept(db)
    _raw_attempt(db, concept, bootstrap.user, version)
    db.commit()
    _raw_attempt(db, concept, bootstrap.user, version)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    other = _second_user(db, bootstrap)
    _raw_attempt(db, concept, other, version)  # a different learner may have their own
    db.commit()

    evidence = _evidence(db, bootstrap.user, concept, version, at=ago(1))
    _raw_attempt(db, concept, bootstrap.user, version, status=ReviewAttemptStatus.PASSED, completed_at=NOW,
                 resulting_learning_evidence_id=evidence.id)
    db.commit()
    _raw_attempt(db, concept, bootstrap.user, version, status=ReviewAttemptStatus.PASSED, completed_at=NOW,
                 resulting_learning_evidence_id=evidence.id)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_the_lifecycle_has_no_dismissed_or_snoozed_state():
    assert [s.value for s in ReviewAttemptStatus] == ["started", "passed", "failed"]


# =============================================================================
# The review evidence-quality contract
# =============================================================================


def test_only_reviewed_deterministic_choice_items_may_back_a_review(db, bootstrap):
    concept, _ = _concept(db)
    other, _ = _concept(db)
    good = _item(db, concept)
    assert qualifies_as_review_item(good, concept.id) is None
    assert qualifies_as_review_item(_item(db, concept, item_type=LearningItemType.SCENARIO), concept.id) is None
    cases = {
        "NOT_REVIEWED": _item(db, concept, reviewed=False),
        "NOT_DETERMINISTIC": _item(db, concept, grading_mode=GradingMode.AI_RUBRIC),
        "WRONG_ITEM_TYPE": _item(db, concept, item_type=LearningItemType.RESOURCE),
        "INVALID_SPEC": _item(db, concept, spec={"kind": "choice", "options": ["a", "b"], "answer_key": [5]}),
    }
    for reason, item in cases.items():
        assert qualifies_as_review_item(item, concept.id) == reason
    assert qualifies_as_review_item(good, other.id) == "NOT_A_CONCEPT_ITEM"
    assert qualifies_as_review_item(None, concept.id) == "NOT_A_CONCEPT_ITEM"


@pytest.mark.parametrize("spec", [
    None, {}, {"kind": "essay"}, {"kind": "choice"}, {"kind": "choice", "options": ["only one"], "answer_key": [0]},
    {"kind": "choice", "options": ["a", "b"], "answer_key": []},
    {"kind": "choice", "options": ["a", "b"], "answer_key": [0, 0]},
    {"kind": "choice", "options": ["a", "b"], "answer_key": [True]},
    {"kind": "choice", "options": ["a", "b"], "answer_key": [-1]},
    {"kind": "choice", "options": ["a", "b"], "answer_key": [0, 1]},  # single-choice needs exactly one key
    {"kind": "choice", "options": ["a", " "], "answer_key": [0]},
])
def test_malformed_choice_specs_are_rejected(db, spec):
    concept, _ = _concept(db)
    assert choice_spec(_item(db, concept, spec=spec)) is None


# =============================================================================
# Starting a review
# =============================================================================


def test_starting_a_review_freezes_the_concept_version_and_picks_a_qualifying_item(db, bootstrap):
    concept, version = _due_core(db, bootstrap.user)
    _item(db, concept, reviewed=False)
    item = _item(db, concept)
    result = ReviewAttemptService(db).start(bootstrap.user.id, concept.id)
    assert result.created and result.item.id == item.id
    attempt = result.attempt
    assert attempt.status == ReviewAttemptStatus.STARTED
    assert attempt.concept_version_id == version.id  # provenance frozen at start
    assert attempt.learning_item_id == item.id and attempt.resulting_learning_evidence_id is None


def test_starting_is_idempotent_while_a_review_is_in_progress(db, bootstrap):
    concept, _ = _due_core(db, bootstrap.user)
    _item(db, concept)
    service = ReviewAttemptService(db)
    first = service.start(bootstrap.user.id, concept.id)
    second = service.start(bootstrap.user.id, concept.id)
    assert (first.created, second.created) == (True, False) and first.attempt.id == second.attempt.id


@pytest.mark.parametrize("build,reason", [
    ("not_demonstrated", "NOT_DEMONSTRATED"),
    ("not_eligible", "NOT_ELIGIBLE"),
    ("no_item", "NO_REVIEW_ITEM"),
])
def test_a_review_cannot_start_unless_the_learner_may_review(db, bootstrap, build, reason):
    if build == "not_demonstrated":
        concept, version = _concept(db)
        _evidence(db, bootstrap.user, concept, version, at=rago(500), passed=False)
    elif build == "not_eligible":
        concept, version = _concept(db, core=False)
        _evidence(db, bootstrap.user, concept, version, at=rago(500))
        _item(db, concept)
    else:
        concept, _ = _due_core(db, bootstrap.user)
        _item(db, concept, reviewed=False)
    with pytest.raises(ReviewNotAvailableError) as raised:
        ReviewAttemptService(db).start(bootstrap.user.id, concept.id)
    assert raised.value.detail["reason"] == reason
    assert db.execute(select(func.count()).select_from(ReviewAttempt)).scalar() == 0


def test_an_eligible_demonstrated_concept_can_be_reviewed_voluntarily_before_it_is_due(db, bootstrap):
    """REVIEW_DUE controls recommendation, not permission."""
    concept, version = _concept(db)
    _evidence(db, bootstrap.user, concept, version, at=rago(5))  # far inside the 180-day interval
    item = _item(db, concept)
    state = _state(db, bootstrap.user, concept, now=datetime.now(timezone.utc))
    assert "review_due" not in state.overlays and state.review.eligible and not state.review.due
    assert ReviewAttemptService(db).availability(bootstrap.user.id, concept.id).action == "start"

    result = ReviewAttemptService(db).start(bootstrap.user.id, concept.id)
    assert result.created and result.item.id == item.id
    done = ReviewAttemptService(db).complete(bootstrap.user.id, result.attempt.id, [1])
    assert done.passed and done.evidence.concept_version_id == version.id
    # Early voluntary review: evidence recorded, but does NOT advance interval
    assert done.state.ladder == DEMONSTRATED and done.state.review.successful_reviews_on_schedule == 0
    assert done.state.review.interval_days == 180  # interval unchanged


def test_voluntary_review_still_needs_eligibility_history_and_a_valid_item(db, bootstrap):
    not_core, version = _concept(db, core=False)
    _evidence(db, bootstrap.user, not_core, version, at=rago(5))
    _item(db, not_core)
    assert ReviewAttemptService(db).availability(bootstrap.user.id, not_core.id).reason == "NOT_ELIGIBLE"
    _plan(db, bootstrap.user, not_core)  # an active plan makes it eligible, due or not
    assert ReviewAttemptService(db).availability(bootstrap.user.id, not_core.id).action == "start"

    fresh, _ = _concept(db)  # core but never demonstrated
    _item(db, fresh)
    assert ReviewAttemptService(db).availability(bootstrap.user.id, fresh.id).reason == "NOT_DEMONSTRATED"
    itemless, iv = _concept(db)
    _evidence(db, bootstrap.user, itemless, iv, at=rago(5))
    assert ReviewAttemptService(db).availability(bootstrap.user.id, itemless.id).reason == "NO_REVIEW_ITEM"


def test_a_voluntary_early_review_keeps_the_single_active_attempt_rule(db, bootstrap):
    concept, version = _concept(db)
    _evidence(db, bootstrap.user, concept, version, at=rago(5))
    _item(db, concept)
    service = ReviewAttemptService(db)
    first = service.start(bootstrap.user.id, concept.id)
    second = service.start(bootstrap.user.id, concept.id)
    assert (first.created, second.created) == (True, False) and first.attempt.id == second.attempt.id


def test_the_failed_review_cooldown_still_blocks_an_early_retry_and_then_allows_it(db, bootstrap):
    concept, version = _concept(db)
    _evidence(db, bootstrap.user, concept, version, at=rago(5))  # not due
    _item(db, concept, title="One")
    _item(db, concept, title="Two")
    service = ReviewAttemptService(db)
    attempt = service.start(bootstrap.user.id, concept.id).attempt
    assert not service.complete(bootstrap.user.id, attempt.id, [0]).passed  # a voluntary review, failed

    with pytest.raises(ReviewNotAvailableError) as raised:  # the cooldown applies to voluntary reviews too
        service.start(bootstrap.user.id, concept.id)
    assert raised.value.detail["reason"] == "COOLDOWN"

    later = ReviewAttemptService(db, now=datetime.now(timezone.utc) + RETRY_COOLDOWN + timedelta(minutes=1))
    retry = later.start(bootstrap.user.id, concept.id).attempt  # after the wait, still not due, and allowed
    assert retry.id != attempt.id and later.complete(bootstrap.user.id, retry.id, [1]).passed


def test_starting_an_unknown_concept_is_a_404(db, bootstrap):
    from app.errors import NotFoundError

    with pytest.raises(NotFoundError):
        ReviewAttemptService(db).start(bootstrap.user.id, "no-such-concept")


# =============================================================================
# Completing a review
# =============================================================================


def _start(db, user, concept, service=None):
    return (service or ReviewAttemptService(db)).start(user.id, concept.id).attempt


def test_a_correct_answer_appends_canonical_evidence_and_clears_the_review(db, bootstrap):
    concept, version = _due_core(db, bootstrap.user)
    item = _item(db, concept)
    attempt = _start(db, bootstrap.user, concept)
    before = db.execute(select(func.count()).select_from(LearningEvidence)).scalar()

    result = ReviewAttemptService(db).complete(bootstrap.user.id, attempt.id, [1])

    assert result.passed and not result.replay
    assert result.attempt.status == ReviewAttemptStatus.PASSED
    evidence = result.evidence
    assert db.execute(select(func.count()).select_from(LearningEvidence)).scalar() == before + 1
    assert result.attempt.resulting_learning_evidence_id == evidence.id
    assert (evidence.evidence_type, evidence.grader, evidence.passed) == (
        EvidenceType.KNOWLEDGE_CHECK, GradingMode.DETERMINISTIC, True)
    assert evidence.question_origin.value == "reviewed" and evidence.on_demo_data is False
    assert evidence.learning_item_id == item.id
    assert evidence.concept_version_id == version.id == attempt.concept_version_id  # provenance
    assert evidence.score == {"raw": 1, "max": 1, "pct": 100}
    assert result.state.ladder == DEMONSTRATED
    assert not ({"review_due", "review_failed"} & result.state.overlays)
    assert result.state.review.successful_reviews_on_schedule == 1 and result.state.review.interval_days == 360


def test_the_evidence_cites_the_version_frozen_at_start_even_if_the_concept_changes_meanwhile(db, bootstrap):
    concept, v1 = _due_core(db, bootstrap.user)
    _item(db, concept)
    attempt = _start(db, bootstrap.user, concept)
    v2 = _publish(db, concept, at=datetime.now(timezone.utc), severity=ChangeSeverity.MATERIAL)
    result = ReviewAttemptService(db).complete(bootstrap.user.id, attempt.id, [1])
    assert result.evidence.concept_version_id == v1.id != v2.id
    assert "review_due" in result.state.overlays  # honestly: the newer material version is still unreviewed


def test_a_wrong_answer_fails_the_attempt_without_evidence_or_a_ladder_change(db, bootstrap):
    concept, _ = _due_core(db, bootstrap.user)
    _item(db, concept)
    attempt = _start(db, bootstrap.user, concept)
    before = db.execute(select(func.count()).select_from(LearningEvidence)).scalar()
    result = ReviewAttemptService(db).complete(bootstrap.user.id, attempt.id, [0])
    assert not result.passed and result.evidence is None
    assert result.attempt.status == ReviewAttemptStatus.FAILED
    assert result.attempt.resulting_learning_evidence_id is None and result.attempt.completed_at is not None
    assert db.execute(select(func.count()).select_from(LearningEvidence)).scalar() == before
    assert result.state.ladder == DEMONSTRATED and "review_failed" in result.state.overlays


def test_a_failed_review_waits_out_the_cooldown_then_uses_a_different_item_and_a_pass_clears_it(db, bootstrap):
    concept, _ = _due_core(db, bootstrap.user)
    first, second = _item(db, concept, title="First"), _item(db, concept, title="Second")
    service = ReviewAttemptService(db)
    attempt = _start(db, bootstrap.user, concept, service)
    used = attempt.learning_item_id
    service.complete(bootstrap.user.id, attempt.id, [0])

    with pytest.raises(ReviewNotAvailableError) as raised:
        service.start(bootstrap.user.id, concept.id)
    assert raised.value.detail["reason"] == "COOLDOWN" and raised.value.detail["available_after"]

    later = ReviewAttemptService(db, now=datetime.now(timezone.utc) + RETRY_COOLDOWN + timedelta(minutes=1))
    retry = later.start(bootstrap.user.id, concept.id).attempt
    assert retry.learning_item_id != used and {used, retry.learning_item_id} == {first.id, second.id}
    result = later.complete(bootstrap.user.id, retry.id, [1])
    assert result.passed and "review_failed" not in result.state.overlays


def test_a_check_that_stopped_qualifying_cannot_be_completed(db, bootstrap):
    concept, _ = _due_core(db, bootstrap.user)
    item = _item(db, concept)
    attempt = _start(db, bootstrap.user, concept)
    item.reviewed = False  # e.g. withdrawn from the reviewed bank after the attempt began
    db.commit()
    before = db.execute(select(func.count()).select_from(LearningEvidence)).scalar()
    with pytest.raises(ReviewItemNotQualifyingError) as raised:
        ReviewAttemptService(db).complete(bootstrap.user.id, attempt.id, [1])
    assert raised.value.detail["reason"] == "NOT_REVIEWED"
    db.refresh(attempt)
    assert attempt.status == ReviewAttemptStatus.STARTED and attempt.resulting_learning_evidence_id is None
    assert db.execute(select(func.count()).select_from(LearningEvidence)).scalar() == before


@pytest.mark.parametrize("selected", [[], [7], [-1], [1, 1], [0, 1], [True], ["1"], None, 1])
def test_incomplete_or_invalid_submissions_change_nothing(db, bootstrap, selected):
    concept, _ = _due_core(db, bootstrap.user)
    _item(db, concept)
    attempt = _start(db, bootstrap.user, concept)
    before = db.execute(select(func.count()).select_from(LearningEvidence)).scalar()
    with pytest.raises(ReviewSubmissionInvalidError):
        ReviewAttemptService(db).complete(bootstrap.user.id, attempt.id, selected)
    db.refresh(attempt)
    assert attempt.status == ReviewAttemptStatus.STARTED  # neither passed nor failed
    assert db.execute(select(func.count()).select_from(LearningEvidence)).scalar() == before
    assert ReviewAttemptService(db).complete(bootstrap.user.id, attempt.id, [1]).passed  # still completable


def test_a_multiple_choice_item_needs_exactly_the_answer_key(db, bootstrap):
    concept, _ = _due_core(db, bootstrap.user)
    _item(db, concept, spec={"kind": "choice", "options": ["a", "b", "c"], "answer_key": [0, 2], "multiple": True})
    service = ReviewAttemptService(db)
    attempt = _start(db, bootstrap.user, concept, service)
    assert not service.complete(bootstrap.user.id, attempt.id, [0]).passed  # partial credit is not a pass


def test_completing_twice_is_idempotent_and_a_finished_attempt_never_regrades(db, bootstrap):
    concept, _ = _due_core(db, bootstrap.user)
    _item(db, concept)
    service = ReviewAttemptService(db)
    attempt = _start(db, bootstrap.user, concept, service)
    first = service.complete(bootstrap.user.id, attempt.id, [1])
    again = service.complete(bootstrap.user.id, attempt.id, [1])
    different = service.complete(bootstrap.user.id, attempt.id, [0])  # a later, different answer changes nothing
    assert (first.replay, again.replay, different.replay) == (False, True, True)
    assert again.evidence.id == first.evidence.id == different.evidence.id
    assert db.execute(select(func.count()).select_from(LearningEvidence).where(
        LearningEvidence.learning_item_id.is_not(None))).scalar() == 1

    failed_concept, _ = _due_core(db, bootstrap.user)
    _item(db, failed_concept)
    failed = _start(db, bootstrap.user, failed_concept, service)
    service.complete(bootstrap.user.id, failed.id, [0])
    replay = service.complete(bootstrap.user.id, failed.id, [1])  # a correct answer after failing does not rewrite history
    assert replay.replay and not replay.passed and replay.evidence is None


def test_concurrent_completions_create_exactly_one_evidence_row(db, session_factory, bootstrap):
    concept, _ = _due_core(db, bootstrap.user)
    _item(db, concept)
    attempt = _start(db, bootstrap.user, concept)
    before = db.execute(select(func.count()).select_from(LearningEvidence)).scalar()
    barrier, results, errors = threading.Barrier(5), [], []

    def worker():
        session = session_factory()
        try:
            barrier.wait(timeout=10)
            results.append(ReviewAttemptService(session).complete(bootstrap.user.id, attempt.id, [1]))
        except Exception as exc:  # noqa: BLE001  # pragma: no cover - surfaced by the assertion below
            errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors, errors
    assert len(results) == 5 and all(r.passed for r in results)
    assert sorted(r.replay for r in results) == [False, True, True, True, True]  # exactly one winner
    assert len({r.evidence.id for r in results}) == 1
    db.expire_all()
    assert db.execute(select(func.count()).select_from(LearningEvidence)).scalar() == before + 1


def test_a_forced_lost_race_discards_the_losers_evidence_and_reports_the_winner(db, session_factory, bootstrap):
    """Deterministic: the loser read the attempt as STARTED, then another
    session completed it. The loser must not leave a second evidence row."""
    concept, _ = _due_core(db, bootstrap.user)
    _item(db, concept)
    attempt = _start(db, bootstrap.user, concept)
    before = db.execute(select(func.count()).select_from(LearningEvidence)).scalar()

    session = session_factory()
    try:
        loser = ReviewAttemptService(session)
        stale = loser.get(bootstrap.user.id, attempt.id)  # still STARTED when read
        assert stale.status == ReviewAttemptStatus.STARTED
        winner = ReviewAttemptService(db).complete(bootstrap.user.id, attempt.id, [1])
        real_get, calls = loser.get, {"n": 0}

        def stale_first(user_id, attempt_id):
            calls["n"] += 1
            return stale if calls["n"] == 1 else real_get(user_id, attempt_id)

        loser.get = stale_first
        result = loser.complete(bootstrap.user.id, attempt.id, [1])
        assert result.replay and result.passed and result.evidence.id == winner.evidence.id
    finally:
        session.close()
    db.expire_all()
    assert db.execute(select(func.count()).select_from(LearningEvidence)).scalar() == before + 1


def test_concurrent_starts_create_one_attempt(db, session_factory, bootstrap):
    concept, _ = _due_core(db, bootstrap.user)
    _item(db, concept)
    barrier, results, errors = threading.Barrier(4), [], []

    def worker():
        session = session_factory()
        try:
            barrier.wait(timeout=10)
            results.append(ReviewAttemptService(session).start(bootstrap.user.id, concept.id))
        except Exception as exc:  # noqa: BLE001  # pragma: no cover
            errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not errors, errors
    assert len({r.attempt.id for r in results}) == 1
    assert sorted(r.created for r in results) == [False, False, False, True]
    db.expire_all()
    assert db.execute(select(func.count()).select_from(ReviewAttempt)).scalar() == 1


# =============================================================================
# Isolation and non-interference
# =============================================================================


def test_another_users_attempt_is_invisible_and_cannot_be_completed(db, bootstrap):
    from app.errors import NotFoundError

    concept, _ = _due_core(db, bootstrap.user)
    _item(db, concept)
    attempt = _start(db, bootstrap.user, concept)
    other = _second_user(db, bootstrap)
    with pytest.raises(NotFoundError):
        ReviewAttemptService(db).get(other.id, attempt.id)
    with pytest.raises(NotFoundError):
        ReviewAttemptService(db).complete(other.id, attempt.id, [1])
    db.refresh(attempt)
    assert attempt.status == ReviewAttemptStatus.STARTED


def test_one_users_reviews_never_change_anothers_state(db, bootstrap):
    concept, version = _concept(db)
    other = _second_user(db, bootstrap)
    for user in (bootstrap.user, other):
        _evidence(db, user, concept, version, at=ago(200))
    _failed_review(db, bootstrap.user, concept, version, at=ago(3))
    assert "review_failed" in _state(db, bootstrap.user, concept).overlays
    theirs = _state(db, other, concept)
    assert "review_failed" not in theirs.overlays and theirs.review.successful_reviews_on_schedule == 0 and theirs.review.latest_completed is None


def _dump(engine, exclude=()):
    with engine.connect() as conn:
        return {t.name: conn.execute(text(f'SELECT * FROM "{t.name}" ORDER BY 1')).fetchall()
                for t in Base.metadata.sorted_tables if t.name not in exclude}


def test_a_review_touches_only_review_attempts_and_evidence(db, bootstrap, engine):
    """No Learning Plan, Radar, experiment, MA6 or MA8 routing row moves."""
    concept, _ = _due_core(db, bootstrap.user)
    _plan(db, bootstrap.user, concept)
    _item(db, concept)
    before = _dump(engine, exclude={"review_attempts", "learning_evidence"})
    service = ReviewAttemptService(db)
    attempt = _start(db, bootstrap.user, concept, service)
    service.complete(bootstrap.user.id, attempt.id, [0])  # fail
    assert _dump(engine, exclude={"review_attempts", "learning_evidence"}) == before
    later = ReviewAttemptService(db, now=datetime.now(timezone.utc) + timedelta(days=1))
    retry = later.start(bootstrap.user.id, concept.id).attempt
    later.complete(bootstrap.user.id, retry.id, [1])  # pass
    assert _dump(engine, exclude={"review_attempts", "learning_evidence"}) == before


def test_reading_state_never_writes(db, bootstrap, engine):
    concept, version = _concept(db)
    _evidence(db, bootstrap.user, concept, version, at=ago(200))
    _failed_review(db, bootstrap.user, concept, version, at=ago(3))
    before = _dump(engine)
    statements = []

    def spy(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().split(None, 1)[0].upper() in {"INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER"}:
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", spy)
    try:
        _state(db, bootstrap.user, concept)
        ReviewAttemptService(db, now=NOW).availability(bootstrap.user.id, concept.id)
    finally:
        event.remove(engine, "before_cursor_execute", spy)
    assert statements == [] and _dump(engine) == before


# =============================================================================
# Today: the review section (delivered prompts only) and the weekly quota
# =============================================================================


def _today(db, user, now=NOW, **kwargs):
    return StayAheadService(db, now=now).today(user.id, **kwargs)


def _deliver(db, user, now=NOW):
    """The one write path for prompt delivery (what POST /prompts/allocate calls)."""
    return ReviewPromptService(db, now=now).allocate(user.id)


def _prompts(db, now=NOW):
    return ReviewPromptService(db, now=now)


def _delivery_rows(db):
    return db.execute(select(ReviewPromptDelivery).order_by(ReviewPromptDelivery.week_start, ReviewPromptDelivery.slot)).scalars().all()


def _due_at(db, user, *, name, age, kind=ConceptKind.DEFINITIONAL, item=True):
    concept, version = _concept(db, kind=kind, name=name)
    _evidence(db, user, concept, version, at=ago(age))
    if item:
        _item(db, concept)
    return concept, version


def _review_section(db, user, now=NOW):
    return _today(db, user, now=now).sections.review


def test_today_shows_only_delivered_prompts_and_never_delivers_by_itself(db, bootstrap):
    _due_at(db, bootstrap.user, name="Due one", age=200)
    section = _review_section(db, bootstrap.user)
    assert (section.total, section.items) == (0, [])  # due, but nothing has been delivered yet
    assert section.allocation_pending is True and section.not_prompted == 1
    assert section.quota["limit"] == 2 and section.quota["delivered"] == 0 and section.quota["remaining"] == 2
    assert _delivery_rows(db) == []  # reading Today created nothing

    result = _deliver(db, bootstrap.user)
    assert result.new and len(_delivery_rows(db)) == 1
    section = _review_section(db, bootstrap.user)
    assert section.total == 1 and section.allocation_pending is False and section.not_prompted == 0


def test_a_delivered_review_card_exposes_everything_the_contract_asks_for(db, bootstrap):
    concept, _ = _due_at(db, bootstrap.user, name="Tokens", age=190)
    _deliver(db, bootstrap.user)
    section = _review_section(db, bootstrap.user)
    assert (section.total, section.shown) == (1, 1)
    card = section.items[0]
    assert card.id == f"REVIEW:{concept.id}" and card.kind == "REVIEW_DUE"
    assert card.concept["name"] == "Tokens" and card.concept["kind"] == "definitional"
    assert card.learner_state == {"ladder": "demonstrated", "overlays": ["review_due"]}
    assert card.reason_codes == ["REVIEW_ELIGIBLE_CORE", "REVIEW_INTERVAL_ELAPSED"]
    assert "180 days" in card.what and "core Concept" in card.why
    assert card.baseline["evidence_type"] == "knowledge_check" and card.baseline["concept_version"] == 1
    assert card.baseline["recorded_at"] == ago(190)
    assert card.interval["days"] == 180 and card.interval["successful_reviews_on_schedule"] == 0 and card.due_at == ago(10)
    assert card.attempt is None
    assert card.action["kind"] == "start_review" and card.action["learning_item_id"]
    assert card.prompt["slot"] == 1 and card.prompt["delivered_at"] == NOW
    assert {(r.type, r.role) for r in card.evidence_refs} == {("learning_evidence", "baseline"), ("concept_version", "baseline_version")}
    assert "SECRET-QUESTION-BODY" not in card.model_dump_json()  # the question text is never in Today


def test_prompt_kinds_take_precedence_failed_then_changed_then_interval(db, bootstrap):
    failed, fv = _due_at(db, bootstrap.user, name="F failed", age=200)
    _failed_review(db, bootstrap.user, failed, fv, at=ago(4) - timedelta(hours=20))  # cooldown long over
    changed, cv = _concept(db, name="C changed")
    _evidence(db, bootstrap.user, changed, cv, at=ago(20))
    _publish(db, changed, at=ago(3), severity=ChangeSeverity.MATERIAL, note="Rewritten.")
    _item(db, changed)
    _due_at(db, bootstrap.user, name="D due", age=200)

    # Classify through the uncapped candidate list (the section only shows what was delivered).
    candidates = _prompts(db).candidates(bootstrap.user.id)
    assert [(c.name, c.kind) for c in candidates] == [
        ("F failed", "REVIEW_FAILED"), ("C changed", "CONCEPT_CHANGED_REVIEW"), ("D due", "REVIEW_DUE")]
    assert "REVIEW_FAILED_LATEST" in candidates[0].state.review.reason_codes
    assert candidates[1].state.review.reason_codes == ["REVIEW_ELIGIBLE_CORE", "REVIEW_CONCEPT_CHANGED"]

    _deliver(db, bootstrap.user)  # two per week: the failed review, then the material change
    cards = {c.concept["name"]: c for c in _review_section(db, bootstrap.user).items}
    assert set(cards) == {"F failed", "C changed"}
    assert "still recorded as Demonstrated" in cards["F failed"].why
    assert "Rewritten." in cards["C changed"].what and cards["C changed"].material_changes[0]["version"] == 2
    assert len({c.concept["id"] for c in cards.values()}) == len(cards)  # one card per concept


def test_at_most_two_new_prompts_per_week_and_a_third_due_concept_is_not_newly_prompted(db, bootstrap):
    _due_at(db, bootstrap.user, name="Older due", age=400)
    _due_at(db, bootstrap.user, name="Newer due", age=250)
    failed, fv = _due_at(db, bootstrap.user, name="Failed", age=200)
    _failed_review(db, bootstrap.user, failed, fv, at=ago(4) - timedelta(hours=20))

    first = _deliver(db, bootstrap.user)
    assert len(first.new) == 2 and [d.slot for d in first.delivered] == [1, 2]
    section = _review_section(db, bootstrap.user)
    assert [c.concept["name"] for c in section.items] == ["Failed", "Older due"]  # failed first, then longest overdue
    assert section.not_prompted == 1 and section.allocation_pending is False  # known, but not newly prompted
    assert section.quota["remaining"] == 0

    again = _deliver(db, bootstrap.user)  # the week is full: nothing more is delivered
    assert again.new == [] and len(_delivery_rows(db)) == 2
    assert {c.concept["name"] for c in _review_section(db, bootstrap.user).items} == {"Failed", "Older due"}


def test_refreshing_today_and_repeating_allocation_never_consume_more_quota(db, bootstrap, engine):
    for i in range(3):
        _due_at(db, bootstrap.user, name=f"Due {i}", age=200 + i)
    _deliver(db, bootstrap.user)
    rows = [(r.id, r.concept_id, r.slot, r.delivered_at) for r in _delivery_rows(db)]
    before = _dump(engine)
    statements = []

    def spy(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().split(None, 1)[0].upper() in {"INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER"}:
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", spy)
    try:
        for _ in range(5):  # refresh Today repeatedly
            _today(db, bootstrap.user)
    finally:
        event.remove(engine, "before_cursor_execute", spy)
    assert statements == [] and _dump(engine) == before  # zero writes, nothing consumed

    for _ in range(3):  # and repeating the allocation action is idempotent
        assert _deliver(db, bootstrap.user).new == []
    assert [(r.id, r.concept_id, r.slot, r.delivered_at) for r in _delivery_rows(db)] == rows


def test_the_same_logical_prompt_is_never_counted_twice_in_a_week(db, bootstrap):
    concept, _ = _due_at(db, bootstrap.user, name="Once", age=200)
    _deliver(db, bootstrap.user)
    v2 = _publish(db, concept, at=ago(1), severity=ChangeSeverity.MATERIAL)  # its reason changes to "changed"
    _deliver(db, bootstrap.user)
    _passed_review(db, bootstrap.user, concept, v2, at=ago(0.5))  # resolved...
    _publish(db, concept, at=ago(0.1), severity=ChangeSeverity.MATERIAL)  # ...and due again the same week
    assert ReviewPromptService(db, now=NOW).preview(bootstrap.user.id).delivered  # still known as delivered
    _deliver(db, bootstrap.user)
    rows = _delivery_rows(db)
    assert len(rows) == 1 and rows[0].concept_id == concept.id and rows[0].slot == 1
    assert ReviewPromptService(db, now=NOW).preview(bootstrap.user.id).delivered[0].id == rows[0].id


def test_a_delivered_prompt_keeps_its_slot_after_the_review_is_done_and_the_card_goes_away(db, bootstrap):
    a, av = _due_at(db, bootstrap.user, name="A", age=400)
    _due_at(db, bootstrap.user, name="B", age=300)
    _due_at(db, bootstrap.user, name="C", age=250)
    _deliver(db, bootstrap.user)
    _passed_review(db, bootstrap.user, a, av, at=ago(0.5))  # A is reviewed, so it is no longer due
    assert {c.concept["name"] for c in _review_section(db, bootstrap.user).items} == {"B"}  # no card for a resolved prompt
    assert _deliver(db, bootstrap.user).new == []  # ...but its delivery still counts: C is not newly prompted
    assert len(_delivery_rows(db)) == 2


def test_a_concept_that_becomes_due_later_in_the_week_takes_the_remaining_slot(db, bootstrap):
    first, _ = _due_at(db, bootstrap.user, name="First", age=300)
    one = _deliver(db, bootstrap.user)
    assert [d.concept_id for d in one.delivered] == [first.id] and [d.slot for d in one.delivered] == [1]
    second, _ = _due_at(db, bootstrap.user, name="Second", age=200)  # becomes due after the first prompt
    two = _deliver(db, bootstrap.user)
    assert two.new == [second.id]  # only the new Concept is delivered; the first is untouched
    assert [(d.concept_id, d.slot) for d in two.delivered] == [(first.id, 1), (second.id, 2)]
    assert len(_delivery_rows(db)) == 2


def test_the_next_week_can_deliver_again_when_still_relevant(db, bootstrap):
    _due_at(db, bootstrap.user, name="Still due", age=400)
    week_one = _deliver(db, bootstrap.user)
    next_week = NOW + timedelta(days=7)
    assert _review_section(db, bootstrap.user, now=next_week).quota["delivered"] == 0  # a fresh week
    week_two = _deliver(db, bootstrap.user, now=next_week)
    assert len(week_one.new) == 1 and len(week_two.new) == 1
    rows = _delivery_rows(db)
    assert len(rows) == 2 and rows[0].week_start != rows[1].week_start and [r.slot for r in rows] == [1, 1]
    assert _review_section(db, bootstrap.user, now=next_week).total == 1


def test_the_week_runs_monday_to_monday_utc():
    from app.services.review_prompt_service import week_start_of

    monday = datetime(2026, 9, 14, tzinfo=timezone.utc)
    assert week_start_of(datetime(2026, 9, 14, 0, 0, 0, tzinfo=timezone.utc)) == monday
    assert week_start_of(datetime(2026, 9, 20, 23, 59, 59, tzinfo=timezone.utc)) == monday  # Sunday night
    assert week_start_of(datetime(2026, 9, 21, 0, 0, 0, tzinfo=timezone.utc)) == monday + timedelta(days=7)
    assert week_start_of(datetime(2026, 9, 16, 12, tzinfo=timezone.utc)) == monday


def test_one_users_quota_never_affects_anothers(db, bootstrap):
    other = _second_user(db, bootstrap)
    for i in range(3):
        _due_at(db, bootstrap.user, name=f"Mine {i}", age=200 + i)
        c, v = _concept(db, name=f"Theirs {i}")
        _evidence(db, other, c, v, at=ago(200 + i))
        _item(db, c)
    assert len(_deliver(db, bootstrap.user).new) == 2
    theirs = _deliver(db, other)
    assert len(theirs.new) == 2 and all(d.user_id == other.id for d in theirs.delivered)
    assert _review_section(db, bootstrap.user).quota["delivered"] == 2
    assert {c.concept["name"] for c in _review_section(db, other).items} <= {"Theirs 0", "Theirs 1", "Theirs 2"}
    assert {d.user_id for d in _delivery_rows(db)} == {bootstrap.user.id, other.id}
    assert len([d for d in _delivery_rows(db) if d.user_id == other.id]) == 2


def test_an_exhausted_weekly_quota_never_prevents_a_voluntary_review(db, bootstrap):
    _due_at(db, bootstrap.user, name="One", age=400)
    _due_at(db, bootstrap.user, name="Two", age=300)
    third, _ = _due_at(db, bootstrap.user, name="Three", age=200)
    early, ev = _concept(db, name="Early")
    _evidence(db, bootstrap.user, early, ev, at=rago(3))  # not even due
    _item(db, early)
    real = datetime.now(timezone.utc)
    _deliver(db, bootstrap.user, now=real)
    assert _review_section(db, bootstrap.user, now=real).quota["remaining"] == 0

    service = ReviewAttemptService(db)
    for concept in (third, early):  # a never-prompted due Concept, and one that is not due at all
        assert service.availability(bootstrap.user.id, concept.id).action == "start"
        attempt = service.start(bootstrap.user.id, concept.id).attempt
        assert service.complete(bootstrap.user.id, attempt.id, [1]).passed
    assert len(_delivery_rows(db)) == 2  # reviewing consumed no prompt quota


def test_only_reviews_that_can_actually_be_started_are_prompted(db, bootstrap):
    _due_at(db, bootstrap.user, name="No item", age=400, item=False)
    cooling, cv = _due_at(db, bootstrap.user, name="Cooling", age=300)
    _failed_review(db, bootstrap.user, cooling, cv, at=NOW - timedelta(hours=1))
    ready, _ = _due_at(db, bootstrap.user, name="Ready", age=200)
    assert [c.name for c in _prompts(db).candidates(bootstrap.user.id)] == ["Ready"]
    _deliver(db, bootstrap.user)
    assert {d.concept_id for d in _delivery_rows(db)} == {ready.id}  # quota is not spent on unusable prompts


def test_a_delivered_prompt_stays_visible_even_when_the_review_then_enters_cooldown(db, bootstrap):
    concept, version = _due_at(db, bootstrap.user, name="Cooling", age=202)
    _deliver(db, bootstrap.user)
    failed = _failed_review(db, bootstrap.user, concept, version, at=NOW - timedelta(hours=1))
    card = _review_section(db, bootstrap.user).items[0]
    assert card.kind == "REVIEW_FAILED" and card.attempt["id"] == failed.id and card.attempt["status"] == "failed"
    assert card.action["kind"] == "unavailable" and card.action["reason"] == "COOLDOWN"
    assert card.action["available_after"] == NOW + timedelta(hours=11)
    later = _review_section(db, bootstrap.user, now=NOW + timedelta(hours=11, minutes=30))  # cooldown over, same week
    assert later.items[0].action["kind"] == "start_review"


def test_a_delivered_prompt_offers_to_continue_an_in_progress_review(db, bootstrap):
    concept, _ = _due_at(db, bootstrap.user, name="Started", age=201)
    _deliver(db, bootstrap.user)
    attempt = ReviewAttemptService(db, now=NOW).start(bootstrap.user.id, concept.id).attempt
    card = _review_section(db, bootstrap.user).items[0]
    assert card.action["kind"] == "continue_review" and card.action["attempt_id"] == attempt.id
    assert card.attempt["status"] == "started" and card.attempt["id"] == attempt.id


def test_the_full_action_states_are_reachable(db, bootstrap):
    service = ReviewAttemptService(db, now=NOW)
    no_item, _ = _due_at(db, bootstrap.user, name="No item", age=200, item=False)
    assert service.availability(bootstrap.user.id, no_item.id).reason == "NO_REVIEW_ITEM"
    started, _ = _due_at(db, bootstrap.user, name="Started", age=201)
    attempt = service.start(bootstrap.user.id, started.id).attempt
    available = service.availability(bootstrap.user.id, started.id)
    assert (available.action, available.attempt_id) == ("continue", attempt.id)
    cooling, cv = _due_at(db, bootstrap.user, name="Cooling", age=202)
    _failed_review(db, bootstrap.user, cooling, cv, at=NOW - timedelta(hours=1))
    waiting = service.availability(bootstrap.user.id, cooling.id)
    assert (waiting.action, waiting.reason, waiting.available_after) == ("unavailable", "COOLDOWN", NOW + timedelta(hours=11))
    assert service.availability(bootstrap.user.id, "missing").reason == "NOT_FOUND"


def test_the_review_section_is_empty_for_ineligible_or_undemonstrated_concepts(db, bootstrap):
    concept, version = _concept(db, core=False)
    _evidence(db, bootstrap.user, concept, version, at=ago(400))
    _item(db, concept)
    other, ov = _concept(db)
    _evidence(db, bootstrap.user, other, ov, at=ago(400), passed=False)
    _item(db, other)
    _deliver(db, bootstrap.user)
    section = _review_section(db, bootstrap.user)
    assert (section.total, section.items, section.not_prompted, section.allocation_pending) == (0, [], 0, False)
    _plan(db, bootstrap.user, concept)  # an active plan makes the first one eligible
    _deliver(db, bootstrap.user)
    assert _review_section(db, bootstrap.user).total == 1


def test_another_users_review_state_never_appears_on_todays_review_section(db, bootstrap):
    other = _second_user(db, bootstrap)
    concept, version = _concept(db)
    _evidence(db, other, concept, version, at=ago(400))
    _item(db, concept)
    _deliver(db, bootstrap.user)
    _deliver(db, other)
    assert _review_section(db, bootstrap.user).total == 0
    assert _review_section(db, other).total == 1


def test_the_review_section_does_not_change_the_existing_signal_sections(db, bootstrap):
    """4A semantics are frozen: a review-due concept adds review cards only."""
    _due_at(db, bootstrap.user, name="Quiet", age=200)
    _deliver(db, bootstrap.user)
    s = _today(db, bootstrap.user).sections
    assert all(section.total == 0 for section in (
        s.worth_revisiting, s.used_models_changed, s.watched_developments, s.experiments_to_rerun, s.concepts_changed))
    assert s.review.total == 1


def test_the_database_caps_prompts_at_two_per_user_per_week(db, bootstrap):
    a, _ = _concept(db)
    b, _ = _concept(db)
    c, _ = _concept(db)
    week = datetime(2026, 9, 14, tzinfo=timezone.utc)

    def add(concept, slot, *, when=week, user=None):
        db.add(ReviewPromptDelivery(user_id=(user or bootstrap.user).id, concept_id=concept.id, week_start=when,
                                    slot=slot, prompt_kind="REVIEW_DUE", delivered_at=NOW))

    def refused(concept, slot, **kwargs):
        add(concept, slot, **kwargs)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    add(a, 1)
    add(b, 2)
    db.commit()
    refused(c, 3)  # a third delivery in a week cannot exist (slot is 1 or 2)
    refused(c, 0)
    refused(c, 1)  # slot 1 is taken this week
    week_two = week + timedelta(days=7)
    add(a, 1, when=week_two)
    db.commit()
    refused(a, 2, when=week_two)  # the same Concept twice in one week, even in a free slot
    add(a, 1, when=week + timedelta(days=14))  # a new week starts fresh
    add(c, 1, user=_second_user(db, bootstrap))  # and another user has their own quota
    db.commit()
    assert len(_delivery_rows(db)) == 5


def test_concurrent_allocations_never_exceed_two_prompts(db, session_factory, bootstrap):
    for i in range(4):
        _due_at(db, bootstrap.user, name=f"Due {i}", age=400 - i)
    base = datetime.now(timezone.utc)
    barrier, results, errors = threading.Barrier(6), [], []

    def worker():
        session = session_factory()
        try:
            barrier.wait(timeout=10)
            results.append(ReviewPromptService(session, now=base).allocate(bootstrap.user.id))
        except Exception as exc:  # noqa: BLE001  # pragma: no cover - surfaced by the assertion below
            errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not errors, errors
    db.expire_all()
    rows = _delivery_rows(db)
    assert len(rows) == 2 and sorted(r.slot for r in rows) == [1, 2]
    assert len({r.concept_id for r in rows}) == 2
    assert sum(len(r.new) for r in results) == 2  # exactly two newly delivered across every caller
    assert all(len(r.delivered) == 2 for r in results)


# =============================================================================
# API
# =============================================================================


@pytest.fixture()
def as_user():
    def _override(user):
        fastapi_app.dependency_overrides[get_current_user] = lambda: user

    yield _override
    fastapi_app.dependency_overrides.pop(get_current_user, None)


def test_api_requires_authentication(client, bootstrap):
    assert client.post("/learning-reviews", json={"concept_id": "x"}).status_code == 401
    assert client.get("/learning-reviews/x").status_code == 401
    assert client.post("/learning-reviews/x/complete", json={"selected": [0]}).status_code == 401


def test_api_review_flow_never_exposes_the_answer_key(client, auth_headers, db, bootstrap):
    concept, _ = _due_core(db, bootstrap.user)
    _item(db, concept)
    started = client.post("/learning-reviews", json={"concept_id": concept.id}, headers=auth_headers)
    assert started.status_code == 201, started.text
    body = started.json()
    assert body["created"] is True and body["attempt"]["status"] == "started"
    assert body["item"]["options"] == ["wrong", "right", "also wrong"] and body["item"]["multiple"] is False
    assert "answer_key" not in started.text and "SECRET-QUESTION-BODY" in started.text  # the question, not the key

    again = client.post("/learning-reviews", json={"concept_id": concept.id}, headers=auth_headers)
    assert again.status_code == 200 and again.json()["attempt"]["id"] == body["attempt"]["id"]
    assert client.get(f"/learning-reviews/{body['attempt']['id']}", headers=auth_headers).json()["item"] is not None

    done = client.post(f"/learning-reviews/{body['attempt']['id']}/complete", json={"selected": [1]}, headers=auth_headers)
    assert done.status_code == 200, done.text
    result = done.json()
    assert result["passed"] is True and result["replay"] is False and result["evidence_id"]
    assert result["learner_state"]["ladder"] == "demonstrated"
    assert "review_due" not in result["learner_state"]["overlays"]
    assert "answer_key" not in done.text
    replay = client.post(f"/learning-reviews/{body['attempt']['id']}/complete", json={"selected": [0]}, headers=auth_headers)
    assert replay.json()["replay"] is True and replay.json()["evidence_id"] == result["evidence_id"]
    # A finished attempt no longer serves its question.
    assert client.get(f"/learning-reviews/{body['attempt']['id']}", headers=auth_headers).json()["item"] is None


def test_api_errors_are_specific_and_requests_forbid_a_client_user(client, auth_headers, db, bootstrap):
    concept, _ = _due_core(db, bootstrap.user)
    assert client.post("/learning-reviews", json={"concept_id": concept.id}, headers=auth_headers).status_code == 409
    assert client.post("/learning-reviews", json={"concept_id": "nope"}, headers=auth_headers).status_code == 404
    assert client.post("/learning-reviews", json={"concept_id": concept.id, "user_id": "x"}, headers=auth_headers).status_code == 422
    assert client.post("/learning-reviews/x/complete", json={"selected": [0], "user_id": "x"}, headers=auth_headers).status_code == 422
    assert client.post("/learning-reviews/x/complete", json={"selected": [True]}, headers=auth_headers).status_code == 422
    _item(db, concept)
    attempt_id = client.post("/learning-reviews", json={"concept_id": concept.id}, headers=auth_headers).json()["attempt"]["id"]
    assert client.post(f"/learning-reviews/{attempt_id}/complete", json={"selected": [9]}, headers=auth_headers).status_code == 422
    assert client.post(f"/learning-reviews/{attempt_id}/complete", json={"selected": []}, headers=auth_headers).status_code == 422
    assert client.get(f"/learning-reviews/{attempt_id}", headers=auth_headers).json()["attempt"]["status"] == "started"


def test_api_cross_user_isolation(client, db, bootstrap, as_user):
    concept, _ = _due_core(db, bootstrap.user)
    _item(db, concept)
    other = _second_user(db, bootstrap)
    as_user(bootstrap.user)
    attempt_id = client.post("/learning-reviews", json={"concept_id": concept.id}).json()["attempt"]["id"]
    as_user(other)
    assert client.get(f"/learning-reviews/{attempt_id}").status_code == 404
    assert client.post(f"/learning-reviews/{attempt_id}/complete", json={"selected": [1]}).status_code == 404
    assert client.post("/learning-reviews", json={"concept_id": concept.id}).status_code == 409  # not theirs to review
    as_user(bootstrap.user)
    assert client.get(f"/learning-reviews/{attempt_id}").json()["attempt"]["status"] == "started"


def test_today_get_with_review_data_is_read_only_and_private(client, auth_headers, db, bootstrap, engine):
    concept, _ = _due_core(db, bootstrap.user)
    _item(db, concept)
    _plan(db, bootstrap.user, concept)
    failed, fv = _due_core(db, bootstrap.user)
    _item(db, failed)
    _failed_review(db, bootstrap.user, failed, fv, at=rago(2))
    assert ReviewPromptService(db).allocate(bootstrap.user.id).new  # prompts were delivered earlier this week
    db.expire_all()
    before = _dump(engine)
    statements = []

    def spy(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().split(None, 1)[0].upper() in {"INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER"}:
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", spy)
    try:
        response = client.get("/stay-ahead/today", headers=auth_headers)
    finally:
        event.remove(engine, "before_cursor_execute", spy)
    assert response.status_code == 200, response.text
    assert statements == [] and _dump(engine) == before  # nothing moved: learner state, evidence, plans, Radar, MA8
    review = response.json()["sections"]["review"]
    assert review["quota"]["limit"] == 2 and "allocation_pending" in review and "not_prompted" in review
    assert "SECRET-QUESTION-BODY" not in response.text and "answer_key" not in response.text
    assert set(review["items"][0]["action"]) <= {"kind", "reason", "available_after", "learning_item_id", "attempt_id"}


def test_no_score_or_ranking_fields_in_the_review_section(db, bootstrap):
    _due_at(db, bootstrap.user, name="A", age=200)
    _deliver(db, bootstrap.user)
    payload = _today(db, bootstrap.user).model_dump(mode="json")["sections"]["review"]

    def keys(node):
        if isinstance(node, dict):
            for k, v in node.items():
                yield k
                yield from keys(v)
        elif isinstance(node, list):
            for item in node:
                yield from keys(item)

    assert not ({"score", "priority", "rank", "ranking", "importance", "urgency"} & set(keys(payload)))


def test_api_allocation_delivers_at_most_two_prompts_and_is_idempotent(client, auth_headers, db, bootstrap):
    for i in range(3):
        concept, _ = _due_core(db, bootstrap.user, name=f"Alloc {i}", evidence_age_days=400 - i)
        _item(db, concept)
    first = client.post("/learning-reviews/prompts/allocate", headers=auth_headers)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["limit"] == 2 and body["new_count"] == 2 and len(body["delivered"]) == 2
    assert [d["slot"] for d in body["delivered"]] == [1, 2] and all(d["new"] for d in body["delivered"])
    again = client.post("/learning-reviews/prompts/allocate", headers=auth_headers).json()
    assert again["new_count"] == 0 and not any(d["new"] for d in again["delivered"])
    assert [d["concept_id"] for d in again["delivered"]] == [d["concept_id"] for d in body["delivered"]]
    assert db.execute(select(func.count()).select_from(ReviewPromptDelivery)).scalar() == 2


def test_api_allocation_requires_authentication_and_is_user_scoped(client, db, bootstrap, as_user):
    assert client.post("/learning-reviews/prompts/allocate").status_code == 401
    for i in range(2):
        concept, _ = _due_core(db, bootstrap.user, name=f"Mine {i}")
        _item(db, concept)
    other = _second_user(db, bootstrap)
    as_user(other)
    theirs = client.post("/learning-reviews/prompts/allocate").json()
    assert theirs["new_count"] == 0 and theirs["delivered"] == []  # nothing of the owner's is theirs
    as_user(bootstrap.user)
    assert client.post("/learning-reviews/prompts/allocate").json()["new_count"] == 2


def test_the_quota_never_blocks_starting_a_review_through_the_api(client, auth_headers, db, bootstrap):
    concepts = []
    for i in range(3):
        concept, _ = _due_core(db, bootstrap.user, name=f"Quota {i}", evidence_age_days=400 - i)
        _item(db, concept)
        concepts.append(concept)
    client.post("/learning-reviews/prompts/allocate", headers=auth_headers)
    delivered = {d.concept_id for d in db.execute(select(ReviewPromptDelivery)).scalars()}
    unprompted = next(c for c in concepts if c.id not in delivered)
    started = client.post("/learning-reviews", json={"concept_id": unprompted.id}, headers=auth_headers)
    assert started.status_code == 201, started.text  # quota exhausted, yet a voluntary review starts


def test_today_reports_pending_allocation_without_writing(client, auth_headers, db, bootstrap, engine):
    concept, _ = _due_core(db, bootstrap.user)
    _item(db, concept)
    before = _dump(engine)
    review = client.get("/stay-ahead/today", headers=auth_headers).json()["sections"]["review"]
    assert review["allocation_pending"] is True and review["total"] == 0 and review["not_prompted"] == 1
    assert _dump(engine) == before  # asking for Today did not deliver anything
    client.post("/learning-reviews/prompts/allocate", headers=auth_headers)
    review = client.get("/stay-ahead/today", headers=auth_headers).json()["sections"]["review"]
    assert review["allocation_pending"] is False and review["total"] == 1 and review["quota"]["delivered"] == 1
