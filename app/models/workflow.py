"""Workflow / DAG persistence — Section 16, 24.4 #1/#2.

MA0 establishes structure only — no DAG engine, validator, or executor.
The one exception is the ``repair_loop`` max_iterations bounding rule
(Section 16.3), which is cheap and valuable to enforce as a DB-level
``CHECK`` constraint as defense-in-depth alongside the future publish-time
validator, not a runtime hope.
"""

from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.enums import VersionStatus, WorkflowNodeRunStatus, WorkflowNodeType, WorkflowRunStatus
from app.db.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin, utcnow
from app.db.types import sa_enum

if TYPE_CHECKING:
    from app.models.tasks import AgentRun


class Workflow(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Stable identity — Section 16.4."""

    __tablename__ = "workflows"

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    current_status: Mapped[Optional[VersionStatus]] = mapped_column(sa_enum(VersionStatus), nullable=True)

    versions: Mapped[List["WorkflowVersion"]] = relationship(back_populates="workflow")


class WorkflowVersion(UUIDPrimaryKeyMixin, Base):
    """Immutable once published — same principle as Agent Versions (16.4)."""

    __tablename__ = "workflow_versions"
    __table_args__ = (
        UniqueConstraint("workflow_id", "version", name="uq_workflow_versions_workflow_id_version"),
    )

    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[VersionStatus] = mapped_column(
        sa_enum(VersionStatus),
        nullable=False,
        default=VersionStatus.DRAFT,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    workflow: Mapped["Workflow"] = relationship(back_populates="versions")
    nodes: Mapped[List["WorkflowNode"]] = relationship(back_populates="workflow_version")
    edges: Mapped[List["WorkflowEdge"]] = relationship(back_populates="workflow_version")


class WorkflowNode(UUIDPrimaryKeyMixin, Base):
    """Node definition — Section 16.2."""

    __tablename__ = "workflow_nodes"
    __table_args__ = (
        UniqueConstraint("workflow_version_id", "node_key", name="uq_workflow_nodes_version_id_node_key"),
        # Bare suffix only — see providers.py note on naming_convention.
        CheckConstraint(
            "node_type != 'repair_loop' OR max_iterations IS NOT NULL",
            name="repair_loop_requires_max_iterations",
        ),
    )

    workflow_version_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_versions.id", ondelete="CASCADE"), nullable=False
    )
    node_key: Mapped[str] = mapped_column(String(120), nullable=False)
    node_type: Mapped[WorkflowNodeType] = mapped_column(sa_enum(WorkflowNodeType), nullable=False)
    config: Mapped[Optional[dict]] = mapped_column("config_json", nullable=True)
    max_iterations: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    timeout_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    workflow_version: Mapped["WorkflowVersion"] = relationship(back_populates="nodes")


class WorkflowEdge(UUIDPrimaryKeyMixin, Base):
    """Edge definition — Section 16.1."""

    __tablename__ = "workflow_edges"

    workflow_version_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_versions.id", ondelete="CASCADE"), nullable=False
    )
    from_node_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_nodes.id", ondelete="CASCADE"), nullable=False
    )
    to_node_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_nodes.id", ondelete="CASCADE"), nullable=False
    )
    condition: Mapped[Optional[dict]] = mapped_column("condition_json", nullable=True)

    workflow_version: Mapped["WorkflowVersion"] = relationship(back_populates="edges")


class WorkflowRun(UUIDPrimaryKeyMixin, Base):
    """One durable execution of a Workflow Version — Section 24.4 #1."""

    __tablename__ = "workflow_runs"

    task_run_id: Mapped[str] = mapped_column(
        ForeignKey("task_runs.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    workflow_version_id: Mapped[str] = mapped_column(ForeignKey("workflow_versions.id"), nullable=False)
    status: Mapped[WorkflowRunStatus] = mapped_column(
        sa_enum(WorkflowRunStatus),
        nullable=False,
        default=WorkflowRunStatus.CREATED,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    cancellation_requested_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancellation_requested_by: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id"), nullable=True)

    node_runs: Mapped[List["WorkflowNodeRun"]] = relationship(back_populates="workflow_run")


class WorkflowNodeRun(UUIDPrimaryKeyMixin, Base):
    """One execution instance of one DAG node — Section 24.4 #2.

    A ``repair_loop`` executing N iterations produces N rows with the same
    ``workflow_node_id``, distinguished by ``iteration``.
    """

    __tablename__ = "workflow_node_runs"
    __table_args__ = (
        UniqueConstraint(
            "workflow_run_id",
            "workflow_node_id",
            "iteration",
            name="uq_workflow_node_runs_run_node_iteration",
        ),
    )

    workflow_run_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False
    )
    workflow_node_id: Mapped[str] = mapped_column(ForeignKey("workflow_nodes.id"), nullable=False)
    iteration: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[WorkflowNodeRunStatus] = mapped_column(
        sa_enum(WorkflowNodeRunStatus),
        nullable=False,
        default=WorkflowNodeRunStatus.PENDING,
    )
    agent_run_id: Mapped[Optional[str]] = mapped_column(ForeignKey("agent_runs.id"), nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    output_snapshot_ref: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)

    lease_owner: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    fencing_token: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    workflow_run: Mapped["WorkflowRun"] = relationship(back_populates="node_runs")
    agent_run: Mapped[Optional["AgentRun"]] = relationship(back_populates="workflow_node_run")
