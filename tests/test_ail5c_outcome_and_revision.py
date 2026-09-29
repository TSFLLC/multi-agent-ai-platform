"""AIL.5C curriculum outcome semantics and the definition-driven revision policy.

The engine keeps its richer internal outcomes. The authored curriculum's PASS /
NEEDS_REVISION vocabulary is a pure mapping on top, and "revise and resubmit
once" is *definition policy* (grading_policy.max_revisions), never a global rule.
"""

from datetime import timedelta, timezone

import pytest

from app.assessment_curriculum import (
    FOUNDATION_ASSESSMENTS,
    LEVEL1_EXPLAIN_BACK_ASSESSMENTS,
    seed_level1_explain_back_assessments,
)
from app.assessment_outcome import (
    CURRICULUM_NEEDS_REVISION,
    CURRICULUM_NOT_COUNTED,
    CURRICULUM_PASS,
    CURRICULUM_PENDING_CONFIRMATION,
    CURRICULUM_PENDING_HUMAN_REVIEW,
    CURRICULUM_UNABLE_TO_ASSESS,
    curriculum_outcome,
)
from app.db.enums import (
    AssessmentOrigin,
    AssessmentOutcome,
    AssessmentReviewDecision,
    DemonstrationEffect,
)
from app.errors import ConflictError
from app.models.assessment import AssessmentAttempt
from app.services.assessment_definition_service import AssessmentDefinitionService
from app.services.assessment_review_service import AssessmentReviewService
from app.services.assessment_service import AssessmentNotReady, AssessmentService
from tests.ail1a_factories import make_concept, make_published_version
from tests.ail5c_factories import (
    ScriptedGrader,
    default_findings,
    explain_definition,
    make_ail_concept,
    second_user,
    setup_free_models,
)

WORDS = (
    "Structured output makes a model follow a schema so the answers can be parsed reliably. "
    "A schema does not guarantee the content itself is true."
)
D = DemonstrationEffect
O = AssessmentOutcome


# -- the mapping ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "outcome, effect, code, final",
    [
        (O.PASSED, D.COUNTS_TOWARD_DEMONSTRATED, CURRICULUM_PASS, True),
        (O.PASSED, D.COUNTS_TOWARD_PRACTICED_ONLY, CURRICULUM_PASS, True),
        (O.PASSED, D.FORMATIVE_ONLY, CURRICULUM_NOT_COUNTED, True),
        (O.PASSED, D.NONE, CURRICULUM_NOT_COUNTED, True),
        (O.NEEDS_WORK, D.NONE, CURRICULUM_NEEDS_REVISION, True),
        (O.PROVISIONAL, D.NONE, CURRICULUM_PENDING_CONFIRMATION, False),
        (O.HUMAN_REVIEW_REQUIRED, D.NONE, CURRICULUM_PENDING_HUMAN_REVIEW, False),
        (O.UNABLE_TO_ASSESS, D.NONE, CURRICULUM_UNABLE_TO_ASSESS, False),
    ],
)
def test_internal_outcomes_map_to_curriculum_semantics(outcome, effect, code, final):
    assert curriculum_outcome(outcome, effect) == {"code": code, "final": final}


def test_pending_and_unable_states_are_never_reported_as_a_final_pass_or_revision():
    for outcome in (O.PROVISIONAL, O.HUMAN_REVIEW_REQUIRED, O.UNABLE_TO_ASSESS):
        mapped = curriculum_outcome(outcome, D.NONE)
        assert mapped["code"] not in (CURRICULUM_PASS, CURRICULUM_NEEDS_REVISION)
        assert mapped["final"] is False
    assert curriculum_outcome(None, None) == {"code": None, "final": False}


def test_every_internal_outcome_is_mapped_so_a_new_outcome_cannot_be_forgotten():
    for outcome in O:
        assert curriculum_outcome(outcome, D.NONE)["code"] is not None


# -- helpers --------------------------------------------------------------------------------------------------


def _weak():
    return ScriptedGrader(
        lambda n, keys, quote: [
            {
                "key": k,
                "finding": "not_met" if k == "limits" else "met",
                "confidence": "high",
                "rationale": "ok",
                "quotes": [] if k == "limits" else [quote],
                "gap": "No limit." if k == "limits" else None,
            }
            for k in keys
        ]
    )


def _setup(db, bootstrap, grading_extra=None):
    setup_free_models(db, count=2)
    learner = second_user(db, bootstrap.organization)
    concept, _v = make_ail_concept(db)
    defn = explain_definition(db, bootstrap.user, concept, grading_extra=grading_extra)
    return learner, concept, defn


def _submit(db, learner, defn, adapter, *, now=None, previous=None, text=WORDS):
    svc = AssessmentService(db, adapter_factory=lambda _d, _p: adapter, now=now)
    attempt = svc.start(learner, defn.definition_key, previous_attempt_id=previous)
    svc.save_draft(learner.id, attempt.id, {"fields": {"explanation": text}})
    svc.submit(learner, attempt.id, {"declaration": "no_external_help"})
    db.refresh(attempt)
    return svc, attempt


def _later(attempt, hours):
    return attempt.finalized_at.replace(tzinfo=None).replace(tzinfo=timezone.utc) + timedelta(hours=hours)


# -- revision policy ---------------------------------------------------------------------------------------------


def test_a_one_revision_definition_allows_one_revision_then_points_to_human_review(db, bootstrap):
    learner, _c, defn = _setup(db, bootstrap, {"max_revisions": 1})
    svc, first = _submit(db, learner, defn, _weak())
    assert svc.effective_result(first.id).outcome == O.NEEDS_WORK
    assert svc.revision_status(learner.id, defn) == {
        "max_revisions": 1,
        "used": 0,
        "remaining": 1,
        "exhausted": False,
    }

    # the one revision is allowed (after the unchanged retry cooldown)
    svc2, second = _submit(
        db, learner, defn, _weak(), now=_later(first, 13), previous=first.id, text=WORDS + " (revised)"
    )
    assert svc2.effective_result(second.id).outcome == O.NEEDS_WORK
    status = svc2.revision_status(learner.id, defn)
    assert status["exhausted"] is True and status["remaining"] == 0

    # a third automatic attempt is refused, pointing at human review, even after the cooldown
    later = AssessmentService(db, now=_later(second, 30))
    with pytest.raises(AssessmentNotReady) as blocked:
        later.start(learner, defn.definition_key, previous_attempt_id=second.id)
    checks = {c["key"]: c for c in blocked.value.detail["checks"]}
    assert checks["revision_limit"]["met"] is False and checks["revision_limit"]["review_available"] is True
    assert "human review" in checks["revision_limit"]["detail"]
    assert checks["cooldown"]["met"] is True  # the limit, not the cooldown, is what blocks


def test_a_human_requested_new_assessment_is_not_blocked_by_the_revision_limit(db, bootstrap):
    learner, _c, defn = _setup(db, bootstrap, {"max_revisions": 1})
    _svc, first = _submit(db, learner, defn, _weak())
    _svc2, second = _submit(
        db, learner, defn, _weak(), now=_later(first, 13), previous=first.id, text=WORDS + " (revised)"
    )
    review = AssessmentReviewService(db).request(
        learner, second.id, reason="Please look again.", consent=True
    )
    AssessmentReviewService(db).decide(
        bootstrap.user, review.id, AssessmentReviewDecision.NEW_ASSESSMENT, "Start a fresh assessment."
    )
    human = (
        db.query(AssessmentAttempt)
        .filter_by(user_id=learner.id, origin=AssessmentOrigin.HUMAN_REQUESTED)
        .one()
    )
    assert human.previous_attempt_id == second.id


def test_a_passing_revision_ends_the_streak(db, bootstrap):
    learner, _c, defn = _setup(db, bootstrap, {"max_revisions": 1})
    _svc, first = _submit(db, learner, defn, _weak())
    svc2, second = _submit(
        db,
        learner,
        defn,
        ScriptedGrader(),
        now=_later(first, 13),
        previous=first.id,
        text=WORDS + " (revised)",
    )
    assert svc2.effective_result(second.id).outcome == O.PASSED
    assert svc2.revision_status(learner.id, defn)["exhausted"] is False


def test_a_provisional_result_does_not_consume_a_revision(db, bootstrap):
    learner, _c, defn = _setup(db, bootstrap, {"max_revisions": 1})
    unsure = ScriptedGrader(lambda n, keys, quote: default_findings(keys, quote, confidence="low"))
    svc, first = _submit(db, learner, defn, unsure)
    assert svc.effective_result(first.id).outcome == O.PROVISIONAL
    svc2, second = _submit(db, learner, defn, _weak(), previous=first.id, text=WORDS + " (again)")
    assert svc2.effective_result(second.id).outcome == O.NEEDS_WORK
    assert svc2.revision_status(learner.id, defn) == {
        "max_revisions": 1,
        "used": 0,
        "remaining": 1,
        "exhausted": False,
    }


def test_the_generic_engine_has_no_revision_limit_unless_a_definition_sets_one(db, bootstrap):
    learner, _c, defn = _setup(db, bootstrap)  # no max_revisions
    attempt = None
    for round_no in range(3):
        svc, attempt = _submit(
            db,
            learner,
            defn,
            _weak(),
            now=None if attempt is None else _later(attempt, 13),
            previous=None if attempt is None else attempt.id,
            text=WORDS + f" (try {round_no})",
        )
        assert svc.effective_result(attempt.id).outcome == O.NEEDS_WORK
    assert svc.revision_status(learner.id, defn) == {
        "max_revisions": None,
        "used": 0,
        "remaining": None,
        "exhausted": False,
    }
    later = AssessmentService(db, now=_later(attempt, 13))
    assert not any(
        c["key"] == "revision_limit" for c in later.readiness(learner, defn.definition_key)["checks"]
    )


def test_max_revisions_is_validated_and_shown_to_the_learner(db, bootstrap):
    _learner, _c, defn = _setup(db, bootstrap, {"max_revisions": 1})
    assert AssessmentDefinitionService.learner_view(defn, [])["max_revisions"] == 1
    concept, _ = make_ail_concept(db)
    for n, bad in enumerate((-1, 4, "1", True, 1.5)):
        with pytest.raises(ConflictError, match="max_revisions"):
            explain_definition(
                db, bootstrap.user, concept, key=f"bad-{n}", grading_extra={"max_revisions": bad}
            )
    for ok in (0, 2, 3):
        explain_definition(db, bootstrap.user, concept, key=f"ok-{ok}", grading_extra={"max_revisions": ok})


# -- the authored curriculum's definitions carry the policy -------------------------------------------------------


def test_only_the_curriculum_bound_explain_back_definitions_carry_the_one_revision_rule(db, bootstrap):
    for slug in sorted({s["concepts"][0] for s in LEVEL1_EXPLAIN_BACK_ASSESSMENTS}):
        make_published_version(db, make_concept(db, slug=slug, name=slug))
    created = seed_level1_explain_back_assessments(db, bootstrap.user.id)
    assert len(created) == len(LEVEL1_EXPLAIN_BACK_ASSESSMENTS) > 0
    assert all(d.grading_policy["max_revisions"] == 1 for d in created)

    by_key = {s["key"]: s.get("grading") or {} for s in FOUNDATION_ASSESSMENTS}
    assert by_key["eb-structured-output"]["max_revisions"] == 1
    assert by_key["capstone-foundations"]["max_revisions"] == 1
    others = [k for k in by_key if k not in ("eb-structured-output", "capstone-foundations")]
    assert others and all("max_revisions" not in by_key[k] for k in others)
