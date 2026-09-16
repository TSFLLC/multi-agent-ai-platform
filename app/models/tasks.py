"""Task / Task Run / Agent Run / Agent Run Attempt — Section 24.2, 24.4 #9/#10/#12/#17.

Task definition and execution state are kept structurally separate
(Section 26.1/26.7 #20): ``tasks.status`` is template/lifecycle-only;
*all* execution status lives on ``task_runs``/``agent_runs``. A Task may
have any number of Task Runs, including concurrent ones.
"""

from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.enums import AgentRunAttemptStatus, AgentRunStatus, ExecutionMode, TaskRunStatus, TaskStatus
from app.db.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin, utcnow
from app.db.types import sa_enum

if TYPE_CHECKING:
    from app.models.workflow import WorkflowNodeRun


class Task(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Durable, reusable template/definition object — Section 26.1."""

    __tablename__ = "tasks"

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    requirements: Mapped[Optional[dict]] = mapped_column("requirements_json", nullable=True)
    execution_mode: Mapped[ExecutionMode] = mapped_column(sa_enum(ExecutionMode), nullable=False)
    created_by: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id"), nullable=True)
    status: Mapped[TaskStatus] = mapped_column(sa_enum(TaskStatus), nullable=False, default=TaskStatus.DRAFT)

    runs: Mapped[List["TaskRun"]] = relationship(back_populates="task")


class TaskRun(UUIDPrimaryKeyMixin, Base):
    """One execution attempt of a Task — Section 24.2, 26.2."""

    __tablename__ = "task_runs"

    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    workflow_version_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("workflow_versions.id"), nullable=True
    )
    status: Mapped[TaskRunStatus] = mapped_column(
        sa_enum(TaskRunStatus),
        nullable=False,
        default=TaskRunStatus.CREATED,
    )
    budget_id: Mapped[Optional[str]] = mapped_column(ForeignKey("budgets.id"), nullable=True)
    # Section 24.4 #9 — point-in-time copy of the Task's requirements,
    # execution mode, agent/model selection, tool grants, budget refs, and
    # approval rules as they were when this run started.
    config_snapshot: Mapped[Optional[dict]] = mapped_column("config_snapshot_json", nullable=True)

    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=90 * 60)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    cancellation_requested_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancellation_requested_by: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id"), nullable=True)

    task: Mapped["Task"] = relationship(back_populates="runs")
    agent_runs: Mapped[List["AgentRun"]] = relationship(back_populates="task_run")
    source_snapshot: Mapped[Optional["SourceSnapshot"]] = relationship(
        back_populates="task_run", uselist=False
    )


class AgentRun(UUIDPrimaryKeyMixin, Base):
    """The execution unit — Section 24.2, 26.3.

    Carries the same lease/heartbeat/fencing columns as ``job_queue``
    (Section 24.4 #12) at its own granularity, and
    ``provider_model_snapshot_id`` (Section 24.4 #8) binding it to the
    immutable pricing/capability context that applied at Router resolution
    time — never the live, mutable registry row.
    """

    __tablename__ = "agent_runs"

    task_run_id: Mapped[str] = mapped_column(ForeignKey("task_runs.id", ondelete="CASCADE"), nullable=False)
    agent_version_id: Mapped[str] = mapped_column(ForeignKey("agent_versions.id"), nullable=False)
    # Reverse direction only: a Workflow Node Run (Mode 4) points at the
    # Agent Run it drove via workflow_node_runs.agent_run_id (Section 24.4
    # #1-2). No column here — a single source of truth avoids the two FKs
    # ever disagreeing.

    model_id: Mapped[Optional[str]] = mapped_column(ForeignKey("models.id"), nullable=True)
    provider_id: Mapped[Optional[str]] = mapped_column(ForeignKey("providers.id"), nullable=True)
    provider_model_snapshot_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("provider_model_snapshots.id"), nullable=True
    )

    status: Mapped[AgentRunStatus] = mapped_column(
        sa_enum(AgentRunStatus),
        nullable=False,
        default=AgentRunStatus.CREATED,
    )
    sandbox_ref: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=30 * 60)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    cancellation_requested_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancellation_requested_by: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id"), nullable=True)

    lease_owner: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    fencing_token: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    task_run: Mapped["TaskRun"] = relationship(back_populates="agent_runs")
    attempts: Mapped[List["AgentRunAttempt"]] = relationship(back_populates="agent_run")
    workflow_node_run: Mapped[Optional["WorkflowNodeRun"]] = relationship(
        back_populates="agent_run", uselist=False
    )


class AgentRunAttempt(UUIDPrimaryKeyMixin, Base):
    """Durable per-attempt record — Section 24.4 #10.

    A retried Agent Run gets a *new row here* (never a mutation of the
    prior attempt), so a run that failed then succeeded on attempt 2 never
    collapses into one ambiguous cost/evaluation record.
    """

    __tablename__ = "agent_run_attempts"

    agent_run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[AgentRunAttemptStatus] = mapped_column(
        sa_enum(AgentRunAttemptStatus),
        nullable=False,
        default=AgentRunAttemptStatus.RUNNING,
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[Optional[dict]] = mapped_column("error_json", nullable=True)
    worker_id: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)

    agent_run: Mapped["AgentRun"] = relationship(back_populates="attempts")

    __table_args__ = (
        UniqueConstraint(
            "agent_run_id", "attempt_number", name="uq_agent_run_attempts_run_id_attempt_number"
        ),
    )


class SourceSnapshot(UUIDPrimaryKeyMixin, Base):
    """Repo/base-commit anchoring for coding tasks — Section 24.4 #17."""

    __tablename__ = "source_snapshots"

    task_run_id: Mapped[str] = mapped_column(
        ForeignKey("task_runs.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    repo_url_or_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    base_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    base_commit_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    working_branch_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    task_run: Mapped["TaskRun"] = relationship(back_populates="source_snapshot")
