"""Comparison Service — MA5 Parallel Comparison orchestration.

Builds Mode 3 (Section 7.3, 18) entirely on MA3's existing execution engine
(Task Run -> Agent Run -> job_queue -> Worker -> AgentExecutionService ->
Provider Adapter) and MA4's Flight-Recorder-anchored patterns -- this module
never invokes a provider itself and never runs a second inference pipeline.
It only ever decides *which* Task Runs/Agent Runs to create and when the
comparison's own bookkeeping state should change.

Lifecycle::

    create_comparison   -- configure 2+ candidates (PENDING, nothing running)
        -> launch_comparison  -- one independent Task Run + Agent Run per
                                  candidate, each its own job_queue row
            -> (workers execute each candidate exactly like any MA3/MA4
                Task Run; app.services.comparison_progress_service narrates
                per-candidate completion and flips the comparison to a
                stored FAILED only if every candidate fails/cancels)
                -> compute_comparison_phase(...) == "ready_for_selection"
                   once every candidate is terminal and >=1 succeeded
                    -> select_canonical -- the ONLY way a comparison ever
                       reaches COMPLETED. The platform never picks a winner
                       itself (Section 18.3 Principle 2/4) -- no Judge
                       Agent, scoring engine, ranking algorithm, or learned
                       router lives anywhere in this module.

Isolation (Section 7.3/21) is structural, not a convention: each candidate
gets its own Task Run and Agent Run, so one candidate's failure, budget
rejection, or cancellation can never affect a sibling's already-recorded
result -- app.services.comparison_progress_service only stores a terminal
FAILED for the comparison once *every* candidate has failed/cancelled.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.enums import (
    AgentRunRole,
    ComparisonRunStatus,
    ExecutionMode,
    JobType,
    TaskRunStatus,
    VersionStatus,
)
from app.errors import ArtifactHashMismatchError, ConflictError, InvalidStateTransitionError, NotFoundError
from app.models.agents import AgentVersion
from app.models.artifacts_eval import Artifact, ComparisonCandidate, ComparisonRun
from app.models.execution import ModelCall
from app.models.identity import Project
from app.models.tasks import AgentRun, Task, TaskRun
from app.repositories.job_queue_repository import JobQueueRepository
from app.services.audit_service import AuditService
from app.services.base import BaseService
from app.services.flight_recorder import FlightRecorderService
from app.services.task_service import TaskService

_TERMINAL_TASK_RUN_STATUSES = {TaskRunStatus.COMPLETED, TaskRunStatus.FAILED, TaskRunStatus.CANCELLED}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ComparisonService(BaseService):
    def __init__(self, db: Session):
        super().__init__(db)
        self._jobs = JobQueueRepository()
        self._recorder = FlightRecorderService(db)

    # -- create: configure candidates, launch nothing yet -------------------

    def create_comparison(
        self,
        *,
        task_id: str,
        candidates: List[Dict[str, Any]],
        budget_id: Optional[str] = None,
        experiment_id: Optional[str] = None,
        experiment_task_position: Optional[int] = None,
        experiment_repetition: Optional[int] = None,
        frozen_task_snapshot: Optional[dict] = None,
    ) -> ComparisonRun:
        """``candidates`` items: ``{"agent_version_id", "label",
        "model_policy_override"?, "review"?}`` where ``review`` (if present)
        is ``{"reviewer_agent_version_id", "max_repair_iterations"?,
        "review_instructions"?}`` -- MA4 review, opted into per candidate,
        never required. Every candidate is validated *before* any row is
        written, matching ``TaskService.start_reviewed_task_run``'s
        all-or-nothing validation discipline."""
        task = self.db.get(Task, task_id)
        if task is None:
            raise NotFoundError(f"Task {task_id} not found.")
        if task.execution_mode != ExecutionMode.PARALLEL_COMPARISON and experiment_id is None:
            raise ConflictError(
                f"Task {task_id} has execution_mode={task.execution_mode.value!r}, not "
                "parallel_comparison -- cannot create a comparison for it."
            )
        if len(candidates) < 2:
            raise ConflictError(
                "A Parallel Comparison requires at least 2 candidates (Section 7.3) -- "
                f"got {len(candidates)}."
            )

        labels = [spec["label"] for spec in candidates]
        if len(set(labels)) != len(labels):
            raise ConflictError("Candidate labels must be unique within one comparison.")

        for spec in candidates:
            self._get_published_agent_version(spec["agent_version_id"])
            review = spec.get("review")
            if review:
                self._get_published_agent_version(review["reviewer_agent_version_id"])
                max_iterations = review.get("max_repair_iterations")
                if max_iterations is not None and max_iterations < 0:
                    raise ConflictError("max_repair_iterations must be zero or greater.")

        bookkeeping_run = TaskRun(
            task_id=task.id,
            experiment_id=experiment_id,
            status=TaskRunStatus.CREATED,
            budget_id=budget_id,
            config_snapshot={
                "task_title": task.title,
                "execution_mode": task.execution_mode.value,
                "candidate_labels": labels,
                "budget_id": budget_id,
                "frozen_task_snapshot": frozen_task_snapshot,
            },
            timeout_seconds=settings.default_task_run_timeout_seconds,
        )
        self.db.add(bookkeeping_run)
        self.db.flush()

        comparison = ComparisonRun(
            task_run_id=bookkeeping_run.id,
            experiment_id=experiment_id,
            experiment_task_position=experiment_task_position,
            experiment_repetition=experiment_repetition,
            status=ComparisonRunStatus.PENDING,
        )
        self.db.add(comparison)
        self.db.flush()

        for spec in candidates:
            self.db.add(
                ComparisonCandidate(
                    comparison_run_id=comparison.id,
                    agent_version_id=spec["agent_version_id"],
                    model_policy_override_json=spec.get("model_policy_override"),
                    review_config_json=spec.get("review"),
                    label=spec["label"],
                )
            )
        self.db.flush()

        self._recorder.record(
            task_id=task.id,
            task_run_id=bookkeeping_run.id,
            event_type="comparison.created",
            decision_summary=f"comparison {comparison.id} created with {len(candidates)} candidates: {labels}",
        )
        self.db.commit()
        self.db.refresh(comparison)
        return comparison

    # -- launch: one independent Task Run + Agent Run per candidate --------

    def launch_comparison(self, comparison_id: str) -> ComparisonRun:
        comparison = self._get_comparison(comparison_id)
        if comparison.status != ComparisonRunStatus.PENDING:
            raise ConflictError(
                f"Comparison {comparison_id} is {comparison.status.value!r} and cannot be launched again."
            )

        bookkeeping_run = self.db.get(TaskRun, comparison.task_run_id)
        if bookkeeping_run is None:
            raise NotFoundError(f"Task Run {comparison.task_run_id} not found.")
        task = self.db.get(Task, bookkeeping_run.task_id)
        if task is None:
            raise NotFoundError(f"Task {bookkeeping_run.task_id} not found.")

        candidates = list(
            self.db.execute(
                select(ComparisonCandidate)
                .where(ComparisonCandidate.comparison_run_id == comparison.id)
                .order_by(ComparisonCandidate.created_at)
            )
            .scalars()
            .all()
        )

        for candidate in candidates:
            agent_version = self._get_published_agent_version(candidate.agent_version_id)
            review = candidate.review_config_json

            candidate_config: Dict[str, Any] = {
                "task_title": task.title,
                "execution_mode": task.execution_mode.value,
                "comparison_run_id": comparison.id,
                "comparison_candidate_id": candidate.id,
                "comparison_candidate_label": candidate.label,
                "agent_version_id": agent_version.id,
                "agent_version_number": agent_version.version,
                "model_policy": agent_version.model_policy,
                "model_policy_override": candidate.model_policy_override_json,
                "budget_id": bookkeeping_run.budget_id,
                "frozen_task_snapshot": (bookkeeping_run.config_snapshot or {}).get("frozen_task_snapshot"),
            }
            if review:
                candidate_config["reviewer_agent_version_id"] = review["reviewer_agent_version_id"]
                candidate_config["max_repair_iterations"] = (
                    review["max_repair_iterations"]
                    if review.get("max_repair_iterations") is not None
                    else settings.default_max_repair_iterations
                )
                candidate_config["review_instructions"] = review.get("review_instructions")

            candidate_run = TaskRun(
                task_id=task.id,
                experiment_id=comparison.experiment_id,
                status=TaskRunStatus.CREATED,
                budget_id=bookkeeping_run.budget_id,
                config_snapshot=candidate_config,
                timeout_seconds=settings.default_task_run_timeout_seconds,
            )
            self.db.add(candidate_run)
            self.db.flush()

            agent_run = AgentRun(
                task_run_id=candidate_run.id,
                agent_version_id=agent_version.id,
                # Tagged PRIMARY exactly like MA4's own BUILD_REVIEW flow so
                # AgentExecutionService._is_build_review / on_agent_run_succeeded
                # route it into ReviewOrchestrationService -- untagged (None)
                # for a plain candidate, identical to a MA3 SINGLE_AGENT run.
                role=AgentRunRole.PRIMARY if review else None,
                model_policy_override_json=candidate.model_policy_override_json,
                timeout_seconds=agent_version.timeout_seconds or settings.default_agent_run_timeout_seconds,
            )
            self.db.add(agent_run)
            self.db.flush()

            candidate.task_run_id = candidate_run.id
            candidate.agent_run_id = agent_run.id
            candidate_run.status = TaskRunStatus.QUEUED
            self.db.commit()

            self._jobs.enqueue(self.db, job_type=JobType.AGENT_RUN, payload_ref=agent_run.id)

            self._recorder.record(
                task_id=task.id,
                task_run_id=candidate_run.id,
                agent_run_id=agent_run.id,
                agent_id=agent_version.agent_id,
                agent_version_id=agent_version.id,
                event_type="task_run.created",
                decision_summary=(
                    f"comparison candidate {candidate.label!r} ({candidate.id}) task_run created "
                    f"using agent_version {agent_version.id}"
                ),
            )
            self._recorder.record(
                task_id=task.id,
                task_run_id=candidate_run.id,
                agent_run_id=agent_run.id,
                event_type="task_run.queued",
                decision_summary="comparison candidate agent_run job enqueued for worker pickup",
            )
            self._recorder.record(
                task_id=task.id,
                task_run_id=bookkeeping_run.id,
                agent_run_id=agent_run.id,
                event_type="comparison.candidate_queued",
                decision_summary=(
                    f"candidate {candidate.label!r} ({candidate.id}) queued as task_run {candidate_run.id} "
                    f"/ agent_run {agent_run.id}"
                ),
            )

        bookkeeping_run.status = TaskRunStatus.RUNNING
        bookkeeping_run.started_at = bookkeeping_run.started_at or _utcnow()
        comparison.status = ComparisonRunStatus.RUNNING
        self.db.commit()

        self._recorder.record(
            task_id=task.id,
            task_run_id=bookkeeping_run.id,
            event_type="comparison.launched",
            decision_summary=f"comparison {comparison.id} launched with {len(candidates)} candidates",
        )
        self.db.refresh(comparison)
        return comparison

    # -- human canonical selection -- the ONLY path to COMPLETED -----------

    def select_canonical(
        self, comparison_id: str, *, candidate_id: str, artifact_hash: str, selected_by: Optional[str]
    ) -> ComparisonRun:
        comparison = self._get_comparison(comparison_id)
        if comparison.status == ComparisonRunStatus.COMPLETED:
            raise ConflictError(f"Comparison {comparison_id} already has a canonical selection.")
        if comparison.status in (ComparisonRunStatus.FAILED, ComparisonRunStatus.CANCELLED):
            raise InvalidStateTransitionError(
                f"Comparison {comparison_id} is {comparison.status.value!r} -- there is nothing left to select."
            )

        phase = compute_comparison_phase(self.db, comparison)
        if phase != "ready_for_selection":
            raise InvalidStateTransitionError(
                f"Comparison {comparison_id} is not ready for selection (phase={phase!r}) -- every "
                "candidate must be terminal, with at least one successful, before a human can select one."
            )

        candidate = self.db.get(ComparisonCandidate, candidate_id)
        if candidate is None or candidate.comparison_run_id != comparison_id:
            raise NotFoundError(
                f"Comparison candidate {candidate_id} not found in comparison {comparison_id}."
            )

        candidate_run = self.db.get(TaskRun, candidate.task_run_id) if candidate.task_run_id else None
        if candidate_run is None or candidate_run.status != TaskRunStatus.COMPLETED:
            raise ConflictError(
                f"Comparison candidate {candidate_id} did not complete successfully and cannot be selected."
            )

        artifact = _candidate_artifact(self.db, candidate, candidate_run)
        if artifact is None:
            raise ConflictError(f"Comparison candidate {candidate_id} produced no artifact to select.")
        if artifact.content_hash != artifact_hash:
            raise ArtifactHashMismatchError(
                "The provided artifact_hash no longer matches this candidate's actual artifact content -- "
                "refusing to canonicalize a different result than was reviewed.",
                detail={"expected_artifact_hash": artifact.content_hash},
            )

        candidate.is_winner = True
        comparison.winner_agent_run_id = candidate.agent_run_id
        comparison.winner_artifact_id = artifact.id
        comparison.winner_artifact_hash = artifact.content_hash
        comparison.status = ComparisonRunStatus.COMPLETED
        comparison.completed_at = _utcnow()

        bookkeeping_run = self.db.get(TaskRun, comparison.task_run_id)
        if bookkeeping_run is not None:
            bookkeeping_run.status = TaskRunStatus.COMPLETED
            bookkeeping_run.ended_at = _utcnow()
            bookkeeping_run.final_artifact_id = artifact.id
        self.db.commit()

        self._recorder.record(
            task_id=bookkeeping_run.task_id if bookkeeping_run else candidate_run.task_id,
            task_run_id=comparison.task_run_id,
            agent_run_id=candidate.agent_run_id,
            artifact_refs=[artifact.id],
            event_type="comparison.canonical_selected",
            decision_summary=(
                f"candidate {candidate.label!r} ({candidate.id}) selected as canonical result; "
                f"artifact={artifact.id} hash={artifact.content_hash}"
            ),
        )

        task = self.db.get(Task, bookkeeping_run.task_id) if bookkeeping_run else None
        project = self.db.get(Project, task.project_id) if task is not None else None
        if project is not None:
            AuditService(self.db).record(
                org_id=project.org_id,
                event_type="comparison.canonical_candidate_selected",
                actor_user_id=selected_by,
                target_ref=comparison.id,
                detail={
                    "comparison_candidate_id": candidate.id,
                    "agent_run_id": candidate.agent_run_id,
                    "artifact_id": artifact.id,
                    "artifact_hash": artifact.content_hash,
                },
            )

        self.db.refresh(comparison)
        return comparison

    # -- cancellation --------------------------------------------------------

    def cancel_comparison(self, comparison_id: str, *, requested_by: Optional[str]) -> ComparisonRun:
        """Cooperative, like every other cancellation in this platform
        (Section 26.7): requests propagate immediately to every in-flight
        candidate's own Task Run cancellation, but the comparison itself
        only reaches the stored CANCELLED status once
        app.services.comparison_progress_service observes every candidate
        has actually stopped."""
        comparison = self._get_comparison(comparison_id)
        if comparison.status in (
            ComparisonRunStatus.COMPLETED,
            ComparisonRunStatus.FAILED,
            ComparisonRunStatus.CANCELLED,
        ):
            raise InvalidStateTransitionError(
                f"Comparison {comparison_id} is already {comparison.status.value!r} and cannot be cancelled."
            )
        if comparison.cancellation_requested_at is not None:
            raise InvalidStateTransitionError(
                f"Comparison {comparison_id} cancellation was already requested."
            )

        comparison.cancellation_requested_at = _utcnow()
        bookkeeping_run = self.db.get(TaskRun, comparison.task_run_id)
        if bookkeeping_run is None:
            # comparison_runs.task_run_id is NOT NULL and CASCADE-deletes
            # with its Task Run, so a comparison row existing without its
            # own bookkeeping run is a genuine data-integrity violation,
            # never an expected/silent case.
            raise NotFoundError(f"Task Run {comparison.task_run_id} not found.")

        if comparison.status == ComparisonRunStatus.PENDING:
            # Nothing was ever launched -- no cooperative wait is needed.
            comparison.status = ComparisonRunStatus.CANCELLED
            comparison.completed_at = _utcnow()
            bookkeeping_run.status = TaskRunStatus.CANCELLED
            bookkeeping_run.ended_at = _utcnow()
            self.db.commit()
            self._recorder.record(
                task_id=bookkeeping_run.task_id,
                task_run_id=comparison.task_run_id,
                event_type="comparison.cancelled",
                decision_summary="comparison cancelled before launch; no candidates were running",
            )
            self.db.refresh(comparison)
            return comparison

        self.db.commit()

        task_service = TaskService(self.db)
        candidates = list(
            self.db.execute(
                select(ComparisonCandidate).where(ComparisonCandidate.comparison_run_id == comparison.id)
            )
            .scalars()
            .all()
        )
        for candidate in candidates:
            if candidate.task_run_id is None:
                continue
            try:
                task_service.cancel_task_run(candidate.task_run_id, requested_by=requested_by)
            except InvalidStateTransitionError:
                continue  # already terminal or already cancelling -- nothing to do

        self._recorder.record(
            task_id=bookkeeping_run.task_id,
            task_run_id=comparison.task_run_id,
            event_type="comparison.cancellation_requested",
            decision_summary="cooperative cancellation requested for every in-flight candidate",
        )
        self.db.refresh(comparison)
        return comparison

    # -- helpers -------------------------------------------------------------

    def _get_comparison(self, comparison_id: str) -> ComparisonRun:
        comparison = self.db.get(ComparisonRun, comparison_id)
        if comparison is None:
            raise NotFoundError(f"Comparison {comparison_id} not found.")
        return comparison

    def _get_published_agent_version(self, agent_version_id: str) -> AgentVersion:
        agent_version = self.db.get(AgentVersion, agent_version_id)
        if agent_version is None:
            raise NotFoundError(f"Agent Version {agent_version_id} not found.")
        if agent_version.status != VersionStatus.ACTIVE:
            raise ConflictError(
                f"Agent Version {agent_version_id} is not published (status="
                f"{agent_version.status.value!r}) and cannot be used in a comparison."
            )
        return agent_version


# -- read-side: derived phase + consolidated detail, shared by API/scripts --


def _candidate_artifact(
    db: Session, candidate: ComparisonCandidate, candidate_run: TaskRun
) -> Optional[Artifact]:
    """The exact artifact this candidate produced. A BUILD_REVIEW-configured
    candidate's accepted artifact lives on ``final_artifact_id`` (set once,
    only on reviewer ACCEPT); a plain candidate's is its own Agent Run's
    single Artifact row -- resolved even for a FAILED/CANCELLED candidate
    with a partial result, so no execution evidence is ever discarded."""
    if candidate_run.final_artifact_id:
        return db.get(Artifact, candidate_run.final_artifact_id)
    if candidate.agent_run_id is None:
        return None
    stmt = (
        select(Artifact)
        .where(Artifact.agent_run_id == candidate.agent_run_id)
        .order_by(Artifact.created_at.desc())
    )
    return db.execute(stmt).scalars().first()


def compute_comparison_phase(db: Session, comparison: ComparisonRun) -> str:
    """Returns "pending" | "running" | "ready_for_selection" |
    "completed" | "failed" | "cancelled" -- a derived, read-time fact, never
    stored (app.db.enums.ComparisonRunStatus's own docstring: "map to
    existing names, don't invent unnecessary statuses"). "ready_for_selection"
    is the only phase without a literal ComparisonRunStatus counterpart:
    the platform never auto-transitions into it as a stored status because
    doing so would still just be RUNNING waiting on the human (Section
    18.3)."""
    if comparison.status == ComparisonRunStatus.PENDING:
        return "pending"
    if comparison.status == ComparisonRunStatus.COMPLETED:
        return "completed"
    if comparison.status == ComparisonRunStatus.FAILED:
        return "failed"
    if comparison.status == ComparisonRunStatus.CANCELLED:
        return "cancelled"

    candidates = list(
        db.execute(select(ComparisonCandidate).where(ComparisonCandidate.comparison_run_id == comparison.id))
        .scalars()
        .all()
    )
    if not candidates:
        return "running"

    task_runs: List[TaskRun] = []
    for candidate in candidates:
        if candidate.task_run_id is None:
            return "running"
        task_run = db.get(TaskRun, candidate.task_run_id)
        if task_run is None or task_run.status not in _TERMINAL_TASK_RUN_STATUSES:
            return "running"
        task_runs.append(task_run)

    if any(tr.status == TaskRunStatus.COMPLETED for tr in task_runs):
        return "ready_for_selection"
    return "failed"


@dataclass
class CandidateUsage:
    tokens_in: int = 0
    tokens_out: int = 0
    cost_amount: Decimal = field(default_factory=lambda: Decimal(0))
    latency_ms: int = 0


@dataclass
class CandidateDetail:
    candidate: ComparisonCandidate
    status: str
    model_id: Optional[str]
    provider_id: Optional[str]
    artifact: Optional[Artifact]
    usage: CandidateUsage


@dataclass
class ComparisonDetail:
    comparison: ComparisonRun
    phase: str
    candidates: List[CandidateDetail]
    total_tokens_in: int
    total_tokens_out: int
    total_cost: Decimal


def get_comparison_detail(db: Session, comparison_id: str) -> ComparisonDetail:
    """One consolidated read: per-candidate model/artifact/status/tokens/
    cost/latency plus comparison-level aggregate tokens/cost -- usage is
    summed across *every* Agent Run under a candidate's own Task Run (not
    just its primary Agent Run), so an MA4-reviewed candidate's reviewer/
    repair cost is never silently dropped from its total."""
    comparison = db.get(ComparisonRun, comparison_id)
    if comparison is None:
        raise NotFoundError(f"Comparison {comparison_id} not found.")

    candidates = list(
        db.execute(
            select(ComparisonCandidate)
            .where(ComparisonCandidate.comparison_run_id == comparison.id)
            .order_by(ComparisonCandidate.created_at)
        )
        .scalars()
        .all()
    )

    phase = compute_comparison_phase(db, comparison)

    details: List[CandidateDetail] = []
    total_tokens_in = 0
    total_tokens_out = 0
    total_cost = Decimal(0)

    for candidate in candidates:
        candidate_run = db.get(TaskRun, candidate.task_run_id) if candidate.task_run_id else None
        status = candidate_run.status.value if candidate_run is not None else "not_launched"

        primary_agent_run = db.get(AgentRun, candidate.agent_run_id) if candidate.agent_run_id else None
        model_id = primary_agent_run.model_id if primary_agent_run else None
        provider_id = primary_agent_run.provider_id if primary_agent_run else None

        usage = CandidateUsage()
        artifact: Optional[Artifact] = None
        if candidate_run is not None:
            agent_run_ids = [
                row[0]
                for row in db.execute(
                    select(AgentRun.id).where(AgentRun.task_run_id == candidate_run.id)
                ).all()
            ]
            if agent_run_ids:
                calls = list(
                    db.execute(select(ModelCall).where(ModelCall.agent_run_id.in_(agent_run_ids)))
                    .scalars()
                    .all()
                )
                usage.tokens_in = sum(c.tokens_in or 0 for c in calls)
                usage.tokens_out = sum(c.tokens_out or 0 for c in calls)
                usage.cost_amount = sum((c.cost_amount or Decimal(0) for c in calls), Decimal(0))
                usage.latency_ms = sum(c.latency_ms or 0 for c in calls)

            artifact = _candidate_artifact(db, candidate, candidate_run)

        total_tokens_in += usage.tokens_in
        total_tokens_out += usage.tokens_out
        total_cost += usage.cost_amount

        details.append(
            CandidateDetail(
                candidate=candidate,
                status=status,
                model_id=model_id,
                provider_id=provider_id,
                artifact=artifact,
                usage=usage,
            )
        )

    return ComparisonDetail(
        comparison=comparison,
        phase=phase,
        candidates=details,
        total_tokens_in=total_tokens_in,
        total_tokens_out=total_tokens_out,
        total_cost=total_cost,
    )
