"""Task Service — Section 11. Task CRUD, Task Run creation/cancellation.

Real implementation, MA3. ``start_task_run`` is the one place the queue-
based execution contract (Section 24.4 #12) begins: it persists the
Task Run + Agent Run in one short transaction, then enqueues exactly one
``JobType.AGENT_RUN`` job whose ``payload_ref`` is the new Agent Run's id
— the local worker (``app.worker``) is the only thing that ever actually
invokes a provider. No LLM call happens inside this method or the HTTP
request that calls it.
"""

from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import select

from app.config import settings
from app.db.enums import (
    AgentRunRole,
    AgentRunStatus,
    ExecutionMode,
    JobType,
    TaskRunStatus,
    TaskStatus,
    VersionStatus,
)
from app.errors import ConflictError, InvalidStateTransitionError, NotFoundError
from app.models.agents import AgentVersion
from app.models.tasks import AgentRun, Task, TaskRun
from app.repositories.job_queue_repository import JobQueueRepository
from app.services.base import BaseService
from app.services.flight_recorder import FlightRecorderService


class TaskService(BaseService):
    def __init__(self, db):
        super().__init__(db)
        self._jobs = JobQueueRepository()
        self._recorder = FlightRecorderService(db)

    # -- Task CRUD --------------------------------------------------------

    def create_task(
        self,
        *,
        project_id: str,
        title: str,
        description: Optional[str],
        execution_mode,
        requirements: Optional[dict],
        created_by: Optional[str],
    ) -> Task:
        task = Task(
            project_id=project_id,
            title=title,
            description=description,
            execution_mode=execution_mode,
            requirements=requirements,
            created_by=created_by,
            status=TaskStatus.READY,
        )
        self.db.add(task)
        self.db.commit()
        self.db.refresh(task)
        return task

    def get_task(self, task_id: str) -> Task:
        task = self.db.get(Task, task_id)
        if task is None:
            raise NotFoundError(f"Task {task_id} not found.")
        return task

    def list_tasks(self, *, project_id: str) -> List[Task]:
        stmt = select(Task).where(Task.project_id == project_id).order_by(Task.created_at.desc())
        return list(self.db.execute(stmt).scalars().all())

    # -- Task Run lifecycle -------------------------------------------------

    def start_task_run(
        self, *, task_id: str, agent_version_id: str, budget_id: Optional[str] = None
    ) -> TaskRun:
        task = self.get_task(task_id)

        agent_version = self.db.get(AgentVersion, agent_version_id)
        if agent_version is None:
            raise NotFoundError(f"Agent Version {agent_version_id} not found.")
        if agent_version.status != VersionStatus.ACTIVE:
            raise ConflictError(
                f"Agent Version {agent_version_id} is not published (status="
                f"{agent_version.status.value!r}) and cannot be run."
            )

        config_snapshot = {
            "task_title": task.title,
            "task_description": task.description,
            "execution_mode": task.execution_mode.value,
            "agent_version_id": agent_version.id,
            "agent_version_number": agent_version.version,
            "model_policy": agent_version.model_policy,
            "budget_id": budget_id,
        }

        task_run = TaskRun(
            task_id=task.id,
            status=TaskRunStatus.CREATED,
            budget_id=budget_id,
            config_snapshot=config_snapshot,
            timeout_seconds=settings.default_task_run_timeout_seconds,
        )
        self.db.add(task_run)
        self.db.flush()

        agent_run = AgentRun(
            task_run_id=task_run.id,
            agent_version_id=agent_version.id,
            status=AgentRunStatus.CREATED,
            timeout_seconds=agent_version.timeout_seconds or settings.default_agent_run_timeout_seconds,
        )
        self.db.add(agent_run)
        self.db.flush()

        self._recorder.record(
            task_id=task.id,
            task_run_id=task_run.id,
            agent_run_id=agent_run.id,
            agent_id=agent_version.agent_id,
            agent_version_id=agent_version.id,
            event_type="task_run.created",
            decision_summary=f"task_run created for task {task.id} using agent_version {agent_version.id}",
        )

        task_run.status = TaskRunStatus.QUEUED
        self.db.commit()
        self.db.refresh(task_run)

        self._jobs.enqueue(self.db, job_type=JobType.AGENT_RUN, payload_ref=agent_run.id)
        self._recorder.record(
            task_id=task.id,
            task_run_id=task_run.id,
            agent_run_id=agent_run.id,
            event_type="task_run.queued",
            decision_summary="agent_run job enqueued for worker pickup",
        )

        return task_run

    def start_reviewed_task_run(
        self,
        *,
        task_id: str,
        primary_agent_version_id: str,
        reviewer_agent_version_id: str,
        max_repair_iterations: Optional[int] = None,
        review_instructions: Optional[str] = None,
        budget_id: Optional[str] = None,
    ) -> TaskRun:
        """MA4: launches a BUILD_REVIEW Task Run. Creates only the
        *primary* Agent Run/job here, exactly like start_task_run does for
        SINGLE_AGENT -- the reviewer/repair Agent Runs are created later by
        app.services.review_orchestration_service as each stage completes,
        never all up front (a repair Agent Run that might never be needed
        must not exist as a real row before the reviewer actually asks
        for one).

        Both primary and reviewer are referenced purely by Agent Version
        id/configuration here, never by name -- this method has no
        knowledge of which Agents they are (Section: "do not hard-code
        Agent names into orchestration logic")."""
        task = self.get_task(task_id)
        if task.execution_mode != ExecutionMode.BUILD_REVIEW:
            raise ConflictError(
                f"Task {task_id} has execution_mode={task.execution_mode.value!r}, "
                "not build_review -- cannot start a reviewed Task Run for it."
            )

        primary_agent_version = self._get_published_agent_version(primary_agent_version_id)
        reviewer_agent_version = self._get_published_agent_version(reviewer_agent_version_id)

        repair_limit = (
            max_repair_iterations
            if max_repair_iterations is not None
            else settings.default_max_repair_iterations
        )
        if repair_limit < 0:
            raise ConflictError("max_repair_iterations must be zero or greater.")

        config_snapshot = {
            "task_title": task.title,
            "task_description": task.description,
            "execution_mode": task.execution_mode.value,
            "primary_agent_version_id": primary_agent_version.id,
            "primary_agent_version_number": primary_agent_version.version,
            "reviewer_agent_version_id": reviewer_agent_version.id,
            "reviewer_agent_version_number": reviewer_agent_version.version,
            "max_repair_iterations": repair_limit,
            "review_instructions": review_instructions,
            "budget_id": budget_id,
        }

        task_run = TaskRun(
            task_id=task.id,
            status=TaskRunStatus.CREATED,
            budget_id=budget_id,
            config_snapshot=config_snapshot,
            timeout_seconds=settings.default_task_run_timeout_seconds,
        )
        self.db.add(task_run)
        self.db.flush()

        primary_run = AgentRun(
            task_run_id=task_run.id,
            agent_version_id=primary_agent_version.id,
            role=AgentRunRole.PRIMARY,
            status=AgentRunStatus.CREATED,
            timeout_seconds=(
                primary_agent_version.timeout_seconds or settings.default_agent_run_timeout_seconds
            ),
        )
        self.db.add(primary_run)
        self.db.flush()

        self._recorder.record(
            task_id=task.id,
            task_run_id=task_run.id,
            agent_run_id=primary_run.id,
            agent_id=primary_agent_version.agent_id,
            agent_version_id=primary_agent_version.id,
            event_type="task_run.created",
            decision_summary=(
                f"reviewed task_run created for task {task.id}: primary={primary_agent_version.id} "
                f"reviewer={reviewer_agent_version.id} max_repair_iterations={repair_limit}"
            ),
        )

        task_run.status = TaskRunStatus.QUEUED
        self.db.commit()
        self.db.refresh(task_run)

        self._jobs.enqueue(self.db, job_type=JobType.AGENT_RUN, payload_ref=primary_run.id)
        self._recorder.record(
            task_id=task.id,
            task_run_id=task_run.id,
            agent_run_id=primary_run.id,
            event_type="task_run.queued",
            decision_summary="primary agent_run job enqueued for worker pickup",
        )

        return task_run

    def _get_published_agent_version(self, agent_version_id: str) -> AgentVersion:
        agent_version = self.db.get(AgentVersion, agent_version_id)
        if agent_version is None:
            raise NotFoundError(f"Agent Version {agent_version_id} not found.")
        if agent_version.status != VersionStatus.ACTIVE:
            raise ConflictError(
                f"Agent Version {agent_version_id} is not published (status="
                f"{agent_version.status.value!r}) and cannot be run."
            )
        return agent_version

    def get_task_run(self, task_run_id: str) -> TaskRun:
        task_run = self.db.get(TaskRun, task_run_id)
        if task_run is None:
            raise NotFoundError(f"Task Run {task_run_id} not found.")
        return task_run

    def list_task_runs(self, *, task_id: str) -> List[TaskRun]:
        stmt = select(TaskRun).where(TaskRun.task_id == task_id).order_by(TaskRun.created_at.desc())
        return list(self.db.execute(stmt).scalars().all())

    def cancel_task_run(self, task_run_id: str, *, requested_by: Optional[str]) -> TaskRun:
        """Cooperative cancellation only (Section 26.7): flips to
        CANCELLING and records the request; the worker observes it at its
        own checkpoints and performs the actual CANCELLED transition. Never
        claims cancellation is complete here."""
        task_run = self.get_task_run(task_run_id)

        if task_run.status in (
            TaskRunStatus.COMPLETED,
            TaskRunStatus.FAILED,
            TaskRunStatus.CANCELLED,
            TaskRunStatus.CANCELLING,
        ):
            raise InvalidStateTransitionError(
                f"Task Run {task_run_id} is already {task_run.status.value!r} and cannot be cancelled again."
            )

        task_run.cancellation_requested_at = datetime.now(timezone.utc)
        task_run.cancellation_requested_by = requested_by
        task_run.status = TaskRunStatus.CANCELLING
        self.db.commit()
        self.db.refresh(task_run)

        self._recorder.record(
            task_id=task_run.task_id,
            task_run_id=task_run.id,
            event_type="task_run.cancellation_requested",
            decision_summary="cooperative cancellation requested; worker will observe at next checkpoint",
        )
        return task_run
