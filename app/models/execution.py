"""Model Calls / Tool Calls / durable job queue / idempotency / routing.

Section 6 ("Model calls" contract), 15 (Tool Bus), 24.4 #11/#12/#14.
"""

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.enums import (
    IdempotencyScope,
    IdempotencyStatus,
    JobQueueStatus,
    JobType,
    ModelCallStatus,
    ModelSelectionMode,
    PermissionDecision,
    ToolCallStatus,
)
from app.db.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin, utcnow
from app.db.types import sa_enum


class RouterPolicyVersion(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Versions the Auto-routing scoring policy — Section 24.4 #14.

    ``free_policy`` is the Owner-required FREE_ONLY/PREFER_FREE/ANY
    contract (Section 3); it is not consumed by any router logic in MA0 —
    the Router itself is out of MA0 scope — but the column exists so a
    later phase can wire it without a schema change.
    """

    __tablename__ = "router_policy_versions"

    version: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    scoring_config: Mapped[Optional[dict]] = mapped_column("scoring_config_json", nullable=True)
    free_policy: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")


class ModelRoutingDecision(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Routing reproducibility — Section 24.4 #14."""

    __tablename__ = "model_routing_decisions"

    agent_run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False)
    router_policy_version_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("router_policy_versions.id"), nullable=True
    )
    selection_mode: Mapped[ModelSelectionMode] = mapped_column(sa_enum(ModelSelectionMode), nullable=False)
    eligible_candidates: Mapped[Optional[list]] = mapped_column("eligible_candidates_json", nullable=True)
    selected_provider_model_snapshot_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("provider_model_snapshots.id"), nullable=True
    )
    rationale: Mapped[Optional[str]] = mapped_column(String(2000), nullable=True)


class ModelCall(UUIDPrimaryKeyMixin, Base):
    """Per-inference record — Section 6 "Model calls" contract, 24.2.

    Captures canonical model + provider + provider model id (via the FK
    chain to ``provider_models``/``models``/``providers``), the pricing and
    capability *snapshot* that applied (``provider_model_snapshot_id``,
    never the live row), router policy/version and selection rationale (via
    ``model_routing_decision_id``), token usage, actual-or-estimated cost,
    latency, and status/error information.
    """

    __tablename__ = "model_calls"

    agent_run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False)
    agent_run_attempt_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("agent_run_attempts.id"), nullable=True
    )
    model_id: Mapped[str] = mapped_column(ForeignKey("models.id"), nullable=False)
    provider_id: Mapped[str] = mapped_column(ForeignKey("providers.id"), nullable=False)
    provider_model_id: Mapped[Optional[str]] = mapped_column(ForeignKey("provider_models.id"), nullable=True)
    provider_model_snapshot_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("provider_model_snapshots.id"), nullable=True
    )
    model_routing_decision_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("model_routing_decisions.id"), nullable=True
    )

    tokens_in: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    tokens_out: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cost_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 8), nullable=True)
    cost_currency: Mapped[str] = mapped_column(String(10), nullable=False, default="USD")
    # Acceptance Criterion 4 (Section 32): every cost is either an actual
    # provider-reported figure or explicitly flagged estimated — never
    # presented ambiguously.
    cost_is_estimated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    structured_output_ok: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    status: Mapped[ModelCallStatus] = mapped_column(
        sa_enum(ModelCallStatus),
        nullable=False,
        default=ModelCallStatus.PENDING,
    )
    error: Mapped[Optional[dict]] = mapped_column("error_json", nullable=True)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # FREE/PAID/UNKNOWN for this call is derived from the joined
    # ProviderModelSnapshot's pricing fields (app.domain.pricing) at query
    # time, not stored redundantly on this row.


class ToolCall(UUIDPrimaryKeyMixin, Base):
    """Per-tool-invocation record — Section 15.1, 24.2."""

    __tablename__ = "tool_calls"

    agent_run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False)
    agent_run_attempt_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("agent_run_attempts.id"), nullable=True
    )
    tool_id: Mapped[str] = mapped_column(ForeignKey("tools.id"), nullable=False)
    idempotency_key_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("idempotency_keys.id"), nullable=True
    )

    inputs_ref: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    outputs_ref: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    permission_decision: Mapped[PermissionDecision] = mapped_column(
        sa_enum(PermissionDecision), nullable=False
    )
    status: Mapped[ToolCallStatus] = mapped_column(
        sa_enum(ToolCallStatus),
        nullable=False,
        default=ToolCallStatus.PENDING,
    )
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error: Mapped[Optional[dict]] = mapped_column("error_json", nullable=True)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class IdempotencyKey(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Generalizes API-request and side-effecting-tool dedup — Section 24.4 #11."""

    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    scope: Mapped[IdempotencyScope] = mapped_column(sa_enum(IdempotencyScope), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(120), nullable=False)
    resource_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    result_ref: Mapped[Optional[dict]] = mapped_column("result_ref_json", nullable=True)
    status: Mapped[IdempotencyStatus] = mapped_column(
        sa_enum(IdempotencyStatus),
        nullable=False,
        default=IdempotencyStatus.IN_PROGRESS,
    )


class JobQueue(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Durable local job table — Section 10.5.3, 24.4 #12.

    The lease/heartbeat/fencing-token protocol: a worker claims a row in
    one short transaction (set lease_owner/lease_expires_at, increment
    fencing_token), does the work with no open transaction, then writes
    completion in a second short transaction. A late write from a zombie
    worker using a stale fencing_token must be rejected at commit time by
    the (future) repository layer — MA0 only establishes the columns/index
    this protocol depends on.
    """

    __tablename__ = "job_queue"
    __table_args__ = (UniqueConstraint("job_type", "payload_ref", name="uq_job_queue_job_type_payload_ref"),)

    job_type: Mapped[JobType] = mapped_column(sa_enum(JobType), nullable=False)
    payload_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[JobQueueStatus] = mapped_column(
        sa_enum(JobQueueStatus),
        nullable=False,
        default=JobQueueStatus.PENDING,
        index=True,
    )
    lease_owner: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    fencing_token: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
