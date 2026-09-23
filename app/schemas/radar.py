from datetime import datetime
from typing import List, Optional

from decimal import Decimal

from pydantic import BaseModel, Field

from app.models.radar import (
    ClaimCreationMethod,
    ClaimStatus,
    ClaimType,
    AttentionSample,
    AttentionState,
    DevelopmentConceptProposedBy,
    DevelopmentConceptState,
    DevelopmentStatus,
    RadarReasonCode,
    RadarItemState,
    RadarSourceClass,
    RadarSourceState,
    TriageDecisionKind,
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


class RadarIngestionClaimCreate(BaseModel):
    claim_type: ClaimType
    text: str = Field(min_length=1)
    as_of: datetime
    quote_span: Optional[str] = None
    conditions: Optional[dict] = None

    class Config:
        extra = "forbid"


class RadarIngestionCreate(BaseModel):
    title: Optional[str] = Field(default=None, max_length=500)
    development_type: Optional[str] = Field(default=None, max_length=80)
    subject_key: Optional[str] = Field(default=None, max_length=300)
    change_key: Optional[str] = Field(default=None, max_length=300)
    announced_at: Optional[datetime] = None
    effective_at: Optional[datetime] = None
    development_id: Optional[str] = None
    claims: List[RadarIngestionClaimCreate] = Field(default_factory=list)
    model_ids: List[str] = Field(default_factory=list)

    class Config:
        extra = "forbid"


class RadarIngestionRead(BaseModel):
    development: DevelopmentRead
    source_item: RadarItemRead
    claim_ids: List[str]
    development_model_ids: List[str]
    verification_level: Optional[str] = None
    freshness: str


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


class ManualRadarItemCreate(BaseModel):
    title: str = Field(min_length=1, max_length=1000)
    canonical_url: str = Field(min_length=1, max_length=2000)
    normalized_content: str = Field(min_length=1)
    external_identity: Optional[str] = Field(default=None, max_length=500)
    published_at: Optional[datetime] = None
    storage_ref: Optional[str] = Field(default=None, max_length=1000)


class DevelopmentTermCreate(BaseModel):
    term_id: str
    created_by: ClaimCreationMethod = ClaimCreationMethod.USER


class DevelopmentTermRead(BaseModel):
    id: str
    development_id: str
    term_id: str
    created_by: ClaimCreationMethod
    created_at: datetime

    class Config:
        orm_mode = True


class AttentionSampleCreate(BaseModel):
    metric: str = Field(min_length=1, max_length=120)
    value: Decimal
    unit: Optional[str] = Field(default=None, max_length=40)
    measurement_metadata: Optional[dict] = None
    sampled_at: datetime
    external_identity: Optional[str] = Field(default=None, max_length=500)
    sample_hash: Optional[str] = Field(default=None, min_length=64, max_length=64)
    development_id: Optional[str] = None
    model_id: Optional[str] = None


class AttentionSampleRead(BaseModel):
    id: str
    development_id: Optional[str] = None
    model_id: Optional[str] = None
    source_id: str
    metric: str
    value: Decimal
    unit: Optional[str] = None
    measurement_metadata: Optional[dict] = None
    sampled_at: datetime
    external_identity: Optional[str] = None
    sample_hash: Optional[str] = None

    class Config:
        orm_mode = True


class TriageDecisionCreate(BaseModel):
    decision: TriageDecisionKind
    rationale: Optional[str] = None
    reason_codes: List[str] = Field(default_factory=list)
    revisit_at: Optional[datetime] = None
    revisit_condition: Optional[dict] = None


class TriageDecisionRead(BaseModel):
    id: str
    user_id: str
    development_id: Optional[str] = None
    model_id: Optional[str] = None
    decision: TriageDecisionKind
    rationale: Optional[str] = None
    reason_codes: List[str]
    revisit_at: Optional[datetime] = None
    revisit_condition: Optional[dict] = None
    decided_at: datetime
    superseded_by_id: Optional[str] = None

    class Config:
        orm_mode = True


class AttentionStateRead(BaseModel):
    subject_id: str
    subject_type: str
    state: AttentionState
