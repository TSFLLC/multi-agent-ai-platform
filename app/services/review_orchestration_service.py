"""Agent-to-Agent Review orchestration — MA4.

The thin seam between MA3's AgentExecutionService (which knows how to run
*one* Agent Run to completion and nothing about "review") and a
BUILD_REVIEW Task Run's multi-stage cycle:

    primary AgentRun (produces candidate)
        -> reviewer AgentRun (produces a structured review of it)
            -> ACCEPT: Task Run COMPLETED, final_artifact_id set
            -> REPAIR_REQUIRED (iterations remain): repair AgentRun queued
                -> reviewer AgentRun again, iteration + 1
            -> REPAIR_REQUIRED (iterations exhausted): Task Run FAILED,
               category=repair_limit_exhausted -- never falsely marked
               accepted
            -> INVALID (malformed/unparseable reviewer output): Task Run
               FAILED, category=reviewer_response_invalid -- never
               inferred as ACCEPT

Every stage is dispatched through the exact same job_queue/Worker/
AgentExecutionService machinery MA3 already built (Section: "do not build
a second inference pipeline") -- this module only ever decides *which*
AgentRun to create next, never invokes a provider itself. Budget
enforcement, cancellation checkpoints, lease/fencing, and retries are all
inherited for free because every stage is a normal Agent Run.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.enums import AgentRunRole, JobType, ReviewDecision, TaskRunStatus
from app.models.agents import AgentVersion
from app.models.artifacts_eval import Artifact
from app.models.execution import ModelCall
from app.models.reviews import AgentReview
from app.models.tasks import AgentRun, TaskRun
from app.repositories.job_queue_repository import JobQueueRepository
from app.review_contract import parse_review_response
from app.services.flight_recorder import FlightRecorderService

logger = logging.getLogger("app.services.review_orchestration_service")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ReviewOrchestrationService:
    def __init__(self, db: Session):
        self.db = db
        self.recorder = FlightRecorderService(db)
        self._jobs = JobQueueRepository()

    # -- entry point, called by AgentExecutionService right after a --------
    # -- BUILD_REVIEW-mode Agent Run's attempt succeeds ---------------------

    def on_agent_run_succeeded(self, ctx: Any, agent_run: AgentRun, artifact: Artifact) -> None:
        role = agent_run.role
        if role in (AgentRunRole.PRIMARY, AgentRunRole.REPAIR):
            self._queue_reviewer(ctx, agent_run, artifact)
        elif role == AgentRunRole.REVIEWER:
            self._process_review(ctx, agent_run, artifact)
        else:
            # Every BUILD_REVIEW Agent Run must be tagged at creation time
            # (start_reviewed_task_run / this service) -- an untagged run
            # here is a contract violation, not a state to silently
            # complete or hang on.
            raise RuntimeError(f"AgentRun {agent_run.id} has no role in a BUILD_REVIEW Task Run.")

    # -- primary/repair succeeded: queue the reviewer -----------------------

    def _queue_reviewer(self, ctx: Any, candidate_agent_run: AgentRun, candidate_artifact: Artifact) -> None:
        task_run = ctx.task_run
        self.db.refresh(task_run)
        if task_run.cancellation_requested_at is not None:
            self._finalize_cancelled(
                ctx, decision_summary="task run cancelled before the reviewer could be queued"
            )
            return

        config = task_run.config_snapshot or {}
        reviewer_agent_version_id = config["reviewer_agent_version_id"]
        reviewer_agent_version = self.db.get(AgentVersion, reviewer_agent_version_id)
        if reviewer_agent_version is None:
            self._finalize_failed(
                ctx,
                category="model_resolution_error",
                message=f"reviewer Agent Version {reviewer_agent_version_id} no longer exists.",
            )
            return

        iteration_number = (candidate_agent_run.input_context_json or {}).get("iteration_number", 0)

        reviewer_run = AgentRun(
            task_run_id=task_run.id,
            agent_version_id=reviewer_agent_version_id,
            role=AgentRunRole.REVIEWER,
            timeout_seconds=(
                reviewer_agent_version.timeout_seconds or settings.default_agent_run_timeout_seconds
            ),
            input_context_json={
                "kind": "review_request",
                "candidate_agent_run_id": candidate_agent_run.id,
                "candidate_artifact_id": candidate_artifact.id,
                "candidate_artifact_hash": candidate_artifact.content_hash,
                "iteration_number": iteration_number,
                "review_instructions": config.get("review_instructions"),
            },
        )
        self.db.add(reviewer_run)
        self.db.commit()
        self.db.refresh(reviewer_run)

        self._jobs.enqueue(self.db, job_type=JobType.AGENT_RUN, payload_ref=reviewer_run.id)

        task_run.status = TaskRunStatus.WAITING_FOR_AGENT
        self.db.commit()

        self._event(
            ctx,
            "review.queued",
            agent_run_id=reviewer_run.id,
            agent_id=reviewer_agent_version.agent_id,
            agent_version_id=reviewer_agent_version.id,
            artifact_refs=[candidate_artifact.id],
            decision_summary=(
                f"reviewer agent_run {reviewer_run.id} queued for candidate artifact "
                f"{candidate_artifact.id} (iteration {iteration_number})"
            ),
        )

    # -- reviewer succeeded: parse its output and decide -----------------

    def _process_review(self, ctx: Any, reviewer_agent_run: AgentRun, reviewer_artifact: Artifact) -> None:
        task_run = ctx.task_run
        input_ctx = reviewer_agent_run.input_context_json or {}
        candidate_agent_run_id = input_ctx.get("candidate_agent_run_id")
        candidate_artifact_id = input_ctx.get("candidate_artifact_id")
        expected_hash = input_ctx.get("candidate_artifact_hash")
        iteration_number = input_ctx.get("iteration_number", 0)

        raw_text = Path(reviewer_artifact.storage_ref).read_text(encoding="utf-8")
        parsed = parse_review_response(raw_text)

        candidate_artifact = self.db.get(Artifact, candidate_artifact_id) if candidate_artifact_id else None
        hash_matches = candidate_artifact is not None and candidate_artifact.content_hash == expected_hash

        parse_error: Optional[str]
        if not hash_matches:
            decision = ReviewDecision.INVALID
            parse_error = (
                "candidate_artifact_hash no longer matches the reviewed artifact -- stale review refused."
            )
        elif parsed.decision is None:
            decision = ReviewDecision.INVALID
            parse_error = parsed.parse_error
        else:
            decision = parsed.decision
            parse_error = None

        review = AgentReview(
            task_run_id=task_run.id,
            candidate_agent_run_id=candidate_agent_run_id,
            candidate_artifact_id=candidate_artifact_id,
            candidate_artifact_hash=expected_hash,
            reviewer_agent_run_id=reviewer_agent_run.id,
            reviewer_agent_version_id=reviewer_agent_run.agent_version_id,
            reviewer_provider_model_snapshot_id=reviewer_agent_run.provider_model_snapshot_id,
            iteration_number=iteration_number,
            decision=decision,
            summary=parsed.summary,
            issues=parsed.issues,
            repair_instructions=parsed.repair_instructions,
            parse_error=parse_error,
        )
        self.db.add(review)
        self.db.commit()
        self.db.refresh(review)

        self._event(
            ctx,
            f"review.decision_{decision.value}",
            agent_run_id=reviewer_agent_run.id,
            artifact_refs=[reviewer_artifact.id],
            decision_summary=f"review {review.id} decision={decision.value} iteration={iteration_number}",
            error={"category": "reviewer_response_invalid", "message": parse_error} if parse_error else None,
        )

        if decision == ReviewDecision.ACCEPT:
            task_run.final_artifact_id = candidate_artifact_id
            self.db.commit()
            self._event(
                ctx,
                "final_artifact.selected",
                artifact_refs=[candidate_artifact_id],
                decision_summary=f"final artifact {candidate_artifact_id} accepted at iteration {iteration_number}",
            )
            self._finalize_completed(ctx)
            return

        if decision == ReviewDecision.INVALID:
            self._finalize_failed(
                ctx,
                category="reviewer_response_invalid",
                message=parse_error or "reviewer output did not match the required review contract.",
            )
            return

        # REPAIR_REQUIRED
        max_iterations = (task_run.config_snapshot or {}).get(
            "max_repair_iterations", settings.default_max_repair_iterations
        )
        if iteration_number >= max_iterations:
            self._event(
                ctx,
                "repair_limit.reached",
                decision_summary=f"repair limit ({max_iterations}) reached without an ACCEPT decision",
            )
            self._finalize_failed(
                ctx,
                category="repair_limit_exhausted",
                message=(
                    f"reviewer requested repair but the configured repair limit "
                    f"({max_iterations}) was already reached; no artifact was approved."
                ),
            )
            return

        self.db.refresh(task_run)
        if task_run.cancellation_requested_at is not None:
            self._finalize_cancelled(
                ctx, decision_summary="task run cancelled before the repair could be queued"
            )
            return

        # REPAIR_REQUIRED only reaches here via the `else` branch above,
        # which is only taken when hash_matches was True -- so
        # candidate_artifact is guaranteed non-None; asserted for mypy and
        # as a defensive runtime invariant check.
        assert candidate_artifact is not None
        self._queue_repair(ctx, review, candidate_artifact)

    # -- REPAIR_REQUIRED with iterations remaining: queue the repair ------

    def _queue_repair(self, ctx: Any, review: AgentReview, previous_artifact: Artifact) -> None:
        task_run = ctx.task_run
        config = task_run.config_snapshot or {}
        primary_agent_version_id = config["primary_agent_version_id"]
        primary_agent_version = self.db.get(AgentVersion, primary_agent_version_id)
        if primary_agent_version is None:
            self._finalize_failed(
                ctx,
                category="model_resolution_error",
                message=f"primary Agent Version {primary_agent_version_id} no longer exists.",
            )
            return

        next_iteration = review.iteration_number + 1

        repair_run = AgentRun(
            task_run_id=task_run.id,
            agent_version_id=primary_agent_version_id,
            role=AgentRunRole.REPAIR,
            repair_of_review_id=review.id,
            timeout_seconds=(
                primary_agent_version.timeout_seconds or settings.default_agent_run_timeout_seconds
            ),
            input_context_json={
                "kind": "repair_request",
                "previous_artifact_id": previous_artifact.id,
                "issues": review.issues,
                "repair_instructions": review.repair_instructions,
                "iteration_number": next_iteration,
            },
        )
        self.db.add(repair_run)
        self.db.commit()
        self.db.refresh(repair_run)

        self._jobs.enqueue(self.db, job_type=JobType.AGENT_RUN, payload_ref=repair_run.id)

        task_run.status = TaskRunStatus.WAITING_FOR_AGENT
        self.db.commit()

        self._event(
            ctx,
            "repair.queued",
            agent_run_id=repair_run.id,
            agent_id=primary_agent_version.agent_id,
            agent_version_id=primary_agent_version.id,
            decision_summary=(
                f"repair agent_run {repair_run.id} queued (iteration {next_iteration}) from review {review.id}"
            ),
        )

    # -- Task Run terminal states (this orchestrator's own AgentRun for --
    # -- this stage is already terminal -- these finalize the Task Run) ---

    def _finalize_completed(self, ctx: Any) -> None:
        task_run = ctx.task_run
        task_run.status = TaskRunStatus.COMPLETED
        task_run.ended_at = _utcnow()
        self.db.commit()
        self._event(
            ctx,
            "task_run.completed",
            decision_summary="task run completed: reviewer accepted the candidate artifact",
        )

    def _finalize_failed(self, ctx: Any, *, category: str, message: str) -> None:
        task_run = ctx.task_run
        task_run.status = TaskRunStatus.FAILED
        task_run.ended_at = _utcnow()
        self.db.commit()
        self._event(
            ctx, "task_run.failed", error={"category": category, "message": message}, decision_summary=message
        )

    def _finalize_cancelled(self, ctx: Any, *, decision_summary: str) -> None:
        task_run = ctx.task_run
        task_run.status = TaskRunStatus.CANCELLED
        task_run.ended_at = _utcnow()
        self.db.commit()
        self._event(ctx, "task_run.cancelled", decision_summary=decision_summary)

    # -- Flight Recorder helper (mirrors AgentExecutionService._event) ----

    def _event(self, ctx: Any, event_type: str, **fields: Any) -> None:
        fields = {k: v for k, v in fields.items() if v is not None}
        fields.setdefault("agent_id", ctx.agent_version.agent_id)
        fields.setdefault("agent_version_id", ctx.agent_version.id)
        self.recorder.record(
            task_id=ctx.task.id,
            task_run_id=ctx.task_run.id,
            event_type=event_type,
            **fields,
        )


# -- read-side: review-cycle summary, shared by the API and reporting -------


@dataclass
class UsageTotal:
    agent_run_id: str
    role: Optional[str]
    tokens_in: int
    tokens_out: int
    cost_amount: Decimal


@dataclass
class ReviewSummary:
    task_run: TaskRun
    outcome: str
    primary_agent_version_id: Optional[str]
    reviewer_agent_version_id: Optional[str]
    max_repair_iterations: Optional[int]
    agent_runs: List[AgentRun]
    reviews: List[AgentReview]
    final_artifact: Optional[Artifact]
    usage: List[UsageTotal]
    total_cost: Decimal


def get_review_summary(db: Session, task_run_id: str) -> Optional[ReviewSummary]:
    task_run = db.get(TaskRun, task_run_id)
    if task_run is None:
        return None

    config = task_run.config_snapshot or {}
    agent_runs = list(
        db.execute(select(AgentRun).where(AgentRun.task_run_id == task_run_id).order_by(AgentRun.created_at))
        .scalars()
        .all()
    )
    reviews = list(
        db.execute(
            select(AgentReview)
            .where(AgentReview.task_run_id == task_run_id)
            .order_by(AgentReview.iteration_number)
        )
        .scalars()
        .all()
    )
    final_artifact = db.get(Artifact, task_run.final_artifact_id) if task_run.final_artifact_id else None

    usage: List[UsageTotal] = []
    total_cost = Decimal(0)
    agent_run_roles = {run.id: (run.role.value if run.role else None) for run in agent_runs}
    for agent_run in agent_runs:
        calls = list(
            db.execute(select(ModelCall).where(ModelCall.agent_run_id == agent_run.id)).scalars().all()
        )
        tokens_in = sum(c.tokens_in or 0 for c in calls)
        tokens_out = sum(c.tokens_out or 0 for c in calls)
        cost = sum((c.cost_amount or Decimal(0) for c in calls), Decimal(0))
        total_cost += cost
        if calls:
            usage.append(
                UsageTotal(
                    agent_run_id=agent_run.id,
                    role=agent_run_roles[agent_run.id],
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    cost_amount=cost,
                )
            )

    outcome = _compute_outcome(task_run, reviews)

    return ReviewSummary(
        task_run=task_run,
        outcome=outcome,
        primary_agent_version_id=config.get("primary_agent_version_id"),
        reviewer_agent_version_id=config.get("reviewer_agent_version_id"),
        max_repair_iterations=config.get("max_repair_iterations"),
        agent_runs=agent_runs,
        reviews=reviews,
        final_artifact=final_artifact,
        usage=usage,
        total_cost=total_cost,
    )


def _compute_outcome(task_run: TaskRun, reviews: List[AgentReview]) -> str:
    """Derived purely from durable state (never from Flight Recorder
    events, which remain an audit trail, not a state source) -- so the API
    always distinguishes an accepted result from a repair-limit-exhausted,
    execution-failure, or cancelled outcome (Section: MA4 repair-limit
    behavior)."""
    if task_run.status == TaskRunStatus.COMPLETED and task_run.final_artifact_id is not None:
        return "accepted"
    if task_run.status == TaskRunStatus.CANCELLED:
        return "cancelled"
    if task_run.status == TaskRunStatus.FAILED:
        last_review = reviews[-1] if reviews else None
        if last_review is not None and last_review.decision == ReviewDecision.REPAIR_REQUIRED:
            return "repair_limit_exhausted"
        if last_review is not None and last_review.decision == ReviewDecision.INVALID:
            return "reviewer_response_invalid"
        return "execution_failed"
    return "in_progress"
