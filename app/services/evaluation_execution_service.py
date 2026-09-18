"""Evaluation Execution Service — MA6 Slice 2.

Builds on MA6 Slice 1's immutable Evaluation Definition Registry and MA1's
job_queue/Worker plane (app.worker) exactly the way ComparisonService
builds on MA3's execution engine: this module never invokes a provider or
model, and never runs a second inference pipeline -- ``create_run`` only
ever decides whether an Evaluation Run may be created, and ``execute``
(dispatched by the Worker via ``JobType.EVALUATION``) only ever runs the
*deterministic* checkers registered in this module.

No evaluator Agent, evaluator Role, or model inference exists here --
that is MA6 Slice 3's addition. A criterion whose key has no registered
deterministic checker is recorded as ``EvaluationFinding.NOT_APPLICABLE``,
never fabricated as MET/NOT_MET (Section: MA6 non-negotiable invariant --
a deterministic check must never pretend to semantically evaluate
something like architecture quality or factual correctness).

Exact-artifact protection (Section 24.4 #18's hash-binding principle,
already used by ``app.models.reviews.AgentReview`` and
``ComparisonService.select_canonical``) is enforced twice: once at
``create_run`` (the artifact must belong to the subject Agent Run and have
a computed content hash to bind), and again at ``execute`` immediately
before reading any content (the artifact must still belong to that Agent
Run and its current content hash must still match the hash frozen at
bind time) -- never silently evaluating a different artifact than the one
an operator requested.
"""

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.enums import EvaluationFinding, EvaluationMethod, EvaluationRunStatus, JobType, VersionStatus
from app.errors import ArtifactHashMismatchError, ConflictError, NotFoundError
from app.models.artifacts_eval import Artifact
from app.models.evaluation_definitions import (
    EvaluationCriterion,
    EvaluationDefinition,
    EvaluationDefinitionVersion,
)
from app.models.evaluation_runs import EvaluationCriterionResult, EvaluationRun
from app.models.tasks import AgentRun, Task, TaskRun
from app.repositories.job_queue_repository import JobQueueRepository
from app.services.base import BaseService
from app.services.flight_recorder import FlightRecorderService

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
