"""AIL.5C observability and budget behaviour.

Two append-only streams carry the assessment's events, by design:

* the **audit log** (``audit_events``) carries the lifecycle of the attempt, which has no
  task run to hang a Flight Recorder row on: started, submitted, deterministic_completed,
  finalized, evidence_written (ids / hashes / outcomes only, never learner content);
* the **Flight Recorder** (``execution_events``) carries every Grader run: grading_requested,
  grading_completed / grading_failed / grader_response_rejected, plus the executor's own
  budget events.
"""

from decimal import Decimal

from app.db.enums import (
    AssessmentAttemptStatus,
    AssessmentOutcome,
    AssessmentResultKind,
    BudgetReservationStatus,
    BudgetScope,
)
from app.models.assessment import AssessmentResult
from app.models.governance import Budget, BudgetReservation
from app.models.learner import LearningEvidence
from app.models.observability import AuditEvent, ExecutionEvent
from app.services.assessment_grader_service import AssessmentGraderService
from app.services.assessment_service import AssessmentService
from app.services.system_project_service import ensure_ail_system_project
from tests.ail5c_factories import (
    ScriptedGrader,
    explain_definition,
    make_ail_concept,
    second_user,
    setup_free_models,
)

WORDS = (
    "Structured output makes a model follow a schema so the answers can be parsed reliably. "
    "A schema does not guarantee the content itself is true. SECRET-LEARNER-WORDS"
)


def _setup(db, bootstrap):
    setup_free_models(db, count=2)
    learner = second_user(db, bootstrap.organization)
    concept, _v = make_ail_concept(db)
    defn = explain_definition(db, bootstrap.user, concept)
    return learner, concept, defn


def _audit(db, attempt):
    rows = (
        db.query(AuditEvent)
        .filter(AuditEvent.target_ref == f"assessment_attempt:{attempt.id}")
        .order_by(AuditEvent.occurred_at)
        .all()
    )
    return rows


def test_the_attempt_lifecycle_emits_every_stage_event_in_order_without_learner_content(db, bootstrap):
    learner, _c, defn = _setup(db, bootstrap)
    svc = AssessmentService(db, adapter_factory=lambda _d, _p: ScriptedGrader())
    attempt = svc.start(learner, defn.definition_key)
    svc.save_draft(learner.id, attempt.id, {"fields": {"explanation": WORDS}})
    svc.submit(learner, attempt.id, {"declaration": "no_external_help"})
    db.refresh(attempt)
    assert svc.effective_result(attempt.id).outcome == AssessmentOutcome.PASSED

    events = _audit(db, attempt)
    assert [e.event_type for e in events] == [
        "assessment.started",
        "assessment.submitted",
        "assessment.deterministic_completed",
        "assessment.finalized",
        "assessment.evidence_written",
    ]
    for event in events:
        assert "SECRET-LEARNER-WORDS" not in repr(event.detail)
    deterministic = events[2].detail
    det_row = (
        db.query(AssessmentResult)
        .filter_by(attempt_id=attempt.id, result_kind=AssessmentResultKind.DETERMINISTIC)
        .one()
    )
    assert deterministic == {"result_id": det_row.id, "criteria": 1, "required_unmet": 0}

    # the two Grader runs (primary + cross-check) each leave a request and a completion in the Flight Recorder
    flight = [
        e.event_type
        for e in db.query(ExecutionEvent).filter(ExecutionEvent.event_type.like("assessment.grading_%"))
    ]
    assert sorted(flight) == ["assessment.grading_completed"] * 2 + ["assessment.grading_requested"] * 2
    for row in db.query(ExecutionEvent).filter(ExecutionEvent.event_type.like("assessment.%")):
        assert "SECRET-LEARNER-WORDS" not in repr((row.decision_summary, row.error))


def test_a_failed_required_platform_check_still_records_the_deterministic_stage_and_never_grades(
    db, bootstrap
):
    learner, _c, defn = _setup(db, bootstrap)
    adapter = ScriptedGrader()
    svc = AssessmentService(db, adapter_factory=lambda _d, _p: adapter)
    attempt = svc.start(learner, defn.definition_key)
    svc.save_draft(learner.id, attempt.id, {"fields": {"explanation": "too short"}})
    svc.submit(learner, attempt.id, {"declaration": "no_external_help"})
    db.refresh(attempt)
    assert svc.effective_result(attempt.id).outcome == AssessmentOutcome.NEEDS_WORK
    types = [e.event_type for e in _audit(db, attempt)]
    assert types == [
        "assessment.started",
        "assessment.submitted",
        "assessment.deterministic_completed",
        "assessment.finalized",
    ]
    assert _audit(db, attempt)[2].detail["required_unmet"] == 1
    assert adapter.requests == []
    assert (
        db.query(ExecutionEvent).filter(ExecutionEvent.event_type.like("assessment.grading_%")).count() == 0
    )


def _exhausted_budget(db, learner) -> Budget:
    project = ensure_ail_system_project(db, learner)
    budget = Budget(project_id=project.id, scope=BudgetScope.TASK, limit_amount=Decimal("1.00"))
    db.add(budget)
    db.flush()
    db.add(
        BudgetReservation(
            budget_id=budget.id, reserved_amount=Decimal("1.00"), status=BudgetReservationStatus.ACTIVE
        )
    )
    db.commit()
    return budget


def test_an_exhausted_grading_budget_pauses_grading_without_losing_or_inventing_anything(db, bootstrap):
    learner, _c, defn = _setup(db, bootstrap)
    adapter = ScriptedGrader()
    svc = AssessmentService(db, adapter_factory=lambda _d, _p: adapter)
    attempt = svc.start(learner, defn.definition_key)
    svc.save_draft(learner.id, attempt.id, {"fields": {"explanation": WORDS}})
    budget = _exhausted_budget(db, learner)

    svc.submit(learner, attempt.id, {"declaration": "no_external_help"}, budget_id=budget.id)
    db.refresh(attempt)

    # paused, not failed: the submission and the deterministic stage are durable and the provider was never called
    assert attempt.status == AssessmentAttemptStatus.AWAITING_GRADING
    assert attempt.submission is not None and attempt.submission_hash
    assert adapter.requests == []
    kinds = [r.result_kind for r in db.query(AssessmentResult).filter_by(attempt_id=attempt.id)]
    assert kinds == [AssessmentResultKind.DETERMINISTIC]
    assert db.query(LearningEvidence).filter_by(user_id=learner.id).count() == 0
    assert svc.effective_result(attempt.id) is None

    # the platform says why: the executor's own rejection event, and a distinct pause reason
    rejected = db.query(ExecutionEvent).filter_by(event_type="budget.reservation_rejected").all()
    assert rejected, "the budget governor's rejection is recorded in the Flight Recorder"
    assert AssessmentGraderService(db).pause_reason(learner, attempt) == "budget_exhausted"
    assert "assessment.finalized" not in [e.event_type for e in _audit(db, attempt)]

    # a retry with the budget still exhausted is idempotent: still paused, still nothing invented
    svc.grade(learner, attempt.id, budget_id=budget.id)
    db.refresh(attempt)
    assert attempt.status == AssessmentAttemptStatus.AWAITING_GRADING
    assert adapter.requests == []
    assert db.query(LearningEvidence).filter_by(user_id=learner.id).count() == 0

    # when budget is available the SAME attempt resumes and finishes: one result, one evidence row
    svc.grade(learner, attempt.id)
    db.refresh(attempt)
    assert attempt.status == AssessmentAttemptStatus.FINALIZED
    assert svc.effective_result(attempt.id).outcome == AssessmentOutcome.PASSED
    assert db.query(LearningEvidence).filter_by(user_id=learner.id).count() == 1
    assert AssessmentGraderService(db).pause_reason(learner, attempt) is None


def test_a_provider_outage_is_a_retry_safe_pause_that_is_not_reported_as_a_budget_problem(db, bootstrap):
    from app.providers.base import ProviderConnectionError

    learner, _c, defn = _setup(db, bootstrap)
    down = ScriptedGrader(lambda n, keys, quote: ProviderConnectionError("provider unreachable"))
    svc = AssessmentService(db, adapter_factory=lambda _d, _p: down)
    attempt = svc.start(learner, defn.definition_key)
    svc.save_draft(learner.id, attempt.id, {"fields": {"explanation": WORDS}})
    svc.submit(learner, attempt.id, {"declaration": "no_external_help"})
    db.refresh(attempt)
    assert attempt.status == AssessmentAttemptStatus.AWAITING_GRADING
    assert AssessmentGraderService(db).pause_reason(learner, attempt) is None
