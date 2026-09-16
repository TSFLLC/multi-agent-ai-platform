"""Flight Recorder (``execution_events``) and security ``audit_events``.

Section 22 (Flight Recorder), 20.5 (Security Audit), 24.4 #4. These are two
structurally separate tables with different retention/query audiences —
never merged (spec explicitly calls out this ambiguity in v1.0 and resolves
it in v1.1). Flight Recorder events never store hidden chain-of-thought
(Section 22.5) — only structured ``decision_summary`` text.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import UUIDPrimaryKeyMixin, utcnow


class ExecutionEvent(UUIDPrimaryKeyMixin, Base):
    """The Flight Recorder's append-only storage — Section 24.4 #4.

    ``sequence_number`` is monotonic *per task_run_id*, enforced by the
    unique constraint below — this is what makes SSE resumability
    (Section 25.3's ``last_event_id`` semantics) and ordered replay
    possible. Assigning the next sequence number atomically (e.g.
    ``max(sequence_number) + 1`` within one short transaction) is a
    repository-layer responsibility for a later phase; MA0 only
    establishes the constraint that makes a violation impossible to miss.
    """

    __tablename__ = "execution_events"
    __table_args__ = (
        UniqueConstraint(
            "task_run_id", "sequence_number", name="uq_execution_events_task_run_id_sequence_number"
        ),
    )

    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), nullable=False)
    task_run_id: Mapped[str] = mapped_column(ForeignKey("task_runs.id", ondelete="CASCADE"), nullable=False)
    workflow_run_id: Mapped[Optional[str]] = mapped_column(ForeignKey("workflow_runs.id"), nullable=True)
    workflow_node_run_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("workflow_node_runs.id"), nullable=True
    )
    agent_run_id: Mapped[Optional[str]] = mapped_column(ForeignKey("agent_runs.id"), nullable=True)
    agent_id: Mapped[Optional[str]] = mapped_column(ForeignKey("agents.id"), nullable=True)
    agent_version_id: Mapped[Optional[str]] = mapped_column(ForeignKey("agent_versions.id"), nullable=True)
    model_id: Mapped[Optional[str]] = mapped_column(ForeignKey("models.id"), nullable=True)
    provider_id: Mapped[Optional[str]] = mapped_column(ForeignKey("providers.id"), nullable=True)
    prompt_version_id: Mapped[Optional[str]] = mapped_column(ForeignKey("prompt_versions.id"), nullable=True)
    tool_call_id: Mapped[Optional[str]] = mapped_column(ForeignKey("tool_calls.id"), nullable=True)

    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    # References only, not a relational membership (Section 10.5.5), so a
    # JSON list is the correct representation here.
    artifact_refs: Mapped[Optional[list]] = mapped_column("artifact_refs_json", nullable=True)

    tokens_in: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    tokens_out: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cost: Mapped[Optional[float]] = mapped_column(Numeric(18, 8), nullable=True)
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    decision_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error: Mapped[Optional[dict]] = mapped_column("error_json", nullable=True)

    # Redaction metadata/policy (Owner requirement, Section 6) — e.g. which
    # fields were redacted and under which policy version, never the raw
    # pre-redaction content.
    redaction_policy_version: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    redacted_fields: Mapped[Optional[list]] = mapped_column("redacted_fields_json", nullable=True)

    actor_type: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    actor_user_id: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id"), nullable=True)


class AuditEvent(UUIDPrimaryKeyMixin, Base):
    """Security/compliance log — Section 20.5. Never merged with Flight Recorder."""

    __tablename__ = "audit_events"

    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    actor_user_id: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id"), nullable=True)
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    target_ref: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    detail: Mapped[Optional[dict]] = mapped_column("detail_json", nullable=True)
