"""Agent execution orchestration — MA3.

The worker's entry point for one AGENT_RUN job. Resolves
Task -> TaskRun -> AgentVersion -> ModelPolicy -> ProviderModel ->
ProviderModelSnapshot -> AgentRun/Attempt -> provider invocation ->
Artifact, with the Flight Recorder narrating every step (Section 22) and
the Budget Governor guarding every possibly-paid call (Section 19).

Retries are bounded and explicit (settings.max_agent_run_attempts,
frozen default 2) and only for retryable failure categories (connection/
timeout) — never for authentication or budget rejection, which are
permanent for this attempt sequence.

Cancellation is cooperative (Section 26.7): checked at each checkpoint
before starting the next irreversible step. If cancellation is observed
after a provider call already completed, whatever real cost/artifact it
produced is still recorded (it happened and cost real money/tokens) but
the run's terminal state is CANCELLED, not COMPLETED, and no further step
is taken.

Worker-restart recovery (MA7.8): a job whose worker died is reclaimed once
its lease expires, so ``execute`` may be handed an Agent Run that already
has durable history. Before any provider work it inspects that history
(``_recover_before_execution``):

* Agent Run already terminal -> nothing is executed (the caller propagates).
* no attempt yet -> normal execution from attempt 1.
* an attempt was interrupted BEFORE any provider call -> that attempt is kept
  as FAILED ``worker_interrupted`` and execution continues with the next
  attempt number, within ``max_agent_run_attempts``.
* a provider call was started (or already succeeded) but the run was never
  finalized -> the outcome/cost is ambiguous, so the provider is NEVER called
  again automatically: the open call, attempt, Agent Run and Task Run are
  finalized FAILED ``worker_interrupted`` (``finalize_interrupted``) and an
  operator may retry explicitly.

Every terminal write is a compare-and-swap on the row's still-open status,
so a late ("zombie") writer from a worker that lost its lease cannot turn an
interrupted run back into a completed one, and two recoveries racing finalize
it exactly once.
"""

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional, Type, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.db.enums import (
    AgentRunAttemptStatus,
    AgentRunStatus,
    ArtifactType,
    BudgetReservationStatus,
    ExecutionMode,
    ModelCallStatus,
    PricingClassification,
    TaskRunStatus,
    UsageSourceType,
)
from app.evaluation_contract import build_evaluator_extra_context
from app.model_resolution import (
    ResolvedModel,
    RoutingDecision,
    RoutingError,
    freeze_snapshot,
    record_routing_decision,
    route,
)
from app.models.agents import AgentVersion, PromptVersion
from app.models.artifacts_eval import Artifact
from app.models.evaluation_definitions import EvaluationDefinitionVersion
from app.models.execution import ModelCall
from app.models.governance import BudgetReservation
from app.models.identity import Project
from app.models.providers import ProviderModelSnapshot
from app.models.tasks import AgentRun, AgentRunAttempt, Task, TaskRun
from app.prompt_builder import build_prompt, build_workflow_upstream_extra_context
from app.providers.base import (
    InvokeRequest,
    ProviderAdapter,
    ProviderAuthenticationError,
    ProviderConnectionError,
    ProviderInvalidResponseError,
    ProviderTimeoutError,
)
from app.providers.factory import build_provider_adapter
from app.review_contract import build_repair_extra_context, build_reviewer_extra_context
from app.routing_evidence import RoutingContext
from app.services.budget_service import BudgetExceededError, BudgetGovernor, estimate_cost
from app.services.comparison_progress_service import notify_task_run_terminal
from app.services.evaluation_progress_service import (
    notify_task_run_terminal as notify_evaluation_run_terminal,
)
from app.services.flight_recorder import FlightRecorderService
from app.services.review_orchestration_service import ReviewOrchestrationService

logger = logging.getLogger("app.services.execution_service")

_RETRYABLE_ERROR_CATEGORIES = {"provider_connection_error", "provider_timeout"}

# MA7.8: the worker executing this run stopped (container restart, crash)
# before the run was finalized. Infrastructure evidence only -- deliberately
# NOT one of app.routing_evidence.PROVIDER_FAILURE_CATEGORIES, so it never
# counts against the model/provider (MA8) and is never quality evidence.
WORKER_INTERRUPTED = "worker_interrupted"

_TERMINAL_AGENT_RUN_STATUSES = (AgentRunStatus.COMPLETED, AgentRunStatus.FAILED, AgentRunStatus.STOPPED)

# A ModelCall in one of these states crossed (or may have crossed) the
# irreversible provider boundary without a failure being recorded: its
# outcome/cost is unknown or already real, so it is never replayed.
_UNRESOLVED_OR_SUCCEEDED_CALL_STATUSES = (ModelCallStatus.PENDING, ModelCallStatus.RUNNING, ModelCallStatus.SUCCESS)
_OPEN_CALL_STATUSES = (ModelCallStatus.PENDING, ModelCallStatus.RUNNING)

# Pessimistic characters-per-token used ONLY to compare a character count with
# a model's known context window (in tokens). Real text is usually 3-4; using 2
# means we fail closed on evidence that might not fit rather than admit it.
_CHARS_PER_TOKEN_FLOOR = 2


class ExecutionAborted(Exception):
    """Internal control-flow signal: the run reached a terminal
    (non-retryable) state — callers should stop, not retry."""


@dataclass
class _Context:
    agent_run: AgentRun
    task_run: TaskRun
    task: Task
    agent_version: AgentVersion
    prompt_version: Optional[PromptVersion]
    project: Project
    frozen_task_snapshot: Optional[dict] = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AgentExecutionService:
    def __init__(self, db: Session, adapter_factory=build_provider_adapter):
        self.db = db
        self.adapter_factory = adapter_factory
        self.recorder = FlightRecorderService(db)
        self.governor = BudgetGovernor(db)

    # -- entry point, called by the worker -----------------------------

    def execute(self, agent_run_id: str, *, worker_id: str) -> None:
        ctx = self._load_context(agent_run_id)

        first_attempt = self._recover_before_execution(ctx)
        if first_attempt is None:
            return  # terminal already, or finalized as interrupted: no provider work

        for attempt_number in range(first_attempt, settings.max_agent_run_attempts + 1):
            try:
                self._run_one_attempt(ctx, attempt_number=attempt_number, worker_id=worker_id)
                return  # success or a non-retryable terminal state was reached
            except _RetryableFailure as exc:
                logger.warning(
                    "agent_run_attempt_retryable_failure agent_run_id=%s attempt=%s category=%s",
                    ctx.agent_run.id,
                    attempt_number,
                    exc.category,
                )
                if attempt_number >= settings.max_agent_run_attempts:
                    self._finalize_failed(ctx, category=exc.category, message=exc.message)
                    return
                continue
            except ExecutionAborted:
                return
            except Exception as exc:
                # Anything not already categorized (a bug, a DB error, an
                # unexpected exception type) must still leave the Agent
                # Run / Task Run in a terminal state rather than stuck
                # RUNNING forever — Section 16's worker-failure-handling
                # guarantee applies even to genuinely unforeseen errors.
                logger.exception(
                    "agent_run_unexpected_error agent_run_id=%s attempt=%s", ctx.agent_run.id, attempt_number
                )
                # A failed flush leaves the session unusable until rolled
                # back; without this the finalization below would itself
                # raise and leave the run RUNNING (the MA7.8 incident).
                self.db.rollback()
                self._fail_dangling_attempt(ctx, message=str(exc))
                self._finalize_failed(ctx, category="internal_error", message=str(exc))
                return

    # -- context loading -------------------------------------------------

    def _load_context(self, agent_run_id: str) -> _Context:
        agent_run = self.db.get(AgentRun, agent_run_id)
        if agent_run is None:
            raise ExecutionAborted(f"AgentRun {agent_run_id} does not exist.")
        task_run = self.db.get(TaskRun, agent_run.task_run_id)
        if task_run is None:
            raise ExecutionAborted(f"TaskRun {agent_run.task_run_id} does not exist.")
        task = self.db.get(Task, task_run.task_id)
        if task is None:
            raise ExecutionAborted(f"Task {task_run.task_id} does not exist.")
        agent_version = self.db.get(AgentVersion, agent_run.agent_version_id)
        if agent_version is None:
            raise ExecutionAborted(f"AgentVersion {agent_run.agent_version_id} does not exist.")
        prompt_version = (
            self.db.get(PromptVersion, agent_version.prompt_version_id)
            if agent_version.prompt_version_id
            else None
        )
        project = self.db.get(Project, task.project_id)
        if project is None:
            raise ExecutionAborted(f"Project {task.project_id} does not exist.")
        return _Context(
            agent_run=agent_run,
            task_run=task_run,
            task=task,
            agent_version=agent_version,
            prompt_version=prompt_version,
            project=project,
            frozen_task_snapshot=(task_run.config_snapshot or {}).get("frozen_task_snapshot"),
        )

    # -- worker-restart recovery (MA7.8) ------------------------------------

    def _recover_before_execution(self, ctx: _Context) -> Optional[int]:
        """Decides, from durable state only, what this (possibly reclaimed)
        execution may do. Returns the attempt number to start from, or None
        when nothing may be executed. See the module docstring for the cases;
        the rule that matters is that a provider call which was started, or
        whose success was never finalized, is never replayed automatically."""
        agent_run = ctx.agent_run
        self.db.refresh(agent_run)
        if agent_run.status in _TERMINAL_AGENT_RUN_STATUSES:
            logger.info(
                "agent_run_already_terminal_on_claim agent_run_id=%s status=%s", agent_run.id, agent_run.status.value
            )
            return None

        attempts = list(
            self.db.execute(
                select(AgentRunAttempt)
                .where(AgentRunAttempt.agent_run_id == agent_run.id)
                .order_by(AgentRunAttempt.attempt_number)
                .execution_options(populate_existing=True)
            )
            .scalars()
            .all()
        )
        if not attempts:
            return 1

        crossed_provider_boundary = any(a.status == AgentRunAttemptStatus.COMPLETED for a in attempts) or (
            self.db.execute(
                select(ModelCall.id)
                .where(
                    ModelCall.agent_run_id == agent_run.id,
                    ModelCall.status.in_(_UNRESOLVED_OR_SUCCEEDED_CALL_STATUSES),
                )
                .limit(1)
            ).first()
            is not None
        )
        if crossed_provider_boundary:
            self._finalize_interrupted(
                ctx,
                reason=(
                    "Interrupted by worker restart after the provider call started; its outcome and cost are "
                    "unknown, so it was not repeated automatically."
                ),
            )
            return None

        # Interrupted before any provider call: nothing irreversible happened.
        interrupted = [a for a in attempts if a.status == AgentRunAttemptStatus.RUNNING]
        for attempt in interrupted:
            if self._cas_attempt(
                attempt,
                status=AgentRunAttemptStatus.FAILED,
                ended_at=_utcnow(),
                error={
                    "category": WORKER_INTERRUPTED,
                    "message": "Interrupted by worker restart before any provider call.",
                },
            ):
                self._event(
                    ctx,
                    "agent_run_attempt.interrupted",
                    decision_summary=f"attempt {attempt.attempt_number} interrupted before any provider call",
                    error={"category": WORKER_INTERRUPTED},
                )
        if interrupted:
            self._release_active_reservations(agent_run.id)

        next_attempt = max(a.attempt_number for a in attempts) + 1
        if next_attempt > settings.max_agent_run_attempts:
            self._finalize_interrupted(
                ctx, reason="Interrupted by worker restart; the attempt limit is exhausted, so it was not retried."
            )
            return None
        return next_attempt

    def finalize_interrupted(self, agent_run_id: str, *, reason: str) -> bool:
        """Shared interrupted-finalization (MA7.8), for reconciliation: an
        Agent Run whose executing worker is known to be gone. Never calls a
        provider. True only for the caller that made the transition."""
        try:
            ctx = self._load_context(agent_run_id)
        except ExecutionAborted:
            return False
        return self._finalize_interrupted(ctx, reason=reason)

    def _finalize_interrupted(self, ctx: _Context, *, reason: str) -> bool:
        """Open ModelCalls -> ERROR (cost left unknown), running attempts ->
        FAILED, then the ordinary FAILED finalization of Agent Run and Task
        Run, all with category ``worker_interrupted``. A ModelCall that
        already SUCCEEDED is left exactly as recorded -- it is real provider
        evidence. Budget reservations of an ambiguous call are left held
        (conservative: the spend may have happened)."""
        self.db.refresh(ctx.agent_run)
        if ctx.agent_run.status in _TERMINAL_AGENT_RUN_STATUSES:
            return False
        now = _utcnow()
        error = {"category": WORKER_INTERRUPTED, "message": reason}
        self.db.execute(
            update(ModelCall)
            .where(ModelCall.agent_run_id == ctx.agent_run.id, ModelCall.status.in_(_OPEN_CALL_STATUSES))
            .values(status=ModelCallStatus.ERROR, completed_at=now, error=error)
            .execution_options(synchronize_session=False)
        )
        self.db.execute(
            update(AgentRunAttempt)
            .where(
                AgentRunAttempt.agent_run_id == ctx.agent_run.id,
                AgentRunAttempt.status == AgentRunAttemptStatus.RUNNING,
            )
            .values(status=AgentRunAttemptStatus.FAILED, ended_at=now, error=error)
            .execution_options(synchronize_session=False)
        )
        self.db.commit()
        return self._finalize_failed(ctx, category=WORKER_INTERRUPTED, message=reason)

    def _release_active_reservations(self, agent_run_id: str) -> None:
        """Only for an attempt interrupted BEFORE its provider call: the
        estimated spend never happened (same rule as ``BudgetGovernor.release``)."""
        self.db.execute(
            update(BudgetReservation)
            .where(
                BudgetReservation.agent_run_id == agent_run_id,
                BudgetReservation.status == BudgetReservationStatus.ACTIVE,
            )
            .values(status=BudgetReservationStatus.RELEASED)
            .execution_options(synchronize_session=False)
        )
        self.db.commit()

    # -- compare-and-swap guards (MA7.8) ----------------------------------------

    def _cas_attempt(self, attempt: AgentRunAttempt, **values) -> bool:
        """``attempt`` RUNNING -> ``values``; False if it is no longer RUNNING
        (e.g. recovery already marked it interrupted)."""
        result = self.db.execute(
            update(AgentRunAttempt)
            .where(AgentRunAttempt.id == attempt.id, AgentRunAttempt.status == AgentRunAttemptStatus.RUNNING)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        self.db.commit()
        self.db.refresh(attempt)
        return cast(CursorResult, result).rowcount == 1

    def _cas_model_call(self, model_call: ModelCall, **values) -> bool:
        result = self.db.execute(
            update(ModelCall)
            .where(ModelCall.id == model_call.id, ModelCall.status.in_(_OPEN_CALL_STATUSES))
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        self.db.commit()
        self.db.refresh(model_call)
        return cast(CursorResult, result).rowcount == 1

    def _claim_agent_run_terminal(self, ctx: _Context, status: AgentRunStatus) -> bool:
        """The single authority for an Agent Run's terminal transition: only
        the caller that moves it out of a non-terminal status wins."""
        result = self.db.execute(
            update(AgentRun)
            .where(AgentRun.id == ctx.agent_run.id, AgentRun.status.notin_(_TERMINAL_AGENT_RUN_STATUSES))
            .values(status=status, ended_at=_utcnow())
            .execution_options(synchronize_session=False)
        )
        self.db.commit()
        self.db.refresh(ctx.agent_run)
        return cast(CursorResult, result).rowcount == 1

    def _superseded(self, ctx: _Context, what: str) -> ExecutionAborted:
        logger.warning(
            "agent_run_execution_superseded agent_run_id=%s at=%s -- the run was finalized elsewhere "
            "(e.g. as interrupted); this execution stops without writing a result",
            ctx.agent_run.id,
            what,
        )
        return ExecutionAborted(f"execution superseded at {what}")

    def _is_cancelled(self, ctx: _Context) -> bool:
        """Cooperative cancellation request, from EITHER level: the Task Run
        (MA3 -- ``TaskService.cancel_task_run``) or this Agent Run itself
        (MA7 -- the Workflow Engine cancels one node's Agent Run without
        cancelling anything else, e.g. a failed workflow's still-running
        sibling branch). Both are re-read from the database at every
        checkpoint -- the request is written by a different process."""
        self.db.refresh(ctx.task_run)
        self.db.refresh(ctx.agent_run)
        return (
            ctx.task_run.cancellation_requested_at is not None
            or ctx.agent_run.cancellation_requested_at is not None
        )

    # -- one attempt -------------------------------------------------------

    def _run_one_attempt(self, ctx: _Context, *, attempt_number: int, worker_id: str) -> None:
        agent_run, task_run = ctx.agent_run, ctx.task_run

        attempt = AgentRunAttempt(
            agent_run_id=agent_run.id,
            attempt_number=attempt_number,
            status=AgentRunAttemptStatus.RUNNING,
            started_at=_utcnow(),
            worker_id=worker_id,
        )
        self.db.add(attempt)
        try:
            self.db.commit()
        except IntegrityError as exc:
            # (agent_run_id, attempt_number) is unique: another executor
            # already owns this attempt. Never fight it -- and never leave
            # this session broken (MA7.8).
            self.db.rollback()
            logger.warning(
                "agent_run_attempt_already_exists agent_run_id=%s attempt=%s", agent_run.id, attempt_number
            )
            raise ExecutionAborted(f"attempt {attempt_number} already exists") from exc
        self.db.refresh(attempt)

        if agent_run.status != AgentRunStatus.RUNNING:
            agent_run.status = AgentRunStatus.RUNNING
            agent_run.started_at = agent_run.started_at or _utcnow()
        self.db.commit()

        self._event(ctx, "agent_run_attempt.started", decision_summary=f"attempt {attempt_number} started")

        if self._is_cancelled(ctx):
            self._finalize_cancelled(ctx, attempt)
            raise ExecutionAborted("cancelled before model resolution")

        # 1. Resolve model ------------------------------------------------
        try:
            decision = self._route_model(ctx)
        except RoutingError as exc:
            # MA8.1: a failed routing decision is audited too, so "why did
            # this run get no model" is answerable from durable state.
            if exc.decision is not None:
                record_routing_decision(
                    self.db, agent_run_id=agent_run.id, decision=exc.decision, agent_run_attempt_id=attempt.id
                )
                self.db.commit()
            self._finalize_failed(ctx, category="model_resolution_error", message=str(exc), attempt=attempt)
            raise ExecutionAborted(str(exc)) from exc
        resolved = decision.selected

        self._event(
            ctx,
            "agent_run.model_selected",
            model_id=resolved.model.id,
            provider_id=resolved.provider.id,
            decision_summary=resolved.rationale,
        )

        pinned_snapshot_id = (ctx.agent_run.model_policy_override_json or {}).get("provider_model_snapshot_id")
        snapshot = self.db.get(ProviderModelSnapshot, pinned_snapshot_id) if pinned_snapshot_id else None
        if snapshot is not None and snapshot.provider_model_id != resolved.provider_model.id:
            self._finalize_failed(
                ctx,
                category="model_snapshot_mismatch",
                message="Pinned provider model snapshot does not match the resolved provider model.",
                attempt=attempt,
            )
            raise ExecutionAborted("pinned model snapshot mismatch")
        snapshot = snapshot or freeze_snapshot(self.db, resolved)
        routing_record = record_routing_decision(
            self.db,
            agent_run_id=agent_run.id,
            decision=decision,
            snapshot=snapshot,
            agent_run_attempt_id=attempt.id,
        )
        agent_run.model_id = resolved.model.id
        agent_run.provider_id = resolved.provider.id
        agent_run.provider_model_snapshot_id = snapshot.id
        self.db.commit()
        self._event(
            ctx,
            "agent_run.provider_model_snapshot_selected",
            model_id=resolved.model.id,
            provider_id=resolved.provider.id,
            decision_summary=f"snapshot {snapshot.id} pricing={resolved.pricing_classification.value}",
        )

        if self._is_cancelled(ctx):
            self._finalize_cancelled(ctx, attempt)
            raise ExecutionAborted("cancelled after model resolution")

        # 2. Budget reservation ---------------------------------------------
        budget = self.governor.get_budget(task_run.budget_id)
        cost_estimate = estimate_cost(resolved)
        try:
            reservation = self.governor.reserve(
                budget=budget,
                agent_run_id=agent_run.id,
                task_run_id=task_run.id,
                amount=cost_estimate.amount,
            )
        except BudgetExceededError as exc:
            self._event(
                ctx, "budget.reservation_rejected", decision_summary=str(exc), error={"message": str(exc)}
            )
            self._finalize_failed(ctx, category="budget_exceeded", message=str(exc), attempt=attempt)
            raise ExecutionAborted(str(exc)) from exc

        if budget is not None:
            state = self.governor.threshold_state(budget)
            if state in ("warn", "critical"):
                self._event(
                    ctx,
                    "budget.threshold_warning",
                    decision_summary=f"budget {budget.id} at {state} threshold after reservation",
                )

        if self._is_cancelled(ctx):
            self.governor.release(reservation)
            self._finalize_cancelled(ctx, attempt)
            raise ExecutionAborted("cancelled after budget reservation")

        # 3. Prompt assembly --------------------------------------------------
        try:
            extra_context = self._build_extra_context(agent_run, resolved=resolved)
        except _WorkflowContextTooLarge as exc:
            self.governor.release(reservation)
            self._finalize_failed(ctx, category="workflow_context_too_large", message=str(exc), attempt=attempt)
            raise ExecutionAborted(str(exc)) from exc
        except _EvaluationContextTooLarge as exc:
            self.governor.release(reservation)
            self._finalize_failed(ctx, category="evaluation_context_too_large", message=str(exc), attempt=attempt)
            raise ExecutionAborted(str(exc)) from exc
        except _MissingReviewContext as exc:
            self.governor.release(reservation)
            self._finalize_failed(ctx, category="review_context_error", message=str(exc), attempt=attempt)
            raise ExecutionAborted(str(exc)) from exc
        except _MissingEvaluationContext as exc:
            self.governor.release(reservation)
            self._finalize_failed(ctx, category="evaluation_context_error", message=str(exc), attempt=attempt)
            raise ExecutionAborted(str(exc)) from exc
        except _MissingWorkflowContext as exc:
            self.governor.release(reservation)
            self._finalize_failed(ctx, category="workflow_context_error", message=str(exc), attempt=attempt)
            raise ExecutionAborted(str(exc)) from exc

        assembly = build_prompt(
            agent_version=ctx.agent_version,
            prompt_version=ctx.prompt_version,
            task=ctx.task,
            task_snapshot=ctx.frozen_task_snapshot,
            extra_context=extra_context,
        )
        self._event(
            ctx,
            "agent_run.prompt_assembled",
            decision_summary=(
                f"prompt_version_id={ctx.prompt_version.id if ctx.prompt_version else None} "
                f"sha256={assembly.content_hash} length_chars={assembly.length_chars}"
            ),
        )

        if self._is_cancelled(ctx):
            self.governor.release(reservation)
            self._finalize_cancelled(ctx, attempt)
            raise ExecutionAborted("cancelled before provider invocation")

        # MA7.8: last check before the irreversible step -- a worker that lost
        # its lease (its attempt already recovered as interrupted) must not
        # start a provider call.
        self.db.refresh(attempt)
        self.db.refresh(agent_run)
        if attempt.status != AgentRunAttemptStatus.RUNNING or agent_run.status in _TERMINAL_AGENT_RUN_STATUSES:
            self.governor.release(reservation)
            raise self._superseded(ctx, "provider invocation")

        # 4. Provider invocation ----------------------------------------------
        model_call = ModelCall(
            agent_run_id=agent_run.id,
            agent_run_attempt_id=attempt.id,
            model_id=resolved.model.id,
            provider_id=resolved.provider.id,
            provider_model_id=resolved.provider_model.id,
            provider_model_snapshot_id=snapshot.id,
            model_routing_decision_id=routing_record.id,
            status=ModelCallStatus.RUNNING,
            started_at=_utcnow(),
        )
        self.db.add(model_call)
        self.db.commit()
        self.db.refresh(model_call)

        self._event(
            ctx,
            "model_call.started",
            decision_summary=f"invoking {resolved.provider_model.provider_model_id!r}",
        )

        adapter = self.adapter_factory(self.db, resolved.provider)
        request = InvokeRequest(
            provider_model_id=resolved.provider_model.provider_model_id,
            system_prompt=assembly.system_prompt,
            user_prompt=assembly.user_prompt,
            timeout_seconds=float(agent_run.timeout_seconds),
            max_tokens=(ctx.task.requirements or {}).get("_professor_max_output_tokens")
            if ctx.agent_version.role == "professor"
            else None,
            response_format=(
                {"type": "json_object"}
                if ctx.agent_version.role == "professor" and resolved.model.structured_output_support is True
                else None
            ),
        )

        try:
            response = self._invoke(adapter, request)
        except _CategorizedProviderError as exc:
            if not self._cas_model_call(
                model_call,
                status=ModelCallStatus.ERROR if exc.category != "provider_timeout" else ModelCallStatus.TIMEOUT,
                error={"category": exc.category, "message": exc.message},
                completed_at=_utcnow(),
            ):
                raise self._superseded(ctx, "provider failure") from exc
            self._event(
                ctx,
                "model_call.failed",
                decision_summary=exc.message,
                error={"category": exc.category, "message": exc.message},
            )
            self.governor.release(reservation)

            if exc.category in _RETRYABLE_ERROR_CATEGORIES:
                if not self._cas_attempt(
                    attempt,
                    status=AgentRunAttemptStatus.FAILED,
                    ended_at=_utcnow(),
                    error={"category": exc.category, "message": exc.message},
                ):
                    raise self._superseded(ctx, "retryable failure") from exc
                raise _RetryableFailure(exc.category, exc.message) from exc

            self._finalize_failed(ctx, category=exc.category, message=exc.message, attempt=attempt)
            raise ExecutionAborted(exc.message) from exc

        # 5. Success: record usage/cost, artifact, finalize ---------------------
        actual_cost = self._actual_cost(resolved, response)
        if not self._cas_model_call(
            model_call,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            cost_amount=actual_cost.amount,
            cost_is_estimated=actual_cost.is_estimated,
            latency_ms=response.latency_ms,
            provider_request_id=response.provider_request_id,
            status=ModelCallStatus.SUCCESS,
            completed_at=_utcnow(),
        ):
            # Recovery already recorded this call as interrupted: that
            # verdict stands; the late response is not turned into output.
            # Its real spend is known here, so the held reservation is settled.
            self.governor.settle(reservation, actual_amount=actual_cost.amount)
            raise self._superseded(ctx, "provider response")

        self._event(
            ctx,
            "model_call.completed",
            model_id=resolved.model.id,
            provider_id=resolved.provider.id,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            cost=actual_cost.amount,
            latency_ms=response.latency_ms,
            decision_summary=f"cost={actual_cost.amount} estimated={actual_cost.is_estimated}",
        )

        self.governor.settle(reservation, actual_amount=actual_cost.amount)
        self.governor.record_usage(
            project_id=ctx.project.id,
            budget=budget,
            amount=actual_cost.amount,
            source_type=UsageSourceType.MODEL_CALL,
            source_ref_id=model_call.id,
        )

        artifact = self._create_artifact(ctx, response.text)
        self._event(
            ctx,
            "artifact.created",
            artifact_refs=[artifact.id],
            decision_summary=f"artifact {artifact.id} ({artifact.size_bytes} bytes)",
        )

        if not self._cas_attempt(attempt, status=AgentRunAttemptStatus.COMPLETED, ended_at=_utcnow()):
            raise self._superseded(ctx, "attempt completion")

        if self._is_cancelled(ctx):
            self._finalize_cancelled(ctx, attempt, already_produced=True)
            return

        if not self._claim_agent_run_terminal(ctx, AgentRunStatus.COMPLETED):
            raise self._superseded(ctx, "agent run completion")
        self._event(ctx, "agent_run.completed", decision_summary="agent run completed successfully")

        if self._is_build_review(ctx):
            # MA4: this Agent Run was one stage (primary/reviewer/repair)
            # of a review cycle -- what happens to the Task Run next
            # depends on that cycle's state, not on "one Agent Run means
            # the Task Run is done" (true only for SINGLE_AGENT, below).
            ReviewOrchestrationService(self.db).on_agent_run_succeeded(ctx, agent_run, artifact)
            return

        task_run.status = TaskRunStatus.VERIFYING
        self.db.commit()
        self._event(ctx, "task_run.verifying", decision_summary="no objective evaluation configured (MA3)")

        task_run.status = TaskRunStatus.COMPLETED
        task_run.ended_at = _utcnow()
        self.db.commit()
        self._event(ctx, "task_run.completed", decision_summary="task run completed")
        notify_task_run_terminal(self.db, task_run)
        notify_evaluation_run_terminal(self.db, task_run)

    # -- model resolution --------------------------------------------------

    def _route_model(self, ctx: _Context) -> RoutingDecision:
        # MA5: a comparison candidate may override its Agent Version's own
        # (immutable, shared, published) model_policy to resolve a
        # *different* concrete model without needing a second Agent
        # Version just to vary the model ("same Agent, different models").
        # The same run-level override carries a workflow node's
        # model_policy_override, an EVALUATION node's evaluator override and
        # an operator's failed-node replacement model (always MANUAL).
        # Absent an override (the MA3/MA4 default), behavior is unchanged.
        policy = ctx.agent_run.model_policy_override_json or ctx.agent_version.model_policy or {}
        # MA8.2: AUTO routing may consult this project's own history for this
        # Agent role; MANUAL (including every replacement retry) never does.
        context = RoutingContext(project_id=ctx.project.id, agent_role=ctx.agent_version.role)
        return route(self.db, policy, context=context)

    # -- extra prompt context (MA4 reviewer/repair runs only) --------------

    def _build_extra_context(
        self, agent_run: AgentRun, resolved: Optional[ResolvedModel] = None
    ) -> Optional[str]:
        """Resolves ``agent_run.input_context_json`` (set only for
        REVIEWER/REPAIR Agent Runs by ReviewOrchestrationService) into the
        rendered text block ``build_prompt`` appends to the user prompt.
        Stores only durable pointers (artifact ids/hashes), never full
        content, so a worker that reclaims this job after a crash
        reconstructs the exact same prompt from DB state alone -- the same
        guarantee MA3 already gives a plain SINGLE_AGENT run."""
        input_context = agent_run.input_context_json
        if not input_context:
            return None

        kind = input_context.get("kind")
        if kind == "review_request":
            artifact_id = input_context.get("candidate_artifact_id")
            candidate_artifact = self.db.get(Artifact, artifact_id) if artifact_id else None
            if candidate_artifact is None:
                raise _MissingReviewContext(f"Candidate artifact {artifact_id} for review no longer exists.")
            candidate_text = Path(candidate_artifact.storage_ref).read_text(encoding="utf-8")
            return build_reviewer_extra_context(
                candidate_text=candidate_text,
                candidate_artifact_id=candidate_artifact.id,
                candidate_artifact_hash=candidate_artifact.content_hash or "",
                review_instructions=input_context.get("review_instructions"),
            )

        if kind == "repair_request":
            artifact_id = input_context.get("previous_artifact_id")
            previous_artifact = self.db.get(Artifact, artifact_id) if artifact_id else None
            if previous_artifact is None:
                raise _MissingReviewContext(
                    f"Previous candidate artifact {artifact_id} for repair no longer exists."
                )
            previous_text = Path(previous_artifact.storage_ref).read_text(encoding="utf-8")
            return build_repair_extra_context(
                previous_candidate_text=previous_text,
                issues=input_context.get("issues") or [],
                repair_instructions=input_context.get("repair_instructions"),
            )

        if kind == "evaluation_request":
            return self._build_evaluator_extra_context(input_context, resolved)

        if kind == "workflow_upstream":
            return self._build_workflow_upstream_context(input_context, resolved)

        return None

    def _build_workflow_upstream_context(
        self, input_context: dict, resolved: Optional[ResolvedModel] = None
    ) -> Optional[str]:
        """MA7.3b: a Workflow-dispatched Agent Run's ``input_context_json``
        lists the immutable upstream artifact ids (already passed through any
        Human Approval node). Each artifact is read from disk and checked
        against its recorded sha256, so the downstream agent receives exactly
        the content that was produced -- and, behind an approval gate, exactly
        what the human approved -- or the run fails with a categorized error;
        it never proceeds on missing or altered upstream output."""
        # ids are the authority (canonical, de-duplicated); ``upstream`` only
        # supplies the producing node_key label(s) for each -- absent on runs
        # dispatched before MA7.4a, which simply render unlabelled.
        labels = {
            entry["artifact_id"]: ", ".join(entry.get("source_node_keys") or [entry.get("node_key", "")])
            for entry in input_context.get("upstream") or []
            if entry.get("artifact_id") and (entry.get("source_node_keys") or entry.get("node_key"))
        }
        upstream = []
        for artifact_id in input_context.get("upstream_artifact_ids") or []:
            artifact = self.db.get(Artifact, artifact_id)
            if artifact is None:
                raise _MissingWorkflowContext(f"Upstream artifact {artifact_id} no longer exists.")
            try:
                content = Path(artifact.storage_ref).read_bytes()
                text = content.decode("utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise _MissingWorkflowContext(
                    f"Upstream artifact {artifact_id} content could not be read: {type(exc).__name__}."
                ) from exc
            if artifact.content_hash and hashlib.sha256(content).hexdigest() != artifact.content_hash:
                raise _MissingWorkflowContext(
                    f"Upstream artifact {artifact_id} content no longer matches its recorded sha256."
                )
            upstream.append((artifact.id, artifact.content_hash or "", text))
        if not upstream:
            return None
        rendered = build_workflow_upstream_extra_context(upstream, labels)
        self._enforce_upstream_context_limit(len(rendered), len(upstream), resolved)
        return rendered

    def _enforce_upstream_context_limit(
        self, size_chars: int, artifact_count: int, resolved: Optional[ResolvedModel]
    ) -> None:
        """MA7.4c: fan-in can multiply prompt size, and the evidence a human
        approved must reach the model COMPLETE -- so oversized evidence FAILS
        the node here, before any provider call. Never truncated, summarized
        or partially dropped. (See ``_enforce_context_limit`` for the rule.)"""
        self._enforce_context_limit(
            size_chars,
            artifact_count,
            resolved,
            cap=settings.workflow_upstream_context_max_chars,
            cap_name="platform cap (MAP_WORKFLOW_UPSTREAM_CONTEXT_MAX_CHARS)",
            error_cls=_WorkflowContextTooLarge,
            what="The upstream evidence for this node",
        )

    def _enforce_context_limit(
        self,
        size_chars: int,
        artifact_count: int,
        resolved: Optional[ResolvedModel],
        *,
        cap: int,
        cap_name: str,
        error_cls: Type[Exception],
        what: str,
    ) -> None:
        """The platform hard cap (``cap``) is authoritative. A model's KNOWN
        context window can only lower the effective limit
        (``window_tokens * _CHARS_PER_TOKEN_FLOOR`` -- a deliberately
        pessimistic conversion, since a window is in tokens and the evidence
        in characters); a model with no recorded window is held to the
        platform cap only -- no limit is invented for it. The message carries
        counts and limits only, never content."""
        limit = cap
        source = cap_name
        window = None
        if resolved is not None:
            window = resolved.model.context_window
        if window:
            model_limit = window * _CHARS_PER_TOKEN_FLOOR
            if model_limit < limit:
                limit, source = model_limit, f"model context window ({window} tokens)"
        if size_chars > limit:
            raise error_cls(
                f"{what} ({artifact_count} artifact(s), {size_chars} characters) "
                f"exceeds the limit of {limit} characters set by the {source}. It is never truncated: "
                "reduce or split the evidence, use a model with a larger context window, or raise "
                "the platform cap."
            )

    def _build_evaluator_extra_context(self, input_context: dict, resolved: Optional[ResolvedModel] = None) -> str:
        """MA6 Slice 3B: renders the subject task's own title/description/
        requirements + the exact subject candidate Artifact content +
        the immutable rubric's full ordered criteria + structured-output
        instructions for one evaluator Agent Run's single inference call.
        The evaluator's own dedicated bookkeeping Task (never the
        subject's) never supplies any of this -- everything the evaluator
        needs to reason about comes from this rendered block, exactly
        like a REVIEWER/REPAIR run's review-specific content arrives
        entirely through review_request/repair_request's own extra
        context above, never through Task fields.

        Re-validates the subject artifact/hash binding *before* any
        provider call or budget spend -- cost avoidance, and the first of
        the two defense-in-depth checks
        app.services.evaluation_execution_service already documents for
        the deterministic path (the second runs again once the
        evaluator's own inference completes, in
        finalize_agent_evaluator_run)."""
        subject_agent_run_id = input_context.get("subject_agent_run_id")
        subject_artifact_id = input_context.get("subject_artifact_id")
        subject_artifact_hash = input_context.get("subject_artifact_hash")
        evaluation_definition_version_id = input_context.get("evaluation_definition_version_id")

        subject_artifact = self.db.get(Artifact, subject_artifact_id) if subject_artifact_id else None
        if subject_artifact is None or subject_artifact.agent_run_id != subject_agent_run_id:
            raise _MissingEvaluationContext(
                f"Subject artifact {subject_artifact_id} for evaluation no longer belongs to Agent Run "
                f"{subject_agent_run_id}."
            )
        if subject_artifact.content_hash != subject_artifact_hash:
            raise _MissingEvaluationContext(
                "Subject artifact content changed since this Evaluation Run was created -- refusing to "
                "evaluate a different artifact than was bound."
            )

        subject_agent_run = self.db.get(AgentRun, subject_agent_run_id)
        subject_task_run = self.db.get(TaskRun, subject_agent_run.task_run_id) if subject_agent_run else None
        subject_task = self.db.get(Task, subject_task_run.task_id) if subject_task_run else None
        if subject_agent_run is None or subject_task_run is None or subject_task is None:
            raise _MissingEvaluationContext(
                f"Subject Agent Run {subject_agent_run_id}'s own Task Run/Task no longer exists."
            )

        version = self.db.get(EvaluationDefinitionVersion, evaluation_definition_version_id)
        if version is None:
            raise _MissingEvaluationContext(
                f"Evaluation Definition Version {evaluation_definition_version_id} no longer exists."
            )

        # MA7.5A: the recorded hash is only a claim. The subject bytes handed to
        # the evaluator must still hash to the hash bound when the run was
        # created -- a missing, unreadable, non-UTF-8 or altered file fails the
        # run here, categorized, before any provider call or spend.
        try:
            subject_bytes = Path(subject_artifact.storage_ref).read_bytes()
            candidate_text = subject_bytes.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise _MissingEvaluationContext(
                f"Subject artifact {subject_artifact_id} content could not be read: {type(exc).__name__}."
            ) from exc
        if hashlib.sha256(subject_bytes).hexdigest() != subject_artifact_hash:
            raise _MissingEvaluationContext(
                f"Subject artifact {subject_artifact_id} content no longer matches its bound sha256 -- "
                "refusing to evaluate different bytes than were bound."
            )
        requirements_text = (
            json.dumps(subject_task.requirements, sort_keys=True, default=str)
            if subject_task.requirements
            else None
        )
        rendered = build_evaluator_extra_context(
            subject_task_title=subject_task.title,
            subject_task_description=subject_task.description,
            subject_task_requirements=requirements_text,
            candidate_text=candidate_text,
            candidate_artifact_id=subject_artifact.id,
            candidate_artifact_hash=subject_artifact.content_hash or "",
            criteria=[
                {"key": c.key, "label": c.label, "description": c.description} for c in version.criteria
            ],
        )
        self._enforce_context_limit(
            len(rendered),
            1,
            resolved,
            cap=settings.evaluation_context_max_chars,
            cap_name="platform cap (MAP_EVALUATION_CONTEXT_MAX_CHARS)",
            error_cls=_EvaluationContextTooLarge,
            what="The evaluator input for this run",
        )
        return rendered

    # -- provider invocation -------------------------------------------------

    def _invoke(self, adapter: ProviderAdapter, request: InvokeRequest):
        try:
            return adapter.invoke(request)
        except ProviderAuthenticationError as exc:
            raise _CategorizedProviderError("provider_authentication_error", str(exc)) from exc
        except ProviderTimeoutError as exc:
            raise _CategorizedProviderError("provider_timeout", str(exc)) from exc
        except ProviderInvalidResponseError as exc:
            raise _CategorizedProviderError("provider_invalid_response", str(exc)) from exc
        except ProviderConnectionError as exc:
            raise _CategorizedProviderError("provider_connection_error", str(exc)) from exc

    def _actual_cost(self, resolved: ResolvedModel, response) -> "_CostResult":
        if response.cost_amount is not None:
            return _CostResult(amount=response.cost_amount, is_estimated=False)

        if resolved.pricing_classification == PricingClassification.FREE:
            # A verified-free model's actual cost is a known zero, not an
            # estimate — matches app.services.budget_service.estimate_cost's
            # same FREE special case.
            return _CostResult(amount=Decimal(0), is_estimated=False)

        pm = resolved.provider_model
        if pm.cost_input_per_mtok is None or pm.cost_output_per_mtok is None:
            # UNKNOWN pricing and the provider didn't report actual cost —
            # never fabricate $0.00; fall back to the same conservative
            # reserve used for budgeting, still flagged as an estimate.
            return _CostResult(amount=Decimal(settings.unknown_pricing_reserve_amount), is_estimated=True)

        tokens_in = Decimal(response.tokens_in or 0)
        tokens_out = Decimal(response.tokens_out or 0)
        amount = (tokens_in / Decimal(1_000_000)) * pm.cost_input_per_mtok + (
            tokens_out / Decimal(1_000_000)
        ) * pm.cost_output_per_mtok
        return _CostResult(amount=amount, is_estimated=True)

    # -- artifact -------------------------------------------------------------

    def _create_artifact(self, ctx: _Context, text: str) -> Artifact:
        artifacts_dir = settings.artifacts_dir / ctx.task_run.id
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        path = artifacts_dir / f"{ctx.agent_run.id}.md"
        content_bytes = text.encode("utf-8")
        path.write_bytes(content_bytes)

        artifact = Artifact(
            agent_run_id=ctx.agent_run.id,
            type=ArtifactType.REPORT,
            storage_ref=str(path),
            content_hash=hashlib.sha256(content_bytes).hexdigest(),
            size_bytes=len(content_bytes),
            mime_type="text/markdown",
        )
        self.db.add(artifact)
        self.db.commit()
        self.db.refresh(artifact)
        return artifact

    # -- finalization -------------------------------------------------------

    def _fail_dangling_attempt(self, ctx: _Context, *, message: str) -> None:
        from sqlalchemy import select

        stmt = select(AgentRunAttempt).where(
            AgentRunAttempt.agent_run_id == ctx.agent_run.id,
            AgentRunAttempt.status == AgentRunAttemptStatus.RUNNING,
        )
        for attempt in self.db.execute(stmt).scalars().all():
            attempt.status = AgentRunAttemptStatus.FAILED
            attempt.ended_at = _utcnow()
            attempt.error = {"category": "internal_error", "message": message}
        self.db.commit()

    def _finalize_failed(
        self, ctx: _Context, *, category: str, message: str, attempt: Optional[AgentRunAttempt] = None
    ) -> bool:
        """Returns False (and writes nothing more) when the Agent Run was
        already terminal -- finalized elsewhere, e.g. recovered as interrupted
        (MA7.8): exactly one finalizer records the outcome."""
        if attempt is not None:
            self._cas_attempt(
                attempt,
                status=AgentRunAttemptStatus.FAILED,
                ended_at=_utcnow(),
                error={"category": category, "message": message},
            )
        if not self._claim_agent_run_terminal(ctx, AgentRunStatus.FAILED):
            logger.warning("agent_run_already_terminal_on_failure agent_run_id=%s", ctx.agent_run.id)
            return False
        if category == WORKER_INTERRUPTED:
            self._event(
                ctx, "agent_run.interrupted", decision_summary=message, error={"category": WORKER_INTERRUPTED}
            )
        self._event(
            ctx,
            "agent_run.failed",
            error={"category": category, "message": message},
            decision_summary=message,
        )

        ctx.task_run.status = TaskRunStatus.FAILED
        ctx.task_run.ended_at = _utcnow()
        self.db.commit()
        self._event(
            ctx, "task_run.failed", error={"category": category, "message": message}, decision_summary=message
        )
        notify_task_run_terminal(self.db, ctx.task_run)
        notify_evaluation_run_terminal(self.db, ctx.task_run)
        return True

    def _finalize_cancelled(
        self, ctx: _Context, attempt: AgentRunAttempt, *, already_produced: bool = False
    ) -> None:
        if not self._claim_agent_run_terminal(ctx, AgentRunStatus.STOPPED):
            logger.warning("agent_run_already_terminal_on_cancel agent_run_id=%s", ctx.agent_run.id)
            return
        attempt.status = AgentRunAttemptStatus.FAILED
        attempt.ended_at = _utcnow()
        attempt.error = {"category": "cancelled", "message": "cancellation requested"}
        self.db.commit()
        self._event(
            ctx,
            "agent_run.stopped",
            decision_summary="cooperative cancellation observed at checkpoint"
            + (" (partial result already produced and preserved)" if already_produced else ""),
        )

        ctx.task_run.status = TaskRunStatus.CANCELLED
        ctx.task_run.ended_at = _utcnow()
        self.db.commit()
        self._event(ctx, "task_run.cancelled", decision_summary="task run cancelled")
        notify_task_run_terminal(self.db, ctx.task_run)
        notify_evaluation_run_terminal(self.db, ctx.task_run)

    # -- comparison compatibility (MA5) --------------------------------------

    def _is_build_review(self, ctx: _Context) -> bool:
        """True for MA4's own BUILD_REVIEW Task Runs (unchanged), and also
        for an MA5 comparison candidate that opted into review
        (Section 11) even though the *shared* comparison Task's
        execution_mode isn't BUILD_REVIEW (candidates within one
        comparison may mix reviewed and plain — one shared Task can only
        carry one execution_mode, but each candidate's own Task Run
        config_snapshot is independent). Purely additive: for every
        existing MA3/MA4 Task Run this evaluates identically to the old
        ``ctx.task.execution_mode == ExecutionMode.BUILD_REVIEW`` check,
        since that flow always sets both together."""
        if ctx.task.execution_mode == ExecutionMode.BUILD_REVIEW:
            return True
        config_snapshot = ctx.task_run.config_snapshot or {}
        return config_snapshot.get("reviewer_agent_version_id") is not None

    # -- flight recorder helper -----------------------------------------------

    def _event(self, ctx: _Context, event_type: str, **fields) -> None:
        self.recorder.record(
            task_id=ctx.task.id,
            task_run_id=ctx.task_run.id,
            agent_run_id=ctx.agent_run.id,
            agent_id=ctx.agent_version.agent_id,
            agent_version_id=ctx.agent_version.id,
            event_type=event_type,
            **fields,
        )


def agent_run_was_interrupted(db: Session, agent_run_id: str) -> bool:
    """True when this Agent Run FAILED because its worker was interrupted
    (MA7.8) -- its latest attempt carries ``worker_interrupted``."""
    latest = db.execute(
        select(AgentRunAttempt)
        .where(AgentRunAttempt.agent_run_id == agent_run_id)
        .order_by(AgentRunAttempt.attempt_number.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    error = latest.error if latest is not None else None
    return isinstance(error, dict) and error.get("category") == WORKER_INTERRUPTED


@dataclass
class _CostResult:
    amount: Decimal
    is_estimated: bool


class _CategorizedProviderError(Exception):
    def __init__(self, category: str, message: str):
        self.category = category
        self.message = message
        super().__init__(message)


class _RetryableFailure(Exception):
    def __init__(self, category: str, message: str):
        self.category = category
        self.message = message
        super().__init__(message)


class _MissingReviewContext(Exception):
    """MA4: a REVIEWER/REPAIR run's input_context_json points at an
    artifact that no longer exists -- should never happen (artifacts are
    immutable and never deleted while their owning Agent Run exists), but
    handled as a proper categorized failure rather than an unhandled I/O
    error, consistent with every other failure category in this module."""


class _EvaluationContextTooLarge(Exception):
    """MA7.5A: the input for an MA6 agent evaluator (subject task text + the
    complete subject artifact + rubric) exceeds the configured limit -- a
    categorized failure (``evaluation_context_too_large``) raised before any
    provider call/budget spend; the evidence is never cut down."""


class _WorkflowContextTooLarge(Exception):
    """MA7.4c: the complete required upstream evidence exceeds the configured
    limit -- a categorized failure (``workflow_context_too_large``) raised
    before any provider call/budget spend; the evidence is never cut down."""


class _MissingWorkflowContext(Exception):
    """MA7.3b: a Workflow-dispatched Agent Run's upstream artifact is gone,
    unreadable, or no longer matches its recorded content hash -- a
    categorized failure (``workflow_context_error``) raised before any
    provider call/budget spend, same as the review/evaluation context
    failures."""


class _MissingEvaluationContext(Exception):
    """MA6 Slice 3B: an EVALUATOR run's input_context_json points at a
    subject artifact/Evaluation Definition Version that no longer exists,
    or the subject artifact's content hash no longer matches the hash
    frozen at Evaluation Run creation time -- checked here (before any
    provider call/budget spend) as well as again by
    app.services.evaluation_execution_service.finalize_agent_evaluator_run
    once the evaluator's own inference completes, same defense-in-depth
    the deterministic Evaluation Run path already uses."""
