"""Concept Graph — AIL.1A, docs/ail-learning-spec-v1.md Sec 15.

``ConceptVersion`` follows the exact immutability discipline already used
by ``app.models.agents.AgentVersion`` and
``app.models.evaluation_definitions.EvaluationDefinitionVersion``:
published content is never updated in place — "editing" means inserting a
new ``version + 1`` row. ``Concept.status`` mirrors the latest version's
lifecycle the same non-authoritative way ``Agent.current_status`` mirrors
``AgentVersion.status`` — kept in sync at the service layer only, never a
DB constraint, and deliberately not a FK to a specific version row (that
would create a concepts<->concept_versions table-creation cycle for no
benefit; "current version" is always derived by the service, per the
platform's "derive, don't cache" principle, spec Sec 32.1).

Prerequisite acyclicity (``ConceptRelation`` with
``relation_type=prerequisite``) is enforced only at the service layer
(``app.services.concept_graph_service``) — SQLite cannot express graph
acyclicity declaratively, the same trade-off already accepted for
``job_queue``'s lease/fencing protocol.
"""

from datetime import datetime
from typing import List, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.enums import (
    ChangeSeverity,
    ConceptKind,
    ConceptLevel,
    ConceptRelationType,
    ContentOrigin,
    GradingMode,
    LearningItemType,
    VersionStatus,
)
from app.db.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin, utcnow
from app.db.types import sa_enum


class Concept(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Stable identity — spec Sec 15.1. Canonical FK target for Radar's
    ``development_concepts`` and any future Model<->Concept relationship
    (AIL architecture reconciliation, frozen contract #4); ``id``/``slug``
    stability here is a contract AIL.1A owes those slices."""

    __tablename__ = "concepts"

    slug: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    aliases: Mapped[Optional[list]] = mapped_column("aliases_json", nullable=True)
    level: Mapped[ConceptLevel] = mapped_column(sa_enum(ConceptLevel), nullable=False)
    kind: Mapped[ConceptKind] = mapped_column(sa_enum(ConceptKind), nullable=False)
    is_core: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    freshness_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Denormalized convenience mirror of the latest transitioned version's
    # status — same pattern and caveat as Agent.current_status /
    # EvaluationDefinition.current_status: never authoritative, kept in
    # sync only where a version's status changes (ConceptGraphService).
    status: Mapped[Optional[VersionStatus]] = mapped_column(sa_enum(VersionStatus), nullable=True)

    versions: Mapped[List["ConceptVersion"]] = relationship(back_populates="concept", passive_deletes=True)
    outgoing_relations: Mapped[List["ConceptRelation"]] = relationship(
        back_populates="from_concept", foreign_keys="ConceptRelation.from_concept_id", passive_deletes=True
    )
    incoming_relations: Mapped[List["ConceptRelation"]] = relationship(
        back_populates="to_concept", foreign_keys="ConceptRelation.to_concept_id", passive_deletes=True
    )
    terms: Mapped[List["ConceptTerm"]] = relationship(back_populates="concept", passive_deletes=True)
    learning_items: Mapped[List["LearningItem"]] = relationship(
        back_populates="concept", passive_deletes=True
    )


class ConceptVersion(UUIDPrimaryKeyMixin, Base):
    """Immutable once published — spec Sec 15.1, 18.4 rule 5."""

    __tablename__ = "concept_versions"
    __table_args__ = (
        UniqueConstraint("concept_id", "version", name="uq_concept_versions_concept_id_version"),
    )

    concept_id: Mapped[str] = mapped_column(ForeignKey("concepts.id", ondelete="CASCADE"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    plain_definition: Mapped[str] = mapped_column(Text, nullable=False)
    technical_explanation: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    examples_md: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Rule payload (spec Sec 18.3): the per-kind evidence requirement set,
    # including any alternative requirement set for gated concepts (spec
    # Sec 18.4 rule 4) — a JSON spec, not a relationship (Sec 24.4/10.5.5).
    evidence_requirements: Mapped[Optional[dict]] = mapped_column("evidence_requirements_json", nullable=True)
    content_origin: Mapped[ContentOrigin] = mapped_column(
        sa_enum(ContentOrigin), nullable=False, default=ContentOrigin.AI_DRAFTED_UNREVIEWED
    )
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    change_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    change_severity: Mapped[Optional[ChangeSeverity]] = mapped_column(sa_enum(ChangeSeverity), nullable=True)
    status: Mapped[VersionStatus] = mapped_column(
        sa_enum(VersionStatus), nullable=False, default=VersionStatus.DRAFT
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    concept: Mapped["Concept"] = relationship(back_populates="versions")


class ConceptRelation(UUIDPrimaryKeyMixin, Base):
    """Spec Sec 15.2 — deliberately few relation types."""

    __tablename__ = "concept_relations"
    __table_args__ = (
        UniqueConstraint(
            "from_concept_id",
            "to_concept_id",
            "relation_type",
            name="uq_concept_relations_from_to_type",
        ),
    )

    from_concept_id: Mapped[str] = mapped_column(
        ForeignKey("concepts.id", ondelete="CASCADE"), nullable=False
    )
    to_concept_id: Mapped[str] = mapped_column(ForeignKey("concepts.id", ondelete="CASCADE"), nullable=False)
    relation_type: Mapped[ConceptRelationType] = mapped_column(sa_enum(ConceptRelationType), nullable=False)
    # Only meaningful for relation_type=related (e.g. "contrasts_with").
    label: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)

    from_concept: Mapped["Concept"] = relationship(
        back_populates="outgoing_relations", foreign_keys=[from_concept_id]
    )
    to_concept: Mapped["Concept"] = relationship(
        back_populates="incoming_relations", foreign_keys=[to_concept_id]
    )


class ConceptTerm(UUIDPrimaryKeyMixin, Base):
    """Concept <-> shared taxonomy membership (tracks, platform components,
    lanes, etc.) — spec Sec 15.1 "Track membership"."""

    __tablename__ = "concept_terms"
    __table_args__ = (UniqueConstraint("concept_id", "term_id", name="uq_concept_terms_concept_id_term_id"),)

    concept_id: Mapped[str] = mapped_column(ForeignKey("concepts.id", ondelete="CASCADE"), nullable=False)
    term_id: Mapped[str] = mapped_column(ForeignKey("taxonomy_terms.id", ondelete="CASCADE"), nullable=False)

    concept: Mapped["Concept"] = relationship(back_populates="terms")


class LearningItem(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """A reusable teaching unit attached to a Concept — spec Sec 15.1.

    ``requires_capability_term_id`` points at AIL.1A's own
    ``taxonomy_terms`` (vocabulary ``platform_component``), never a
    Radar-owned table — graceful gating (spec Sec 4.3) works from AIL.1A
    alone. No ``source_id``/external-source column: the spec's "for
    external resources" use case implies a Radar-shaped source table that
    doesn't exist yet (AIL architecture reconciliation, frozen contract
    #6) — external-source linkage is added in the coordinated AIL.1/AIL.2
    integration migration, not here.
    """

    __tablename__ = "learning_items"

    concept_id: Mapped[str] = mapped_column(ForeignKey("concepts.id", ondelete="CASCADE"), nullable=False)
    item_type: Mapped[LearningItemType] = mapped_column(sa_enum(LearningItemType), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body_md: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Polymorphic per item_type: question/options/answer key, observation
    # query key, or lab config (Sec 24.4/10.5.5: a payload, not a
    # relationship).
    spec: Mapped[Optional[dict]] = mapped_column("spec_json", nullable=True)
    grading_mode: Mapped[Optional[GradingMode]] = mapped_column(sa_enum(GradingMode), nullable=True)
    reviewed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    est_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    requires_capability_term_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("taxonomy_terms.id"), nullable=True
    )

    concept: Mapped["Concept"] = relationship(back_populates="learning_items")
