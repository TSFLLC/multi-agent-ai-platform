"""AIL.2A evidence ledger models.

This module deliberately stops at Sources -> Items -> Developments -> Claims.
Concept identity is owned by AIL.1 and is therefore not declared here until
that migration is available.  Model links point at the frozen MA2 registry.
"""

from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, Numeric, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin, utcnow
from app.db.types import sa_enum


class RadarSourceClass(str, Enum):
    S1 = "s1"
    S2 = "s2"
    S3 = "s3"
    S4 = "s4"
    S5 = "s5"
    S6 = "s6"
    S7 = "s7"


class RadarSourceState(str, Enum):
    CANDIDATE = "candidate"
    APPROVED = "approved"
    ACTIVE = "active"
    PAUSED = "paused"
    RETIRED = "retired"


class RadarItemState(str, Enum):
    RECEIVED = "received"
    PROCESSED = "processed"
    FAILED = "failed"
    REJECTED = "rejected"


class DevelopmentStatus(str, Enum):
    ACTIVE = "active"
    MERGED = "merged"
    ARCHIVED = "archived"


class ClaimType(str, Enum):
    FACT = "FACT"
    PROVIDER_CLAIM = "PROVIDER_CLAIM"
    RESEARCH_RESULT = "RESEARCH_RESULT"
    BENCHMARK_RESULT = "BENCHMARK_RESULT"
    COMMUNITY_SIGNAL = "COMMUNITY_SIGNAL"
    PLATFORM_OBSERVATION = "PLATFORM_OBSERVATION"
    AI_EXPLANATION = "AI_EXPLANATION"


class ClaimCreationMethod(str, Enum):
    RULE = "rule"
    AGENT = "agent"
    USER = "user"


class ClaimStatus(str, Enum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    DISPUTED = "disputed"


class ClaimOriginKind(str, Enum):
    SOURCE_ITEM = "source_item"
    PLATFORM_EVALUATION = "platform_evaluation"
    AIL_AGENT_RUN = "ail_agent_run"


class RadarSource(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "radar_sources"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_class: Mapped[RadarSourceClass] = mapped_column(sa_enum(RadarSourceClass), nullable=False)
    endpoint_url: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    independence_group: Mapped[str] = mapped_column(String(120), nullable=False)
    fetch_method: Mapped[str] = mapped_column(String(40), nullable=False)
    cadence_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    state: Mapped[RadarSourceState] = mapped_column(
        sa_enum(RadarSourceState), nullable=False, default=RadarSourceState.CANDIDATE
    )
    tos_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    owner_reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    tos_reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    review_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    credential_ref: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    last_success_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (UniqueConstraint("name", name="uq_radar_sources_name"),)


class RadarItem(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "radar_items"

    source_id: Mapped[str] = mapped_column(ForeignKey("radar_sources.id", ondelete="RESTRICT"), nullable=False)
    development_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("developments.id", ondelete="SET NULL"), nullable=True
    )
    external_identity: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    canonical_url: Mapped[Optional[str]] = mapped_column(String(2000), nullable=True)
    title: Mapped[str] = mapped_column(String(1000), nullable=False)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    normalized_content: Mapped[str] = mapped_column(Text, nullable=False)
    storage_ref: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    processing_state: Mapped[RadarItemState] = mapped_column(
        sa_enum(RadarItemState), nullable=False, default=RadarItemState.RECEIVED
    )
    frozen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint("source_id", "external_identity", name="uq_radar_items_source_external_identity"),
        UniqueConstraint("source_id", "content_hash", name="uq_radar_items_source_hash"),
    )


class Development(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "developments"

    title: Mapped[str] = mapped_column(String(500), nullable=False)
    development_type: Mapped[str] = mapped_column(String(80), nullable=False)
    announced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    effective_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    candidate_key: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[DevelopmentStatus] = mapped_column(
        sa_enum(DevelopmentStatus), nullable=False, default=DevelopmentStatus.ACTIVE
    )
    merged_into_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("developments.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (UniqueConstraint("candidate_key", name="uq_developments_candidate_key"),)


class DevelopmentModel(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "development_models"

    development_id: Mapped[str] = mapped_column(
        ForeignKey("developments.id", ondelete="CASCADE"), nullable=False
    )
    model_id: Mapped[str] = mapped_column(ForeignKey("models.id", ondelete="RESTRICT"), nullable=False)

    __table_args__ = (UniqueConstraint("development_id", "model_id", name="uq_development_models_pair"),)


class Claim(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "claims"

    claim_type: Mapped[ClaimType] = mapped_column(sa_enum(ClaimType), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    development_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("developments.id", ondelete="CASCADE"), nullable=True
    )
    model_id: Mapped[Optional[str]] = mapped_column(ForeignKey("models.id", ondelete="RESTRICT"), nullable=True)
    quote_span: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    conditions: Mapped[Optional[dict]] = mapped_column("conditions_json", nullable=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[ClaimCreationMethod] = mapped_column(sa_enum(ClaimCreationMethod), nullable=False)
    status: Mapped[ClaimStatus] = mapped_column(sa_enum(ClaimStatus), nullable=False, default=ClaimStatus.ACTIVE)
    superseded_by_id: Mapped[Optional[str]] = mapped_column(ForeignKey("claims.id"), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "development_id IS NOT NULL OR model_id IS NOT NULL", name="claim_subject_required"
        ),
    )


class ClaimOrigin(UUIDPrimaryKeyMixin, Base):
    """Exactly one typed origin per claim; nullable FKs are constrained below."""

    __tablename__ = "claim_origins"

    claim_id: Mapped[str] = mapped_column(ForeignKey("claims.id", ondelete="CASCADE"), nullable=False, unique=True)
    origin_kind: Mapped[ClaimOriginKind] = mapped_column(sa_enum(ClaimOriginKind), nullable=False)
    source_item_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("radar_items.id", ondelete="RESTRICT"), nullable=True
    )
    evaluation_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("evaluations.id", ondelete="RESTRICT"), nullable=True
    )
    agent_run_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="RESTRICT"), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "((source_item_id IS NOT NULL) + (evaluation_id IS NOT NULL) + (agent_run_id IS NOT NULL)) = 1",
            name="claim_origin_exactly_one_ref",
        ),
        CheckConstraint(
            "(origin_kind = 'source_item' AND source_item_id IS NOT NULL AND evaluation_id IS NULL AND agent_run_id IS NULL) OR "
            "(origin_kind = 'platform_evaluation' AND evaluation_id IS NOT NULL AND source_item_id IS NULL AND agent_run_id IS NULL) OR "
            "(origin_kind = 'ail_agent_run' AND agent_run_id IS NOT NULL AND source_item_id IS NULL AND evaluation_id IS NULL)",
            name="claim_origin_kind_matches_ref",
        ),
    )


class ClaimCitation(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "claim_citations"

    explanation_claim_id: Mapped[str] = mapped_column(
        ForeignKey("claims.id", ondelete="CASCADE"), nullable=False
    )
    cited_claim_id: Mapped[str] = mapped_column(ForeignKey("claims.id", ondelete="RESTRICT"), nullable=False)

    __table_args__ = (
        UniqueConstraint("explanation_claim_id", "cited_claim_id", name="uq_claim_citations_pair"),
        CheckConstraint("explanation_claim_id <> cited_claim_id", name="claim_citation_not_self"),
    )

class DevelopmentConceptState(str, Enum):
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class DevelopmentConceptProposedBy(str, Enum):
    RULE = "rule"
    AGENT = "agent"
    USER = "user"


class AttentionState(str, Enum):
    UNKNOWN = "UNKNOWN"
    LOW = "LOW"
    RISING = "RISING"
    HIGH = "HIGH"
    SUSTAINED = "SUSTAINED"


class TriageDecisionKind(str, Enum):
    IGNORE = "IGNORE"
    WATCH = "WATCH"
    LEARN = "LEARN"
    EXPERIMENT = "EXPERIMENT"
    INVESTIGATE = "INVESTIGATE"


class RadarReasonCode(str, Enum):
    NEW_MODEL = "NEW_MODEL"
    PRICE_CHANGE = "PRICE_CHANGE"
    CAPABILITY_CHANGE = "CAPABILITY_CHANGE"
    CONTEXT_CHANGE = "CONTEXT_CHANGE"
    STATUS_CHANGE = "STATUS_CHANGE"
    RELATED_TO_INTEREST = "RELATED_TO_INTEREST"
    RELATED_TO_LEARNING_PLAN = "RELATED_TO_LEARNING_PLAN"
    RELATED_TO_USED_MODEL = "RELATED_TO_USED_MODEL"
    RELATED_TO_USED_PROVIDER = "RELATED_TO_USED_PROVIDER"
    RELATED_TO_PLATFORM_COMPONENT = "RELATED_TO_PLATFORM_COMPONENT"
    NEEDS_VERIFICATION = "NEEDS_VERIFICATION"
    CONFLICTING_CLAIMS = "CONFLICTING_CLAIMS"
    SOURCE_STALE = "SOURCE_STALE"
    ATTENTION_RISING = "ATTENTION_RISING"
    WATCH_TRIGGERED = "WATCH_TRIGGERED"
    UNREVIEWED = "UNREVIEWED"
    NEW_RESEARCH = "NEW_RESEARCH"
    NEW_BENCHMARK = "NEW_BENCHMARK"


class DevelopmentConcept(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "development_concepts"

    development_id: Mapped[str] = mapped_column(
        ForeignKey("developments.id", ondelete="RESTRICT"), nullable=False
    )
    concept_id: Mapped[str] = mapped_column(
        ForeignKey("concepts.id", ondelete="RESTRICT"), nullable=False
    )
    state: Mapped[DevelopmentConceptState] = mapped_column(
        sa_enum(DevelopmentConceptState),
        nullable=False,
        default=DevelopmentConceptState.PROPOSED,
    )
    proposed_by: Mapped[DevelopmentConceptProposedBy] = mapped_column(
        sa_enum(DevelopmentConceptProposedBy), nullable=False
    )
    proposed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    reviewed_by: Mapped[Optional[str]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint(
            "development_id",
            "concept_id",
            name="uq_development_concepts_pair",
        ),
    )


class DevelopmentTerm(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "development_terms"

    development_id: Mapped[str] = mapped_column(
        ForeignKey("developments.id", ondelete="CASCADE"), nullable=False
    )
    term_id: Mapped[str] = mapped_column(
        ForeignKey("taxonomy_terms.id", ondelete="RESTRICT"), nullable=False
    )
    created_by: Mapped[ClaimCreationMethod] = mapped_column(
        sa_enum(ClaimCreationMethod), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    __table_args__ = (
        UniqueConstraint("development_id", "term_id", name="uq_development_terms_pair"),
        Index("ix_development_terms_development_id", "development_id"),
        Index("ix_development_terms_term_id", "term_id"),
    )


class AttentionSample(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "attention_samples"

    development_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("developments.id", ondelete="CASCADE"), nullable=True
    )
    model_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("models.id", ondelete="RESTRICT"), nullable=True
    )
    source_id: Mapped[str] = mapped_column(
        ForeignKey("radar_sources.id", ondelete="RESTRICT"), nullable=False
    )
    metric: Mapped[str] = mapped_column(String(120), nullable=False)
    value: Mapped[float] = mapped_column(Numeric(20, 8), nullable=False)
    unit: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    measurement_metadata: Mapped[Optional[dict]] = mapped_column(
        "measurement_metadata_json", JSON, nullable=True
    )
    sampled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    external_identity: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    sample_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "((development_id IS NOT NULL) + (model_id IS NOT NULL)) = 1",
            name="attention_sample_exactly_one_subject",
        ),
        Index("ix_attention_samples_development_sampled", "development_id", "sampled_at"),
        Index("ix_attention_samples_model_sampled", "model_id", "sampled_at"),
        Index(
            "uq_attention_samples_development_identity",
            "development_id",
            "source_id",
            "metric",
            "sampled_at",
            unique=True,
            sqlite_where=text("development_id IS NOT NULL"),
        ),
        Index(
            "uq_attention_samples_model_identity",
            "model_id",
            "source_id",
            "metric",
            "sampled_at",
            unique=True,
            sqlite_where=text("model_id IS NOT NULL"),
        ),
    )


class TriageDecision(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "triage_decisions"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    development_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("developments.id", ondelete="CASCADE"), nullable=True
    )
    model_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("models.id", ondelete="RESTRICT"), nullable=True
    )
    decision: Mapped[TriageDecisionKind] = mapped_column(
        sa_enum(TriageDecisionKind), nullable=False
    )
    rationale: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reason_codes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    revisit_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    revisit_condition: Mapped[Optional[dict]] = mapped_column(
        "revisit_condition_json", JSON, nullable=True
    )
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    superseded_by_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("triage_decisions.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "((development_id IS NOT NULL) + (model_id IS NOT NULL)) = 1",
            name="triage_decision_exactly_one_target",
        ),
        Index("ix_triage_decisions_user_decided", "user_id", "decided_at"),
        Index(
            "uq_triage_decisions_active_development",
            "user_id",
            "development_id",
            unique=True,
            sqlite_where=text("development_id IS NOT NULL AND superseded_by_id IS NULL"),
        ),
        Index(
            "uq_triage_decisions_active_model",
            "user_id",
            "model_id",
            unique=True,
            sqlite_where=text("model_id IS NOT NULL AND superseded_by_id IS NULL"),
        ),
    )
