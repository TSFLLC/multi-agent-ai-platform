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
from app.db.enums import (
    AgentRunAttemptStatus,
    AgentRunRole,
    AgentRunStatus,
    ExecutionMode,
    TaskRunStatus,
    TaskStatus,
)
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
    # AIL.3: nullable foundation seam. Runs tagged with an Experiment are
    # excluded from MA8 historical evidence; ordinary runs remain unchanged.
    experiment_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("experiments.id", ondelete="SET NULL"), nullable=True, index=True
    )
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
    # approval rules as they were when this run started. A BUILD_REVIEW
    # run's primary_agent_version_id/reviewer_agent_version_id/
    # max_repair_iterations (MA4) live here too — no new columns needed
    # for what is already, by contract, a point-in-time run configuration.
    config_snapshot: Mapped[Optional[dict]] = mapped_column("config_snapshot_json", nullable=True)
    # MA4: set exactly once, only when a reviewer ACCEPTs a candidate
    # (app.services.review_orchestration_service) — never guessed, never
    # set on repair-limit exhaustion/failure/cancellation, so "is there an
    # approved result" is always a plain NULL check, never inferred from
    # TaskRunStatus alone.
    # use_alter=True: artifacts.agent_run_id -> agent_runs.id and
    # agent_runs.task_run_id -> task_runs.id already exist, so this column
    # would close a 3-table cycle (task_runs -> artifacts -> agent_runs ->
    # task_runs) without it — same reasoning as AgentRun.repair_of_review_id.
    final_artifact_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey(
            "artifacts.id",
            use_alter=True,
            name="fk_task_runs_final_artifact_id_artifacts",
        ),
        nullable=True,
    )

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
    # MA4: which stage of a BUILD_REVIEW execution cycle this run is
    # (PRIMARY/REVIEWER/REPAIR) — NULL for a plain SINGLE_AGENT run
    # (unset, never applicable pre-MA4).
    role: Mapped[Optional[AgentRunRole]] = mapped_column(sa_enum(AgentRunRole), nullable=True)
    # MA4: for a REPAIR run only — the REPAIR_REQUIRED review that caused
    # it, completing the candidate -> review -> repair -> next candidate
    # lineage without duplicating that review's content here.
    # agent_reviews.candidate_agent_run_id/reviewer_agent_run_id already
    # point *at* agent_runs, so this column pointing back would form a
    # genuine table-creation cycle without use_alter=True (Alembic/
    # create_all emit it as a separate ALTER TABLE after both tables
    # exist) — same category of ordering issue Provider/SecretReference's
    # docstring (app.models.providers.Provider) already calls out.
    repair_of_review_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey(
            "agent_reviews.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_agent_runs_repair_of_review_id_agent_reviews",
        ),
        nullable=True,
    )
    # MA4: deterministic pointers (never full content) a REVIEWER/REPAIR
    # run's prompt is built from (app.prompt_builder's extra_context) — so
    # a worker that reclaims this run's job after a crash reconstructs the
    # exact same prompt from durable DB state alone, the same guarantee
    # MA3 already gives a plain SINGLE_AGENT run. NULL/unused for
    # SINGLE_AGENT and PRIMARY runs.
    input_context_json: Mapped[Optional[dict]] = mapped_column(nullable=True)
    # MA5: a per-run model_policy override (same shape as
    # agent_versions.model_policy_json: {"mode": "manual",
    # "manual_provider_model_id": ...} or {"mode": "auto", ...|}) — lets a
    # comparison candidate resolve to a *different* concrete model than
    # its (immutable, shared, published) Agent Version's own model_policy
    # without needing a second Agent Version just to vary the model.
    # app.services.execution_service._resolve_model prefers this over
    # agent_version.model_policy when present; NULL (the default) means
    # "use the Agent Version's own policy, unmodified" — MA3/MA4 behavior
    # is exactly unchanged when this column is never populated.
    model_policy_override_json: Mapped[Optional[dict]] = mapped_column(nullable=True)
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
