"""Evaluation Execution Service — MA6 Slice 2, extended by Slice 3B.

Builds on MA6 Slice 1's immutable Evaluation Definition Registry and MA1's
job_queue/Worker plane (app.worker) exactly the way ComparisonService
builds on MA3's execution engine: this module never invokes a provider or
model itself and never runs a second inference pipeline.

Two independent methods, two independent execution paths, sharing the
same domain rows (``EvaluationRun``/``EvaluationCriterionResult``):

- ``EvaluationMethod.DETERMINISTIC`` (Slice 2): ``create_run``/``execute``,
  dispatched by the Worker via ``JobType.EVALUATION``, running only the
  narrow deterministic checkers registered in this module. A criterion
  whose key has no registered checker is recorded as
  ``EvaluationFinding.NOT_APPLICABLE``, never fabricated as MET/NOT_MET.
- ``EvaluationMethod.AGENT_EVALUATOR`` (Slice 3B): ``create_agent_evaluator_run``
  creates a normal, first-class Agent Run (``AgentRunRole.EVALUATOR``)
  under its own dedicated SINGLE_AGENT bookkeeping Task/Task Run --
  *never* the subject candidate's own -- and dispatches it through the
  **unmodified** MA3 ``AgentExecutionService``/``JobType.AGENT_RUN``
  pipeline (model resolution/override, ``ProviderModelSnapshot``,
  ``ModelCall``, ``BudgetGovernor``, retries, cancellation checkpoints,
  Artifact creation -- all reused verbatim, no second inference system).
  ``finalize_agent_evaluator_run`` (dispatched by
  app.services.evaluation_progress_service's terminal-notification seam,
  the same pattern MA5 already established for ComparisonRun) parses the
  evaluator's structured output via ``app.evaluation_contract`` once the
  evaluator's Agent Run reaches a terminal state.

No evaluator Judge/scoring/ranking/winner concept exists anywhere in this
module -- the evaluator provides evaluation *evidence*, never a verdict
that picks a winner (MA6 non-negotiable invariant).

Exact-artifact protection (Section 24.4 #18's hash-binding principle,
already used by ``app.models.reviews.AgentReview`` and
``ComparisonService.select_canonical``) is enforced twice for both
methods: once at creation (the artifact must belong to the subject Agent
Run and have a computed content hash to bind), and again immediately
before trusting any result (the artifact must still belong to that Agent
Run and its current content hash must still match the hash frozen at
bind time) -- never silently evaluating a different artifact than the one
an operator requested.
"""

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.enums import (
    AgentRunRole,
    EvaluationFinding,
    EvaluationMethod,
    EvaluationRunStatus,
    ExecutionMode,
    IdempotencyScope,
    JobType,
    TaskRunStatus,
    TaskStatus,
    VersionStatus,
)
from app.errors import ArtifactHashMismatchError, ConflictError, NotFoundError
from app.evaluation_contract import parse_evaluation_response
from app.models.agents import Agent, AgentVersion
from app.models.artifacts_eval import Artifact, ComparisonCandidate, ComparisonRun
from app.models.evaluation_definitions import (
    EvaluationCriterion,
    EvaluationDefinition,
    EvaluationDefinitionVersion,
)
from app.models.evaluation_runs import EvaluationCriterionResult, EvaluationRun
from app.models.tasks import AgentRun, AgentRunAttempt, Task, TaskRun
from app.repositories.job_queue_repository import JobQueueRepository
from app.services.base import BaseService
from app.services.comparison_service import _candidate_artifact
from app.services.flight_recorder import FlightRecorderService
from app.services.idempotency_service import BeginOutcome, IdempotencyService

logger = logging.getLogger("app.services.evaluation_execution_service")

# The only deterministic check Slice 2 ships: does the subject artifact
# contain any non-empty, non-whitespace content at all. Intentionally
# narrow -- a deterministic checker cannot semantically judge something
# like architecture quality or factual correctness, so it doesn't pretend
# to. An operator authors an EvaluationCriterion with this exact key
# (app.models.evaluation_definitions.EvaluationCriterion.key) to opt a
# criterion into this check; any other key falls through to
# NOT_APPLICABLE below.
NON_EMPTY_OUTPUT_CRITERION_KEY = "non_empty_output"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class ComparisonCandidateEvaluationOutcome:
    """One bounded per-candidate result of a comparison evaluation fan-out
    (Section: MA6 Slice 3C) -- never a comparative verdict across
    candidates, just "what happened when this candidate was evaluated."
    ``status`` is one of "created" (a new EvaluationRun was made),
    "reused" (an identical prior request already made one -- idempotent
    retry), "skipped" (the candidate is not yet eligible; not an error),
    or "failed" (eligible but EvaluationRun creation itself was rejected,
    e.g. a since-unpublished Evaluation Definition Version)."""

    comparison_candidate_id: str
    subject_agent_run_id: Optional[str]
    status: str
    evaluation_run_id: Optional[str] = None
    reason: Optional[str] = None


def _comparison_evaluation_idempotency_key(
    *,
    comparison_id: str,
    candidate_id: str,
    evaluation_definition_version_id: str,
    method: EvaluationMethod,
    evaluator_agent_version_id: Optional[str],
    evaluator_model_policy_override: Optional[dict],
) -> str:
    """Deterministic, content-derived key for one candidate's slot in one
    comparison evaluation fan-out request -- a retry of the exact same
    logical request (same comparison, candidate, Evaluation Definition
    Version, method, and evaluator configuration) always recomputes this
    same key, so IdempotencyService.begin naturally returns
    ALREADY_COMPLETED instead of creating a duplicate EvaluationRun/
    evaluator Task/Task Run/Agent Run/queued job -- no client-supplied
    Idempotency-Key header required, unlike the whole-request header
    pattern app.api.routers.comparisons already uses for create/launch
    (that pattern can't express "distinguish per candidate" within one
    request). ``comparison_id`` already pins a single, immutable project
    (Section: ComparisonRun.task_run_id -> Task.project_id never
    reassigned), so project isolation falls out of this key without
    needing to hash project_id separately. A *different* evaluator
    AgentVersion/model-policy-override/Evaluation Definition Version
    intentionally produces a different key, so those coexist as distinct
    historical EvaluationRuns rather than colliding."""
    override_fingerprint = "none"
    if evaluator_model_policy_override:
        override_fingerprint = hashlib.sha256(
            json.dumps(evaluator_model_policy_override, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:16]
    payload = "|".join(
        [
            comparison_id,
            candidate_id,
            evaluation_definition_version_id,
            method.value,
            evaluator_agent_version_id or "none",
            override_fingerprint,
        ]
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"comparison_evaluation:{digest}"


def _check_non_empty_output(
    content: str, artifact: Artifact
) -> Tuple[EvaluationFinding, str, Optional[List[str]]]:
    if content.strip():
        return (
            EvaluationFinding.MET,
            f"Artifact {artifact.id} contains non-empty content ({len(content)} characters).",
            [artifact.id],
        )
    return (
        EvaluationFinding.NOT_MET,
        f"Artifact {artifact.id} content is empty or whitespace-only.",
        [artifact.id],
    )


_DETERMINISTIC_CHECKERS: Dict[
    str, Callable[[str, Artifact], Tuple[EvaluationFinding, str, Optional[List[str]]]]
] = {
    NON_EMPTY_OUTPUT_CRITERION_KEY: _check_non_empty_output,
}


class EvaluationExecutionService(BaseService):
    def __init__(self, db: Session):
        super().__init__(db)
        self._jobs = JobQueueRepository()
        self.recorder = FlightRecorderService(db)

    # -- creation: operator-triggered, manual only ---------------------------

    def create_run(
        self,
        *,
        agent_run_id: str,
        subject_artifact_id: str,
        evaluation_definition_version_id: str,
        requested_by_user_id: Optional[str] = None,
    ) -> EvaluationRun:
        _agent_run, task_run, task = self._load_subject_chain(agent_run_id)

        artifact = self.db.get(Artifact, subject_artifact_id)
        if artifact is None:
            raise NotFoundError(f"Artifact {subject_artifact_id} not found.")
        if artifact.agent_run_id != agent_run_id:
            raise ConflictError(
                f"Artifact {subject_artifact_id} does not belong to Agent Run {agent_run_id}."
            )
        if not artifact.content_hash:
            raise ConflictError(
                f"Artifact {subject_artifact_id} has no computed content hash and cannot be evaluated."
            )

        version = self.db.get(EvaluationDefinitionVersion, evaluation_definition_version_id)
        if version is None:
            raise NotFoundError(
                f"Evaluation Definition Version {evaluation_definition_version_id} not found."
            )
        if version.status != VersionStatus.ACTIVE:
            raise ConflictError(
                f"Evaluation Definition Version {evaluation_definition_version_id} is not published "
                f"(status={version.status.value!r}) and cannot be used for evaluation."
            )
        definition = self.db.get(EvaluationDefinition, version.evaluation_definition_id)
        if definition is None or definition.project_id != task.project_id:
            raise ConflictError(
                f"Evaluation Definition Version {evaluation_definition_version_id} belongs to a different "
                f"project than Agent Run {agent_run_id}."
            )

        run = EvaluationRun(
            subject_agent_run_id=agent_run_id,
            subject_artifact_id=subject_artifact_id,
            subject_artifact_content_hash=artifact.content_hash,
            evaluation_definition_version_id=evaluation_definition_version_id,
            method=EvaluationMethod.DETERMINISTIC,
            status=EvaluationRunStatus.PENDING,
            requested_by_user_id=requested_by_user_id,
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)

        self._jobs.enqueue(self.db, job_type=JobType.EVALUATION, payload_ref=run.id)

        self._event(
            task,
            task_run,
            run,
            "evaluation_run.requested",
            decision_summary=(
                f"evaluation requested for artifact {subject_artifact_id} against definition version "
                f"{evaluation_definition_version_id}"
            ),
        )
        return run

    # -- creation: AGENT_EVALUATOR (Slice 3B) --------------------------------

    def create_agent_evaluator_run(
        self,
        *,
        agent_run_id: str,
        subject_artifact_id: str,
        evaluation_definition_version_id: str,
        evaluator_agent_version_id: str,
        evaluator_model_policy_override: Optional[dict] = None,
        budget_id: Optional[str] = None,
        requested_by_user_id: Optional[str] = None,
    ) -> EvaluationRun:
        """Creates one real, first-class evaluator Agent Run under its own
        dedicated SINGLE_AGENT bookkeeping Task/Task Run -- never the
        subject candidate's own (Section: MA6 Slice 3 frozen architecture)
        -- and dispatches it through the unmodified MA3 execution pipeline
        via ``JobType.AGENT_RUN`` (never ``JobType.EVALUATION``, which
        remains the deterministic path's own dispatch). One evaluator
        Agent Run performs exactly one inference covering the complete
        immutable rubric -- never one call per criterion."""
        _agent_run, task_run, task = self._load_subject_chain(agent_run_id)

        artifact = self.db.get(Artifact, subject_artifact_id)
        if artifact is None:
            raise NotFoundError(f"Artifact {subject_artifact_id} not found.")
        if artifact.agent_run_id != agent_run_id:
            raise ConflictError(
                f"Artifact {subject_artifact_id} does not belong to Agent Run {agent_run_id}."
            )
        if not artifact.content_hash:
            raise ConflictError(
                f"Artifact {subject_artifact_id} has no computed content hash and cannot be evaluated."
            )

        version = self.db.get(EvaluationDefinitionVersion, evaluation_definition_version_id)
        if version is None:
            raise NotFoundError(
                f"Evaluation Definition Version {evaluation_definition_version_id} not found."
            )
        if version.status != VersionStatus.ACTIVE:
            raise ConflictError(
                f"Evaluation Definition Version {evaluation_definition_version_id} is not published "
                f"(status={version.status.value!r}) and cannot be used for evaluation."
            )
        definition = self.db.get(EvaluationDefinition, version.evaluation_definition_id)
        if definition is None or definition.project_id != task.project_id:
            raise ConflictError(
                f"Evaluation Definition Version {evaluation_definition_version_id} belongs to a different "
                f"project than Agent Run {agent_run_id}."
            )

        evaluator_agent_version = self._require_published_agent_version(evaluator_agent_version_id)
        evaluator_agent = self.db.get(Agent, evaluator_agent_version.agent_id)
        if evaluator_agent is None or evaluator_agent.project_id != task.project_id:
            raise ConflictError(
                f"Evaluator Agent Version {evaluator_agent_version_id} belongs to a different project "
                f"than Agent Run {agent_run_id}."
            )

        # Dedicated internal bookkeeping Task/Task Run -- the evaluator's
        # own Agent Run is never attached to the subject's original Task
        # Run. Its title is scaffolding only; the real subject question/
        # context is delivered through extra_context
        # (execution_service._build_extra_context's "evaluation_request"
        # branch), never through this Task's own title/description.
        evaluator_task = Task(
            project_id=task.project_id,
            title=f"Evaluation — {definition.name} v{version.version}",
            execution_mode=ExecutionMode.SINGLE_AGENT,
            status=TaskStatus.READY,
        )
        self.db.add(evaluator_task)
        self.db.flush()

        evaluator_task_run = TaskRun(
            task_id=evaluator_task.id,
            status=TaskRunStatus.CREATED,
            budget_id=budget_id,
            timeout_seconds=settings.default_task_run_timeout_seconds,
        )
        self.db.add(evaluator_task_run)
        self.db.flush()

        evaluator_agent_run = AgentRun(
            task_run_id=evaluator_task_run.id,
            agent_version_id=evaluator_agent_version.id,
            role=AgentRunRole.EVALUATOR,
            model_policy_override_json=evaluator_model_policy_override,
            timeout_seconds=(
                evaluator_agent_version.timeout_seconds or settings.default_agent_run_timeout_seconds
            ),
            # Durable pointers only (never full content) -- a worker that
            # reclaims this job after a crash reconstructs the exact same
            # prompt from DB state alone, the same guarantee MA4's own
            # review_request/repair_request kinds already give.
            input_context_json={
                "kind": "evaluation_request",
                "subject_agent_run_id": agent_run_id,
                "subject_artifact_id": subject_artifact_id,
                "subject_artifact_hash": artifact.content_hash,
                "evaluation_definition_version_id": evaluation_definition_version_id,
            },
        )
        self.db.add(evaluator_agent_run)
        self.db.flush()

        run = EvaluationRun(
            subject_agent_run_id=agent_run_id,
            subject_artifact_id=subject_artifact_id,
            subject_artifact_content_hash=artifact.content_hash,
            evaluation_definition_version_id=evaluation_definition_version_id,
            method=EvaluationMethod.AGENT_EVALUATOR,
            # Set directly to RUNNING (not PENDING) at dispatch time --
            # same convention as ComparisonRun.status turning RUNNING at
            # launch_comparison, before any candidate Agent Run has
            # necessarily started. No separate "the worker actually picked
            # this up" signal exists without adding a second notification
            # seam this slice doesn't need.
            status=EvaluationRunStatus.RUNNING,
            requested_by_user_id=requested_by_user_id,
            evaluator_agent_version_id=evaluator_agent_version.id,
            evaluator_model_policy_override_json=evaluator_model_policy_override,
            evaluator_task_run_id=evaluator_task_run.id,
            evaluator_agent_run_id=evaluator_agent_run.id,
            started_at=_utcnow(),
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)

        evaluator_task_run.status = TaskRunStatus.QUEUED
        self.db.commit()

        self._jobs.enqueue(self.db, job_type=JobType.AGENT_RUN, payload_ref=evaluator_agent_run.id)

        self._event(
            task,
            task_run,
            run,
            "evaluation_run.requested",
            decision_summary=(
                f"agent-evaluator evaluation requested for artifact {subject_artifact_id} against "
                f"definition version {evaluation_definition_version_id} using evaluator Agent Version "
                f"{evaluator_agent_version_id}"
            ),
        )
        self._evaluator_event(
            evaluator_task,
            evaluator_task_run,
            evaluator_agent_run,
            "evaluation_run.evaluator_queued",
            agent_id=evaluator_agent_version.agent_id,
            agent_version_id=evaluator_agent_version.id,
            decision_summary=f"evaluator Agent Run {evaluator_agent_run.id} queued for evaluation run {run.id}",
        )
        return run

    # -- creation: Comparison fan-out (Slice 3C) -----------------------------

    def analyze_comparison(
        self,
        *,
        comparison_id: str,
        evaluation_definition_version_id: str,
        method: EvaluationMethod = EvaluationMethod.DETERMINISTIC,
        evaluator_agent_version_id: Optional[str] = None,
        evaluator_model_policy_override: Optional[dict] = None,
        budget_id: Optional[str] = None,
        requested_by_user_id: Optional[str] = None,
    ) -> List[ComparisonCandidateEvaluationOutcome]:
        """Fans out one independent EvaluationRun per eligible
        ComparisonCandidate under one ComparisonRun (Section: MA6 Slice
        3C) -- every candidate is evaluated exactly as if create_run/
        create_agent_evaluator_run had been called for it one at a time;
        no second inference pipeline, no comparative/ranking evaluator
        call across candidates, no ComparisonRun/ComparisonCandidate
        winner-selection state ever read or written here (that remains
        ComparisonService's exclusive surface -- app.services.
        comparison_service.select_canonical). A candidate not yet
        eligible (not launched, its Task Run not COMPLETED, or no
        resolvable output Artifact) is recorded as "skipped" rather than
        aborting its siblings; a candidate whose EvaluationRun creation
        itself fails (e.g. a since-unpublished definition version) is
        recorded as "failed" the same way -- one candidate's outcome never
        prevents any other candidate's independent EvaluationRun."""
        if method == EvaluationMethod.AGENT_EVALUATOR and not evaluator_agent_version_id:
            raise ConflictError("evaluator_agent_version_id is required when method is agent_evaluator.")

        comparison = self.db.get(ComparisonRun, comparison_id)
        if comparison is None:
            raise NotFoundError(f"Comparison {comparison_id} not found.")

        candidates = list(
            self.db.execute(
                select(ComparisonCandidate)
                .where(ComparisonCandidate.comparison_run_id == comparison_id)
                .order_by(ComparisonCandidate.created_at)
            )
            .scalars()
            .all()
        )

        idempotency = IdempotencyService(self.db)
        return [
            self._analyze_one_candidate(
                comparison_id=comparison_id,
                candidate=candidate,
                evaluation_definition_version_id=evaluation_definition_version_id,
                method=method,
                evaluator_agent_version_id=evaluator_agent_version_id,
                evaluator_model_policy_override=evaluator_model_policy_override,
                budget_id=budget_id,
                requested_by_user_id=requested_by_user_id,
                idempotency=idempotency,
            )
            for candidate in candidates
        ]

    def _analyze_one_candidate(
        self,
        *,
        comparison_id: str,
        candidate: ComparisonCandidate,
        evaluation_definition_version_id: str,
        method: EvaluationMethod,
        evaluator_agent_version_id: Optional[str],
        evaluator_model_policy_override: Optional[dict],
        budget_id: Optional[str],
        requested_by_user_id: Optional[str],
        idempotency: IdempotencyService,
    ) -> ComparisonCandidateEvaluationOutcome:
        if candidate.agent_run_id is None or candidate.task_run_id is None:
            return ComparisonCandidateEvaluationOutcome(
                comparison_candidate_id=candidate.id,
                subject_agent_run_id=candidate.agent_run_id,
                status="skipped",
                reason="candidate has not been launched yet (no Agent Run).",
            )

        candidate_run = self.db.get(TaskRun, candidate.task_run_id)
        if candidate_run is None or candidate_run.status != TaskRunStatus.COMPLETED:
            observed = candidate_run.status.value if candidate_run is not None else "unknown"
            return ComparisonCandidateEvaluationOutcome(
                comparison_candidate_id=candidate.id,
                subject_agent_run_id=candidate.agent_run_id,
                status="skipped",
                reason=f"candidate Task Run status is {observed!r}, not completed.",
            )

        artifact = _candidate_artifact(self.db, candidate, candidate_run)
        if artifact is None or not artifact.content_hash:
            return ComparisonCandidateEvaluationOutcome(
                comparison_candidate_id=candidate.id,
                subject_agent_run_id=candidate.agent_run_id,
                status="skipped",
                reason="no valid output artifact could be resolved for this candidate.",
            )

        key = _comparison_evaluation_idempotency_key(
            comparison_id=comparison_id,
            candidate_id=candidate.id,
            evaluation_definition_version_id=evaluation_definition_version_id,
            method=method,
            evaluator_agent_version_id=evaluator_agent_version_id,
            evaluator_model_policy_override=evaluator_model_policy_override,
        )
        begin = idempotency.begin(
            key, scope=IdempotencyScope.API_REQUEST, resource_type="comparison_evaluation"
        )
        if begin.outcome == BeginOutcome.ALREADY_COMPLETED:
            run_id = (begin.key_row.result_ref or {}).get("evaluation_run_id")
            return ComparisonCandidateEvaluationOutcome(
                comparison_candidate_id=candidate.id,
                subject_agent_run_id=candidate.agent_run_id,
                status="reused",
                evaluation_run_id=run_id,
            )

        try:
            if method == EvaluationMethod.AGENT_EVALUATOR:
                # analyze_comparison already validated this is set for
                # method=agent_evaluator -- narrows Optional[str] -> str.
                assert evaluator_agent_version_id is not None
                run = self.create_agent_evaluator_run(
                    agent_run_id=candidate.agent_run_id,
                    subject_artifact_id=artifact.id,
                    evaluation_definition_version_id=evaluation_definition_version_id,
                    evaluator_agent_version_id=evaluator_agent_version_id,
                    evaluator_model_policy_override=evaluator_model_policy_override,
                    budget_id=budget_id,
                    requested_by_user_id=requested_by_user_id,
                )
            else:
                run = self.create_run(
                    agent_run_id=candidate.agent_run_id,
                    subject_artifact_id=artifact.id,
                    evaluation_definition_version_id=evaluation_definition_version_id,
                    requested_by_user_id=requested_by_user_id,
                )
        except (NotFoundError, ConflictError) as exc:
            self.db.rollback()
            idempotency.fail(key)
            return ComparisonCandidateEvaluationOutcome(
                comparison_candidate_id=candidate.id,
                subject_agent_run_id=candidate.agent_run_id,
                status="failed",
                reason=str(exc),
            )
        except Exception:
            self.db.rollback()
            idempotency.fail(key)
            logger.exception(
                "comparison evaluation fan-out: unexpected error creating EvaluationRun "
                "for candidate %s",
                candidate.id,
            )
            return ComparisonCandidateEvaluationOutcome(
                comparison_candidate_id=candidate.id,
                subject_agent_run_id=candidate.agent_run_id,
                status="failed",
                reason="internal error creating evaluation run for this candidate.",
            )

        idempotency.complete(key, result_ref={"evaluation_run_id": run.id})
        return ComparisonCandidateEvaluationOutcome(
            comparison_candidate_id=candidate.id,
            subject_agent_run_id=candidate.agent_run_id,
            status="created",
            evaluation_run_id=run.id,
        )

    def get_run(self, evaluation_run_id: str) -> Optional[EvaluationRun]:
        return self.db.get(EvaluationRun, evaluation_run_id)

    def list_runs_for_agent_run(self, *, agent_run_id: str) -> List[EvaluationRun]:
        stmt = (
            select(EvaluationRun)
            .where(EvaluationRun.subject_agent_run_id == agent_run_id)
            .order_by(EvaluationRun.created_at)
        )
        return list(self.db.execute(stmt).scalars().all())

    # -- execution: called by the Worker (JobType.EVALUATION) ---------------

    def execute(self, evaluation_run_id: str, *, worker_id: str) -> None:
        run = self.db.get(EvaluationRun, evaluation_run_id)
        if run is None:
            logger.error(
                "evaluation_run_missing evaluation_run_id=%s worker_id=%s", evaluation_run_id, worker_id
            )
            return
        if run.status != EvaluationRunStatus.PENDING:
            logger.warning(
                "evaluation_run_not_pending evaluation_run_id=%s status=%s worker_id=%s",
                evaluation_run_id,
                run.status.value,
                worker_id,
            )
            return

        agent_run = self.db.get(AgentRun, run.subject_agent_run_id)
        task_run = self.db.get(TaskRun, agent_run.task_run_id) if agent_run else None
        task = self.db.get(Task, task_run.task_id) if task_run else None
        if agent_run is None or task_run is None or task is None:
            # Subject Agent Run's own lineage no longer resolves -- there is
            # nowhere to safely record a Flight Recorder event (task_id/
            # task_run_id are required columns), so this finalizes with a
            # log line only, same as AgentExecutionService._load_context's
            # ExecutionAborted path finding no context to report through.
            run.status = EvaluationRunStatus.FAILED
            run.ended_at = _utcnow()
            run.failure_reason = "Subject Agent Run's Task Run/Task no longer exists."
            self.db.commit()
            logger.error(
                "evaluation_run_subject_chain_missing evaluation_run_id=%s worker_id=%s",
                evaluation_run_id,
                worker_id,
            )
            return

        run.status = EvaluationRunStatus.RUNNING
        run.started_at = _utcnow()
        self.db.commit()
        self._event(task, task_run, run, "evaluation_run.started", decision_summary="evaluation run started")

        try:
            self._run_checks(run)
        except Exception as exc:
            # Anything not already handled inside _run_checks (a bug, a
            # missing row, a hash mismatch it raised) must still leave the
            # Evaluation Run in a terminal state rather than stuck RUNNING
            # forever -- same worker-failure-handling guarantee
            # AgentExecutionService.execute gives Agent Runs.
            logger.exception(
                "evaluation_run_execution_error evaluation_run_id=%s worker_id=%s",
                evaluation_run_id,
                worker_id,
            )
            run.status = EvaluationRunStatus.FAILED
            run.ended_at = _utcnow()
            run.failure_reason = str(exc)
            self.db.commit()
            self._event(
                task,
                task_run,
                run,
                "evaluation_run.failed",
                decision_summary=str(exc),
                error={"message": str(exc)},
            )
            return

        run.status = EvaluationRunStatus.COMPLETED
        run.ended_at = _utcnow()
        self.db.commit()
        self._event(
            task,
            task_run,
            run,
            "evaluation_run.completed",
            decision_summary=f"{len(run.criterion_results)} criteria evaluated",
        )

    # -- finalization: AGENT_EVALUATOR (Slice 3B) ----------------------------
    # Dispatched by app.services.evaluation_progress_service's terminal-
    # notification seam once the evaluator's own dedicated bookkeeping
    # Task Run reaches a terminal status -- mirrors the seam MA5 already
    # established for ComparisonRun, as an independent, parallel consumer
    # (never touches comparison_progress_service.py or ComparisonRun/
    # ComparisonCandidate state).

    def finalize_agent_evaluator_run(self, evaluation_run_id: str, *, task_run_status: TaskRunStatus) -> None:
        run = self.db.get(EvaluationRun, evaluation_run_id)
        if run is None:
            logger.error("evaluation_run_missing evaluation_run_id=%s", evaluation_run_id)
            return
        if run.status not in (EvaluationRunStatus.PENDING, EvaluationRunStatus.RUNNING):
            logger.warning(
                "evaluation_run_not_awaiting_evaluator evaluation_run_id=%s status=%s",
                evaluation_run_id,
                run.status.value,
            )
            return

        evaluator_agent_run = self.db.get(AgentRun, run.evaluator_agent_run_id)
        task_run = self.db.get(TaskRun, run.evaluator_task_run_id)
        task = self.db.get(Task, task_run.task_id) if task_run else None
        if evaluator_agent_run is None or task_run is None or task is None:
            run.status = EvaluationRunStatus.FAILED
            run.ended_at = _utcnow()
            run.failure_reason = "Evaluator Agent Run's own Task Run/Task no longer exists."
            self.db.commit()
            logger.error("evaluation_run_evaluator_chain_missing evaluation_run_id=%s", evaluation_run_id)
            return

        if task_run_status == TaskRunStatus.CANCELLED:
            run.status = EvaluationRunStatus.CANCELLED
            run.ended_at = _utcnow()
            run.failure_reason = "Evaluator Agent Run was cancelled."
            self.db.commit()
            self._evaluator_event(
                task,
                task_run,
                evaluator_agent_run,
                "evaluation_run.cancelled",
                decision_summary="evaluator Agent Run cancelled",
            )
            return

        if task_run_status == TaskRunStatus.FAILED:
            reason = self._describe_agent_run_failure(evaluator_agent_run)
            run.status = EvaluationRunStatus.FAILED
            run.ended_at = _utcnow()
            run.failure_reason = reason
            self.db.commit()
            self._evaluator_event(
                task,
                task_run,
                evaluator_agent_run,
                "evaluation_run.failed",
                decision_summary=reason,
                error={"message": reason},
            )
            return

        # COMPLETED: the evaluator Agent Run itself succeeded (inference
        # happened) -- revalidate and parse before trusting anything it
        # said.
        try:
            self._finalize_agent_evaluator_success(run, evaluator_agent_run, task, task_run)
        except Exception as exc:
            # Anything not already handled inside (a bug, a missing row, a
            # hash mismatch it raised) must still leave the Evaluation Run
            # in a terminal state rather than stuck RUNNING forever -- same
            # worker-failure-handling guarantee AgentExecutionService.execute
            # gives Agent Runs.
            logger.exception("evaluation_run_finalize_error evaluation_run_id=%s", evaluation_run_id)
            run.status = EvaluationRunStatus.FAILED
            run.ended_at = _utcnow()
            run.failure_reason = str(exc)
            self.db.commit()
            self._evaluator_event(
                task,
                task_run,
                evaluator_agent_run,
                "evaluation_run.failed",
                decision_summary=str(exc),
                error={"message": str(exc)},
            )

    def _finalize_agent_evaluator_success(
        self, run: EvaluationRun, evaluator_agent_run: AgentRun, task: Task, task_run: TaskRun
    ) -> None:
        subject_artifact = self.db.get(Artifact, run.subject_artifact_id)
        if subject_artifact is None or subject_artifact.agent_run_id != run.subject_agent_run_id:
            raise ConflictError(
                f"Artifact {run.subject_artifact_id} no longer belongs to Agent Run "
                f"{run.subject_agent_run_id} -- refusing to trust an evaluation of a different artifact "
                "than was bound."
            )
        if subject_artifact.content_hash != run.subject_artifact_content_hash:
            raise ArtifactHashMismatchError(
                "Subject artifact content changed since this Evaluation Run was created -- refusing to "
                "trust an evaluation of a different artifact than was bound.",
                detail={
                    "expected_artifact_hash": run.subject_artifact_content_hash,
                    "actual_artifact_hash": subject_artifact.content_hash,
                },
            )

        version = self.db.get(EvaluationDefinitionVersion, run.evaluation_definition_version_id)
        if version is None:
            raise NotFoundError(
                f"Evaluation Definition Version {run.evaluation_definition_version_id} not found."
            )

        output_artifact = (
            self.db.execute(select(Artifact).where(Artifact.agent_run_id == evaluator_agent_run.id))
            .scalars()
            .first()
        )
        if output_artifact is None:
            raise ConflictError(f"Evaluator Agent Run {evaluator_agent_run.id} produced no output artifact.")

        raw_text = Path(output_artifact.storage_ref).read_text(encoding="utf-8")
        criteria_by_key = {c.key: c for c in version.criteria}
        parsed = parse_evaluation_response(raw_text, expected_criterion_keys=list(criteria_by_key.keys()))

        if not parsed.is_valid:
            run.status = EvaluationRunStatus.FAILED
            run.ended_at = _utcnow()
            run.failure_reason = parsed.parse_error
            self.db.commit()
            self._evaluator_event(
                task,
                task_run,
                evaluator_agent_run,
                "evaluation_run.failed",
                artifact_refs=[output_artifact.id],
                decision_summary=parsed.parse_error,
                error={"category": "evaluator_response_invalid", "message": parsed.parse_error},
            )
            return

        for finding in parsed.findings:
            criterion = criteria_by_key[finding.key]
            evidence_refs = [
                {"quote": e.quote, "criterion_key": e.criterion_key} for e in finding.evidence
            ] or None
            self.db.add(
                EvaluationCriterionResult(
                    evaluation_run_id=run.id,
                    evaluation_criterion_id=criterion.id,
                    criterion_key=criterion.key,
                    order_index=criterion.order_index,
                    finding=finding.finding,
                    rationale=finding.rationale,
                    evidence_refs=evidence_refs,
                )
            )
        run.status = EvaluationRunStatus.COMPLETED
        run.ended_at = _utcnow()
        self.db.commit()
        self.db.refresh(run)
        self._evaluator_event(
            task,
            task_run,
            evaluator_agent_run,
            "evaluation_run.completed",
            artifact_refs=[output_artifact.id],
            decision_summary=f"{len(parsed.findings)} criteria evaluated",
        )

    def _describe_agent_run_failure(self, evaluator_agent_run: AgentRun) -> str:
        stmt = (
            select(AgentRunAttempt)
            .where(AgentRunAttempt.agent_run_id == evaluator_agent_run.id)
            .order_by(AgentRunAttempt.attempt_number.desc())
        )
        latest_attempt = self.db.execute(stmt).scalars().first()
        if latest_attempt is not None and latest_attempt.error:
            category = latest_attempt.error.get("category", "unknown")
            message = latest_attempt.error.get("message", "")
            return f"evaluator Agent Run failed ({category}): {message}"
        return f"evaluator Agent Run {evaluator_agent_run.id} failed."

    # -- internals ------------------------------------------------------------

    def _run_checks(self, run: EvaluationRun) -> None:
        artifact = self.db.get(Artifact, run.subject_artifact_id)
        if artifact is None or artifact.agent_run_id != run.subject_agent_run_id:
            raise ConflictError(
                f"Artifact {run.subject_artifact_id} no longer belongs to Agent Run {run.subject_agent_run_id} "
                "-- refusing to evaluate a different artifact than was bound."
            )
        if artifact.content_hash != run.subject_artifact_content_hash:
            raise ArtifactHashMismatchError(
                "Artifact content changed since this Evaluation Run was created -- refusing to evaluate a "
                "different artifact than was bound.",
                detail={
                    "expected_artifact_hash": run.subject_artifact_content_hash,
                    "actual_artifact_hash": artifact.content_hash,
                },
            )

        version = self.db.get(EvaluationDefinitionVersion, run.evaluation_definition_version_id)
        if version is None:
            raise NotFoundError(
                f"Evaluation Definition Version {run.evaluation_definition_version_id} not found."
            )

        content = Path(artifact.storage_ref).read_text(encoding="utf-8")

        for criterion in version.criteria:
            finding, rationale, evidence_refs = self._evaluate_criterion(criterion, content, artifact)
            self.db.add(
                EvaluationCriterionResult(
                    evaluation_run_id=run.id,
                    evaluation_criterion_id=criterion.id,
                    criterion_key=criterion.key,
                    order_index=criterion.order_index,
                    finding=finding,
                    rationale=rationale,
                    evidence_refs=evidence_refs,
                )
            )
        self.db.commit()
        self.db.refresh(run)

    def _evaluate_criterion(
        self, criterion: EvaluationCriterion, content: str, artifact: Artifact
    ) -> Tuple[EvaluationFinding, str, Optional[List[str]]]:
        checker = _DETERMINISTIC_CHECKERS.get(criterion.key)
        if checker is None:
            return (
                EvaluationFinding.NOT_APPLICABLE,
                (
                    f"No deterministic checker is registered for criterion key {criterion.key!r} -- this "
                    "criterion requires an evaluator Agent, not implemented until MA6 Slice 3."
                ),
                None,
            )
        return checker(content, artifact)

    def _load_subject_chain(self, agent_run_id: str) -> Tuple[AgentRun, TaskRun, Task]:
        agent_run = self.db.get(AgentRun, agent_run_id)
        if agent_run is None:
            raise NotFoundError(f"Agent Run {agent_run_id} not found.")
        task_run = self.db.get(TaskRun, agent_run.task_run_id)
        if task_run is None:
            raise NotFoundError(f"Task Run {agent_run.task_run_id} not found.")
        task = self.db.get(Task, task_run.task_id)
        if task is None:
            raise NotFoundError(f"Task {task_run.task_id} not found.")
        return agent_run, task_run, task

    def _require_published_agent_version(self, agent_version_id: str) -> AgentVersion:
        """Same published/ACTIVE check as
        ComparisonService._get_published_agent_version -- an evaluator
        Agent Version is validated exactly like any other Agent Version
        used to run something."""
        agent_version = self.db.get(AgentVersion, agent_version_id)
        if agent_version is None:
            raise NotFoundError(f"Agent Version {agent_version_id} not found.")
        if agent_version.status != VersionStatus.ACTIVE:
            raise ConflictError(
                f"Agent Version {agent_version_id} is not published (status="
                f"{agent_version.status.value!r}) and cannot be used as an evaluator."
            )
        return agent_version

    def _event(
        self, task: Task, task_run: TaskRun, run: EvaluationRun, event_type: str, **fields: Any
    ) -> None:
        self.recorder.record(
            task_id=task.id,
            task_run_id=task_run.id,
            agent_run_id=run.subject_agent_run_id,
            artifact_refs=[run.subject_artifact_id],
            event_type=event_type,
            **fields,
        )

    def _evaluator_event(
        self, task: Task, task_run: TaskRun, evaluator_agent_run: AgentRun, event_type: str, **fields: Any
    ) -> None:
        """Like ``_event``, but anchored to the evaluator's own dedicated
        bookkeeping Task/Task Run/Agent Run -- never the subject's
        (``_event`` hardcodes ``run.subject_agent_run_id``/
        ``subject_artifact_id``, which would misattribute an event
        recorded under the evaluator's own Task Run stream)."""
        self.recorder.record(
            task_id=task.id,
            task_run_id=task_run.id,
            agent_run_id=evaluator_agent_run.id,
            event_type=event_type,
            **fields,
        )
