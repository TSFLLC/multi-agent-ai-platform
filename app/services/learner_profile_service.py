"""Learner Profile / Interests Service — AIL.1A, docs/ail-learning-spec-v1.md
Sec 17.1, 30.

Every method takes an explicit ``user_id`` and filters on it (AIL
architecture reconciliation, frozen contract #10) — there is no implicit
"current project" scoping anywhere in this module, unlike most of the
rest of the platform. Interests are only ever written from an explicit
caller-supplied ``term_id``/``concept_id`` — nothing here reads task
content, code, prompts, artifacts, model outputs, or unrelated project
activity to infer one (spec Sec 30 "No inference from unrelated content").
"""

from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.errors import ConflictError, NotFoundError
from app.models.identity import User
from app.models.learner import LearnerInterest, LearnerProfile, LearningEvidence, LearningPlanItem


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class LearnerProfileService:
    def __init__(self, db: Session):
        self.db = db

    # -- Profile --------------------------------------------------------------

    def get_profile(self, user_id: str) -> Optional[LearnerProfile]:
        stmt = select(LearnerProfile).where(LearnerProfile.user_id == user_id)
        return self.db.execute(stmt).scalars().first()

    def get_or_create_profile(self, user_id: str) -> LearnerProfile:
        profile = self.get_profile(user_id)
        if profile is not None:
            return profile
        profile = LearnerProfile(user_id=user_id)
        self.db.add(profile)
        self.db.commit()
        self.db.refresh(profile)
        return profile

    def update_profile(
        self,
        user_id: str,
        *,
        level=None,
        goal_text: Optional[str] = None,
        depth=None,
        weekly_minutes: Optional[int] = None,
    ) -> LearnerProfile:
        profile = self.get_or_create_profile(user_id)
        if level is not None:
            profile.level = level
        if goal_text is not None:
            profile.goal_text = goal_text
        if depth is not None:
            profile.depth = depth
        if weekly_minutes is not None:
            profile.weekly_minutes = weekly_minutes
        profile.updated_at = _utcnow()
        self.db.commit()
        self.db.refresh(profile)
        return profile

    # -- Interests --------------------------------------------------------------

    def set_interest(
        self,
        user_id: str,
        *,
        term_id: Optional[str] = None,
        concept_id: Optional[str] = None,
        watch: bool = False,
    ) -> LearnerInterest:
        if (term_id is None) == (concept_id is None):
            raise ConflictError("Exactly one of term_id or concept_id must be set")

        existing = self._find_interest(user_id, term_id=term_id, concept_id=concept_id)
        if existing is not None:
            existing.watch = watch
            self.db.commit()
            self.db.refresh(existing)
            return existing

        interest = LearnerInterest(user_id=user_id, term_id=term_id, concept_id=concept_id, watch=watch)
        self.db.add(interest)
        self.db.commit()
        self.db.refresh(interest)
        return interest

    def _find_interest(
        self, user_id: str, *, term_id: Optional[str], concept_id: Optional[str]
    ) -> Optional[LearnerInterest]:
        stmt = select(LearnerInterest).where(LearnerInterest.user_id == user_id)
        stmt = (
            stmt.where(LearnerInterest.term_id == term_id)
            if term_id
            else stmt.where(LearnerInterest.concept_id == concept_id)
        )
        return self.db.execute(stmt).scalars().first()

    def list_interests(self, user_id: str) -> List[LearnerInterest]:
        stmt = select(LearnerInterest).where(LearnerInterest.user_id == user_id)
        return list(self.db.execute(stmt).scalars().all())

    def remove_interest(self, user_id: str, interest_id: str) -> None:
        interest = self.db.get(LearnerInterest, interest_id)
        if interest is None or interest.user_id != user_id:
            raise NotFoundError(f"LearnerInterest {interest_id} not found for user {user_id}")
        self.db.delete(interest)
        self.db.commit()

    # -- Export / delete (spec Sec 30) -------------------------------------

    def export_learner_data(self, user_id: str) -> dict:
        profile = self.get_profile(user_id)
        interests = self.list_interests(user_id)
        plan_items = list(
            self.db.execute(select(LearningPlanItem).where(LearningPlanItem.user_id == user_id))
            .scalars()
            .all()
        )
        evidence = list(
            self.db.execute(select(LearningEvidence).where(LearningEvidence.user_id == user_id))
            .scalars()
            .all()
        )
        return {
            "user_id": user_id,
            "profile": _profile_to_dict(profile) if profile else None,
            "interests": [_interest_to_dict(i) for i in interests],
            "plan_items": [_plan_item_to_dict(p) for p in plan_items],
            "evidence": [_evidence_to_dict(e) for e in evidence],
            "assessments": self._export_assessments(user_id),
        }

    def _delete_assessments(self, user_id: str) -> None:
        """AIL.5C: remove the learner's assessment history AND the Grader runs that
        quoted their words (bookkeeping tasks, runs, artifact rows and files)."""
        from app.models.artifacts_eval import Artifact
        from app.models.assessment import AssessmentAttempt, AssessmentResult, AssessmentReview
        from app.models.tasks import AgentRun, Task, TaskRun

        self.db.execute(delete(AssessmentReview).where(AssessmentReview.user_id == user_id))
        # A result may supersede an earlier one (RESTRICT); delete newest-first so no
        # parent is removed while a superseding row still points at it.
        result_ids = self.db.execute(
            select(AssessmentResult.id).where(AssessmentResult.user_id == user_id).order_by(AssessmentResult.seq.desc())
        ).scalars().all()
        for result_id in result_ids:
            self.db.execute(delete(AssessmentResult).where(AssessmentResult.id == result_id))
        self.db.execute(delete(AssessmentAttempt).where(AssessmentAttempt.user_id == user_id))

        grader_tasks = select(Task.id).where(Task.created_by == user_id, Task.title.like("assessment-grading:%"))
        runs = select(AgentRun.id).join(TaskRun, TaskRun.id == AgentRun.task_run_id).where(TaskRun.task_id.in_(grader_tasks))
        files = list(self.db.execute(select(Artifact.storage_ref).where(Artifact.agent_run_id.in_(runs))).scalars())
        self.db.execute(delete(Task).where(Task.id.in_(grader_tasks)))
        _remove_files(files)

    def _export_assessments(self, user_id: str) -> dict:
        """AIL.5C: the learner's own attempts, results and reviews (spec Sec 30)."""
        from app.models.assessment import AssessmentAttempt, AssessmentResult, AssessmentReview

        def rows(model):
            return list(self.db.execute(select(model).where(model.user_id == user_id).order_by(model.created_at)).scalars())

        return {
            "attempts": [
                {"id": a.id, "definition_id": a.definition_id, "status": a.status.value, "origin": a.origin.value,
                 "pinned_versions": a.pinned_versions, "challenge": a.challenge_instance and a.challenge_instance.get("items"),
                 "draft": a.draft, "submission": a.submission, "attestation": a.attestation,
                 "started_at": a.started_at.isoformat() if a.started_at else None,
                 "submitted_at": a.submitted_at.isoformat() if a.submitted_at else None,
                 "finalized_at": a.finalized_at.isoformat() if a.finalized_at else None}
                for a in rows(AssessmentAttempt)
            ],
            "results": [
                {"id": r.id, "attempt_id": r.attempt_id, "kind": r.result_kind.value, "seq": r.seq,
                 "outcome": r.outcome.value if r.outcome else None,
                 "demonstration_effect": r.demonstration_effect.value if r.demonstration_effect else None,
                 "criteria": r.criteria, "gaps": r.gaps, "remediation": r.remediation, "report": r.report,
                 "record": r.record_snapshot, "supersedes_result_id": r.supersedes_result_id}
                for r in rows(AssessmentResult)
            ],
            "reviews": [
                {"id": v.id, "attempt_id": v.attempt_id, "result_id": v.result_id, "trigger": v.trigger.value,
                 "status": v.status.value, "reason": v.reason_text,
                 "decision": v.decision.value if v.decision else None, "decision_rationale": v.decision_rationale}
                for v in rows(AssessmentReview)
            ],
        }

    def delete_learner_data(self, user_id: str) -> None:
        """Hard-deletes every AIL.1A row for this user. Aggregated
        platform records (Agent Runs, evaluations, etc.) are never
        touched — only this user's learner-scoped rows (spec Sec 30)."""
        user = self.db.get(User, user_id)
        if user is None:
            raise NotFoundError(f"User {user_id} not found")

        self._delete_assessments(user_id)
        # A superseded row points at its replacement; clear the pointers first so
        # the bulk delete never trips a row-level foreign-key check.
        self.db.execute(update(LearningEvidence).where(LearningEvidence.user_id == user_id).values(superseded_by_id=None))
        self.db.execute(delete(LearningEvidence).where(LearningEvidence.user_id == user_id))
        self.db.execute(delete(LearningPlanItem).where(LearningPlanItem.user_id == user_id))
        self.db.execute(delete(LearnerInterest).where(LearnerInterest.user_id == user_id))
        self.db.execute(delete(LearnerProfile).where(LearnerProfile.user_id == user_id))
        self.db.commit()


def _remove_files(paths) -> None:
    import os

    for path in paths:
        try:
            os.remove(path)
        except OSError:
            pass  # best effort: the row is gone either way


def _profile_to_dict(profile: LearnerProfile) -> dict:
    return {
        "level": profile.level.value if profile.level else None,
        "goal_text": profile.goal_text,
        "depth": profile.depth.value if profile.depth else None,
        "weekly_minutes": profile.weekly_minutes,
        "updated_at": profile.updated_at.isoformat(),
    }


def _interest_to_dict(interest: LearnerInterest) -> dict:
    return {
        "id": interest.id,
        "term_id": interest.term_id,
        "concept_id": interest.concept_id,
        "watch": interest.watch,
    }


def _plan_item_to_dict(item: LearningPlanItem) -> dict:
    return {
        "id": item.id,
        "concept_id": item.concept_id,
        "position": item.position,
        "state": item.state.value,
        "origin": item.origin.value,
    }


def _evidence_to_dict(evidence: LearningEvidence) -> dict:
    return {
        "id": evidence.id,
        "concept_id": evidence.concept_id,
        "concept_version_id": evidence.concept_version_id,
        "evidence_type": evidence.evidence_type.value,
        "passed": evidence.passed,
        "grader": evidence.grader.value,
        "created_at": evidence.created_at.isoformat(),
    }
