from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.auth import get_current_user
from app.authz import require_platform_admin
from app.errors import NotFoundError
from app.models.identity import User
from app.models.radar import Claim, Development, RadarItem, RadarSource, RadarSourceState
from app.schemas.radar import (
    ClaimRead,
    DevelopmentRead,
    MergeDevelopmentRequest,
    RadarItemRead,
    RadarSourceCreate,
    RadarSourceRead,
    RadarSourceReview,
)
from app.services.radar_service import RadarClaimService, RadarSourceService, derive_verification

router = APIRouter(prefix="/radar", tags=["radar"])


@router.get("/sources", response_model=List[RadarSourceRead])
def list_sources(db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    return list(db.execute(select(RadarSource).order_by(RadarSource.name)).scalars().all())


@router.post("/sources", response_model=RadarSourceRead, status_code=201)
def create_source(
    body: RadarSourceCreate,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_platform_admin),
):
    source = RadarSource(**body.dict())
    db.add(source)
    db.commit()
    db.refresh(source)
    return source


@router.post("/sources/{source_id}/review", response_model=RadarSourceRead)
def review_source(
    source_id: str,
    body: RadarSourceReview,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_platform_admin),
):
    source = db.get(RadarSource, source_id)
    if source is None:
        raise NotFoundError(f"Radar source {source_id} not found.")
    now = datetime.now(timezone.utc)
    source.owner_reviewed_at = now if body.owner_approved else None
    source.tos_reviewed_at = now if body.tos_approved else None
    source.review_note = body.review_note
    if body.owner_approved and body.tos_approved:
        RadarSourceService(db).activate(source)
    else:
        source.state = RadarSourceState.PAUSED
    db.commit()
    db.refresh(source)
    return source


@router.get("/items", response_model=List[RadarItemRead])
def list_items(
    source_id: Optional[str] = Query(default=None),
    development_id: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    stmt = select(RadarItem).order_by(RadarItem.retrieved_at.desc())
    if source_id:
        stmt = stmt.where(RadarItem.source_id == source_id)
    if development_id:
        stmt = stmt.where(RadarItem.development_id == development_id)
    return list(db.execute(stmt).scalars().all())


def _development_read(db: Session, development: Development) -> DevelopmentRead:
    return DevelopmentRead(
        id=development.id,
        title=development.title,
        development_type=development.development_type,
        announced_at=development.announced_at,
        effective_at=development.effective_at,
        first_seen_at=development.first_seen_at,
        candidate_key=development.candidate_key,
        status=development.status,
        merged_into_id=development.merged_into_id,
        verification_level=derive_verification(db, development.id),
    )


@router.get("/developments", response_model=List[DevelopmentRead])
def list_developments(db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    developments = db.execute(select(Development).order_by(Development.first_seen_at.desc())).scalars().all()
    return [_development_read(db, development) for development in developments]


@router.get("/developments/{development_id}", response_model=DevelopmentRead)
def get_development(development_id: str, db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    development = db.get(Development, development_id)
    if development is None:
        raise NotFoundError(f"Development {development_id} not found.")
    return _development_read(db, development)


@router.get("/developments/{development_id}/claims", response_model=List[ClaimRead])
def list_development_claims(
    development_id: str, db: Session = Depends(get_db), _user: User = Depends(get_current_user)
):
    if db.get(Development, development_id) is None:
        raise NotFoundError(f"Development {development_id} not found.")
    return list(
        db.execute(
            select(Claim)
            .where(Claim.development_id == development_id)
            .order_by(Claim.as_of.desc(), Claim.created_at.desc())
        )
        .scalars()
        .all()
    )


@router.post("/developments/{development_id}/merge", response_model=DevelopmentRead)
def merge_development(
    development_id: str,
    body: MergeDevelopmentRequest,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_platform_admin),
):
    source = db.get(Development, development_id)
    target = db.get(Development, body.target_development_id)
    if source is None or target is None:
        raise NotFoundError("Both source and target Developments must exist.")
    RadarClaimService(db).merge_developments(source, target)
    db.commit()
    db.refresh(source)
    return _development_read(db, source)

from app.errors import ForbiddenError
from app.models.radar import (
    DevelopmentConcept,
    DevelopmentConceptProposedBy,
    DevelopmentConceptState,
)
from app.schemas.radar import (
    DevelopmentConceptPropose,
    DevelopmentConceptRead,
)
from app.services.development_concept_service import DevelopmentConceptService


@router.get(
    "/developments/{development_id}/concepts",
    response_model=List[DevelopmentConceptRead],
)
def list_development_concepts(
    development_id: str,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    return DevelopmentConceptService(db).list_links(development_id)


@router.post(
    "/developments/{development_id}/concepts",
    response_model=DevelopmentConceptRead,
    status_code=201,
)
def propose_development_concept(
    development_id: str,
    body: DevelopmentConceptPropose,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_platform_admin),
):
    if body.proposed_by != DevelopmentConceptProposedBy.USER:
        raise ForbiddenError(
            "External API proposals must be user-attributed; rule and agent "
            "proposals require an internal service boundary."
        )
    link = DevelopmentConceptService(db).propose(
        development_id,
        body.concept_id,
        body.proposed_by,
    )
    db.commit()
    db.refresh(link)
    return link


def _review_concept_link(
    development_id: str,
    concept_id: str,
    state: DevelopmentConceptState,
    db: Session,
    reviewer: User,
):
    link = DevelopmentConceptService(db).review(
        development_id,
        concept_id,
        reviewer_id=reviewer.id,
        state=state,
    )
    db.commit()
    db.refresh(link)
    return link


@router.post(
    "/developments/{development_id}/concepts/{concept_id}/confirm",
    response_model=DevelopmentConceptRead,
)
def confirm_development_concept(
    development_id: str,
    concept_id: str,
    db: Session = Depends(get_db),
    reviewer: User = Depends(require_platform_admin),
):
    return _review_concept_link(
        development_id,
        concept_id,
        DevelopmentConceptState.CONFIRMED,
        db,
        reviewer,
    )


@router.post(
    "/developments/{development_id}/concepts/{concept_id}/reject",
    response_model=DevelopmentConceptRead,
)
def reject_development_concept(
    development_id: str,
    concept_id: str,
    db: Session = Depends(get_db),
    reviewer: User = Depends(require_platform_admin),
):
    return _review_concept_link(
        development_id,
        concept_id,
        DevelopmentConceptState.REJECTED,
        db,
        reviewer,
    )
