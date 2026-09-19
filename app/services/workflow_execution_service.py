"""Workflow Execution Engine — MA7.2 Sequential AGENT-only Execution.

Orchestrates durable sequential execution of AGENT nodes in ACTIVE workflows.
Reuses MA3 AgentExecutionService, JobQueue, Worker, and existing infrastructure.

Critical invariant: One (WorkflowRun, WorkflowNode, iteration) → at most one Agent execution.
Crash/replay discovers and reuses existing execution via WorkflowNodeRun.agent_run_id.
"""

from datetime import datetime, timezone
from typing import List, Optional, cast
from sqlalchemy import and_, text
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.db.enums import (
    AgentRunStatus,
    TaskRunStatus,
    WorkflowRunStatus,
    WorkflowNodeRunStatus,
    WorkflowNodeType,
    JobType,
    JobQueueStatus,
    VersionStatus,
)
from app.models.artifacts_eval import Artifact
from app.models.execution import JobQueue
from app.models.tasks import TaskRun, AgentRun
from app.models.workflow import Workflow, WorkflowVersion, WorkflowRun, WorkflowNodeRun, WorkflowNode, WorkflowEdge
from app.services.base import BaseService
from app.services.flight_recorder import FlightRecorderService
from app.repositories.job_queue_repository import JobQueueRepository


class WorkflowExecutionError(Exception):
    """Raised when workflow execution encounters a fatal error."""
    pass


class WorkflowExecutionService(BaseService):
    """Durable sequential workflow execution for ACTIVE versions.

    State machine:
    - WorkflowRun: CREATED → RUNNING → COMPLETED/FAILED/CANCELLED
    - WorkflowNodeRun: PENDING → RUNNING → COMPLETED/FAILED/CANCELLED

    Idempotency via WorkflowNodeRun.agent_run_id:
    - If agent_run_id is set (non-NULL) → execution already created, reuse it
    - If agent_run_id is NULL → no execution yet, safe to create one
    - Crash before setting agent_run_id → reconciliation retries creation
    - Crash after setting agent_run_id → reconciliation finds it via query
    """

    def __init__(self, db: Session):
        super().__init__(db)
        self.flight_recorder = FlightRecorderService(db)
        self.job_queue_repo = JobQueueRepository()

    def _emit_event(self, task_run_id: str, event_type: str, **kwargs: object) -> None:
        """Helper to emit Flight Recorder event with correct parameters."""
        task_run: Optional[TaskRun] = self.db.query(TaskRun).filter(TaskRun.id == task_run_id).first()
        if task_run:
            self.flight_recorder.record(
                task_id=task_run.task_id,
                task_run_id=task_run_id,
                event_type=event_type,
                **kwargs
            )

    def _atomically_claim_node_for_execution(self, node_run_id: str, agent_run_id: str) -> bool:
        """Atomically claim a PENDING node for execution (compare-and-swap).

        Updates node_run to link agent_run_id only if currently PENDING with agent_run_id=NULL.
        Returns True if claim succeeded (this dispatcher won the race).
        Returns False if another dispatcher already claimed it.

        Critical invariant: exactly one dispatcher successfully claims the node.
        Losing dispatcher must not create duplicate execution.
        """
        result = cast(
            CursorResult,
            self.db.execute(
                text("""
                    UPDATE workflow_node_runs
                    SET agent_run_id = :agent_run_id, status = :running
                    WHERE id = :node_run_id
                      AND status = :pending
                      AND agent_run_id IS NULL
                """),
                {
                    "agent_run_id": agent_run_id,
                    "running": WorkflowNodeRunStatus.RUNNING.value,
                    "node_run_id": node_run_id,
                    "pending": WorkflowNodeRunStatus.PENDING.value,
                },
            ),
        )
        self.db.commit()
        return result.rowcount == 1

    def start_workflow_run(self, workflow_version_id: str, task_run_id: str) -> WorkflowRun:
        """Start execution of an ACTIVE workflow version.

        Creates WorkflowRun + initial WorkflowNodeRuns (one per graph node).
        Identifies entry nodes (no incoming edges) and schedules them.

        Raises WorkflowExecutionError if version not ACTIVE or has no nodes.
        Returns WorkflowRun with status=RUNNING.
        """
        wv: Optional[WorkflowVersion] = self.db.query(WorkflowVersion).filter(
            WorkflowVersion.id == workflow_version_id
        ).first()
        if not wv:
            raise WorkflowExecutionError(f"Workflow version {workflow_version_id} not found")

        if wv.status != VersionStatus.ACTIVE:
            raise WorkflowExecutionError(
                f"Can only execute ACTIVE versions; status is {wv.status}"
            )

        # Verify nodes exist
        nodes: List[WorkflowNode] = self.db.query(WorkflowNode).filter(
            WorkflowNode.workflow_version_id == workflow_version_id
        ).all()
        if not nodes:
            raise WorkflowExecutionError("Workflow version has no nodes")

        # Create WorkflowRun
        workflow_run = WorkflowRun(
            task_run_id=task_run_id,
            workflow_version_id=workflow_version_id,
            status=WorkflowRunStatus.CREATED,
            created_at=datetime.now(timezone.utc),
        )
        self.db.add(workflow_run)
        self.db.flush()

        # Create WorkflowNodeRuns (one per node, status=PENDING, agent_run_id=NULL)
        try:
            for node in nodes:
                node_run = WorkflowNodeRun(
                    workflow_run_id=workflow_run.id,
                    workflow_node_id=node.id,
                    iteration=0,
                    status=WorkflowNodeRunStatus.PENDING,
                )
                self.db.add(node_run)
            self.db.flush()
        except IntegrityError:
            # Node runs already exist (concurrent start or replay)
            self.db.rollback()
            # Reload workflow_run to ensure it exists
            reloaded_run: Optional[WorkflowRun] = self.db.query(WorkflowRun).filter(
                WorkflowRun.id == workflow_run.id
            ).first()
            if not reloaded_run:
                raise WorkflowExecutionError("Failed to create or retrieve WorkflowRun")
            workflow_run = reloaded_run

        # Transition to RUNNING
        workflow_run.status = WorkflowRunStatus.RUNNING
        workflow_run.started_at = datetime.now(timezone.utc)
        self.db.commit()

        # Emit event
        self._emit_event(
            task_run_id,
            "workflow.started",
            workflow_run_id=workflow_run.id,
        )

        # Schedule entry nodes
        self._schedule_ready_nodes(workflow_run.id)

        return workflow_run

    def on_agent_run_complete(self, agent_run_id: str) -> None:
        """Reconciliation: handle agent completion and unlock downstream nodes.

        Called after AgentRun reaches terminal status (COMPLETED, FAILED, STOPPED, CANCELLING).
        Updates corresponding WorkflowNodeRun and schedules next eligible nodes.

        Idempotent: replay after completion is no-op (checks status).
        """
        agent_run: Optional[AgentRun] = self.db.query(AgentRun).filter(
            AgentRun.id == agent_run_id
        ).first()
        if not agent_run:
            return  # Agent run doesn't exist

        # Find WorkflowNodeRun that triggered this agent
        node_run: Optional[WorkflowNodeRun] = self.db.query(WorkflowNodeRun).filter(
            WorkflowNodeRun.agent_run_id == agent_run_id
        ).first()

        if not node_run:
            return  # No workflow context for this agent

        # Idempotency: if already terminal, skip
        if node_run.status in (
            WorkflowNodeRunStatus.COMPLETED,
            WorkflowNodeRunStatus.FAILED,
            WorkflowNodeRunStatus.CANCELLED,
        ):
            return

        workflow_run: Optional[WorkflowRun] = self.db.query(WorkflowRun).filter(
            WorkflowRun.id == node_run.workflow_run_id
        ).first()
        if not workflow_run:
            return

        # Propagate status from agent to node
        if agent_run.status == AgentRunStatus.COMPLETED:
            node_run.status = WorkflowNodeRunStatus.COMPLETED
            node_run.ended_at = datetime.now(timezone.utc)

            # Capture output artifact reference (immutable)
            artifact: Optional[Artifact] = self.db.query(Artifact).filter(
                Artifact.agent_run_id == agent_run_id
            ).first()
            if artifact:
                node_run.output_snapshot_ref = artifact.id

            self._emit_event(
                workflow_run.task_run_id,
                "workflow.node.completed",
                workflow_run_id=workflow_run.id,
                workflow_node_run_id=node_run.id,
            )

        elif agent_run.status == AgentRunStatus.FAILED:
            node_run.status = WorkflowNodeRunStatus.FAILED
            node_run.ended_at = datetime.now(timezone.utc)
            workflow_run.status = WorkflowRunStatus.FAILED

            self._emit_event(
                workflow_run.task_run_id,
                "workflow.node.failed",
                workflow_run_id=workflow_run.id,
                workflow_node_run_id=node_run.id,
            )

            self._emit_event(
                workflow_run.task_run_id,
                "workflow.failed",
                workflow_run_id=workflow_run.id,
                decision_summary="Agent node failed",
            )

        elif agent_run.status in (AgentRunStatus.STOPPED, AgentRunStatus.CANCELLING):
            node_run.status = WorkflowNodeRunStatus.CANCELLED
            node_run.ended_at = datetime.now(timezone.utc)

            if workflow_run.status == WorkflowRunStatus.RUNNING:
                workflow_run.status = WorkflowRunStatus.CANCELLING

            self._emit_event(
                workflow_run.task_run_id,
                "workflow.node.cancelled",
                workflow_run_id=workflow_run.id,
                workflow_node_run_id=node_run.id,
            )

        self.db.commit()

        # If workflow still running, try to schedule next nodes
        if workflow_run.status == WorkflowRunStatus.RUNNING:
            self._schedule_ready_nodes(workflow_run.id)

    def cancel_workflow_run(self, workflow_run_id: str, cancelled_by_user_id: Optional[str] = None) -> None:
        """Cancel a running workflow and propagate to executing agents.

        Idempotent: multiple calls converge safely.
        """
        workflow_run: Optional[WorkflowRun] = self.db.query(WorkflowRun).filter(
            WorkflowRun.id == workflow_run_id
        ).first()
        if not workflow_run:
            return

        if workflow_run.status in (
            WorkflowRunStatus.COMPLETED,
            WorkflowRunStatus.FAILED,
            WorkflowRunStatus.CANCELLED,
        ):
            return  # Already terminal

        # Mark cancellation
        workflow_run.cancellation_requested_at = datetime.now(timezone.utc)
        workflow_run.cancellation_requested_by = cancelled_by_user_id
        workflow_run.status = WorkflowRunStatus.CANCELLING
        self.db.commit()

        # Cancel pending node runs
        pending_nodes: List[WorkflowNodeRun] = self.db.query(WorkflowNodeRun).filter(
            and_(
                WorkflowNodeRun.workflow_run_id == workflow_run_id,
                WorkflowNodeRun.status == WorkflowNodeRunStatus.PENDING,
            )
        ).all()

        for node_run in pending_nodes:
            node_run.status = WorkflowNodeRunStatus.CANCELLED
            node_run.ended_at = datetime.now(timezone.utc)

        self.db.commit()

        # Propagate to running agents
        running_nodes: List[WorkflowNodeRun] = self.db.query(WorkflowNodeRun).filter(
            and_(
                WorkflowNodeRun.workflow_run_id == workflow_run_id,
                WorkflowNodeRun.status == WorkflowNodeRunStatus.RUNNING,
                WorkflowNodeRun.agent_run_id.isnot(None),
            )
        ).all()

        for node_run in running_nodes:
            agent_run: Optional[AgentRun] = self.db.query(AgentRun).filter(
                AgentRun.id == node_run.agent_run_id
            ).first()
            if agent_run:
                agent_run.cancellation_requested_at = datetime.now(timezone.utc)
                agent_run.cancellation_requested_by = cancelled_by_user_id

        self.db.commit()

        self._emit_event(
            workflow_run.task_run_id,
            "workflow.cancelled",
            workflow_run_id=workflow_run_id,
            actor_user_id=cancelled_by_user_id,
        )

        # Check if all nodes are now terminal
        self._check_workflow_complete(workflow_run_id)

    def reconcile_workflow_run(self, workflow_run_id: str) -> None:
        """Recovery: reconcile WorkflowRun state after crash.

        Discovers incomplete nodes, handles all crash boundaries.
        Re-running reconciliation converges to same state without duplicates.
        """
        workflow_run: Optional[WorkflowRun] = self.db.query(WorkflowRun).filter(
            WorkflowRun.id == workflow_run_id
        ).first()
        if not workflow_run or workflow_run.status not in (
            WorkflowRunStatus.CREATED,
            WorkflowRunStatus.RUNNING,
        ):
            return

        # Ensure all nodes have node_run entries
        nodes: List[WorkflowNode] = self.db.query(WorkflowNode).filter(
            WorkflowNode.workflow_version_id == workflow_run.workflow_version_id
        ).all()

        existing_node_runs: List[WorkflowNodeRun] = self.db.query(WorkflowNodeRun).filter(
            WorkflowNodeRun.workflow_run_id == workflow_run_id
        ).all()

        existing_ids = {nr.workflow_node_id for nr in existing_node_runs}

        # Create missing node runs (crash before node creation)
        for node in nodes:
            if node.id not in existing_ids:
                try:
                    node_run = WorkflowNodeRun(
                        workflow_run_id=workflow_run_id,
                        workflow_node_id=node.id,
                        iteration=0,
                        status=WorkflowNodeRunStatus.PENDING,
                    )
                    self.db.add(node_run)
                except IntegrityError:
                    self.db.rollback()
                    # Race condition: another worker/reconciliation created it
                    continue

        self.db.commit()

        # Reload node runs to check for completed agents
        existing_node_runs = self.db.query(WorkflowNodeRun).filter(
            WorkflowNodeRun.workflow_run_id == workflow_run_id
        ).all()

        for node_run in existing_node_runs:
            # Crash window: AgentRun completed but NodeRun not updated
            if node_run.agent_run_id and node_run.status == WorkflowNodeRunStatus.RUNNING:
                agent_run: Optional[AgentRun] = self.db.query(AgentRun).filter(
                    AgentRun.id == node_run.agent_run_id
                ).first()
                if agent_run and agent_run.status in (
                    AgentRunStatus.COMPLETED,
                    AgentRunStatus.FAILED,
                    AgentRunStatus.STOPPED,
                ):
                    self.on_agent_run_complete(agent_run.id)

        # Try to schedule ready nodes
        if workflow_run.status == WorkflowRunStatus.RUNNING:
            self._schedule_ready_nodes(workflow_run_id)

        # Check for completion
        self._check_workflow_complete(workflow_run_id)

    # ===== Private helpers =====

    def _schedule_ready_nodes(self, workflow_run_id: str) -> None:
        """Identify and dispatch ready nodes.

        Ready = all upstream dependencies COMPLETED.
        Sequential enforcement: fail closed if multiple nodes simultaneously ready.
        """
        workflow_run: Optional[WorkflowRun] = self.db.query(WorkflowRun).filter(
            WorkflowRun.id == workflow_run_id
        ).first()
        if not workflow_run:
            return

        ready: List[WorkflowNodeRun] = self._find_ready_nodes(workflow_run_id)

        # Sequential-only: multiple ready = parallel (unsupported in MA7.2)
        if len(ready) > 1:
            self._emit_event(
                workflow_run.task_run_id,
                "workflow.error",
                workflow_run_id=workflow_run_id,
                error=f"Multiple nodes ready: parallel execution unsupported in MA7.2",
            )
            workflow_run.status = WorkflowRunStatus.FAILED
            self.db.commit()
            return

        for node_run in ready:
            self._dispatch_node_for_execution(node_run)

    def _find_ready_nodes(self, workflow_run_id: str) -> List[WorkflowNodeRun]:
        """Find PENDING nodes with all dependencies COMPLETED.

        Ready = no incoming edges OR all upstream nodes COMPLETED.
        """
        ready: List[WorkflowNodeRun] = []

        node_runs: List[WorkflowNodeRun] = self.db.query(WorkflowNodeRun).filter(
            and_(
                WorkflowNodeRun.workflow_run_id == workflow_run_id,
                WorkflowNodeRun.status == WorkflowNodeRunStatus.PENDING,
            )
        ).all()

        for node_run in node_runs:
            # Find incoming edges for this node
            incoming_edges: List[WorkflowEdge] = self.db.query(WorkflowEdge).filter(
                WorkflowEdge.to_node_id == node_run.workflow_node_id
            ).all()

            # No incoming = entry node, ready
            if not incoming_edges:
                ready.append(node_run)
                continue

            # Check if all dependencies completed
            all_deps_done = True
            for edge in incoming_edges:
                dep_node_run: Optional[WorkflowNodeRun] = self.db.query(WorkflowNodeRun).filter(
                    and_(
                        WorkflowNodeRun.workflow_run_id == workflow_run_id,
                        WorkflowNodeRun.workflow_node_id == edge.from_node_id,
                    )
                ).first()

                if not dep_node_run or dep_node_run.status != WorkflowNodeRunStatus.COMPLETED:
                    all_deps_done = False
                    break

            if all_deps_done:
                ready.append(node_run)

        return ready

    def _dispatch_node_for_execution(self, node_run: WorkflowNodeRun) -> None:
        """Dispatch a ready node for execution.

        Critical invariant: WorkflowNodeRun.agent_run_id = "already created" marker.
        If agent_run_id is set (non-NULL), execution already exists, skip.
        If agent_run_id is NULL, create TaskRun + AgentRun + queue job.
        """
        # Idempotency check: if agent_run_id already set, execution exists
        if node_run.agent_run_id is not None:
            return

        workflow_run: Optional[WorkflowRun] = self.db.query(WorkflowRun).filter(
            WorkflowRun.id == node_run.workflow_run_id
        ).first()
        if not workflow_run:
            return

        node: Optional[WorkflowNode] = self.db.query(WorkflowNode).filter(
            WorkflowNode.id == node_run.workflow_node_id
        ).first()
        if not node:
            return

        # Handle TERMINAL: no execution needed
        if node.node_type == WorkflowNodeType.TERMINAL:
            node_run.status = WorkflowNodeRunStatus.COMPLETED
            node_run.ended_at = datetime.now(timezone.utc)
            self.db.commit()

            self._emit_event(
                workflow_run.task_run_id,
                "workflow.node.completed",
                workflow_run_id=workflow_run.id,
                workflow_node_run_id=node_run.id,
            )
            return

        # Unsupported node type: fail closed
        if node.node_type != WorkflowNodeType.AGENT:
            node_run.status = WorkflowNodeRunStatus.FAILED
            node_run.ended_at = datetime.now(timezone.utc)
            workflow_run.status = WorkflowRunStatus.FAILED
            self.db.commit()

            self._emit_event(
                workflow_run.task_run_id,
                "workflow.error",
                workflow_run_id=workflow_run.id,
                workflow_node_run_id=node_run.id,
                error={"message": f"Unsupported node type: {node.node_type}"},
            )
            return

        # Get agent_version_id from node config
        agent_version_id: Optional[str] = node.config.get("agent_version_id") if node.config else None
        if not agent_version_id:
            node_run.status = WorkflowNodeRunStatus.FAILED
            node_run.ended_at = datetime.now(timezone.utc)
            workflow_run.status = WorkflowRunStatus.FAILED
            self.db.commit()

            self._emit_event(
                workflow_run.task_run_id,
                "workflow.error",
                workflow_run_id=workflow_run.id,
                workflow_node_run_id=node_run.id,
                error={"message": "AGENT node missing config.agent_version_id"},
            )
            return

        # Get parent Task for budget
        parent_task_run: Optional[TaskRun] = self.db.query(TaskRun).filter(
            TaskRun.id == workflow_run.task_run_id
        ).first()
        if not parent_task_run:
            return

        # Create TaskRun for this node's execution (follows MA5 pattern)
        node_task_run = TaskRun(
            task_id=parent_task_run.task_id,
            workflow_version_id=workflow_run.workflow_version_id,
            status=TaskRunStatus.CREATED,
            budget_id=parent_task_run.budget_id,  # Share budget
            config_snapshot={
                "node_key": node.node_key,
                "node_type": node.node_type.value,
                "iteration": node_run.iteration,
            },
            created_at=datetime.now(timezone.utc),
        )
        self.db.add(node_task_run)
        self.db.flush()

        # Create AgentRun (tied to this node's TaskRun)
        agent_run = AgentRun(
            task_run_id=node_task_run.id,
            agent_version_id=agent_version_id,
            status=AgentRunStatus.CREATED,
            created_at=datetime.now(timezone.utc),
        )

        # Record immutable upstream artifact lineage
        upstream_artifacts: List[str] = self._collect_upstream_artifacts(node_run)
        upstream_node_runs: List[WorkflowNodeRun] = self._get_upstream_node_runs(node_run)

        if upstream_artifacts or upstream_node_runs:
            agent_run.input_context_json = {
                "upstream_artifact_ids": upstream_artifacts,
                "upstream_node_run_ids": [nr.id for nr in upstream_node_runs],
            }

        # Apply model policy override if present
        if node.config and "model_policy_override" in node.config:
            agent_run.model_policy_override_json = node.config.get("model_policy_override")

        self.db.add(agent_run)
        self.db.flush()

        # Atomically claim node for execution (compare-and-swap)
        # This ensures exactly one dispatcher succeeds even if multiple race simultaneously
        claim_succeeded: bool = self._atomically_claim_node_for_execution(node_run.id, agent_run.id)

        if not claim_succeeded:
            # Another dispatcher claimed this node first
            # Idempotent: this is safe, just clean up our partial execution and return
            # (The node will be executed by whoever claimed it)
            return

        # Queue job (idempotent: duplicate raises IntegrityError, safe to ignore)
        try:
            self.job_queue_repo.enqueue(
                self.db,
                job_type=JobType.AGENT_RUN,
                payload_ref=agent_run.id,
            )
        except IntegrityError:
            # Job already queued (race condition or replay)
            self.db.rollback()
            pass

        self._emit_event(
            workflow_run.task_run_id,
            "workflow.node.scheduled",
            workflow_run_id=workflow_run.id,
            workflow_node_run_id=node_run.id,
            agent_run_id=agent_run.id,
        )

    def _collect_upstream_artifacts(self, node_run: WorkflowNodeRun) -> List[str]:
        """Collect immutable upstream artifact references (output_snapshot_refs)."""
        artifacts: List[str] = []

        upstream_nodes: List[WorkflowNodeRun] = self._get_upstream_node_runs(node_run)
        for upstream_node_run in upstream_nodes:
            if upstream_node_run.output_snapshot_ref:
                artifacts.append(upstream_node_run.output_snapshot_ref)

        return artifacts

    def _get_upstream_node_runs(self, node_run: WorkflowNodeRun) -> List[WorkflowNodeRun]:
        """Get all upstream completed node runs for a given node."""
        node: Optional[WorkflowNode] = self.db.query(WorkflowNode).filter(
            WorkflowNode.id == node_run.workflow_node_id
        ).first()
        if not node:
            return []

        incoming_edges: List[WorkflowEdge] = self.db.query(WorkflowEdge).filter(
            WorkflowEdge.to_node_id == node.id
        ).all()

        upstream: List[WorkflowNodeRun] = []
        for edge in incoming_edges:
            upstream_node_run: Optional[WorkflowNodeRun] = self.db.query(WorkflowNodeRun).filter(
                and_(
                    WorkflowNodeRun.workflow_run_id == node_run.workflow_run_id,
                    WorkflowNodeRun.workflow_node_id == edge.from_node_id,
                    WorkflowNodeRun.status == WorkflowNodeRunStatus.COMPLETED,
                )
            ).first()
            if upstream_node_run:
                upstream.append(upstream_node_run)

        return upstream

    def _check_workflow_complete(self, workflow_run_id: str) -> None:
        """Check if workflow is terminal and update status.

        Complete when all nodes are terminal.
        Idempotent: multiple calls safe.
        """
        workflow_run: Optional[WorkflowRun] = self.db.query(WorkflowRun).filter(
            WorkflowRun.id == workflow_run_id
        ).first()
        if not workflow_run:
            return

        if workflow_run.status not in (WorkflowRunStatus.RUNNING, WorkflowRunStatus.CANCELLING):
            return  # Already terminal

        node_runs: List[WorkflowNodeRun] = self.db.query(WorkflowNodeRun).filter(
            WorkflowNodeRun.workflow_run_id == workflow_run_id
        ).all()

        all_terminal = True
        has_failure = False

        for node_run in node_runs:
            if node_run.status not in (
                WorkflowNodeRunStatus.COMPLETED,
                WorkflowNodeRunStatus.FAILED,
                WorkflowNodeRunStatus.CANCELLED,
            ):
                all_terminal = False
                break

            if node_run.status == WorkflowNodeRunStatus.FAILED:
                has_failure = True

        if all_terminal:
            workflow_run.ended_at = datetime.now(timezone.utc)

            if has_failure:
                workflow_run.status = WorkflowRunStatus.FAILED
                self._emit_event(
                    workflow_run.task_run_id,
                    "workflow.failed",
                    workflow_run_id=workflow_run_id,
                )
            elif workflow_run.status == WorkflowRunStatus.CANCELLING:
                workflow_run.status = WorkflowRunStatus.CANCELLED
                self._emit_event(
                    workflow_run.task_run_id,
                    "workflow.cancelled",
                    workflow_run_id=workflow_run_id,
                )
            else:
                workflow_run.status = WorkflowRunStatus.COMPLETED
                self._emit_event(
                    workflow_run.task_run_id,
                    "workflow.completed",
                    workflow_run_id=workflow_run_id,
                )

            self.db.commit()
