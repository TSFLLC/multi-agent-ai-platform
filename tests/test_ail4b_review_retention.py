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
from app.models.learning_review import ReviewAttempt
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
    assert not review.due and review.streak == 1
    assert review.interval_days == 360 and review.base_interval_days == 180  # doubled once
    assert review.due_at == ago(1) + timedelta(days=360)


def test_the_interval_doubles_per_success_and_is_capped_at_365_days():
    assert [interval_days_for(ConceptKind.OPERATIONAL, k) for k in range(6)] == [90, 180, 360, 365, 365, 365]
    assert interval_days_for(ConceptKind.DEFINITIONAL, 1) == 360
    assert interval_days_for(ConceptKind.DEFINITIONAL, 2) == 365
    assert interval_days_for(ConceptKind.MECHANISM, 50) == MAX_INTERVAL_DAYS == 365
    assert interval_days_for(ConceptKind.ARCHITECTURAL, -3) == 120


def test_the_cap_holds_through_real_attempt_history(db, bootstrap):
    concept, version = _concept(db, kind=ConceptKind.OPERATIONAL)
    _evidence(db, bootstrap.user, concept, version, at=ago(900))
    for i in range(4):
        _passed_review(db, bootstrap.user, concept, version, at=ago(800 - i * 100))
    review = _review(db, bootstrap.user, concept)
    assert review.streak == 4 and review.interval_days == 365


def test_a_failed_review_resets_the_streak_but_not_the_clock(db, bootstrap):
    concept, version = _concept(db)
    _evidence(db, bootstrap.user, concept, version, at=ago(500))
    _passed_review(db, bootstrap.user, concept, version, at=ago(400))
    _passed_review(db, bootstrap.user, concept, version, at=ago(300))
    assert _review(db, bootstrap.user, concept, now=ago(299)).streak == 2
    _failed_review(db, bootstrap.user, concept, version, at=ago(200))
    review = _review(db, bootstrap.user, concept)
    assert review.streak == 0 and review.interval_days == 180
    assert review.baseline.recorded_at == ago(300)  # failure appended no evidence
    _passed_review(db, bootstrap.user, concept, version, at=ago(100))
    assert _review(db, bootstrap.user, concept).streak == 1


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
    assert review.streak == 0  # nor extended
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
    ("not_due", "NOT_DUE"),
    ("no_item", "NO_REVIEW_ITEM"),
])
def test_a_review_cannot_start_unless_one_is_warranted(db, bootstrap, build, reason):
    if build == "not_demonstrated":
        concept, version = _concept(db)
        _evidence(db, bootstrap.user, concept, version, at=rago(500), passed=False)
    elif build == "not_eligible":
        concept, version = _concept(db, core=False)
        _evidence(db, bootstrap.user, concept, version, at=rago(500))
        _item(db, concept)
    elif build == "not_due":
        concept, version = _concept(db)
        _evidence(db, bootstrap.user, concept, version, at=rago(5))
        _item(db, concept)
    else:
        concept, _ = _due_core(db, bootstrap.user)
        _item(db, concept, reviewed=False)
    with pytest.raises(ReviewNotAvailableError) as raised:
        ReviewAttemptService(db).start(bootstrap.user.id, concept.id)
    assert raised.value.detail["reason"] == reason
    assert db.execute(select(func.count()).select_from(ReviewAttempt)).scalar() == 0


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
    assert result.state.review.streak == 1 and result.state.review.interval_days == 360


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
    assert "review_failed" not in theirs.overlays and theirs.review.streak == 0 and theirs.review.latest_completed is None


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
# Today: the review section
# =============================================================================


def _today(db, user, now=NOW, **kwargs):
    return StayAheadService(db, now=now).today(user.id, **kwargs)


def _due_at(db, user, *, name, age, kind=ConceptKind.DEFINITIONAL, item=True):
    concept, version = _concept(db, kind=kind, name=name)
    _evidence(db, user, concept, version, at=ago(age))
    if item:
        _item(db, concept)
    return concept, version


def test_a_due_review_card_exposes_everything_the_contract_asks_for(db, bootstrap):
    concept, _ = _due_at(db, bootstrap.user, name="Tokens", age=190)
    result = _today(db, bootstrap.user)
    section = result.sections.review
    assert (section.total, section.shown) == (1, 1)
    card = section.items[0]
    assert card.id == f"REVIEW:{concept.id}" and card.kind == "REVIEW_DUE"
    assert card.concept["name"] == "Tokens" and card.concept["kind"] == "definitional"
    assert card.learner_state == {"ladder": "demonstrated", "overlays": ["review_due"]}
    assert card.reason_codes == ["REVIEW_ELIGIBLE_CORE", "REVIEW_INTERVAL_ELAPSED"]
    assert "180 days" in card.what and "core Concept" in card.why
    assert card.baseline["evidence_type"] == "knowledge_check" and card.baseline["concept_version"] == 1
    assert card.baseline["recorded_at"] == ago(190)
    assert card.interval["days"] == 180 and card.interval["streak"] == 0 and card.due_at == ago(10)
    assert card.attempt is None
    assert card.action["kind"] == "start_review" and card.action["learning_item_id"]
    assert {(r.type, r.role) for r in card.evidence_refs} == {("learning_evidence", "baseline"), ("concept_version", "baseline_version")}
    assert "SECRET-QUESTION-BODY" not in card.model_dump_json()  # the question text is never in Today


def test_card_kinds_take_precedence_failed_then_changed_then_interval(db, bootstrap):
    failed, fv = _due_at(db, bootstrap.user, name="F failed", age=200)
    _failed_review(db, bootstrap.user, failed, fv, at=ago(4))
    changed, cv = _concept(db, name="C changed")
    _evidence(db, bootstrap.user, changed, cv, at=ago(20))
    _publish(db, changed, at=ago(3), severity=ChangeSeverity.MATERIAL, note="Rewritten.")
    _item(db, changed)
    _due_at(db, bootstrap.user, name="D due", age=200)

    # The section shows at most two cards, so classify through the uncapped builder.
    service = StayAheadService(db, now=NOW)
    cards = {c.concept["name"]: c for c in service._review_cards(bootstrap.user.id, service._learned_concepts(bootstrap.user.id))}
    assert cards["F failed"].kind == "REVIEW_FAILED"
    assert "REVIEW_FAILED_LATEST" in cards["F failed"].reason_codes and "REVIEW_INTERVAL_ELAPSED" in cards["F failed"].reason_codes
    assert "still recorded as Demonstrated" in cards["F failed"].why
    assert cards["C changed"].kind == "CONCEPT_CHANGED_REVIEW" and "Rewritten." in cards["C changed"].what
    assert cards["C changed"].reason_codes == ["REVIEW_ELIGIBLE_CORE", "REVIEW_CONCEPT_CHANGED"]
    assert cards["C changed"].material_changes[0]["version"] == 2
    assert cards["D due"].kind == "REVIEW_DUE"
    assert len(cards) == 3 and len({c.concept["id"] for c in cards.values()}) == 3  # one card per concept


def test_review_cards_are_ordered_by_visible_groups_and_capped_at_two_with_an_honest_total(db, bootstrap):
    _, _ = _due_at(db, bootstrap.user, name="Older due", age=400)
    _due_at(db, bootstrap.user, name="Newer due", age=250)
    failed, fv = _due_at(db, bootstrap.user, name="Failed", age=200)
    _failed_review(db, bootstrap.user, failed, fv, at=ago(4))
    section = _today(db, bootstrap.user).sections.review
    assert (section.total, section.shown) == (3, 2)
    assert [c.concept["name"] for c in section.items] == ["Failed", "Older due"]  # failed first, then longest overdue
    assert _today(db, bootstrap.user).model_dump_json() == _today(db, bootstrap.user).model_dump_json()  # deterministic


def test_the_review_section_is_empty_for_ineligible_or_undemonstrated_concepts(db, bootstrap):
    concept, version = _concept(db, core=False)
    _evidence(db, bootstrap.user, concept, version, at=ago(400))
    other, ov = _concept(db)
    _evidence(db, bootstrap.user, other, ov, at=ago(400), passed=False)
    section = _today(db, bootstrap.user).sections.review
    assert (section.total, section.shown, section.items) == (0, 0, [])
    _plan(db, bootstrap.user, concept)  # an active plan makes the first one eligible
    assert _today(db, bootstrap.user).sections.review.total == 1


def _only_card(db, user):
    (card,) = _today(db, user).sections.review.items
    return card


def test_a_card_offers_no_action_when_no_reviewed_check_exists(db, bootstrap):
    _due_at(db, bootstrap.user, name="No item", age=200, item=False)
    card = _only_card(db, bootstrap.user)
    assert card.action == {"kind": "unavailable", "reason": "NO_REVIEW_ITEM"}


def test_a_card_offers_to_continue_an_in_progress_review(db, bootstrap):
    concept, _ = _due_at(db, bootstrap.user, name="Started", age=201)
    attempt = ReviewAttemptService(db, now=NOW).start(bootstrap.user.id, concept.id).attempt
    card = _only_card(db, bootstrap.user)
    assert card.action["kind"] == "continue_review" and card.action["attempt_id"] == attempt.id
    assert card.attempt["status"] == "started" and card.attempt["id"] == attempt.id


def test_a_card_after_a_failed_review_shows_the_attempt_and_the_wait(db, bootstrap):
    concept, version = _due_at(db, bootstrap.user, name="Cooling", age=202)
    failed = _failed_review(db, bootstrap.user, concept, version, at=NOW - timedelta(hours=1))
    card = _only_card(db, bootstrap.user)
    assert card.kind == "REVIEW_FAILED"
    assert card.attempt["id"] == failed.id and card.attempt["status"] == "failed"
    assert card.action["kind"] == "unavailable" and card.action["reason"] == "COOLDOWN"
    assert card.action["available_after"] == NOW + timedelta(hours=11)
    assert _today(db, bootstrap.user, now=NOW + timedelta(hours=12)).sections.review.items[0].action["kind"] == "start_review"


def test_the_full_action_states_are_reachable(db, bootstrap):
    """Cap-free check of each availability outcome through the service."""
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


def test_another_users_review_state_never_appears_on_todays_review_section(db, bootstrap):
    other = _second_user(db, bootstrap)
    concept, version = _concept(db)
    _evidence(db, other, concept, version, at=ago(400))
    _item(db, concept)
    assert _today(db, bootstrap.user).sections.review.total == 0
    assert _today(db, other).sections.review.total == 1


def test_the_review_section_does_not_change_the_existing_signal_sections(db, bootstrap):
    """4A semantics are frozen: a review-due concept adds review cards only."""
    _, _ = _due_at(db, bootstrap.user, name="Quiet", age=200)
    result = _today(db, bootstrap.user)
    s = result.sections
    assert all(section.total == 0 for section in (
        s.worth_revisiting, s.used_models_changed, s.watched_developments, s.experiments_to_rerun, s.concepts_changed))
    assert s.review.total == 1


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
    _failed_review(db, bootstrap.user, failed, fv, at=rago(2))
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
    assert review["total"] == 2 and {c["kind"] for c in review["items"]} == {"REVIEW_DUE", "REVIEW_FAILED"}
    assert "SECRET-QUESTION-BODY" not in response.text and "answer_key" not in response.text
    assert set(review["items"][0]["action"]) <= {"kind", "reason", "available_after", "learning_item_id", "attempt_id"}


def test_no_score_or_ranking_fields_in_the_review_section(db, bootstrap):
    _due_at(db, bootstrap.user, name="A", age=200)
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
