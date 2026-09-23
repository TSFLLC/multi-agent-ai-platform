"""Minimal authenticated Development ? Concept relationship service."""

from datetime import datetime, timezone
from typing import List

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import ConflictError, InvalidStateTransitionError, NotFoundError
from app.models.concepts import Concept
from app.models.radar import (
    Development,
    DevelopmentConcept,
    DevelopmentConceptProposedBy,
    DevelopmentConceptState,
)


class DevelopmentConceptService:
    def __init__(self, db: Session):
        self.db = db

    def list_links(self, development_id: str) -> List[DevelopmentConcept]:
        if self.db.get(Development, development_id) is None:
            raise NotFoundError(f"Development {development_id} not found.")
        return list(
            self.db.execute(
                select(DevelopmentConcept)
                .where(DevelopmentConcept.development_id == development_id)
                .order_by(DevelopmentConcept.proposed_at, DevelopmentConcept.id)
            )
            .scalars()
            .all()
        )

    def propose(
        self,
        development_id: str,
        concept_id: str,
        proposed_by: DevelopmentConceptProposedBy,
    ) -> DevelopmentConcept:
        if self.db.get(Development, development_id) is None:
            raise NotFoundError(f"Development {development_id} not found.")
        if self.db.get(Concept, concept_id) is None:
            raise NotFoundError(f"Concept {concept_id} not found.")

        existing = self.db.execute(
            select(DevelopmentConcept).where(
                DevelopmentConcept.development_id == development_id,
                DevelopmentConcept.concept_id == concept_id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise ConflictError("This Development ? Concept link already exists.")

        link = DevelopmentConcept(
            development_id=development_id,
            concept_id=concept_id,
            state=DevelopmentConceptState.PROPOSED,
            proposed_by=proposed_by,
        )
        self.db.add(link)
        self.db.flush()
        return link

    def review(
        self,
        development_id: str,
        concept_id: str,
        *,
        reviewer_id: str,
        state: DevelopmentConceptState,
    ) -> DevelopmentConcept:
        if state not in (
            DevelopmentConceptState.CONFIRMED,
            DevelopmentConceptState.REJECTED,
        ):
            raise InvalidStateTransitionError("A review must confirm or reject a proposed link.")

        link = self.db.execute(
            select(DevelopmentConcept).where(
                DevelopmentConcept.development_id == development_id,
                DevelopmentConcept.concept_id == concept_id,
            )
        ).scalar_one_or_none()
        if link is None:
            raise NotFoundError("Development ? Concept link not found.")
        if link.state != DevelopmentConceptState.PROPOSED:
            raise InvalidStateTransitionError("Only proposed links can be reviewed.")

        link.state = state
        link.reviewed_by = reviewer_id
        link.reviewed_at = datetime.now(timezone.utc)
        self.db.flush()
        return link