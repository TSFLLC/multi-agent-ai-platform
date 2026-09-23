"""Provider / Model Registry — Section 13, 24.2, 24.4 #8.

Nothing here references OpenRouter by name in a way that couples core
domain logic to it (Section 13.5) — ``providers.type`` is a generic enum
where ``openrouter`` is one value among ``direct``/``local``/``enterprise``,
so a future local-model provider (Owner's "Agent -> Model Router -> Local
Model Provider" requirement) fits without a schema change.
"""

from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.enums import (
    CatalogRefreshStatus,
    HealthStatus,
    ModelStatus,
    ProviderType,
    SnapshotSource,
    ToolCallingSupport,
)
from app.db.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin, utcnow
from app.db.types import sa_enum
from app.domain.pricing import classify_pricing


class Provider(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """A configured Provider Adapter instance — Section 13.1."""

    __tablename__ = "providers"

    type: Mapped[ProviderType] = mapped_column(sa_enum(ProviderType), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    base_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    health_status: Mapped[HealthStatus] = mapped_column(
        sa_enum(HealthStatus), nullable=False, default=HealthStatus.UP
    )
    # Credential lookup is one-directional via secret_references.provider_id
    # (never the reverse) — a forward providers.credential_ref_id FK would
    # form a table-creation cycle with secret_references.provider_id that
    # SQLite cannot express (no ALTER TABLE ADD CONSTRAINT support), and a
    # reverse lookup is also the more correct shape: a provider may have
    # multiple secret_references rows over its credential-rotation history,
    # not exactly one. "The current credential for this provider" is
    # secret_references.provider_id == providers.id ordered by
    # created_at/rotated_at — never a plaintext value in this table
    # (Section 20.1/20.2, 24.4 #19).

    provider_models: Mapped[List["ProviderModel"]] = relationship(back_populates="provider")


class Model(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Canonical Model Registry entry — Section 13.2."""

    __tablename__ = "models"

    canonical_model_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    family: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    context_window: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    input_modalities: Mapped[Optional[list]] = mapped_column("input_modalities_json", nullable=True)
    output_modalities: Mapped[Optional[list]] = mapped_column("output_modalities_json", nullable=True)
    # MA2 correction: these three were originally NOT NULL with a
    # false-ish default (NONE / False), which cannot represent "the
    # provider doesn't reliably tell us" without literally guessing —
    # directly contradicting the frozen "store unknown, never guess"
    # pricing/capability principle (Section 3) once a real provider
    # adapter existed to expose the gap. Widened to nullable; NULL now
    # means unknown, never a guessed negative.
    tool_calling_support: Mapped[Optional[ToolCallingSupport]] = mapped_column(
        sa_enum(ToolCallingSupport),
        nullable=True,
        default=None,
    )
    structured_output_support: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, default=None)
    reasoning_tier: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    coding_capability_tier: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    vision_capability: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, default=None)
    status: Mapped[ModelStatus] = mapped_column(
        sa_enum(ModelStatus), nullable=False, default=ModelStatus.ACTIVE
    )
    last_refreshed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    provider_models: Mapped[List["ProviderModel"]] = relationship(back_populates="model")
    capabilities: Mapped[List["ModelCapability"]] = relationship(back_populates="model")


class ModelCapability(UUIDPrimaryKeyMixin, Base):
    """Normalized capability facts — Section 13.2 supported_features etc."""

    __tablename__ = "model_capabilities"
    __table_args__ = (
        UniqueConstraint("model_id", "capability_key", name="uq_model_capabilities_model_id_key"),
    )

    model_id: Mapped[str] = mapped_column(ForeignKey("models.id", ondelete="CASCADE"), nullable=False)
    capability_key: Mapped[str] = mapped_column(String(120), nullable=False)
    value: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    tier: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)

    model: Mapped["Model"] = relationship(back_populates="capabilities")


class ProviderModel(UUIDPrimaryKeyMixin, Base):
    """A Model as reachable via a specific Provider — Section 24.2.

    Pricing columns are nullable: a row with no pricing yet refreshed from
    the provider classifies as UNKNOWN, never guessed as free or paid
    (Owner instruction, Section 3 / app.domain.pricing).
    """

    __tablename__ = "provider_models"
    __table_args__ = (
        UniqueConstraint("model_id", "provider_id", name="uq_provider_models_model_id_provider_id"),
        # Bare suffix only — the naming_convention (app.db.base) already
        # prepends "ck_<table_name>_", so a fully-prefixed name here would
        # get double-prefixed by Alembic autogeneration.
        CheckConstraint(
            "cost_input_per_mtok IS NULL OR cost_input_per_mtok >= 0",
            name="cost_input_nonneg",
        ),
        CheckConstraint(
            "cost_output_per_mtok IS NULL OR cost_output_per_mtok >= 0",
            name="cost_output_nonneg",
        ),
    )

    model_id: Mapped[str] = mapped_column(ForeignKey("models.id", ondelete="CASCADE"), nullable=False)
    provider_id: Mapped[str] = mapped_column(ForeignKey("providers.id", ondelete="CASCADE"), nullable=False)
    provider_model_id: Mapped[str] = mapped_column(String(255), nullable=False)

    cost_input_per_mtok: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 8), nullable=True)
    cost_output_per_mtok: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 8), nullable=True)
    currency: Mapped[str] = mapped_column(String(10), nullable=False, default="USD")

    latency_p50_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    throughput_tokens_per_sec: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)
    availability_status: Mapped[HealthStatus] = mapped_column(
        sa_enum(HealthStatus), nullable=False, default=HealthStatus.UP
    )
    reliability_score: Mapped[Optional[float]] = mapped_column(Numeric(5, 4), nullable=True)
    privacy_characteristics: Mapped[Optional[dict]] = mapped_column(
        "privacy_characteristics_json", nullable=True
    )
    supported_features: Mapped[Optional[dict]] = mapped_column("supported_features_json", nullable=True)
    last_refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    model: Mapped["Model"] = relationship(back_populates="provider_models")
    provider: Mapped["Provider"] = relationship(back_populates="provider_models")

    @property
    def pricing_classification(self):
        return classify_pricing(self.cost_input_per_mtok, self.cost_output_per_mtok)


class ProviderModelSnapshot(UUIDPrimaryKeyMixin, Base):
    """Immutable point-in-time copy — Section 24.4 #8, extended AIL.1B.

    Two distinct writers use this one table, distinguished by ``source``:

    * ``EXECUTION_FREEZE`` — ``app.model_resolution.freeze_snapshot``, a
      fresh row created at every Router resolution, never reused, so a
      historical Agent Run's recorded pricing/capability context can never
      silently drift when the live registry is later refreshed.
      ``change_kinds`` is always ``None`` for these rows — they are not part
      of the registry change-history feed.
    * ``CATALOG_REFRESH`` — ``app.services.model_registry_service``, written
      only when a catalog refresh actually detects a difference from the
      previous catalog-refresh snapshot for that ``provider_model_id`` (a
      no-op refresh writes nothing). ``change_kinds`` then lists every
      dimension that changed (``new``/``price``/``context``/``capability``/
      ``status``) — AIL.1B's What's New / price-and-capability history reads
      only these rows.

    ``LEGACY_UNKNOWN`` (the migration backfill default) marks rows written
    before this distinction existed — never retroactively guessed as one or
    the other.
    """

    __tablename__ = "provider_model_snapshots"
    __table_args__ = (
        Index(
            "ix_provider_model_snapshots_provider_model_id_source_snapshotted_at",
            "provider_model_id",
            "source",
            "snapshotted_at",
        ),
    )

    provider_model_id: Mapped[str] = mapped_column(
        ForeignKey("provider_models.id", ondelete="RESTRICT"), nullable=False
    )
    model_id: Mapped[str] = mapped_column(ForeignKey("models.id", ondelete="RESTRICT"), nullable=False)
    provider_id: Mapped[str] = mapped_column(ForeignKey("providers.id", ondelete="RESTRICT"), nullable=False)
    pricing_input_per_mtok: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 8), nullable=True)
    pricing_output_per_mtok: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 8), nullable=True)
    currency: Mapped[str] = mapped_column(String(10), nullable=False, default="USD")
    context_window: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    capability_snapshot: Mapped[Optional[dict]] = mapped_column("capability_snapshot_json", nullable=True)
    snapshotted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    source: Mapped[SnapshotSource] = mapped_column(
        sa_enum(SnapshotSource), nullable=False, default=SnapshotSource.EXECUTION_FREEZE
    )
    # A JSON list of SnapshotChangeKind values (e.g. ["price", "context"]) —
    # a deterministic set representation, never a single opaque "how much
    # changed" score, so a refresh where several dimensions change at once
    # is still fully and exactly described. None for EXECUTION_FREEZE and
    # LEGACY_UNKNOWN rows.
    change_kinds: Mapped[Optional[list]] = mapped_column("change_kinds_json", nullable=True)

    @property
    def pricing_classification(self):
        return classify_pricing(self.pricing_input_per_mtok, self.pricing_output_per_mtok)


class ProviderCatalogRefresh(UUIDPrimaryKeyMixin, Base):
    """Catalog refresh audit record — MA2 addition (Section 13.3).

    Not part of the MA0 baseline (no prior contract covered "what
    happened during the last refresh attempt"); additive only — no
    existing table/column changed. A failed refresh is still recorded
    (with ``error``) so the operator can see it happened without ever
    mutating the last-known-good ``provider_models`` rows.
    """

    __tablename__ = "provider_catalog_refreshes"

    provider_id: Mapped[str] = mapped_column(ForeignKey("providers.id", ondelete="CASCADE"), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[CatalogRefreshStatus] = mapped_column(sa_enum(CatalogRefreshStatus), nullable=False)
    models_discovered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    models_added: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    models_updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    models_unavailable: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[Optional[dict]] = mapped_column("error_json", nullable=True)
