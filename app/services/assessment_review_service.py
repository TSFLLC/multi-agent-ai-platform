"""AIL.5C human review of an assessment result.

Why not the Approval engine: ``ApprovalService.resolve`` authorizes by project
MODIFY, and every learner is OWNER of the shared per-organisation AIL project,
so an approval carrying one learner's assessment would be listable and
resolvable by every other learner. Approvals also have no learner-record scope
and are binary. This is a small, purpose-built record that REUSES the safe
patterns only: compare-and-swap resolution, an action fingerprint binding the
decision to the exact result the reviewer saw, a write-once decision, and the
audit trail. It is not a generic approval engine.

Rules:
* the learner requests a review and gives explicit consent — consent is the
  ONLY thing that lets a reviewer see their submission;
* the reviewer must be an org OWNER or ADMIN of the learner's organisation and
  is never the learner;
* history is never rewritten: a decision APPENDS a ``human`` result that
  supersedes the reviewed one; a reversed pass appends a failing HUMAN
  evidence row and uses the sanctioned ``supersede`` pointer;
* a reviewer can override a JUDGED criterion but never a required
  deterministic platform fact.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.assessment_contract import review_fingerprint
from app.assessment_outcome import _effect
from app.db.enums import (
    AssessmentAttemptStatus,
    AssessmentOrigin,
    AssessmentOutcome,
    AssessmentResultKind,
    AssessmentReviewDecision,
    AssessmentReviewStatus,
    AssessmentReviewTrigger,
    DemonstrationEffect,
    EvidenceRefType,
    GradingMode,
    OrgRole,
)
from app.db.mixins import new_uuid
from app.errors import AppError, ConflictError, ForbiddenError, NotFoundError
from app.models.assessment import AssessmentAttempt, AssessmentDefinition, AssessmentResult, AssessmentReview
from app.models.identity import User
from app.models.learner import LearningEvidence
from app.services.assessment_evidence_writer import AssessmentEvidenceWriter
from app.services.assessment_report_service import AssessmentReportService
from app.services.assessment_service import AssessmentService
from app.services.audit_service import AuditService
from app.services.learner_state_service import LearnerStateService
from app.services.learning_evidence_service import LearningEvidenceService

REASON_MIN, REASON_MAX = 10, 2000
RATIONALE_MIN = 10
_REVIEWER_ROLES = (OrgRole.OWNER, OrgRole.ADMIN)
_ALLOWED = {
    AssessmentOutcome.PASSED: {AssessmentReviewDecision.CONFIRM, AssessmentReviewDecision.OVERRIDE_NEEDS_WORK, AssessmentReviewDecision.NEW_ASSESSMENT},
    AssessmentOutcome.NEEDS_WORK: {AssessmentReviewDecision.CONFIRM, AssessmentReviewDecision.OVERRIDE_PASS, AssessmentReviewDecision.NEW_ASSESSMENT},
}
_UNDECIDED = {AssessmentReviewDecision.OVERRIDE_PASS, AssessmentReviewDecision.OVERRIDE_NEEDS_WORK, AssessmentReviewDecision.NEW_ASSESSMENT}


class ReviewInvalid(AppError):
    status_code = 422
    code = "review_invalid"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def fingerprint_for(result: AssessmentResult, attempt: AssessmentAttempt) -> str:
    """Binds a review to the exact result, manifest and submission the reviewer sees."""
    return review_fingerprint(result.id, attempt.input_manifest_hash, attempt.submission_hash)


class AssessmentReviewService:
    def __init__(self, db: Session, *, now: Optional[datetime] = None, adapter_factory=None):
        self.db = db
        self._fixed_now = now
        self.assessments = AssessmentService(db, now=now, adapter_factory=adapter_factory)

    def _clock(self) -> datetime:
        return self._fixed_now or _now()

    def _audit(self, actor: User, learner_org_id: str, event: str, review: AssessmentReview, detail: Optional[dict] = None) -> None:
        AuditService(self.db).record(
            org_id=learner_org_id, event_type=event, actor_user_id=actor.id,
            target_ref=f"assessment_review:{review.id}",
            detail={"attempt_id": review.attempt_id, "result_id": review.result_id, **(detail or {})}, commit=False,
        )

    # -- learner side --------------------------------------------------------------------------------

    def request(
        self, user: User, attempt_id: str, *, reason: Optional[str], consent: bool,
        trigger: AssessmentReviewTrigger = AssessmentReviewTrigger.LEARNER_DISPUTE,
    ) -> AssessmentReview:
        attempt = self.assessments.get_attempt(user.id, attempt_id)
        if attempt.status != AssessmentAttemptStatus.FINALIZED:
            raise ConflictError("Only a finished assessment can be reviewed.")
        result = self.assessments.effective_result(attempt.id)
        if result is None:
            raise ConflictError("There is no result to review.")
        text = (reason or "").strip()
        if trigger == AssessmentReviewTrigger.LEARNER_DISPUTE:
            if not (REASON_MIN <= len(text) <= REASON_MAX):
                raise ReviewInvalid(f"Tell us why in {REASON_MIN}-{REASON_MAX} characters.")
            if not consent:
                raise ReviewInvalid("A review needs your explicit consent to share this attempt with a reviewer.")
        existing = self.db.execute(
            select(AssessmentReview).where(AssessmentReview.attempt_id == attempt.id, AssessmentReview.status == AssessmentReviewStatus.OPEN)
        ).scalars().first()
        if existing is not None:
            raise ConflictError("A review of this attempt is already open.", detail={"review_id": existing.id})
        review = AssessmentReview(
            attempt_id=attempt.id, result_id=result.id, user_id=user.id, trigger=trigger, reason_text=text or None,
            consent_shared_at=self._clock() if consent else None, fingerprint=fingerprint_for(result, attempt),
        )
        self.db.add(review)
        self.db.flush()
        self._audit(user, user.org_id, "assessment.review_requested", review, {"trigger": trigger.value, "consent": bool(consent)})
        self.db.commit()
        self.db.refresh(review)
        return review

    def open_system_review(self, user_id: str, attempt: AssessmentAttempt, result: AssessmentResult, trigger: AssessmentReviewTrigger) -> Optional[AssessmentReview]:
        """A review the platform itself needs (disagreement / low confidence).
        Created WITHOUT consent: the reviewer sees no content until the learner agrees."""
        existing = self.db.execute(
            select(AssessmentReview).where(AssessmentReview.attempt_id == attempt.id, AssessmentReview.status == AssessmentReviewStatus.OPEN)
        ).scalars().first()
        if existing is not None:
            return existing
        review = AssessmentReview(attempt_id=attempt.id, result_id=result.id, user_id=user_id, trigger=trigger, fingerprint=fingerprint_for(result, attempt))
        self.db.add(review)
        self.db.flush()
        return review

    def _own_review(self, user_id: str, review_id: str) -> AssessmentReview:
        review = self.db.execute(select(AssessmentReview).where(AssessmentReview.id == review_id, AssessmentReview.user_id == user_id)).scalar_one_or_none()
        if review is None:
            raise NotFoundError("Review not found.")
        return review

    def grant_consent(self, user: User, review_id: str) -> AssessmentReview:
        review = self._own_review(user.id, review_id)
        if review.status != AssessmentReviewStatus.OPEN:
            raise ConflictError("This review is no longer open.")
        if review.consent_shared_at is None:
            review.consent_shared_at = self._clock()
            self._audit(user, user.org_id, "assessment.review_consent_granted", review)
            self.db.commit()
        return review

    def withdraw(self, user: User, review_id: str) -> AssessmentReview:
        """Allowed until a reviewer has been assigned (i.e. has opened it)."""
        review = self._own_review(user.id, review_id)
        changed = self.db.execute(
            update(AssessmentReview)
            .where(AssessmentReview.id == review.id, AssessmentReview.status == AssessmentReviewStatus.OPEN, AssessmentReview.reviewer_user_id.is_(None))
            .values(status=AssessmentReviewStatus.WITHDRAWN)
            .execution_options(synchronize_session=False)
        ).rowcount
        if not changed:
            raise ConflictError("This review can no longer be withdrawn.")
        self._audit(user, user.org_id, "assessment.review_withdrawn", review)
        self.db.commit()
        self.db.refresh(review)
        return review

    def learner_view(self, review: AssessmentReview) -> Dict[str, Any]:
        return {
            "id": review.id, "attempt_id": review.attempt_id, "result_id": review.result_id, "trigger": review.trigger.value,
            "status": review.status.value, "reason_text": review.reason_text, "consented": review.consent_shared_at is not None,
            "decision": review.decision.value if review.decision else None, "decision_rationale": review.decision_rationale,
            "requested_at": review.created_at, "resolved_at": review.resolved_at, "resulting_result_id": review.resulting_result_id,
        }

    # -- reviewer side ---------------------------------------------------------------------------------

    def _authorize_reviewer(self, reviewer: User, review: AssessmentReview) -> User:
        learner = self.db.get(User, review.user_id)
        if learner is None or reviewer.id == learner.id:
            raise ForbiddenError("You cannot review your own assessment.")
        if reviewer.org_id != learner.org_id or reviewer.role not in _REVIEWER_ROLES:
            raise ForbiddenError("Only an organisation owner or admin can review an assessment.")
        return learner

    def queue(self, reviewer: User) -> List[Dict[str, Any]]:
        """Open reviews in the reviewer's organisation — metadata only (no learner
        content, no reason text); content needs consent + an explicit detail read."""
        if reviewer.role not in _REVIEWER_ROLES:
            raise ForbiddenError("Only an organisation owner or admin can review assessments.")
        rows = self.db.execute(
            select(AssessmentReview, User).join(User, User.id == AssessmentReview.user_id)
            .where(AssessmentReview.status == AssessmentReviewStatus.OPEN, User.org_id == reviewer.org_id, AssessmentReview.user_id != reviewer.id)
            .order_by(AssessmentReview.created_at)
        ).all()
        return [
            {"id": r.id, "trigger": r.trigger.value, "requested_at": r.created_at, "consented": r.consent_shared_at is not None}
            for r, _u in rows
        ]

    def detail(self, reviewer: User, review_id: str) -> Dict[str, Any]:
        review = self.db.get(AssessmentReview, review_id)
        if review is None:
            raise NotFoundError("Review not found.")
        learner = self._authorize_reviewer(reviewer, review)
        if review.status == AssessmentReviewStatus.OPEN and review.reviewer_user_id is None and review.consent_shared_at is not None:
            review.reviewer_user_id = reviewer.id  # assigned on first consented read
        attempt = self.db.get(AssessmentAttempt, review.attempt_id)
        result = self.db.get(AssessmentResult, review.result_id)
        self._audit(reviewer, learner.org_id, "assessment.review_viewed", review, {"consented": review.consent_shared_at is not None})
        self.db.commit()
        view: Dict[str, Any] = {
            "id": review.id, "trigger": review.trigger.value, "status": review.status.value,
            "consented": review.consent_shared_at is not None, "fingerprint": review.fingerprint,
            "allowed_decisions": sorted(d.value for d in self._allowed(result)),
        }
        if review.consent_shared_at is None:
            view["message"] = "The learner has not yet agreed to share this attempt, so no content is shown."
            return view
        definition = self.db.get(AssessmentDefinition, attempt.definition_id)
        view.update({
            "reason_text": review.reason_text,
            "definition": {"key": definition.definition_key, "version": definition.version, "title": definition.title},
            "challenge": (attempt.challenge_instance or {}).get("items"),
            "submission": attempt.submission, "attestation": attempt.attestation,
            "manifest_hash": attempt.input_manifest_hash, "submission_hash": attempt.submission_hash,
            "result": {"id": result.id, "outcome": result.outcome.value if result.outcome else None,
                       "effect": result.demonstration_effect.value if result.demonstration_effect else None,
                       "criteria": result.criteria, "gaps": result.gaps},
        })
        return view

    @staticmethod
    def _allowed(result: AssessmentResult):
        return _ALLOWED.get(result.outcome, _UNDECIDED)

    def decide(self, reviewer: User, review_id: str, decision: AssessmentReviewDecision, rationale: str) -> AssessmentReview:
        review = self.db.get(AssessmentReview, review_id)
        if review is None:
            raise NotFoundError("Review not found.")
        learner = self._authorize_reviewer(reviewer, review)
        if review.consent_shared_at is None:
            raise ConflictError("The learner has not consented to a review of this attempt.")
        text = (rationale or "").strip()
        if len(text) < RATIONALE_MIN:
            raise ReviewInvalid("A decision needs a written rationale.")
        attempt = self.db.get(AssessmentAttempt, review.attempt_id)
        result = self.db.get(AssessmentResult, review.result_id)
        if fingerprint_for(result, attempt) != review.fingerprint or self.assessments.effective_result(attempt.id).id != result.id:
            raise ConflictError("This result changed since the review was requested.")
        if decision not in self._allowed(result):
            raise ConflictError(f"'{decision.value}' is not a valid decision for a {result.outcome.value} result.")
        if decision == AssessmentReviewDecision.OVERRIDE_PASS and any(
            c.get("method") == "deterministic" and c.get("required") and c.get("finding") in ("partial", "not_met")
            for c in result.criteria
        ):
            raise ConflictError("A required platform check was not met; a reviewer cannot override a platform fact. Issue a new assessment instead.")

        # Compare-and-swap: the decision is written once.
        won = self.db.execute(
            update(AssessmentReview)
            .where(AssessmentReview.id == review.id, AssessmentReview.status == AssessmentReviewStatus.OPEN, AssessmentReview.fingerprint == review.fingerprint)
            .values(status=AssessmentReviewStatus.RESOLVED, decision=decision, decision_rationale=text,
                    reviewer_user_id=reviewer.id, resolved_at=self._clock())
            .execution_options(synchronize_session=False)
        ).rowcount
        if not won:
            self.db.rollback()
            raise ConflictError("This review was already resolved.")
        try:
            resulting = self._apply(reviewer, learner, review, attempt, result, decision)
            review = self.db.get(AssessmentReview, review_id)
            self.db.refresh(review)
            review.resulting_result_id = resulting.id if resulting is not None else None
            self._audit(reviewer, learner.org_id, "assessment.review_decided", review,
                        {"decision": decision.value, "resulting_result_id": review.resulting_result_id})
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        self.db.refresh(review)
        return review

    # -- applying a decision (append-only) ----------------------------------------------------------------

    def _apply(self, reviewer, learner, review, attempt, reviewed: AssessmentResult, decision) -> Optional[AssessmentResult]:
        if decision == AssessmentReviewDecision.NEW_ASSESSMENT:
            definition = self.db.get(AssessmentDefinition, attempt.definition_id)
            self.assessments.start(
                learner, definition.definition_key, project_attempt_id=attempt.project_attempt_id,
                previous_attempt_id=attempt.id, origin=AssessmentOrigin.HUMAN_REQUESTED, bypass_cooldown=True,
            )
            return None

        if decision == AssessmentReviewDecision.CONFIRM:
            outcome, effect = reviewed.outcome, reviewed.demonstration_effect
        elif decision == AssessmentReviewDecision.OVERRIDE_PASS:
            outcome = AssessmentOutcome.PASSED
            independence = (reviewed.facts or {}).get("independence") or {}
            verified = (reviewed.facts or {}).get("execution_verification") == "platform_verified"
            definition = self.db.get(AssessmentDefinition, attempt.definition_id)
            from app.services.independence_policy import EXECUTION_EVIDENCE_TYPES

            effect = _effect(independence, verified, definition.produces_evidence_type in EXECUTION_EVIDENCE_TYPES, attempt.attestation)
        else:  # OVERRIDE_NEEDS_WORK
            outcome, effect = AssessmentOutcome.NEEDS_WORK, DemonstrationEffect.NONE

        criteria = list(reviewed.criteria)
        gaps, remediation = list(reviewed.gaps), list(reviewed.remediation)
        if decision == AssessmentReviewDecision.OVERRIDE_PASS:
            # The reviewer decides the JUDGED criteria; the original finding stays visible.
            criteria = [
                {**c, "original_finding": c.get("finding"), "finding": "met", "human_override": True}
                if c.get("method") == "grader" and c.get("required", True) else c
                for c in criteria
            ]
            gaps, remediation = [], []
        elif decision == AssessmentReviewDecision.OVERRIDE_NEEDS_WORK:
            if not gaps:
                gaps = [{"criterion_key": None, "label": "Reviewer decision", "source": "human", "finding": "not_met", "detail": review.decision_rationale}]
            remediation = remediation or [{"kind": "retry", "label": "Try again with a new challenge after the cooldown"}]

        seq = self.assessments._next_seq(attempt.id)
        human = AssessmentResult(
            id=new_uuid(), attempt_id=attempt.id, user_id=attempt.user_id, seq=seq, round=0,
            result_kind=AssessmentResultKind.HUMAN, outcome=outcome, demonstration_effect=effect,
            criteria=criteria, gaps=gaps, remediation=remediation,
            facts={**(reviewed.facts or {}), "human_review": {"review_id": review.id, "decision": decision.value, "rationale": review.decision_rationale}},
            grader_agent_run_ids=reviewed.grader_agent_run_ids, grader_agent_version_id=reviewed.grader_agent_version_id,
            grading_contract_version=reviewed.grading_contract_version, review_id=review.id, supersedes_result_id=reviewed.id,
        )
        definition = self.db.get(AssessmentDefinition, attempt.definition_id)
        links = self.assessments._links(definition.id)
        old_rows = self._evidence_for(reviewed.id, attempt.user_id)
        states = LearnerStateService(self.db)
        before = {l.concept_id: states.state(attempt.user_id, l.concept_id, now=self._clock()).ladder for l in links}

        evidence: List[LearningEvidence] = []
        if decision == AssessmentReviewDecision.OVERRIDE_PASS:
            evidence = AssessmentEvidenceWriter(self.db).write(attempt=attempt, definition=definition, links=links, result=human, grader=GradingMode.HUMAN)
        elif decision == AssessmentReviewDecision.OVERRIDE_NEEDS_WORK:
            evidence = self._reverse(old_rows, human)
        after = {l.concept_id: states.state(attempt.user_id, l.concept_id, now=self._clock()) for l in links}
        AssessmentReportService(self.db).populate(
            final=human, attempt=attempt, definition=definition, links=links, evidence=evidence,
            state_before=before, state_after=after, now=self._clock(),
        )
        if decision == AssessmentReviewDecision.CONFIRM:  # a confirmation keeps the original record intact
            human.record_snapshot, human.record_hash = reviewed.record_snapshot, reviewed.record_hash
        human.report = {**(human.report or {}), "human_review": {"decision": decision.value, "rationale": review.decision_rationale, "review_id": review.id}}
        self.db.add(human)
        self.db.flush()
        for old in old_rows:
            replacement = next((e for e in evidence if e.concept_id == old.concept_id), None)
            if replacement is not None and decision == AssessmentReviewDecision.OVERRIDE_NEEDS_WORK:
                LearningEvidenceService(self.db).supersede(old.id, replacement.id, commit=False)
        return human

    def _evidence_for(self, result_id: str, user_id: str) -> List[LearningEvidence]:
        return list(self.db.execute(
            select(LearningEvidence).where(
                LearningEvidence.user_id == user_id, LearningEvidence.ref_type == EvidenceRefType.ASSESSMENT_RESULT,
                LearningEvidence.ref_id == result_id, LearningEvidence.superseded_by_id.is_(None),
            )
        ).scalars())

    def _reverse(self, old_rows: List[LearningEvidence], human: AssessmentResult) -> List[LearningEvidence]:
        """A reversed pass appends a failing HUMAN row per concept; the old row
        stays visible and is pointed at its replacement (never edited)."""
        service = LearningEvidenceService(self.db)
        rows = []
        for old in old_rows:
            rows.append(service.record_evidence(
                user_id=old.user_id, concept_id=old.concept_id, concept_version_id=old.concept_version_id,
                evidence_type=old.evidence_type, grader=GradingMode.HUMAN, passed=False,
                score=old.score, ref_type=EvidenceRefType.ASSESSMENT_RESULT, ref_id=human.id,
                assistance_level=old.assistance_level, execution_verification=old.execution_verification, commit=False,
            ))
        return rows
