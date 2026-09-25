"""AIL.5C Assessment + Demonstration API.

Every learner endpoint is scoped to the authenticated learner (``user_id`` is
filtered in the service layer, not by project RBAC — the shared AIL project
gives every learner OWNER). Reviewer endpoints require an org OWNER/ADMIN who
is not the learner, and show content only with the learner's consent.

There is no endpoint that lets a client set an outcome, a Learner State, or a
Learning Evidence row. Grading is server-side only.
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.auth import get_current_user
from app.db.enums import (
    AssessmentAttemptStatus,
    AssessmentKind,
    AssessmentOutcome,
    AssessmentResultKind,
    AssessmentReviewDecision,
    OrgRole,
)
from app.errors import ForbiddenError, NotFoundError
from app.models.assessment import AssessmentAttempt, AssessmentDefinition, AssessmentResult, AssessmentReview
from app.models.governance import Budget
from app.models.identity import User
from app.services.assessment_definition_service import AssessmentDefinitionService
from app.services.assessment_report_service import AssessmentReportService
from app.services.assessment_review_service import AssessmentReviewService
from app.services.assessment_service import AssessmentService
from app.services.system_project_service import ensure_ail_system_project

router = APIRouter(prefix="/academy/assessments", tags=["assessments"])


class StartAttemptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    definition_key: str = Field(min_length=1, max_length=160)
    project_attempt_id: Optional[str] = Field(default=None, max_length=36)
    previous_attempt_id: Optional[str] = Field(default=None, max_length=36)


class DraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft: Dict[str, Any]


class SubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attestation: Dict[str, Any]
    budget_id: Optional[str] = Field(default=None, max_length=36)


class GradeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    budget_id: Optional[str] = Field(default=None, max_length=36)


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="", max_length=2000)
    consent: bool = False


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: AssessmentReviewDecision
    rationale: str = Field(min_length=1, max_length=2000)


class DefinitionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    definition_key: str = Field(min_length=1, max_length=160)
    kind: AssessmentKind
    title: str = Field(min_length=1, max_length=255)
    instructions_md: str = Field(min_length=1)
    criteria: List[Dict[str, Any]]
    concept_links: List[Dict[str, Any]]
    challenge_spec: Optional[Dict[str, Any]] = None
    independence_policy: Optional[Dict[str, Any]] = None
    grading_policy: Optional[Dict[str, Any]] = None
    allowed_resources: Optional[List[str]] = None
    project_template_id: Optional[str] = None


def _authorized_budget(db: Session, user: User, budget_id: Optional[str]) -> Optional[str]:
    """Grader runs may only bill a budget that belongs to the AIL system project."""
    if budget_id is None:
        return None
    project = ensure_ail_system_project(db, user)
    budget = db.get(Budget, budget_id)
    if budget is None or budget.project_id != project.id:
        raise ForbiddenError("That budget is not authorized for assessment grading.")
    return budget.id


def _require_author(user: User) -> None:
    if user.role not in (OrgRole.OWNER, OrgRole.ADMIN):
        raise ForbiddenError("Only an organisation owner or admin can author assessment definitions.")


# -- center ---------------------------------------------------------------------------------


@router.get("/center")
def assessment_center(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """One place for all assessment work: ready now, in progress, needs work,
    review / provisional, demonstrated, and history."""
    service = AssessmentService(db)
    from app.services.assessment_mode_guard import AssessmentModeGuard

    AssessmentModeGuard(db).expire_stale(user.id)
    attempts = list(
        db.execute(
            select(AssessmentAttempt)
            .where(AssessmentAttempt.user_id == user.id)
            .order_by(AssessmentAttempt.created_at.desc())
        ).scalars()
    )
    definitions = {d.id: d for d in db.execute(select(AssessmentDefinition)).scalars()}

    def row(attempt: AssessmentAttempt) -> Dict[str, Any]:
        definition = definitions[attempt.definition_id]
        result = service.effective_result(attempt.id)
        return {
            "attempt_id": attempt.id,
            "definition_key": definition.definition_key,
            "definition_version": definition.version,
            "title": definition.title,
            "kind": definition.assessment_kind.value,
            "status": attempt.status.value,
            "outcome": result.outcome.value if result and result.outcome else None,
            "demonstration_effect": (
                result.demonstration_effect.value if result and result.demonstration_effect else None
            ),
            "started_at": attempt.started_at,
            "finalized_at": attempt.finalized_at,
            "expires_at": attempt.expires_at,
            "remediation": (
                result.remediation if result and result.outcome == AssessmentOutcome.NEEDS_WORK else None
            ),
            "gaps": (result.gaps if result and result.outcome == AssessmentOutcome.NEEDS_WORK else None),
        }

    rows = [row(a) for a in attempts]
    active = {
        AssessmentAttemptStatus.DRAFT.value,
        AssessmentAttemptStatus.SUBMITTED.value,
        AssessmentAttemptStatus.CHECKING.value,
        AssessmentAttemptStatus.AWAITING_GRADING.value,
        AssessmentAttemptStatus.GRADING.value,
    }
    latest_by_key: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        latest_by_key.setdefault(r["definition_key"], r)

    ready = []
    for definition in AssessmentDefinitionService(db).list_current():
        report = service.readiness(user, definition.definition_key)
        latest = latest_by_key.get(definition.definition_key)
        already_passed = latest is not None and latest["outcome"] == AssessmentOutcome.PASSED.value
        ready.append(
            {
                "definition_key": definition.definition_key,
                "title": definition.title,
                "kind": definition.assessment_kind.value,
                "ready": report["ready"],
                "why_offered": _why(report),
                "checks": report["checks"],
                "fresh_required": report.get("fresh_required"),
                "fresh_reason": report.get("fresh_reason"),
                "project_attempt_id": report.get("project_attempt_id"),
                "already_passed": already_passed,
                "available_after_ma9": definition.requires_platform_capability is not None,
            }
        )

    records = _records(db, service, user)
    return {
        "ready_for_assessment": [r for r in ready if r["ready"] and not r["already_passed"]],
        "not_yet_ready": [r for r in ready if not r["ready"]],
        "in_progress": [r for r in rows if r["status"] in active],
        "needs_work": [
            r for k, r in latest_by_key.items() if r["outcome"] == AssessmentOutcome.NEEDS_WORK.value
        ],
        "provisional_or_review": [
            r
            for r in latest_by_key.values()
            if r["outcome"]
            in (
                AssessmentOutcome.PROVISIONAL.value,
                AssessmentOutcome.HUMAN_REVIEW_REQUIRED.value,
                AssessmentOutcome.UNABLE_TO_ASSESS.value,
            )
        ],
        "demonstrated": records,
        "history": rows,
    }


def _why(report: Dict[str, Any]) -> str:
    if report["ready"]:
        return (
            "Your project work is submitted and everything needed is in place."
            if report.get("project_attempt_id")
            else "You can take this now."
        )
    unmet = [c["label"] for c in report["checks"] if not c["met"]]
    return "Not ready yet: " + "; ".join(unmet)


# -- definitions ---------------------------------------------------------------------------------


@router.get("/definitions/{definition_key}")
def definition_detail(
    definition_key: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    service = AssessmentDefinitionService(db)
    definition = service.current(definition_key)
    if definition is None:
        raise NotFoundError("Assessment not found.")
    return AssessmentDefinitionService.learner_view(definition, service.links(definition.id))


@router.get("/definitions/{definition_key}/readiness")
def definition_readiness(
    definition_key: str,
    project_attempt_id: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return AssessmentService(db).readiness(user, definition_key, project_attempt_id=project_attempt_id)


@router.post("/definitions", status_code=201)
def create_definition(
    body: DefinitionCreateRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    _require_author(user)
    definition = AssessmentDefinitionService(db).create_draft(
        author_user_id=user.id,
        definition_key=body.definition_key,
        kind=body.kind,
        title=body.title,
        instructions_md=body.instructions_md,
        criteria=body.criteria,
        concept_links=body.concept_links,
        challenge_spec=body.challenge_spec,
        independence_policy=body.independence_policy,
        grading_policy=body.grading_policy,
        allowed_resources=body.allowed_resources,
        project_template_id=body.project_template_id,
    )
    return {
        "id": definition.id,
        "definition_key": definition.definition_key,
        "version": definition.version,
        "status": definition.status.value,
    }


@router.post("/definitions/{definition_id}/publish")
def publish_definition(
    definition_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    _require_author(user)
    definition = AssessmentDefinitionService(db).publish(definition_id)
    return {
        "id": definition.id,
        "definition_key": definition.definition_key,
        "version": definition.version,
        "status": definition.status.value,
        "content_hash": definition.content_hash,
    }


@router.post("/definitions/seed-foundation")
def seed_foundation(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Owner-triggered, idempotent seed of the authored AI Foundations set."""
    _require_author(user)
    from app.assessment_curriculum import seed_foundation_assessments

    created = seed_foundation_assessments(db, user.id)
    return {"created": len(created), "definition_keys": [d.definition_key for d in created]}


# -- attempts ------------------------------------------------------------------------------------------


@router.post("/attempts", status_code=201)
def start_attempt(
    body: StartAttemptRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    service = AssessmentService(db)
    attempt = service.start(
        user,
        body.definition_key,
        project_attempt_id=body.project_attempt_id,
        previous_attempt_id=body.previous_attempt_id,
        idempotency_key=idempotency_key,
    )
    return service.attempt_view(attempt)


@router.get("/attempts/{attempt_id}")
def get_attempt(attempt_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    service = AssessmentService(db)
    return service.attempt_view(service.get_attempt(user.id, attempt_id))


@router.put("/attempts/{attempt_id}/draft")
def save_draft(
    attempt_id: str, body: DraftRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    service = AssessmentService(db)
    return service.attempt_view(service.save_draft(user.id, attempt_id, body.draft))


@router.post("/attempts/{attempt_id}/abandon")
def abandon_attempt(attempt_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    service = AssessmentService(db)
    return service.attempt_view(service.abandon(user, attempt_id))


@router.post("/attempts/{attempt_id}/submit")
def submit_attempt(
    attempt_id: str,
    body: SubmitRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    service = AssessmentService(db)
    attempt = service.submit(
        user, attempt_id, body.attestation, budget_id=_authorized_budget(db, user, body.budget_id)
    )
    return _result_view(db, service, user, attempt)


@router.post("/attempts/{attempt_id}/grade")
def grade_attempt(
    attempt_id: str, body: GradeRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    service = AssessmentService(db)
    attempt = service.grade(user, attempt_id, budget_id=_authorized_budget(db, user, body.budget_id))
    return _result_view(db, service, user, attempt)


@router.get("/attempts/{attempt_id}/result")
def get_result(attempt_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    service = AssessmentService(db)
    return _result_view(db, service, user, service.get_attempt(user.id, attempt_id))


def _result_view(
    db: Session, service: AssessmentService, user: User, attempt: AssessmentAttempt
) -> Dict[str, Any]:
    results = service.results(user.id, attempt.id)
    effective = service.effective_result(attempt.id)
    deterministic = next((r for r in results if r.result_kind == AssessmentResultKind.DETERMINISTIC), None)
    reviews = list(
        db.execute(
            select(AssessmentReview)
            .where(AssessmentReview.attempt_id == attempt.id, AssessmentReview.user_id == user.id)
            .order_by(AssessmentReview.created_at)
        ).scalars()
    )
    review_service = AssessmentReviewService(db)
    view: Dict[str, Any] = {
        "attempt": service.attempt_view(attempt),
        "status": attempt.status.value,
        "finalized": effective is not None,
        "history": [
            {
                "id": r.id,
                "kind": r.result_kind.value,
                "seq": r.seq,
                "outcome": r.outcome.value if r.outcome else None,
                "supersedes_result_id": r.supersedes_result_id,
                "created_at": r.created_at,
            }
            for r in results
        ],
        "reviews": [review_service.learner_view(r) for r in reviews],
    }
    if effective is not None:
        # The record lives on whichever result produced it (a confirmed review keeps
        # the original's record; a reversed pass keeps it too, marked superseded).
        record_result = next((r for r in reversed(results) if r.record_snapshot is not None), None)
        view["result"] = {
            "id": effective.id,
            "kind": effective.result_kind.value,
            "outcome": effective.outcome.value,
            "demonstration_effect": (
                effective.demonstration_effect.value if effective.demonstration_effect else None
            ),
            "report": effective.report,
            "gaps": effective.gaps,
            "remediation": effective.remediation,
            "has_record": record_result is not None,
            "record_result_id": record_result.id if record_result is not None else None,
        }
    else:
        view["pending"] = {
            "message": (
                "The Grader could not finish. Your platform checks are saved and it is safe to try again."
                if attempt.status == AssessmentAttemptStatus.AWAITING_GRADING
                else "Your attempt is being checked."
            ),
            "deterministic_checks": [
                {"key": c["key"], "label": c["label"], "finding": c["finding"], "detail": c.get("detail", "")}
                for c in (deterministic.criteria if deterministic else [])
            ],
        }
    return view


# -- human review ----------------------------------------------------------------------------------------


@router.post("/attempts/{attempt_id}/review", status_code=201)
def request_review(
    attempt_id: str,
    body: ReviewRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    service = AssessmentReviewService(db)
    return service.learner_view(service.request(user, attempt_id, reason=body.reason, consent=body.consent))


@router.post("/reviews/{review_id}/consent")
def consent_review(review_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    service = AssessmentReviewService(db)
    return service.learner_view(service.grant_consent(user, review_id))


@router.post("/reviews/{review_id}/withdraw")
def withdraw_review(review_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    service = AssessmentReviewService(db)
    return service.learner_view(service.withdraw(user, review_id))


@router.get("/reviews")
def reviewer_queue(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return AssessmentReviewService(db).queue(user)


@router.get("/reviews/{review_id}")
def reviewer_detail(review_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return AssessmentReviewService(db).detail(user, review_id)


@router.post("/reviews/{review_id}/decision")
def reviewer_decision(
    review_id: str,
    body: DecisionRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    service = AssessmentReviewService(db)
    return service.learner_view(service.decide(user, review_id, body.decision, body.rationale))


# -- demonstration records ------------------------------------------------------------------------------------------


def _records(db: Session, service: AssessmentService, user: User) -> List[Dict[str, Any]]:
    reporter = AssessmentReportService(db)
    finals = db.execute(
        select(AssessmentResult, AssessmentAttempt, AssessmentDefinition)
        .join(AssessmentAttempt, AssessmentAttempt.id == AssessmentResult.attempt_id)
        .join(AssessmentDefinition, AssessmentDefinition.id == AssessmentAttempt.definition_id)
        .where(AssessmentResult.user_id == user.id)
        .order_by(AssessmentResult.created_at.desc())
    ).all()
    # JSON columns store Python None as JSON 'null', so filter in Python.
    finals = [row for row in finals if row[0].record_snapshot is not None]
    out = []
    from app.services.assessment_service import _now

    for result, attempt, definition in finals:
        effective = service.effective_result(attempt.id)
        superseded = (
            effective is not None
            and effective.id != result.id
            and (
                effective.outcome != result.outcome
                or effective.demonstration_effect != result.demonstration_effect
            )
        )
        if (
            result.result_kind == AssessmentResultKind.HUMAN
            and not superseded
            and effective is not None
            and effective.id != result.id
        ):
            superseded = True
        status = reporter.record_status(user.id, result, superseded=superseded, now=_now())
        record = result.record_snapshot
        out.append(
            {
                "result_id": result.id,
                "attempt_id": attempt.id,
                "title": definition.title,
                "kind": definition.assessment_kind.value,
                "concepts": [c["concept_name"] for c in record["concepts"]],
                "assessment_date": record["assessment_date"],
                "record_hash": result.record_hash,
                "status": status["status"],
                "status_reasons": status["reasons"],
                "reassess": (
                    {"definition_key": definition.definition_key}
                    if status["status"] in ("changed_since", "review_due")
                    else None
                ),
            }
        )
    return out


@router.get("/records")
def list_records(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _records(db, AssessmentService(db), user)


@router.get("/records/{result_id}")
def get_record(
    result_id: str,
    format: str = Query(default="json", pattern="^(json|md)$"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = db.execute(
        select(AssessmentResult).where(AssessmentResult.id == result_id, AssessmentResult.user_id == user.id)
    ).scalar_one_or_none()
    if result is None or result.record_snapshot is None:
        raise NotFoundError("Demonstration record not found.")
    service = AssessmentService(db)
    from app.services.assessment_service import _now

    effective = service.effective_result(result.attempt_id)
    superseded = (
        effective is not None
        and effective.id != result.id
        and (
            effective.outcome != result.outcome
            or effective.demonstration_effect != result.demonstration_effect
        )
    )
    status = AssessmentReportService(db).record_status(user.id, result, superseded=superseded, now=_now())
    if format == "md":
        return PlainTextResponse(
            AssessmentReportService.render_markdown(result.record_snapshot), media_type="text/markdown"
        )
    return {"record": result.record_snapshot, "record_hash": result.record_hash, "status": status}
