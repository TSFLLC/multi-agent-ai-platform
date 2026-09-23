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

from sqlalchemy import delete, select
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
        }

    def delete_learner_data(self, user_id: str) -> None:
        """Hard-deletes every AIL.1A row for this user. Aggregated
        platform records (Agent Runs, evaluations, etc.) are never
        touched — only this user's learner-scoped rows (spec Sec 30)."""
        user = self.db.get(User, user_id)
        if user is None:
            raise NotFoundError(f"User {user_id} not found")

        self.db.execute(delete(LearningEvidence).where(LearningEvidence.user_id == user_id))
        self.db.execute(delete(LearningPlanItem).where(LearningPlanItem.user_id == user_id))
        self.db.execute(delete(LearnerInterest).where(LearnerInterest.user_id == user_id))
        self.db.execute(delete(LearnerProfile).where(LearnerProfile.user_id == user_id))
        self.db.commit()


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
