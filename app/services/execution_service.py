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
"""

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.db.enums import (
    AgentRunAttemptStatus,
    AgentRunStatus,
    ArtifactType,
    ExecutionMode,
    ModelCallStatus,
    ModelSelectionMode,
    PricingClassification,
    RouterFreePolicy,
    TaskRunStatus,
    UsageSourceType,
)
from app.model_resolution import (
    ModelUnavailableError,
    NoEligibleModelError,
    ResolvedModel,
    freeze_snapshot,
    resolve_auto,
    resolve_manual,
)
from app.models.agents import AgentVersion, PromptVersion
from app.models.artifacts_eval import Artifact
from app.models.execution import ModelCall
from app.models.identity import Project
from app.models.tasks import AgentRun, AgentRunAttempt, Task, TaskRun
from app.prompt_builder import build_prompt
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
from app.services.budget_service import BudgetExceededError, BudgetGovernor, estimate_cost
from app.services.comparison_progress_service import notify_task_run_terminal
from app.services.flight_recorder import FlightRecorderService
from app.services.review_orchestration_service import ReviewOrchestrationService

logger = logging.getLogger("app.services.execution_service")

_RETRYABLE_ERROR_CATEGORIES = {"provider_connection_error", "provider_timeout"}


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

        for attempt_number in range(1, settings.max_agent_run_attempts + 1):
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
        )

    def _is_cancelled(self, ctx: _Context) -> bool:
        self.db.refresh(ctx.task_run)
        return ctx.task_run.cancellation_requested_at is not None

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
        self.db.commit()
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
            resolved = self._resolve_model(ctx)
        except (ModelUnavailableError, NoEligibleModelError) as exc:
            self._finalize_failed(ctx, category="model_resolution_error", message=str(exc), attempt=attempt)
            raise ExecutionAborted(str(exc)) from exc

        self._event(
            ctx,
            "agent_run.model_selected",
            model_id=resolved.model.id,
            provider_id=resolved.provider.id,
            decision_summary=resolved.rationale,
        )

        snapshot = freeze_snapshot(self.db, resolved)
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
            extra_context = self._build_extra_context(agent_run)
        except _MissingReviewContext as exc:
            self.governor.release(reservation)
            self._finalize_failed(ctx, category="review_context_error", message=str(exc), attempt=attempt)
            raise ExecutionAborted(str(exc)) from exc

        assembly = build_prompt(
            agent_version=ctx.agent_version,
            prompt_version=ctx.prompt_version,
            task=ctx.task,
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

        # 4. Provider invocation ----------------------------------------------
        model_call = ModelCall(
            agent_run_id=agent_run.id,
            agent_run_attempt_id=attempt.id,
            model_id=resolved.model.id,
            provider_id=resolved.provider.id,
            provider_model_id=resolved.provider_model.id,
            provider_model_snapshot_id=snapshot.id,
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
        )

        try:
            response = self._invoke(adapter, request)
        except _CategorizedProviderError as exc:
            model_call.status = (
                ModelCallStatus.ERROR if exc.category != "provider_timeout" else ModelCallStatus.TIMEOUT
            )
            model_call.error = {"category": exc.category, "message": exc.message}
            model_call.completed_at = _utcnow()
            self.db.commit()
            self._event(
                ctx,
                "model_call.failed",
                decision_summary=exc.message,
                error={"category": exc.category, "message": exc.message},
            )
            self.governor.release(reservation)

            if exc.category in _RETRYABLE_ERROR_CATEGORIES:
                attempt.status = AgentRunAttemptStatus.FAILED
                attempt.ended_at = _utcnow()
                attempt.error = {"category": exc.category, "message": exc.message}
                self.db.commit()
                raise _RetryableFailure(exc.category, exc.message) from exc

            self._finalize_failed(ctx, category=exc.category, message=exc.message, attempt=attempt)
            raise ExecutionAborted(exc.message) from exc

        # 5. Success: record usage/cost, artifact, finalize ---------------------
        actual_cost = self._actual_cost(resolved, response)
        model_call.tokens_in = response.tokens_in
        model_call.tokens_out = response.tokens_out
        model_call.cost_amount = actual_cost.amount
        model_call.cost_is_estimated = actual_cost.is_estimated
        model_call.latency_ms = response.latency_ms
        model_call.provider_request_id = response.provider_request_id
        model_call.status = ModelCallStatus.SUCCESS
        model_call.completed_at = _utcnow()
        self.db.commit()

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

        attempt.status = AgentRunAttemptStatus.COMPLETED
        attempt.ended_at = _utcnow()
        self.db.commit()

        if self._is_cancelled(ctx):
            self._finalize_cancelled(ctx, attempt, already_produced=True)
            return

        agent_run.status = AgentRunStatus.COMPLETED
        agent_run.ended_at = _utcnow()
        self.db.commit()
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

    # -- model resolution --------------------------------------------------

    def _resolve_model(self, ctx: _Context) -> ResolvedModel:
        # MA5: a comparison candidate may override its Agent Version's own
        # (immutable, shared, published) model_policy to resolve a
        # *different* concrete model without needing a second Agent
        # Version just to vary the model ("same Agent, different models").
        # Absent an override (the MA3/MA4 default), behavior is unchanged.
        policy = ctx.agent_run.model_policy_override_json or ctx.agent_version.model_policy or {}
        mode = policy.get("mode", ModelSelectionMode.MANUAL.value)
        if mode == ModelSelectionMode.MANUAL.value:
            provider_model_id = policy.get("manual_provider_model_id")
            if not provider_model_id:
                raise ModelUnavailableError(
                    "Agent Version model_policy is manual but has no manual_provider_model_id."
                )
            return resolve_manual(self.db, provider_model_id)

        auto_policy_value = policy.get("auto_policy")
        if not auto_policy_value:
            raise ModelUnavailableError("Agent Version model_policy is auto but has no auto_policy.")
        return resolve_auto(self.db, RouterFreePolicy(auto_policy_value))

    # -- extra prompt context (MA4 reviewer/repair runs only) --------------

    def _build_extra_context(self, agent_run: AgentRun) -> Optional[str]:
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

        return None

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
    ) -> None:
        if attempt is not None and attempt.status == AgentRunAttemptStatus.RUNNING:
            attempt.status = AgentRunAttemptStatus.FAILED
            attempt.ended_at = _utcnow()
            attempt.error = {"category": category, "message": message}
        ctx.agent_run.status = AgentRunStatus.FAILED
        ctx.agent_run.ended_at = _utcnow()
        self.db.commit()
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

    def _finalize_cancelled(
        self, ctx: _Context, attempt: AgentRunAttempt, *, already_produced: bool = False
    ) -> None:
        attempt.status = AgentRunAttemptStatus.FAILED
        attempt.ended_at = _utcnow()
        attempt.error = {"category": "cancelled", "message": "cancellation requested"}
        ctx.agent_run.status = AgentRunStatus.STOPPED
        ctx.agent_run.ended_at = _utcnow()
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
