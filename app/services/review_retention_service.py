"""AIL.4B review & retention — deterministic derivation of the REVIEW_DUE and
REVIEW_FAILED overlays (docs/ail-learning-spec-v1.md Sec 18.2, 22).

This is NOT a second learning engine. ``LearnerStateService`` remains the one
authority for the evidence ladder and calls ``ReviewAssessor`` to add the two
retention overlays; the assessor only *reads* (evidence the state service has
already loaded, review attempts, the plan, concept versions) and never writes.

Rules (frozen AIL.4B contract):

* Overlay only. Neither overlay ever changes the ladder: a Concept that is
  DEMONSTRATED stays DEMONSTRATED whatever a review says.
* Eligibility: the Concept is core OR in the user's active Learning Plan
  (a PLANNED item), AND the learner state is DEMONSTRATED.
* Clock: starts at the latest *qualifying* evidence — which includes the
  evidence a successful review appended. New qualifying evidence resets it.
  Qualifying = not a self-report, ``passed is True``, not demo data, not
  superseded.
* Interval: by Concept kind (definitional 180 days, mechanism 120, operational
  90, architectural 120), doubled after each successful review in the current
  streak, capped at 365 days. The streak is the run of successful reviews since
  the latest failed review. ``Concept.freshness_days`` is not used.
* REVIEW_DUE: eligible AND (interval elapsed OR a material ConceptVersion newer
  than the version the baseline evidence cites has been published).
* REVIEW_FAILED: DEMONSTRATED and the latest completed review attempt failed.
  A later successful review clears it (by derivation — nothing is written).
* A PASSED attempt only counts as a successful review when its linked
  evidence row really exists and qualifies. A PASSED row pointing at
  nothing, or at non-qualifying evidence, is ignored entirely: it neither
  clears REVIEW_FAILED, nor resets the clock, nor extends the interval.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.enums import (
    ChangeSeverity,
    ConceptKind,
    EvidenceType,
    GradingMode,
    PlanItemState,
    ReviewAttemptStatus,
    VersionStatus,
)
from app.models.concepts import Concept, ConceptVersion
from app.models.learner import LearningEvidence, LearningPlanItem
from app.models.learning_review import ReviewAttempt

REVIEW_DUE = "review_due"
REVIEW_FAILED = "review_failed"

BASE_INTERVAL_DAYS = {
    ConceptKind.DEFINITIONAL: 180,
    ConceptKind.MECHANISM: 120,
    ConceptKind.OPERATIONAL: 90,
    ConceptKind.ARCHITECTURAL: 120,
}
MAX_INTERVAL_DAYS = 365
# Spec Sec 19.2: after a failed check, a retry waits (default 12 h).
RETRY_COOLDOWN = timedelta(hours=12)


class ReviewReasonCode(str, Enum):
    """Deterministic reason codes for review overlays. Each names one recorded
    fact; none is a weight."""

    REVIEW_ELIGIBLE_CORE = "REVIEW_ELIGIBLE_CORE"
    REVIEW_ELIGIBLE_PLAN = "REVIEW_ELIGIBLE_PLAN"
    REVIEW_INTERVAL_ELAPSED = "REVIEW_INTERVAL_ELAPSED"
    REVIEW_CONCEPT_CHANGED = "REVIEW_CONCEPT_CHANGED"
    REVIEW_FAILED_LATEST = "REVIEW_FAILED_LATEST"


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def interval_days_for(kind: ConceptKind, streak: int) -> int:
    """Base interval doubled once per successful review in the streak, capped."""
    days = BASE_INTERVAL_DAYS[kind]
    for _ in range(max(0, streak)):
        days *= 2
        if days >= MAX_INTERVAL_DAYS:
            return MAX_INTERVAL_DAYS
    return min(days, MAX_INTERVAL_DAYS)


def evidence_qualifies(evidence: LearningEvidence) -> bool:
    """Evidence that counts for the retention clock and for a successful
    review: a positive, non-self-reported, non-demo, non-superseded row."""
    return (
        evidence.evidence_type != EvidenceType.SELF_REPORT
        and evidence.grader != GradingMode.SELF
        and evidence.passed is True
        and not evidence.on_demo_data
        and evidence.superseded_by_id is None
    )


@dataclass(frozen=True)
class ReviewBaseline:
    evidence_id: str
    evidence_type: str
    recorded_at: datetime
    concept_version_id: str
    concept_version: Optional[int]  # highest version number the qualifying evidence cites


@dataclass(frozen=True)
class ReviewAttemptView:
    id: str
    status: str
    started_at: datetime
    completed_at: Optional[datetime]
    concept_version_id: str
    learning_item_id: Optional[str]
    resulting_learning_evidence_id: Optional[str]


@dataclass
class ReviewAssessment:
    concept_id: str
    kind: str
    demonstrated: bool
    eligible: bool
    eligible_via: List[str]
    baseline: Optional[ReviewBaseline]
    base_interval_days: int
    interval_days: int
    streak: int
    due_at: Optional[datetime]
    interval_elapsed: bool
    material_changes: List[Dict[str, Any]]
    due: bool
    due_reasons: List[str]
    failed: bool
    latest_completed: Optional[ReviewAttemptView]
    active_attempt: Optional[ReviewAttemptView]
    cooldown_until: Optional[datetime]
    reason_codes: List[str] = field(default_factory=list)


def _view(attempt: ReviewAttempt) -> ReviewAttemptView:
    return ReviewAttemptView(
        id=attempt.id,
        status=attempt.status.value,
        started_at=_aware(attempt.started_at),
        completed_at=_aware(attempt.completed_at),
        concept_version_id=attempt.concept_version_id,
        learning_item_id=attempt.learning_item_id,
        resulting_learning_evidence_id=attempt.resulting_learning_evidence_id,
    )


class ReviewAssessor:
    """Read-only. ``evidence`` must be the user's evidence for this Concept
    (the learner-state service already has it), so no extra evidence query."""

    def __init__(self, db: Session):
        self.db = db

    def assess(
        self,
        user_id: str,
        concept_id: str,
        *,
        demonstrated: bool,
        evidence: List[LearningEvidence],
        now: datetime,
    ) -> Optional[ReviewAssessment]:
        concept = self.db.get(Concept, concept_id)
        if concept is None:
            return None
        now = _aware(now)

        attempts = list(
            self.db.execute(
                select(ReviewAttempt)
                .where(ReviewAttempt.user_id == user_id, ReviewAttempt.concept_id == concept_id)
                .order_by(ReviewAttempt.started_at, ReviewAttempt.id)
            ).scalars()
        )
        by_evidence_id = {e.id: e for e in evidence}

        def counts(attempt: ReviewAttempt) -> bool:
            """Completed attempts that count: FAILED, or PASSED whose evidence
            really exists for this learner/Concept and qualifies."""
            if attempt.completed_at is None:
                return False
            if attempt.status == ReviewAttemptStatus.FAILED:
                return True
            if attempt.status != ReviewAttemptStatus.PASSED:
                return False
            linked = by_evidence_id.get(attempt.resulting_learning_evidence_id or "")
            return linked is not None and evidence_qualifies(linked)

        completed = sorted(
            (a for a in attempts if counts(a)), key=lambda a: (_aware(a.completed_at), a.id)
        )
        latest_completed = completed[-1] if completed else None
        active = next((a for a in reversed(attempts) if a.status == ReviewAttemptStatus.STARTED), None)

        streak = 0
        for attempt in reversed(completed):
            if attempt.status == ReviewAttemptStatus.FAILED:
                break
            streak += 1
        base_days = BASE_INTERVAL_DAYS[concept.kind]
        interval_days = interval_days_for(concept.kind, streak)

        qualifying = [e for e in evidence if evidence_qualifies(e)]
        versions = list(
            self.db.execute(
                select(ConceptVersion).where(ConceptVersion.concept_id == concept_id).order_by(ConceptVersion.version)
            ).scalars()
        )
        number_by_id = {v.id: v.version for v in versions}
        baseline: Optional[ReviewBaseline] = None
        if qualifying:
            latest = max(qualifying, key=lambda e: (_aware(e.created_at), e.id))
            cited = [number_by_id[e.concept_version_id] for e in qualifying if e.concept_version_id in number_by_id]
            baseline = ReviewBaseline(
                evidence_id=latest.id,
                evidence_type=latest.evidence_type.value,
                recorded_at=_aware(latest.created_at),
                concept_version_id=latest.concept_version_id,
                concept_version=max(cited) if cited else None,
            )

        eligible_via: List[str] = []
        if concept.is_core:
            eligible_via.append("core")
        in_plan = self.db.execute(
            select(LearningPlanItem.id).where(
                LearningPlanItem.user_id == user_id,
                LearningPlanItem.concept_id == concept_id,
                LearningPlanItem.state == PlanItemState.PLANNED,
            )
        ).first()
        if in_plan is not None:
            eligible_via.append("active_plan")
        eligible = bool(eligible_via) and demonstrated

        due_at: Optional[datetime] = None
        interval_elapsed = False
        material_changes: List[Dict[str, Any]] = []
        if baseline is not None:
            due_at = baseline.recorded_at + timedelta(days=interval_days)
            interval_elapsed = now >= due_at
            baseline_version = baseline.concept_version or 0
            for v in versions:
                if (
                    v.version > baseline_version
                    and v.status in (VersionStatus.ACTIVE, VersionStatus.DEPRECATED)
                    and v.published_at is not None
                    and v.change_severity == ChangeSeverity.MATERIAL
                ):
                    material_changes.append(
                        {
                            "version": v.version,
                            "concept_version_id": v.id,
                            "change_note": v.change_note,
                            "published_at": _aware(v.published_at),
                        }
                    )

        due_reasons: List[str] = []
        if eligible and baseline is not None:
            if interval_elapsed:
                due_reasons.append(ReviewReasonCode.REVIEW_INTERVAL_ELAPSED.value)
            if material_changes:
                due_reasons.append(ReviewReasonCode.REVIEW_CONCEPT_CHANGED.value)
        due = bool(due_reasons)

        failed = (
            demonstrated
            and latest_completed is not None
            and latest_completed.status == ReviewAttemptStatus.FAILED
        )
        cooldown_until = (
            _aware(latest_completed.completed_at) + RETRY_COOLDOWN
            if latest_completed is not None and latest_completed.status == ReviewAttemptStatus.FAILED
            else None
        )

        reason_codes: List[str] = []
        if "core" in eligible_via:
            reason_codes.append(ReviewReasonCode.REVIEW_ELIGIBLE_CORE.value)
        if "active_plan" in eligible_via:
            reason_codes.append(ReviewReasonCode.REVIEW_ELIGIBLE_PLAN.value)
        reason_codes.extend(due_reasons)
        if failed:
            reason_codes.append(ReviewReasonCode.REVIEW_FAILED_LATEST.value)

        return ReviewAssessment(
            concept_id=concept_id,
            kind=concept.kind.value,
            demonstrated=demonstrated,
            eligible=eligible,
            eligible_via=eligible_via,
            baseline=baseline,
            base_interval_days=base_days,
            interval_days=interval_days,
            streak=streak,
            due_at=due_at,
            interval_elapsed=interval_elapsed,
            material_changes=material_changes,
            due=due,
            due_reasons=due_reasons,
            failed=failed,
            latest_completed=_view(latest_completed) if latest_completed is not None else None,
            active_attempt=_view(active) if active is not None else None,
            cooldown_until=cooldown_until,
            reason_codes=reason_codes,
        )
