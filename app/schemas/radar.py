from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field

from app.models.radar import (
    ClaimCreationMethod,
    ClaimStatus,
    ClaimType,
    DevelopmentConceptProposedBy,
    DevelopmentConceptState,
    DevelopmentStatus,
    RadarItemState,
    RadarSourceClass,
    RadarSourceState,
)


class RadarSourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    source_class: RadarSourceClass
    endpoint_url: Optional[str] = None
    independence_group: str = Field(min_length=1, max_length=120)
    fetch_method: str = Field(min_length=1, max_length=40)
    cadence_minutes: Optional[int] = Field(default=None, gt=0)
    tos_note: Optional[str] = None
    credential_ref: Optional[str] = None


class RadarSourceReview(BaseModel):
    owner_approved: bool
    tos_approved: bool
    review_note: Optional[str] = None


class RadarSourceRead(BaseModel):
    id: str
    name: str
    source_class: RadarSourceClass
    endpoint_url: Optional[str] = None
    independence_group: str
    fetch_method: str
    cadence_minutes: Optional[int] = None
    state: RadarSourceState
    tos_note: Optional[str] = None
    owner_reviewed_at: Optional[datetime] = None
    tos_reviewed_at: Optional[datetime] = None
    last_success_at: Optional[datetime] = None
    last_error: Optional[str] = None

    class Config:
        orm_mode = True


class RadarItemRead(BaseModel):
    id: str
    source_id: str
    development_id: Optional[str] = None
    external_identity: Optional[str] = None
    canonical_url: Optional[str] = None
    title: str
    published_at: Optional[datetime] = None
    retrieved_at: datetime
    content_hash: str
    storage_ref: Optional[str] = None
    processing_state: RadarItemState

    class Config:
        orm_mode = True


class DevelopmentRead(BaseModel):
    id: str
    title: str
    development_type: str
    announced_at: Optional[datetime] = None
    effective_at: Optional[datetime] = None
    first_seen_at: datetime
    candidate_key: str
    status: DevelopmentStatus
    merged_into_id: Optional[str] = None
    verification_level: Optional[str] = None

    class Config:
        orm_mode = True


class ClaimRead(BaseModel):
    id: str
    claim_type: ClaimType
    text: str
    development_id: Optional[str] = None
    model_id: Optional[str] = None
    quote_span: Optional[str] = None
    conditions: Optional[dict] = None
    as_of: datetime
    created_by: ClaimCreationMethod
    status: ClaimStatus

    class Config:
        orm_mode = True


class MergeDevelopmentRequest(BaseModel):
    target_development_id: str


class DevelopmentListRead(BaseModel):
    items: List[DevelopmentRead]

class DevelopmentConceptRead(BaseModel):
    id: str
    development_id: str
    concept_id: str
    state: DevelopmentConceptState
    proposed_by: DevelopmentConceptProposedBy
    proposed_at: datetime
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None

    class Config:
        orm_mode = True


class DevelopmentConceptPropose(BaseModel):
    concept_id: str
    proposed_by: DevelopmentConceptProposedBy = DevelopmentConceptProposedBy.USER
