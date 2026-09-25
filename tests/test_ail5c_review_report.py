"""AIL.5C human review, report, Demonstration Record, retention and CHANGED compatibility."""

from datetime import datetime, timedelta, timezone

import pytest

from app.db.enums import (
    AssessmentAttemptStatus,
    AssessmentOrigin,
    AssessmentOutcome,
    AssessmentResultKind,
    AssessmentReviewStatus,
    ChangeSeverity,
    GradingMode,
    OrgRole,
)
from app.db.enums import (
    AssessmentReviewDecision as Decision,
)
from app.errors import ConflictError, ForbiddenError, NotFoundError
from app.models.assessment import AssessmentAttempt, AssessmentReview
from app.models.identity import Organization, User
from app.models.learner import LearningEvidence
from app.models.observability import AuditEvent
from app.services.assessment_report_service import AssessmentReportService, assert_truthful_language
from app.services.assessment_review_service import AssessmentReviewService, ReviewInvalid
from app.services.assessment_service import AssessmentService
from app.services.concept_graph_service import ConceptGraphService
from app.services.learner_state_service import CHANGED, DEMONSTRATED, LearnerStateService
from tests.ail5c_factories import (
    REQ_KC,
    ScriptedGrader,
    default_findings,
    explain_definition,
    judgment,
    kc_definition,
    make_ail_concept,
    second_user,
    setup_free_models,
)

REQ_KC_EB = {
    "requires_all": [
        {"evidence_type": "knowledge_check", "min_passed": 1},
        {"evidence_type": "explain_back", "min_passed": 1},
    ]
}
EXPLANATION = "Structured output makes a model follow a schema so answers parse reliably; a schema does not make content true."
REASON = "I believe my explanation covered the limits clearly."


def _kc(db, bootstrap, *, correct=True, core=False):
    """A learner (member) takes a knowledge check; the org owner is the reviewer."""
    learner = second_user(db, bootstrap.organization)
    concept, _version = make_ail_concept(db, core=core)
    defn = kc_definition(db, bootstrap.user, concept)
    svc = AssessmentService(db)
    attempt = svc.start(learner, defn.definition_key)
    answers = {
        i["entry_key"]: {"selected": [1 if correct else 0]} for i in attempt.challenge_instance["items"]
    }
    svc.save_draft(learner.id, attempt.id, {"responses": answers})
    return learner, concept, defn, svc, svc.submit(learner, attempt.id, {"declaration": "no_external_help"})


def _judged_needs_work(db, bootstrap):
    """Explain-back the Grader confidently fails on one judged criterion (human-overridable)."""
    setup_free_models(db, count=2)
    learner = second_user(db, bootstrap.organization)
    concept, _version = make_ail_concept(db, requirements=REQ_KC_EB)
    defn = explain_definition(db, bootstrap.user, concept)

    def script(n, keys, quote):
        return [
            judgment(
                k,
                quote,
                finding="not_met" if k == "limits" else "met",
                gap="No limit is stated." if k == "limits" else None,
            )
            for k in keys
        ]

    adapter = ScriptedGrader(script)
    svc = AssessmentService(db, adapter_factory=lambda _d, _p: adapter)
    attempt = svc.start(learner, defn.definition_key)
    svc.save_draft(learner.id, attempt.id, {"fields": {"explanation": EXPLANATION}})
    return learner, concept, defn, svc, svc.submit(learner, attempt.id, {"declaration": "no_external_help"})


def _evidence(db, user):
    return db.query(LearningEvidence).filter_by(user_id=user.id).order_by(LearningEvidence.created_at).all()


def _audit_types(db):
    return [e.event_type for e in db.query(AuditEvent).order_by(AuditEvent.occurred_at).all()]


# -- requesting a review ---------------------------------------------------------------------------------------


def test_a_review_needs_a_reason_and_explicit_consent(db, bootstrap):
    learner, _c, _d, _s, attempt = _kc(db, bootstrap, correct=False)
    reviews = AssessmentReviewService(db)
    with pytest.raises(ReviewInvalid):
        reviews.request(learner, attempt.id, reason="", consent=True)
    with pytest.raises(ReviewInvalid):
        reviews.request(learner, attempt.id, reason=REASON, consent=False)
    review = reviews.request(learner, attempt.id, reason=REASON, consent=True)
    assert review.status == AssessmentReviewStatus.OPEN and review.consent_shared_at is not None
    with pytest.raises(ConflictError):  # one open review per attempt
        reviews.request(learner, attempt.id, reason=REASON, consent=True)
    assert "assessment.review_requested" in _audit_types(db)


def test_only_the_owner_can_request_a_review(db, bootstrap):
    _learner, _c, _d, _s, attempt = _kc(db, bootstrap, correct=False)
    other = second_user(db, bootstrap.organization, "other@example.com")
    with pytest.raises(NotFoundError):
        AssessmentReviewService(db).request(other, attempt.id, reason=REASON, consent=True)


def test_an_unfinished_attempt_cannot_be_reviewed(db, bootstrap):
    learner = second_user(db, bootstrap.organization)
    concept, _version = make_ail_concept(db)
    defn = kc_definition(db, bootstrap.user, concept)
    attempt = AssessmentService(db).start(learner, defn.definition_key)
    with pytest.raises(ConflictError):
        AssessmentReviewService(db).request(learner, attempt.id, reason=REASON, consent=True)


# -- reviewer authorization and consent-bound visibility ------------------------------------------------------------------


def test_reviewer_authorization_is_owner_or_admin_in_the_same_org_and_never_the_learner(db, bootstrap):
    learner, _c, _d, _s, attempt = _kc(db, bootstrap, correct=False)
    reviews = AssessmentReviewService(db)
    review = reviews.request(learner, attempt.id, reason=REASON, consent=True)

    member = second_user(db, bootstrap.organization, "member2@example.com")
    with pytest.raises(ForbiddenError):
        reviews.detail(member, review.id)
    with pytest.raises(ForbiddenError):
        reviews.detail(learner, review.id)  # never your own
    elsewhere = Organization(name="Elsewhere")
    db.add(elsewhere)
    db.flush()
    outsider = User(org_id=elsewhere.id, email="admin@else.io", role=OrgRole.OWNER)
    db.add(outsider)
    db.commit()
    with pytest.raises(ForbiddenError):
        reviews.detail(outsider, review.id)
    with pytest.raises(ForbiddenError):
        reviews.queue(member)
    with pytest.raises(ForbiddenError):
        reviews.decide(member, review.id, Decision.CONFIRM, "A long enough rationale.")


def test_a_reviewer_sees_no_content_without_consent_and_every_read_is_audited(db, bootstrap):
    setup_free_models(db, count=2)
    learner = second_user(db, bootstrap.organization)
    concept, _version = make_ail_concept(db, requirements=REQ_KC_EB)
    defn = explain_definition(db, bootstrap.user, concept)
    adapter = ScriptedGrader(
        lambda n, keys, quote: default_findings(keys, quote, finding="met" if n == 1 else "not_met")
    )
    svc = AssessmentService(db, adapter_factory=lambda _d, _p: adapter)
    attempt = svc.start(learner, defn.definition_key)
    svc.save_draft(learner.id, attempt.id, {"fields": {"explanation": EXPLANATION + " PRIVATE-WORDS"}})
    svc.submit(learner, attempt.id, {"declaration": "no_external_help"})
    review = (
        db.query(AssessmentReview).filter_by(attempt_id=attempt.id).one()
    )  # opened by the platform (disagreement)
    assert review.consent_shared_at is None

    reviews = AssessmentReviewService(db)
    queue = reviews.queue(bootstrap.user)
    assert queue[0]["id"] == review.id and "reason_text" not in queue[0] and queue[0]["consented"] is False
    hidden = reviews.detail(bootstrap.user, review.id)
    assert "submission" not in hidden and "PRIVATE-WORDS" not in str(hidden)
    with pytest.raises(ConflictError):
        reviews.decide(bootstrap.user, review.id, Decision.OVERRIDE_PASS, "A long enough rationale.")

    reviews.grant_consent(learner, review.id)
    shown = reviews.detail(bootstrap.user, review.id)
    assert "PRIVATE-WORDS" in str(shown["submission"]) and shown["manifest_hash"] and shown["fingerprint"]
    assert _audit_types(db).count("assessment.review_viewed") == 2
    assert "assessment.review_consent_granted" in _audit_types(db)


def test_withdrawal_is_allowed_only_before_a_reviewer_is_assigned(db, bootstrap):
    learner, _c, _d, _s, attempt = _kc(db, bootstrap, correct=False)
    reviews = AssessmentReviewService(db)
    first = reviews.request(learner, attempt.id, reason=REASON, consent=True)
    assert reviews.withdraw(learner, first.id).status == AssessmentReviewStatus.WITHDRAWN
    again = reviews.request(learner, attempt.id, reason=REASON, consent=True)
    reviews.detail(bootstrap.user, again.id)  # a reviewer is now assigned
    with pytest.raises(ConflictError):
        reviews.withdraw(learner, again.id)


# -- decisions never rewrite history ------------------------------------------------------------------------------------------


def test_override_pass_on_a_judged_failure_appends_human_evidence_and_keeps_history(db, bootstrap):
    learner, _concept, _d, svc, attempt = _judged_needs_work(db, bootstrap)
    original = svc.effective_result(attempt.id)
    assert original.outcome == AssessmentOutcome.NEEDS_WORK and _evidence(db, learner) == []
    reviews = AssessmentReviewService(db)
    review = reviews.request(learner, attempt.id, reason=REASON, consent=True)
    decided = reviews.decide(
        bootstrap.user, review.id, Decision.OVERRIDE_PASS, "The explanation does address the limit."
    )
    assert decided.status == AssessmentReviewStatus.RESOLVED and decided.reviewer_user_id == bootstrap.user.id

    human = svc.effective_result(attempt.id)
    assert human.result_kind == AssessmentResultKind.HUMAN and human.supersedes_result_id == original.id
    assert human.outcome == AssessmentOutcome.PASSED and human.review_id == review.id
    db.refresh(original)
    assert original.outcome == AssessmentOutcome.NEEDS_WORK  # the original result is untouched
    rows = _evidence(db, learner)
    assert len(rows) == 1 and rows[0].grader == GradingMode.HUMAN and rows[0].ref_id == human.id
    override = [c for c in human.criteria if c.get("human_override")]
    assert override and all("original_finding" in c for c in override)
    assert {"assessment.review_requested", "assessment.review_decided"} <= set(_audit_types(db))


def test_a_reviewer_cannot_override_a_platform_fact(db, bootstrap):
    learner, _c, _d, _s, attempt = _kc(db, bootstrap, correct=False)  # deterministic failure
    reviews = AssessmentReviewService(db)
    review = reviews.request(learner, attempt.id, reason=REASON, consent=True)
    with pytest.raises(ConflictError, match="platform"):
        reviews.decide(bootstrap.user, review.id, Decision.OVERRIDE_PASS, "I would like them to pass.")
    still = db.get(AssessmentReview, review.id)
    assert still.status == AssessmentReviewStatus.OPEN and _evidence(db, learner) == []


def test_override_needs_work_reverses_a_pass_with_the_supersede_pointer(db, bootstrap):
    learner, concept, _d, svc, attempt = _kc(db, bootstrap, correct=True)
    original = svc.effective_result(attempt.id)
    (old,) = _evidence(db, learner)
    assert LearnerStateService(db).state(learner.id, concept.id).ladder == DEMONSTRATED
    reviews = AssessmentReviewService(db)
    review = reviews.request(learner, attempt.id, reason=REASON, consent=True)
    reviews.decide(
        bootstrap.user, review.id, Decision.OVERRIDE_NEEDS_WORK, "The attestation was inconsistent."
    )

    rows = _evidence(db, learner)
    assert len(rows) == 2, "history is appended to, never erased"
    db.refresh(old)
    replacement = next(r for r in rows if r.id != old.id)
    assert (
        old.superseded_by_id == replacement.id and old.passed is True
    )  # the old row is intact, only pointed at
    assert replacement.grader == GradingMode.HUMAN and replacement.passed is False
    state = LearnerStateService(db).state(learner.id, concept.id)
    assert state.ladder != DEMONSTRATED and old in state.evidence  # superseded but still visible
    db.refresh(original)
    assert original.outcome == AssessmentOutcome.PASSED
    assert svc.effective_result(attempt.id).outcome == AssessmentOutcome.NEEDS_WORK


def test_confirm_appends_a_human_row_and_changes_nothing_else(db, bootstrap):
    learner, _c, _d, svc, attempt = _kc(db, bootstrap, correct=False)
    reviews = AssessmentReviewService(db)
    review = reviews.request(learner, attempt.id, reason=REASON, consent=True)
    reviews.decide(bootstrap.user, review.id, Decision.CONFIRM, "The result stands after review.")
    human = svc.effective_result(attempt.id)
    assert human.result_kind == AssessmentResultKind.HUMAN and human.outcome == AssessmentOutcome.NEEDS_WORK
    assert _evidence(db, learner) == []
    with pytest.raises(ConflictError):  # the decision is written once
        reviews.decide(bootstrap.user, review.id, Decision.CONFIRM, "Deciding a second time.")


def test_a_new_assessment_decision_issues_a_human_requested_attempt(db, bootstrap):
    learner, _c, _d, svc, attempt = _kc(db, bootstrap, correct=False)
    reviews = AssessmentReviewService(db)
    review = reviews.request(learner, attempt.id, reason=REASON, consent=True)
    reviews.decide(bootstrap.user, review.id, Decision.NEW_ASSESSMENT, "A fresh attempt is fairer here.")
    fresh = (
        db.query(AssessmentAttempt)
        .filter_by(user_id=learner.id, origin=AssessmentOrigin.HUMAN_REQUESTED)
        .one()
    )
    assert (
        fresh.previous_attempt_id == attempt.id and fresh.status == AssessmentAttemptStatus.DRAFT
    )  # cooldown bypassed
    assert svc.effective_result(attempt.id).outcome == AssessmentOutcome.NEEDS_WORK  # the original stands


def test_a_stale_fingerprint_blocks_the_decision(db, bootstrap):
    learner, _c, _d, _s, attempt = _kc(db, bootstrap, correct=False)
    reviews = AssessmentReviewService(db)
    review = reviews.request(learner, attempt.id, reason=REASON, consent=True)
    review.fingerprint = "0" * 64
    db.commit()
    with pytest.raises(ConflictError, match="changed"):
        reviews.decide(bootstrap.user, review.id, Decision.CONFIRM, "A long enough rationale.")


def test_invalid_decisions_for_the_outcome_are_refused(db, bootstrap):
    learner, _c, _d, _s, attempt = _kc(db, bootstrap, correct=True)
    reviews = AssessmentReviewService(db)
    review = reviews.request(learner, attempt.id, reason=REASON, consent=True)
    with pytest.raises(ConflictError):
        reviews.decide(bootstrap.user, review.id, Decision.OVERRIDE_PASS, "It already passed.")
    with pytest.raises(ReviewInvalid):
        reviews.decide(bootstrap.user, review.id, Decision.CONFIRM, "short")


# -- the report ----------------------------------------------------------------------------------------------------------------


def test_the_report_separates_fact_judgment_reflection_and_coaching(db, bootstrap):
    setup_free_models(db, count=2)
    learner = second_user(db, bootstrap.organization)
    concept, _version = make_ail_concept(db, requirements=REQ_KC_EB)
    defn = explain_definition(db, bootstrap.user, concept)
    adapter = ScriptedGrader()
    svc = AssessmentService(db, adapter_factory=lambda _d, _p: adapter)
    attempt = svc.start(learner, defn.definition_key)
    svc.save_draft(
        learner.id,
        attempt.id,
        {"fields": {"explanation": EXPLANATION, "reflection": "I found limits hardest."}},
    )
    done = svc.submit(learner, attempt.id, {"declaration": "used_docs", "note": "read the lesson"})
    report = svc.effective_result(done.id).report

    assert {"platform_fact", "grader_judgment", "learner_reflection", "professor_coaching"} <= set(report)
    labels = [
        report[k]["label"]
        for k in ("platform_fact", "grader_judgment", "learner_reflection", "professor_coaching")
    ]
    assert labels == ["PLATFORM FACT", "GRADER JUDGMENT", "LEARNER REFLECTION", "PROFESSOR COACHING"]
    assert report["learner_reflection"]["reflection"] == "I found limits hardest."
    assert report["learner_reflection"]["attestation"]["declaration"] == "used_docs"
    assert (
        report["professor_coaching"]["available_after_result"]
        and "cannot change it" in report["professor_coaching"]["note"]
    )
    judged = report["grader_judgment"]
    assert (
        judged["ran"]
        and judged["crosscheck_ran"]
        and judged["grading_contract_version"] == "grading_contract_v1"
    )
    assert all(c["confidence"] in ("high", "medium", "low") for c in judged["criteria"])
    assert all(run["cost_status"] in ("exact", "estimated", "unknown") for run in judged["runs"])
    answers = report["answers"]
    assert {
        "what_i_demonstrated",
        "what_evidence_proved_it",
        "what_i_did_independently",
        "where_i_needed_help",
        "what_needs_more_work",
        "what_to_practice_next",
        "learner_state",
        "review_later",
    } <= set(answers)
    state = answers["learner_state"][concept.id]
    assert state["before"] and state["after"] and state["concept_name"] == "Structured Output"


def test_the_grader_never_writes_advice_and_coaching_never_enters_the_result(db, bootstrap):
    _learner, _c, _d, svc, attempt = _judged_needs_work(db, bootstrap)
    final = svc.effective_result(attempt.id)
    blob = str(final.report["grader_judgment"]) + str(final.gaps)
    assert "you should" not in blob.lower() and "next step" not in blob.lower()
    assert set(final.report["professor_coaching"]) == {"label", "available_after_result", "note"}


def test_the_headline_language_is_honest_and_validated():
    for term in ("certified", "mastered", "an expert", "accredited", "professional-level"):
        with pytest.raises(ValueError):
            assert_truthful_language(f"You are {term}.")
    from app.services.assessment_report_service import _HEADLINES, NOTICE

    for sentence in list(_HEADLINES.values()) + [NOTICE]:
        assert_truthful_language(sentence)


# -- Demonstration Record ----------------------------------------------------------------------------------------------------------


def test_a_demonstrated_pass_produces_a_hashed_evidence_linked_record(db, bootstrap):
    learner, concept, defn, svc, attempt = _kc(db, bootstrap, correct=True)
    final = svc.effective_result(attempt.id)
    record = final.record_snapshot
    assert (
        record["title"] == "Demonstration Record" and record["notice"] == "Not a certificate or credential."
    )
    assert record["assessment"]["definition_version"] == 1
    assert record["assessment"]["definition_content_hash"] == defn.content_hash
    assert record["concepts"][0]["concept_id"] == concept.id and record["concepts"][0]["concept_version"] == 1
    assert record["evidence"][0]["learning_evidence_id"] == _evidence(db, learner)[0].id
    assert record["provenance"]["verified_by_platform"] and record["provenance"]["self_reported"]
    from app.assessment_contract import sha256_hex

    assert final.record_hash == sha256_hex(record)
    markdown = AssessmentReportService.render_markdown(record)
    assert markdown.startswith("# Demonstration Record") and "Not a certificate or credential." in markdown
    assert "Verified by the platform" in markdown and "Self-reported" in markdown


def test_only_a_demonstrated_effect_produces_a_record(db, bootstrap):
    _learner, _c, _d, svc, attempt = _kc(db, bootstrap, correct=False)
    assert svc.effective_result(attempt.id).record_snapshot is None
    learner2 = second_user(db, bootstrap.organization, "l3@example.com")
    concept, _version = make_ail_concept(db)
    defn = kc_definition(db, bootstrap.user, concept, key="kc-formative")
    second = svc.start(learner2, defn.definition_key)
    answers = {i["entry_key"]: {"selected": [1]} for i in second.challenge_instance["items"]}
    svc.save_draft(learner2.id, second.id, {"responses": answers})
    done = svc.submit(learner2, second.id, {"declaration": "used_ai_assistant"})
    assert svc.effective_result(done.id).record_snapshot is None  # formative: feedback, not a record


def test_record_status_is_derived_and_history_is_never_edited(db, bootstrap):
    learner, concept, _d, svc, attempt = _kc(db, bootstrap, correct=True)
    final = svc.effective_result(attempt.id)
    reporter = AssessmentReportService(db)
    now = datetime.now(timezone.utc)
    assert reporter.record_status(learner.id, final, superseded=False, now=now)["status"] == "valid"

    graph = ConceptGraphService(db)  # AIL.4A: a material change -> DEMONSTRATED + CHANGED coexist
    draft = graph.create_draft_version(
        concept_id=concept.id,
        plain_definition="Changed.",
        change_severity=ChangeSeverity.MATERIAL,
        evidence_requirements=REQ_KC,
    )
    graph.publish_version(draft.id)
    state = LearnerStateService(db).state(learner.id, concept.id)
    assert state.ladder == DEMONSTRATED and CHANGED in state.overlays
    status = reporter.record_status(learner.id, final, superseded=False, now=now)
    assert status["status"] == "changed_since" and "Concept Version 1" in status["reasons"][0]
    db.refresh(final)
    assert final.record_snapshot["concepts"][0]["concept_version"] == 1  # the historical record is unchanged
    assert (
        reporter.record_status(learner.id, final, superseded=True, now=now)["status"]
        == "superseded_by_review"
    )


def test_assessment_evidence_reuses_the_4b_retention_clock(db, bootstrap):
    learner, concept, _d, svc, attempt = _kc(db, bootstrap, correct=True, core=True)
    (row,) = _evidence(db, learner)
    review = LearnerStateService(db).state(learner.id, concept.id).review
    assert review.baseline.evidence_id == row.id  # the assessment evidence qualifies and sets the baseline
    assert review.eligible and review.due_at is not None
    far = datetime.now(timezone.utc) + timedelta(days=500)
    later = LearnerStateService(db).state(learner.id, concept.id, now=far)
    assert "review_due" in later.overlays and later.ladder == DEMONSTRATED  # DEMONSTRATED is not permanent
    final = svc.effective_result(attempt.id)
    assert (
        AssessmentReportService(db).record_status(learner.id, final, superseded=False, now=far)["status"]
        == "review_due"
    )
    from app.models.learning_review import ReviewAttempt

    assert db.query(ReviewAttempt).count() == 0  # no second scheduler
