from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.auth import get_current_user
from app.authz import require_platform_admin
from app.errors import NotFoundError
from app.models.concepts import Concept
from app.models.identity import User
from app.models.providers import Model, Provider, ProviderModel
from app.models.radar import (
    AttentionSample,
    Claim,
    ClaimCitation,
    ClaimOrigin,
    Development,
    DevelopmentConcept,
    DevelopmentModel,
    DevelopmentStatus,
    DevelopmentTerm,
    RadarItem,
    RadarSource,
    RadarSourceState,
    TriageDecisionKind,
)
from app.schemas.lab import ExperimentCreate, ExperimentRead
from app.schemas.radar import (
    AttentionSampleCreate,
    AttentionSampleRead,
    AttentionStateRead,
    ClaimRead,
    ConceptDiscoveryRead,
    DevelopmentRead,
    DevelopmentTermCreate,
    DevelopmentTermRead,
    ManualRadarItemCreate,
    MergeDevelopmentRequest,
    RadarIngestionCreate,
    RadarIngestionRead,
    RadarItemRead,
    RadarSourceCreate,
    RadarSourceRead,
    RadarSourceReview,
    TriageDecisionCreate,
    TriageDecisionRead,
)
from app.services.lab_service import LabService
from app.services.radar_intelligence_service import RadarIntelligenceService, create_manual_radar_item
from app.services.radar_service import (
    RadarClaimService,
    RadarIngestionService,
    RadarSourceService,
    RadarValidationError,
    derive_verification,
)

router = APIRouter(prefix="/radar", tags=["radar"])


@router.post("/developments/{development_id}/experiments", response_model=ExperimentRead, status_code=201)
def create_experiment_draft_from_development(
    development_id: str,
    body: ExperimentCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Explicit Radar-to-Lab bridge; saving Radar triage never calls this."""
    return LabService(db).experiment_read(
        LabService(db).create_experiment(user, body, development_id=development_id)
    )


@router.get("/concepts", response_model=List[ConceptDiscoveryRead])
def list_concepts_for_selection(
    q: Optional[str] = Query(default=None, min_length=1, max_length=120),
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Read-only discovery over AIL.1A's canonical Concept identities."""
    stmt = select(Concept)
    if q:
        pattern = f"%{q}%"
        stmt = stmt.where(or_(Concept.name.ilike(pattern), Concept.slug.ilike(pattern)))
    stmt = stmt.order_by(Concept.name, Concept.id).limit(limit)
    return list(db.execute(stmt).scalars().all())


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


@router.post("/sources/{source_id}/items/manual", response_model=RadarItemRead, status_code=201)
def create_manual_item(
    source_id: str,
    body: ManualRadarItemCreate,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_platform_admin),
):
    source = db.get(RadarSource, source_id)
    if source is None:
        raise NotFoundError(f"Radar source {source_id} not found.")
    item = create_manual_radar_item(
        db,
        source=source,
        title=body.title,
        canonical_url=body.canonical_url,
        normalized_content=body.normalized_content,
        external_identity=body.external_identity,
        published_at=body.published_at,
        storage_ref=body.storage_ref,
    )
    db.commit()
    db.refresh(item)
    return item


@router.post(
    "/items/{source_item_id}/ingest",
    response_model=RadarIngestionRead,
    status_code=201,
)
def ingest_source_item(
    source_item_id: str,
    body: RadarIngestionCreate,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_platform_admin),
):
    """Turn one approved Source Item into durable Radar evidence."""
    try:
        result = RadarIngestionService(db).ingest(
            source_item_id=source_item_id,
            title=body.title,
            development_type=body.development_type,
            subject_key=body.subject_key,
            change_key=body.change_key,
            announced_at=body.announced_at,
            effective_at=body.effective_at,
            development_id=body.development_id,
            claims=body.claims,
            model_ids=body.model_ids,
        )
        db.commit()
        return result
    except RadarValidationError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        db.rollback()
        raise


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
def list_developments(
    development_type: Optional[str] = Query(default=None),
    status: Optional[DevelopmentStatus] = Query(default=None),
    model_id: Optional[str] = Query(default=None),
    provider_id: Optional[str] = Query(default=None),
    since: Optional[datetime] = Query(default=None),
    until: Optional[datetime] = Query(default=None),
    verification_level: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    stmt = select(Development).order_by(Development.first_seen_at.desc())
    if development_type:
        stmt = stmt.where(Development.development_type == development_type)
    if status:
        stmt = stmt.where(Development.status == status)
    if since:
        stmt = stmt.where(Development.first_seen_at >= since)
    if until:
        stmt = stmt.where(Development.first_seen_at <= until)
    if model_id:
        stmt = stmt.where(
            Development.id.in_(
                select(DevelopmentModel.development_id).where(DevelopmentModel.model_id == model_id)
            )
        )
    if provider_id:
        stmt = stmt.where(
            Development.id.in_(
                select(DevelopmentModel.development_id)
                .join(ProviderModel, ProviderModel.model_id == DevelopmentModel.model_id)
                .where(ProviderModel.provider_id == provider_id)
            )
        )
    developments = db.execute(stmt.distinct().offset(offset).limit(limit)).scalars().all()
    rows = [_development_read(db, development) for development in developments]
    if verification_level:
        rows = [row for row in rows if row.verification_level == verification_level]
    return rows


@router.get("/developments/{development_id}", response_model=DevelopmentRead)
def get_development(development_id: str, db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    development = db.get(Development, development_id)
    if development is None:
        raise NotFoundError(f"Development {development_id} not found.")
    return _development_read(db, development)


@router.get("/developments/{development_id}/intelligence")
def get_development_intelligence(
    development_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    development = db.get(Development, development_id)
    if development is None:
        raise NotFoundError(f"Development {development_id} not found.")
    service = RadarIntelligenceService(db)
    claims = db.execute(
        select(Claim)
        .where(Claim.development_id == development_id)
        .order_by(Claim.as_of.desc(), Claim.created_at.desc())
    ).scalars().all()
    claim_rows = []
    for claim in claims:
        origin = db.execute(
            select(ClaimOrigin).where(ClaimOrigin.claim_id == claim.id)
        ).scalar_one_or_none()
        source_item = db.get(RadarItem, origin.source_item_id) if origin and origin.source_item_id else None
        citations = db.execute(
            select(ClaimCitation).where(ClaimCitation.explanation_claim_id == claim.id)
        ).scalars().all()
        claim_rows.append({
            "id": claim.id,
            "claim_type": claim.claim_type,
            "text": claim.text,
            "development_id": claim.development_id,
            "model_id": claim.model_id,
            "quote_span": claim.quote_span,
            "conditions": claim.conditions,
            "as_of": claim.as_of,
            "created_by": claim.created_by,
            "status": claim.status,
            "origin": {
                "kind": origin.origin_kind if origin else None,
                "source_item_id": origin.source_item_id if origin else None,
                "evaluation_id": origin.evaluation_id if origin else None,
                "agent_run_id": origin.agent_run_id if origin else None,
            },
            "source_item": {
                "id": source_item.id,
                "title": source_item.title,
                "canonical_url": source_item.canonical_url,
                "published_at": source_item.published_at,
                "retrieved_at": source_item.retrieved_at,
                "content_hash": source_item.content_hash,
            } if source_item else None,
            "citations": [
                {"id": citation.id, "cited_claim_id": citation.cited_claim_id}
                for citation in citations
            ],
        })
    model_ids = list(db.execute(
        select(DevelopmentModel.model_id).where(DevelopmentModel.development_id == development_id)
    ).scalars())
    provider_ids = list(db.execute(
        select(ProviderModel.provider_id).where(ProviderModel.model_id.in_(model_ids))
    ).scalars()) if model_ids else []
    models = list(db.execute(select(Model).where(Model.id.in_(model_ids))).scalars()) if model_ids else []
    providers = list(db.execute(select(Provider).where(Provider.id.in_(provider_ids))).scalars()) if provider_ids else []
    concept_links = db.execute(
        select(DevelopmentConcept).where(DevelopmentConcept.development_id == development_id)
    ).scalars().all()
    term_links = db.execute(
        select(DevelopmentTerm).where(DevelopmentTerm.development_id == development_id)
    ).scalars().all()
    samples = db.execute(
        select(AttentionSample)
        .where(AttentionSample.development_id == development_id)
        .order_by(AttentionSample.sampled_at.desc())
    ).scalars().all()
    current_triage = service.current_triage(user.id, development_id=development_id)
    return {
        "development": _development_read(db, development),
        "claims": claim_rows,
        "model_ids": model_ids,
        "provider_ids": sorted(set(provider_ids)),
        "model_links": [
            {"id": model.id, "name": model.canonical_model_id}
            for model in sorted(models, key=lambda row: (row.canonical_model_id, row.id))
        ],
        "provider_links": [
            {"id": provider.id, "name": provider.name}
            for provider in sorted(providers, key=lambda row: (row.name, row.id))
        ],
        "concept_links": [
            {"id": link.id, "concept_id": link.concept_id, "state": link.state, "proposed_by": link.proposed_by}
            for link in concept_links
        ],
        "term_links": [
            {"id": link.id, "term_id": link.term_id, "created_by": link.created_by}
            for link in term_links
        ],
        "verification_level": derive_verification(db, development_id),
        "freshness": service.development_freshness(development_id).value,
        "attention_state": service.attention_state(development_id=development_id).value,
        "reason_codes": [code.value for code in service.relevance_reason_codes(user.id, development_id)],
        "current_triage": {
            "id": current_triage.id,
            "decision": current_triage.decision,
            "rationale": current_triage.rationale,
            "reason_codes": current_triage.reason_codes,
            "revisit_at": current_triage.revisit_at,
            "revisit_condition": current_triage.revisit_condition,
            "decided_at": current_triage.decided_at,
        } if current_triage else None,
        "attention_samples": [
            {"id": sample.id, "source_id": sample.source_id, "metric": sample.metric, "value": sample.value,
             "unit": sample.unit, "sampled_at": sample.sampled_at, "external_identity": sample.external_identity}
            for sample in samples
        ],
    }


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


@router.post("/developments/{development_id}/terms", response_model=DevelopmentTermRead, status_code=201)
def add_development_term(
    development_id: str,
    body: DevelopmentTermCreate,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_platform_admin),
):
    link = RadarIntelligenceService(db).add_term(development_id, body.term_id, body.created_by.value)
    db.commit()
    db.refresh(link)
    return link


@router.post("/attention", response_model=AttentionSampleRead, status_code=201)
def create_attention_sample(
    source_id: str,
    body: AttentionSampleCreate,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_platform_admin),
):
    sample = RadarIntelligenceService(db).create_attention_sample(
        source_id=source_id,
        metric=body.metric,
        value=body.value,
        sampled_at=body.sampled_at,
        development_id=body.development_id,
        model_id=body.model_id,
        unit=body.unit,
        measurement_metadata=body.measurement_metadata,
        external_identity=body.external_identity,
        sample_hash=body.sample_hash,
    )
    db.commit()
    db.refresh(sample)
    return sample


@router.get("/developments/{development_id}/attention", response_model=AttentionStateRead)
def get_development_attention(
    development_id: str,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    state = RadarIntelligenceService(db).attention_state(development_id=development_id)
    return AttentionStateRead(subject_id=development_id, subject_type="development", state=state)


@router.get("/developments/{development_id}/triage", response_model=Optional[TriageDecisionRead])
def get_development_triage(
    development_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return RadarIntelligenceService(db).current_triage(user.id, development_id=development_id)


@router.get("/developments/{development_id}/triage/history", response_model=List[TriageDecisionRead])
def get_development_triage_history(
    development_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return RadarIntelligenceService(db).triage_history(user.id, development_id=development_id)


@router.post("/developments/{development_id}/triage", response_model=TriageDecisionRead, status_code=201)
def create_development_triage(
    development_id: str,
    body: TriageDecisionCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    triage = RadarIntelligenceService(db).create_triage(
        user_id=user.id,
        development_id=development_id,
        decision=body.decision,
        rationale=body.rationale,
        reason_codes=body.reason_codes,
        revisit_at=body.revisit_at,
        revisit_condition=body.revisit_condition,
    )
    db.commit()
    db.refresh(triage)
    return triage


@router.post("/developments/{development_id}/reviewed", response_model=TriageDecisionRead, status_code=201)
def mark_development_reviewed(
    development_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    triage = RadarIntelligenceService(db).create_triage(
        user_id=user.id,
        development_id=development_id,
        decision=TriageDecisionKind.IGNORE,
        rationale="Reviewed without a further action.",
        reason_codes=["UNREVIEWED"],
    )
    db.commit()
    db.refresh(triage)
    return triage


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
