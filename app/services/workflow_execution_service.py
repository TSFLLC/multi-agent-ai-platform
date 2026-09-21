"""Workflow Execution Engine — MA7.2 Sequential AGENT-only Execution.

Orchestrates durable sequential execution of AGENT nodes in ACTIVE workflows.
Reuses MA3 AgentExecutionService, JobQueue, Worker, and existing infrastructure.

Critical invariant: One (WorkflowRun, WorkflowNode, iteration) → at most one Agent execution.
Crash/replay discovers and reuses existing execution via WorkflowNodeRun.agent_run_id.
"""

import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Tuple, cast
from sqlalchemy import select, text, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.db.enums import (
    AgentRunStatus,
    ApprovalScope,
    ApprovalStatus,
    TaskRunStatus,
    WorkflowRunStatus,
    WorkflowNodeRunStatus,
    WorkflowNodeType,
    JobType,
    JobQueueStatus,
    VersionStatus,
)
from app.errors import FingerprintMismatchError, InvalidStateTransitionError
from app.models.artifacts_eval import Artifact
from app.models.execution import JobQueue
from app.models.governance import Approval
from app.models.tasks import Task, TaskRun, AgentRun
from app.models.workflow import Workflow, WorkflowVersion, WorkflowRun, WorkflowNodeRun, WorkflowNode, WorkflowEdge
from app.services.approval_service import (
    WORKFLOW_HUMAN_APPROVAL_OPERATION,
    ApprovalService,
    compute_action_fingerprint,
)
from app.services.base import BaseService
from app.services.flight_recorder import FlightRecorderService
from app.repositories.job_queue_repository import JobQueueRepository

logger = logging.getLogger("app.services.workflow_execution_service")

# Runs the startup/periodic reconciliation sweep looks at: everything that is
# not yet terminal. (NODE_WAITING_FOR_AGENT / REPAIR_LOOP_ACTIVE / ESCALATED
# are never entered by the MA7.2/MA7.3 sequential engine.)
_ACTIVE_RUN_STATUSES = (
    WorkflowRunStatus.CREATED,
    WorkflowRunStatus.RUNNING,
    WorkflowRunStatus.NODE_WAITING_FOR_APPROVAL,
    WorkflowRunStatus.CANCELLING,
)

# ``WorkflowNodeRun``-level reason recorded when a cancelled workflow closes
# a still-pending approval. Never a rejection -- see
# ApprovalService.expire_pending_for_cancellation.
WORKFLOW_CANCELLED_REASON = "workflow_cancelled"


def _now() -> datetime:
    return datetime.now(timezone.utc)


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
        self.job_queue_repo = JobQueueRepository()

    @contextmanager
    def _evidence_session(self) -> Iterator[Session]:
        """A short-lived session on the same database, used ONLY to write
        Flight Recorder evidence. Separate from ``self.db`` on purpose -- see
        ``_emit_event``."""
        bind = self.db.get_bind()
        session = Session(bind=getattr(bind, "engine", bind), autoflush=False, expire_on_commit=False)
        try:
            yield session
        finally:
            session.close()

    def _emit_event(self, task_run_id: str, event_type: str, **kwargs: object) -> None:
        """Flight Recorder evidence: best-effort, and NEVER at the expense of
        workflow state. Durable workflow state first; observability second.

        1. Whatever the caller intended is committed BEFORE any evidence is
           written. (A commit that fails is a real state error and propagates;
           an evidence failure never does.)
        2. The event is written on a SEPARATE session/transaction.
           ``ExecutionEventRepository.record`` rolls its session back when it
           loses a sequence-number race and retries; on the caller's session
           that rollback would also discard the caller's pending changes
           (demonstrated in the MA7.4 investigation: a node stayed RUNNING
           although its completion event was recorded). Parallel branches all
           write engine events to the same parent task run, so this is exactly
           where such collisions occur.
        3. Any evidence failure -- exhausted sequence retries, a locked
           database, a missing task run -- is logged and swallowed. The
           database state is authoritative; events are not.
        """
        self.db.commit()
        try:
            with self._evidence_session() as session:
                task_run = session.get(TaskRun, task_run_id)
                if task_run is None:
                    return
                FlightRecorderService(session).record(
                    task_id=task_run.task_id,
                    task_run_id=task_run_id,
                    event_type=event_type,
                    **kwargs,
                )
        except Exception:
            logger.warning(
                "workflow_event_not_recorded event_type=%s task_run_id=%s", event_type, task_run_id, exc_info=True
            )

    # -- fresh reads / compare-and-swap helpers (MA7.3) ---------------------
    #
    # Sessions are created with expire_on_commit=False, so a plain
    # ``query(...).first()`` can hand back a stale identity-mapped row after
    # another session (or this one, via a raw CAS UPDATE) changed it.
    # Anything that must observe *current* state uses these.

    def _fresh_run(self, workflow_run_id: str) -> Optional[WorkflowRun]:
        return self.db.execute(
            select(WorkflowRun).where(WorkflowRun.id == workflow_run_id).execution_options(populate_existing=True)
        ).scalar_one_or_none()

    def _fresh_node_run(self, node_run_id: str) -> Optional[WorkflowNodeRun]:
        return self.db.execute(
            select(WorkflowNodeRun)
            .where(WorkflowNodeRun.id == node_run_id)
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()

    def _fresh_node_runs(self, workflow_run_id: str) -> List[WorkflowNodeRun]:
        return list(
            self.db.execute(
                select(WorkflowNodeRun)
                .where(WorkflowNodeRun.workflow_run_id == workflow_run_id)
                .execution_options(populate_existing=True)
            )
            .scalars()
            .all()
        )

    def _waiting_node_runs(self, workflow_run_id: str) -> List[WorkflowNodeRun]:
        return list(
            self.db.execute(
                select(WorkflowNodeRun)
                .where(
                    WorkflowNodeRun.workflow_run_id == workflow_run_id,
                    WorkflowNodeRun.status == WorkflowNodeRunStatus.WAITING_FOR_APPROVAL,
                )
                .execution_options(populate_existing=True)
            )
            .scalars()
            .all()
        )

    def _cas_node_run(self, node_run_id: str, expected: WorkflowNodeRunStatus, **values: Any) -> bool:
        """``UPDATE .. WHERE status = expected``; True only if this call made the change."""
        result = cast(
            CursorResult,
            self.db.execute(
                update(WorkflowNodeRun)
                .where(WorkflowNodeRun.id == node_run_id, WorkflowNodeRun.status == expected)
                .values(**values)
                .execution_options(synchronize_session=False)
            ),
        )
        return result.rowcount == 1

    def _cas_run(self, workflow_run_id: str, expected: WorkflowRunStatus, **values: Any) -> bool:
        result = cast(
            CursorResult,
            self.db.execute(
                update(WorkflowRun)
                .where(WorkflowRun.id == workflow_run_id, WorkflowRun.status == expected)
                .values(**values)
                .execution_options(synchronize_session=False)
            ),
        )
        return result.rowcount == 1

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
        if result.rowcount == 1:
            self.db.commit()
            return True
        # Lost the race: discard this dispatcher's flushed-but-unlinked
        # TaskRun/AgentRun instead of committing them (MA7.3b fix -- they
        # would otherwise persist as orphan duplicate rows the winner's
        # execution never uses).
        self.db.rollback()
        return False

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

        # Prove ownership from persisted records before creating any
        # execution state. A caller must not be able to pair a workflow with
        # a TaskRun belonging to another project (or with a missing task).
        workflow: Optional[Workflow] = self.db.query(Workflow).filter(
            Workflow.id == wv.workflow_id
        ).first()
        task_run: Optional[TaskRun] = self.db.query(TaskRun).filter(
            TaskRun.id == task_run_id
        ).first()
        task: Optional[Task] = None
        if task_run:
            task = self.db.query(Task).filter(Task.id == task_run.task_id).first()

        if not workflow or not task_run or not task:
            raise WorkflowExecutionError("Workflow and TaskRun ownership could not be established")
        if workflow.project_id != task.project_id:
            raise WorkflowExecutionError("Workflow and TaskRun must belong to the same project")

        # Verify nodes exist
        nodes: List[WorkflowNode] = self.db.query(WorkflowNode).filter(
            WorkflowNode.workflow_version_id == workflow_version_id
        ).all()
        if not nodes:
            raise WorkflowExecutionError("Workflow version has no nodes")

        # Defense in depth behind publish-time validation: this engine has no
        # conditional routing, so a version that carries an edge condition
        # (e.g. published before that validation existed) is refused rather
        # than executed as if the condition were absent.
        version_edges: List[WorkflowEdge] = self.db.query(WorkflowEdge).filter(
            WorkflowEdge.workflow_version_id == workflow_version_id
        ).all()
        if any(edge.condition is not None for edge in version_edges):
            raise WorkflowExecutionError(
                "Workflow version has conditional edges; conditional routing is not supported yet"
            )

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
        agent_run: Optional[AgentRun] = self.db.execute(
            select(AgentRun).where(AgentRun.id == agent_run_id).execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if not agent_run:
            return  # Agent run doesn't exist

        # Find WorkflowNodeRun that triggered this agent
        node_run: Optional[WorkflowNodeRun] = self.db.execute(
            select(WorkflowNodeRun)
            .where(WorkflowNodeRun.agent_run_id == agent_run_id)
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()

        if not node_run:
            return  # No workflow context for this agent

        workflow_run: Optional[WorkflowRun] = self._fresh_run(node_run.workflow_run_id)
        if not workflow_run:
            return

        # Idempotency: an already-terminal node is not touched again -- but the
        # run is still given the chance to converge (a crash between a node's
        # commit and the run's finalization is healed by any replay).
        if node_run.status in (
            WorkflowNodeRunStatus.COMPLETED,
            WorkflowNodeRunStatus.FAILED,
            WorkflowNodeRunStatus.CANCELLED,
        ):
            self._advance_run(workflow_run.id)
            return

        # Propagate status from agent to node. Every transition is a
        # compare-and-swap on the node's RUNNING status: the outcome is
        # recorded exactly once however many processes replay this call
        # (worker completion, reconciliation sweep, ...), and only the call
        # that made the change emits evidence. State is committed BEFORE any
        # event is written (see _emit_event).
        ended_at = datetime.now(timezone.utc)
        ids = {"workflow_run_id": workflow_run.id, "workflow_node_run_id": node_run.id}
        events: List[Tuple[str, Dict[str, Any]]] = []

        if agent_run.status == AgentRunStatus.COMPLETED:
            # Capture output artifact reference (immutable)
            artifact_id = self.db.execute(
                select(Artifact.id)
                .where(Artifact.agent_run_id == agent_run_id)
                .order_by(Artifact.created_at, Artifact.id)
                .limit(1)
            ).scalar_one_or_none()
            values: Dict[str, Any] = {"status": WorkflowNodeRunStatus.COMPLETED, "ended_at": ended_at}
            if artifact_id:
                values["output_snapshot_ref"] = artifact_id
            if self._cas_node_run(node_run.id, WorkflowNodeRunStatus.RUNNING, **values):
                events.append(("workflow.node.completed", ids))

        elif agent_run.status == AgentRunStatus.FAILED:
            if self._cas_node_run(
                node_run.id,
                WorkflowNodeRunStatus.RUNNING,
                status=WorkflowNodeRunStatus.FAILED,
                ended_at=ended_at,
            ):
                self.db.execute(
                    update(WorkflowRun)
                    .where(
                        WorkflowRun.id == workflow_run.id,
                        WorkflowRun.status.notin_(
                            (WorkflowRunStatus.COMPLETED, WorkflowRunStatus.FAILED, WorkflowRunStatus.CANCELLED)
                        ),
                    )
                    .values(status=WorkflowRunStatus.FAILED)
                    .execution_options(synchronize_session=False)
                )
                events.append(("workflow.node.failed", ids))
                events.append(("workflow.failed", {"workflow_run_id": workflow_run.id, "decision_summary": "Agent node failed"}))

        elif agent_run.status in (AgentRunStatus.STOPPED, AgentRunStatus.CANCELLING):
            if self._cas_node_run(
                node_run.id,
                WorkflowNodeRunStatus.RUNNING,
                status=WorkflowNodeRunStatus.CANCELLED,
                ended_at=ended_at,
            ):
                self._cas_run(workflow_run.id, WorkflowRunStatus.RUNNING, status=WorkflowRunStatus.CANCELLING)
                events.append(("workflow.node.cancelled", ids))

        self.db.commit()
        for event_type, fields in events:
            self._emit_event(workflow_run.task_run_id, event_type, **fields)

        self._advance_run(workflow_run.id)

    def _advance_run(self, workflow_run_id: str) -> None:
        """Make whatever progress is currently possible for a run, from
        current committed state: a RUNNING run schedules its next ready
        nodes (which also finalizes it when nothing is left); a CANCELLING
        run -- finalize-on-drain -- becomes CANCELLED as soon as its last
        in-flight node is terminal, with no reconciliation sweep needed.
        Idempotent; a no-op for any other status."""
        run = self._fresh_run(workflow_run_id)
        if run is None:
            return
        if run.status == WorkflowRunStatus.RUNNING:
            self._schedule_ready_nodes(run.id)
        else:
            self._check_workflow_complete(run.id)

    def cancel_workflow_run(self, workflow_run_id: str, cancelled_by_user_id: Optional[str] = None) -> None:
        """Cancel a running workflow and propagate to executing agents.

        Idempotent: multiple calls converge safely.
        """
        workflow_run: Optional[WorkflowRun] = self._fresh_run(workflow_run_id)
        if not workflow_run:
            return

        if workflow_run.status in (
            WorkflowRunStatus.COMPLETED,
            WorkflowRunStatus.FAILED,
            WorkflowRunStatus.CANCELLED,
        ):
            return  # Already terminal

        # Mark cancellation. A compare-and-swap on the run's non-terminal
        # statuses, so it can never overwrite a run another actor has just
        # finalized; and the original request time/user are written once --
        # a repeated cancel converges without rewriting who asked first.
        marked = self.db.execute(
            update(WorkflowRun)
            .where(
                WorkflowRun.id == workflow_run_id,
                WorkflowRun.status.in_(
                    (
                        WorkflowRunStatus.CREATED,
                        WorkflowRunStatus.RUNNING,
                        WorkflowRunStatus.NODE_WAITING_FOR_APPROVAL,
                        WorkflowRunStatus.CANCELLING,
                    )
                ),
            )
            .values(status=WorkflowRunStatus.CANCELLING)
            .execution_options(synchronize_session=False)
        )
        self.db.execute(
            update(WorkflowRun)
            .where(WorkflowRun.id == workflow_run_id, WorkflowRun.cancellation_requested_at.is_(None))
            .values(
                cancellation_requested_at=datetime.now(timezone.utc),
                cancellation_requested_by=cancelled_by_user_id,
            )
            .execution_options(synchronize_session=False)
        )
        self.db.commit()
        if cast(CursorResult, marked).rowcount != 1:
            return  # finalized by someone else while we were reading
        workflow_run = self._fresh_run(workflow_run_id) or workflow_run

        # Cancel pending node runs
        self._cancel_pending_nodes(workflow_run_id)

        # MA7.3: a node durably waiting on a human is cancelled too, and its
        # pending Approval is closed (EXPIRED -- never REJECTED).
        self._cancel_waiting_approval_nodes(workflow_run, cancelled_by_user_id)

        # Propagate to running agents
        running_agent_run_ids = [
            row[0]
            for row in self.db.execute(
                select(WorkflowNodeRun.agent_run_id).where(
                    WorkflowNodeRun.workflow_run_id == workflow_run_id,
                    WorkflowNodeRun.status == WorkflowNodeRunStatus.RUNNING,
                    WorkflowNodeRun.agent_run_id.isnot(None),
                )
            ).all()
        ]
        for agent_run_id in running_agent_run_ids:
            # Cooperative cancellation request; the first request wins.
            self.db.execute(
                update(AgentRun)
                .where(AgentRun.id == agent_run_id, AgentRun.cancellation_requested_at.is_(None))
                .values(
                    cancellation_requested_at=datetime.now(timezone.utc),
                    cancellation_requested_by=cancelled_by_user_id,
                )
                .execution_options(synchronize_session=False)
            )

        self.db.commit()

        self._emit_event(
            workflow_run.task_run_id,
            "workflow.cancelled",
            workflow_run_id=workflow_run_id,
            actor_user_id=cancelled_by_user_id,
            decision_summary="cancellation requested",
        )

        # Check if all nodes are now terminal
        self._check_workflow_complete(workflow_run_id)

    def reconcile_workflow_run(self, workflow_run_id: str) -> None:
        """Recovery: reconcile WorkflowRun state after crash.

        Discovers incomplete nodes, handles all crash boundaries.
        Re-running reconciliation converges to same state without duplicates.

        MA7.3: also converges a run that is durably waiting on a human
        (NODE_WAITING_FOR_APPROVAL: creates a missing Approval, applies an
        already-resolved one) and a run whose cancellation was interrupted
        (CANCELLING). A healthy waiting run is a no-op -- reconciliation
        never approves, rejects, or re-dispatches a waiting node.
        """
        workflow_run: Optional[WorkflowRun] = self._fresh_run(workflow_run_id)
        if not workflow_run:
            return

        if workflow_run.status == WorkflowRunStatus.CANCELLING:
            self._reconcile_cancelling(workflow_run)
            return

        if workflow_run.status == WorkflowRunStatus.NODE_WAITING_FOR_APPROVAL:
            self._reconcile_waiting_approval(workflow_run)
            workflow_run = self._fresh_run(workflow_run_id)
            if workflow_run is None or workflow_run.status != WorkflowRunStatus.RUNNING:
                return  # still (correctly) waiting, or now terminal
        elif workflow_run.status not in (WorkflowRunStatus.CREATED, WorkflowRunStatus.RUNNING):
            return

        # Ensure all nodes have node_run entries
        nodes: List[WorkflowNode] = self.db.query(WorkflowNode).filter(
            WorkflowNode.workflow_version_id == workflow_run.workflow_version_id
        ).all()

        existing_node_runs = self._fresh_node_runs(workflow_run_id)

        existing_ids = {nr.workflow_node_id for nr in existing_node_runs}

        # Create missing node runs (crash before node creation)
        for node in nodes:
            if node.id not in existing_ids:
                self.db.add(
                    WorkflowNodeRun(
                        workflow_run_id=workflow_run_id,
                        workflow_node_id=node.id,
                        iteration=0,
                        status=WorkflowNodeRunStatus.PENDING,
                    )
                )

        try:
            self.db.commit()
        except IntegrityError:
            # Race: another worker/reconciliation created them first.
            self.db.rollback()

        # Reload node runs (current committed state) to check for completed agents
        self._heal_agent_nodes(self._fresh_node_runs(workflow_run_id))

        # Schedule ready nodes if still RUNNING, and finalize if nothing is left
        self._advance_run(workflow_run_id)

    def reconcile_active_runs(self) -> int:
        """Idempotent sweep over every non-terminal WorkflowRun, run at
        FastAPI startup and Worker startup (MA7.3). Safe to run from several
        processes at once and any number of times: every state change it can
        make is a compare-and-swap or a unique-constraint-guarded insert, so
        two sweeps cannot create a duplicate Approval, AgentRun or job. A
        failure reconciling one run is logged and does not stop the sweep.
        Returns the number of runs successfully reconciled."""
        run_ids = [
            row[0]
            for row in self.db.execute(
                select(WorkflowRun.id)
                .where(WorkflowRun.status.in_(_ACTIVE_RUN_STATUSES))
                .order_by(WorkflowRun.created_at)
            ).all()
        ]
        reconciled = 0
        for run_id in run_ids:
            try:
                self.db.expire_all()
                self.reconcile_workflow_run(run_id)
                reconciled += 1
            except Exception:
                self.db.rollback()
                logger.exception("workflow_reconciliation_failed workflow_run_id=%s", run_id)
        return reconciled

    # ===== MA7.3 Human Approval nodes =====

    def apply_approval_decision(self, approval: Approval, decision: ApprovalStatus) -> None:
        """Applies a human decision on a HUMAN_APPROVAL gate to its node run
        and workflow run. Called by ApprovalService INSIDE the decision's own
        transaction (this method never commits): approval, node and run move
        together, or the caller's rollback undoes all three.

        * APPROVED: re-verifies the action fingerprint against the CURRENT
          upstream state (the "second check" of Section 24.4 #15), then node
          WAITING -> COMPLETED (passing the single upstream artifact through
          as its output -- an approval produces no artifact of its own) and
          run NODE_WAITING_FOR_APPROVAL -> RUNNING.
        * REJECTED: node WAITING -> FAILED, run -> FAILED.

        Raises ``InvalidStateTransitionError`` if the workflow is no longer
        waiting on this node (e.g. it was cancelled first) and
        ``FingerprintMismatchError`` if the gated action changed.
        """
        node_run = self._fresh_node_run(approval.scope_ref_id)
        node = self.db.get(WorkflowNode, node_run.workflow_node_id) if node_run else None
        run = self._fresh_run(node_run.workflow_run_id) if node_run else None
        if node_run is None or node is None or run is None:
            raise InvalidStateTransitionError("The workflow node run for this approval no longer exists.")

        now = _now()
        if decision == ApprovalStatus.APPROVED:
            payload = self._approval_action_payload(node_run, node)
            if compute_action_fingerprint(payload) != approval.action_fingerprint:
                raise FingerprintMismatchError(
                    "The workflow node's upstream action no longer matches the fingerprint that was "
                    "approved -- refusing to advance the workflow.",
                    detail={"expected_action_fingerprint": approval.action_fingerprint},
                )
            output_ref = payload["upstream"][0]["artifact_id"] if payload["upstream"] else None
            applied = self._cas_node_run(
                node_run.id,
                WorkflowNodeRunStatus.WAITING_FOR_APPROVAL,
                status=WorkflowNodeRunStatus.COMPLETED,
                ended_at=now,
                output_snapshot_ref=output_ref,
            ) and self._cas_run(
                run.id, WorkflowRunStatus.NODE_WAITING_FOR_APPROVAL, status=WorkflowRunStatus.RUNNING
            )
        elif decision == ApprovalStatus.REJECTED:
            applied = self._cas_node_run(
                node_run.id,
                WorkflowNodeRunStatus.WAITING_FOR_APPROVAL,
                status=WorkflowNodeRunStatus.FAILED,
                ended_at=now,
            ) and self._cas_run(
                run.id,
                WorkflowRunStatus.NODE_WAITING_FOR_APPROVAL,
                status=WorkflowRunStatus.FAILED,
                ended_at=now,
            )
        else:
            raise ValueError(f"Not a human decision: {decision}")

        if not applied:
            raise InvalidStateTransitionError(
                "The workflow is no longer waiting on this approval (it may have been cancelled).",
                detail={"workflow_run_status": run.status.value, "node_run_status": node_run.status.value},
            )

    def after_approval_resolved(self, approval_id: str, *, actor_user_id: Optional[str], decided_now: bool) -> None:
        """Post-commit half of a decision (called by ApprovalService once the
        decision is durable). ``decided_now`` -- this call performed the
        decision -- emits the Flight Recorder evidence; either way the run is
        reconciled, which is what schedules the next ready node (approval)
        or heals a run that was left un-dispatched (a replayed decision after
        a crash). Database state is authoritative; events are best-effort."""
        approval = ApprovalService(self.db).get(approval_id)
        node_run = self._fresh_node_run(approval.scope_ref_id)
        run = self._fresh_run(node_run.workflow_run_id) if node_run else None
        if node_run is None or run is None:
            return

        if decided_now:
            self._emit_decision_events(approval, node_run, run, actor_user_id=actor_user_id)
        self.reconcile_workflow_run(run.id)

    def _emit_decision_events(
        self,
        approval: Approval,
        node_run: WorkflowNodeRun,
        run: WorkflowRun,
        *,
        actor_user_id: Optional[str],
        recovered: bool = False,
    ) -> None:
        human = {"actor_type": "user", "actor_user_id": actor_user_id}
        ids = {"workflow_run_id": run.id, "workflow_node_run_id": node_run.id}
        prefix = "recovered: " if recovered else ""
        artifact_refs = [approval.bound_artifact_id] if approval.bound_artifact_id else None
        if approval.status == ApprovalStatus.APPROVED:
            self._emit_event(
                run.task_run_id,
                "approval.approved",
                **ids,
                **human,
                artifact_refs=artifact_refs,
                decision_summary=f"{prefix}approval {approval.id} approved by a human; fingerprint={approval.action_fingerprint}",
            )
            self._emit_event(
                run.task_run_id,
                "workflow.node.completed",
                **ids,
                **human,
                artifact_refs=artifact_refs,
                decision_summary=f"{prefix}human approval node completed after approval {approval.id}",
            )
        elif approval.status == ApprovalStatus.REJECTED:
            self._emit_event(
                run.task_run_id,
                "approval.rejected",
                **ids,
                **human,
                artifact_refs=artifact_refs,
                decision_summary=f"{prefix}approval {approval.id} rejected by a human; fingerprint={approval.action_fingerprint}",
            )
            self._emit_event(
                run.task_run_id,
                "workflow.node.failed",
                **ids,
                **human,
                error={"code": "approval_rejected", "approval_id": approval.id},
            )
            self._emit_event(
                run.task_run_id,
                "workflow.failed",
                workflow_run_id=run.id,
                **human,
                decision_summary="Human approval rejected",
            )

    def _approval_action_payload(self, node_run: WorkflowNodeRun, node: WorkflowNode) -> Dict[str, Any]:
        """The exact action a human approves -- what the fingerprint binds:
        this node run, its approval group, and every upstream node run with
        the artifact it produced (id + content hash, read from the database,
        never from a cached object)."""
        upstream: List[Dict[str, Any]] = []
        for _key, up in self._upstream_sources(node_run):
            content_hash = None
            if up.output_snapshot_ref:
                content_hash = self.db.execute(
                    select(Artifact.content_hash).where(Artifact.id == up.output_snapshot_ref)
                ).scalar_one_or_none()
            upstream.append(
                {
                    "node_run_id": up.id,
                    "artifact_id": up.output_snapshot_ref,
                    "artifact_content_hash": content_hash,
                }
            )
        return {
            "kind": WORKFLOW_HUMAN_APPROVAL_OPERATION,
            "workflow_run_id": node_run.workflow_run_id,
            "workflow_node_run_id": node_run.id,
            "workflow_node_id": node.id,
            "node_key": node.node_key,
            "iteration": node_run.iteration,
            "approval_group": (node.config or {}).get("approval_group"),
            "upstream": upstream,
        }

    def _dispatch_human_approval(
        self, node_run: WorkflowNodeRun, node: WorkflowNode, workflow_run: WorkflowRun
    ) -> None:
        """A HUMAN_APPROVAL node becomes a durable wait -- never an execution.

        Creates NO TaskRun, AgentRun, model call or queue job; the Worker
        never sees this node. In ONE transaction: node PENDING ->
        WAITING_FOR_APPROVAL (compare-and-swap, so concurrent dispatchers
        cannot both proceed), exactly one PENDING Approval (idempotent via
        the partial unique index), run RUNNING -> NODE_WAITING_FOR_APPROVAL.
        If the run stopped being RUNNING meanwhile (cancelled), everything
        rolls back and the node stays PENDING for cancellation to handle.
        """
        incoming = self.db.query(WorkflowEdge).filter(WorkflowEdge.to_node_id == node.id).count()
        approval_group = (node.config or {}).get("approval_group")
        problem: Optional[str] = None
        if incoming > 1:
            problem = (
                "HUMAN_APPROVAL node has more than one upstream dependency "
                "(fan-in is not supported before MA7.4)"
            )
        elif not isinstance(approval_group, str) or not approval_group.strip():
            problem = "HUMAN_APPROVAL node missing config.approval_group"
        if problem:
            node_run.status = WorkflowNodeRunStatus.FAILED
            node_run.ended_at = _now()
            workflow_run.status = WorkflowRunStatus.FAILED
            workflow_run.ended_at = _now()
            self.db.commit()
            self._emit_event(
                workflow_run.task_run_id,
                "workflow.error",
                workflow_run_id=workflow_run.id,
                workflow_node_run_id=node_run.id,
                error={"message": problem},
            )
            return

        payload = self._approval_action_payload(node_run, node)
        bound_artifact_id = payload["upstream"][0]["artifact_id"] if payload["upstream"] else None
        try:
            if not self._cas_node_run(
                node_run.id,
                WorkflowNodeRunStatus.PENDING,
                status=WorkflowNodeRunStatus.WAITING_FOR_APPROVAL,
                started_at=_now(),
            ):
                self.db.rollback()
                return  # another dispatcher already took this node
            approval = ApprovalService(self.db).request_approval(
                scope=ApprovalScope.WORKFLOW_NODE_RUN,
                scope_ref_id=node_run.id,
                operation_type=WORKFLOW_HUMAN_APPROVAL_OPERATION,
                action_payload=payload,
                bound_artifact_id=bound_artifact_id,
                commit=False,
            )
            if not self._cas_run(
                workflow_run.id,
                WorkflowRunStatus.RUNNING,
                status=WorkflowRunStatus.NODE_WAITING_FOR_APPROVAL,
            ):
                self.db.rollback()
                return  # run no longer RUNNING (cancelled): stay PENDING
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

        self.db.refresh(node_run)
        self.db.refresh(workflow_run)
        ids = {"workflow_run_id": workflow_run.id, "workflow_node_run_id": node_run.id, "actor_type": "system"}
        self._emit_event(
            workflow_run.task_run_id,
            "workflow.node.waiting_for_approval",
            **ids,
            decision_summary=f"waiting for human approval (approval_group={approval_group}); approval {approval.id}",
        )
        self._emit_event(
            workflow_run.task_run_id,
            "approval.requested",
            **ids,
            artifact_refs=[bound_artifact_id] if bound_artifact_id else None,
            decision_summary=f"approval {approval.id} requested; fingerprint={approval.action_fingerprint}",
        )

    def _reconcile_waiting_approval(self, workflow_run: WorkflowRun) -> None:
        """A run that is NODE_WAITING_FOR_APPROVAL. Healthy case (Approval
        exists and is PENDING): nothing to do. Repairs: a waiting node with no
        Approval row (idempotently re-requested); an Approval that was
        resolved but whose node was never advanced (applied now)."""
        approvals = ApprovalService(self.db)
        for node_run in self._waiting_node_runs(workflow_run.id):
            node = self.db.get(WorkflowNode, node_run.workflow_node_id)
            if node is None:
                continue
            approval = approvals.find(
                ApprovalScope.WORKFLOW_NODE_RUN, node_run.id, WORKFLOW_HUMAN_APPROVAL_OPERATION
            )
            if approval is None:
                payload = self._approval_action_payload(node_run, node)
                approvals.request_approval(
                    scope=ApprovalScope.WORKFLOW_NODE_RUN,
                    scope_ref_id=node_run.id,
                    operation_type=WORKFLOW_HUMAN_APPROVAL_OPERATION,
                    action_payload=payload,
                    bound_artifact_id=payload["upstream"][0]["artifact_id"] if payload["upstream"] else None,
                )
                continue
            if approval.status in (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED):
                try:
                    self.apply_approval_decision(approval, approval.status)
                    self.db.commit()
                except (InvalidStateTransitionError, FingerprintMismatchError):
                    self.db.rollback()
                    logger.exception(
                        "approval_recovery_not_applied approval_id=%s workflow_run_id=%s",
                        approval.id,
                        workflow_run.id,
                    )
                    continue
                fresh_node_run = self._fresh_node_run(node_run.id)
                fresh_run = self._fresh_run(workflow_run.id)
                if fresh_node_run is not None and fresh_run is not None:
                    self._emit_decision_events(
                        approval, fresh_node_run, fresh_run, actor_user_id=approval.resolved_by, recovered=True
                    )

    def _cancel_pending_nodes(self, workflow_run_id: str) -> None:
        """Cancels every not-yet-dispatched node. Per node, a compare-and-swap
        on PENDING: a node a concurrent dispatcher claimed between the read
        and the write is left alone (it is RUNNING and is cancelled through
        its AgentRun instead) -- a blind status write here could mark a node
        CANCELLED while its job is queued and about to run."""
        pending_ids = [
            row[0]
            for row in self.db.execute(
                select(WorkflowNodeRun.id).where(
                    WorkflowNodeRun.workflow_run_id == workflow_run_id,
                    WorkflowNodeRun.status == WorkflowNodeRunStatus.PENDING,
                )
            ).all()
        ]
        for node_run_id in pending_ids:
            self._cas_node_run(
                node_run_id,
                WorkflowNodeRunStatus.PENDING,
                status=WorkflowNodeRunStatus.CANCELLED,
                ended_at=datetime.now(timezone.utc),
            )

        self.db.commit()

    def _cancel_waiting_approval_nodes(self, workflow_run: WorkflowRun, cancelled_by: Optional[str]) -> None:
        """Cancels every node durably waiting on a human. Per node, one
        transaction: the pending Approval -> EXPIRED (reason
        ``workflow_cancelled``, cancelling user preserved) and the node ->
        CANCELLED. Compare-and-swap on both, so it can never overwrite a
        decision that won a race with the cancellation; if the node is no
        longer waiting, nothing is changed."""
        approvals = ApprovalService(self.db)
        for node_run in self._waiting_node_runs(workflow_run.id):
            try:
                expired = approvals.expire_pending_for_cancellation(
                    scope=ApprovalScope.WORKFLOW_NODE_RUN,
                    scope_ref_id=node_run.id,
                    operation_type=WORKFLOW_HUMAN_APPROVAL_OPERATION,
                    cancelled_by=cancelled_by,
                    reason=WORKFLOW_CANCELLED_REASON,
                    commit=False,
                )
                if not self._cas_node_run(
                    node_run.id,
                    WorkflowNodeRunStatus.WAITING_FOR_APPROVAL,
                    status=WorkflowNodeRunStatus.CANCELLED,
                    ended_at=_now(),
                ):
                    self.db.rollback()
                    continue
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise

            actor = {"actor_type": "user" if cancelled_by else "system", "actor_user_id": cancelled_by}
            ids = {"workflow_run_id": workflow_run.id, "workflow_node_run_id": node_run.id}
            if expired is not None:
                self._emit_event(
                    workflow_run.task_run_id,
                    "approval.cancelled",
                    **ids,
                    **actor,
                    decision_summary=f"approval {expired.id} closed as expired: {WORKFLOW_CANCELLED_REASON}",
                )
            self._emit_event(workflow_run.task_run_id, "workflow.node.cancelled", **ids, **actor)

    def _reconcile_cancelling(self, workflow_run: WorkflowRun) -> None:
        """Converges a cancellation that was interrupted (or that is waiting
        for its last in-flight agent): pending and waiting nodes are
        cancelled, finished agents are recorded, and the run is finalized once
        every node is terminal. Never rewrites the original cancellation
        request time/user."""
        self._cancel_pending_nodes(workflow_run.id)
        self._cancel_waiting_approval_nodes(workflow_run, workflow_run.cancellation_requested_by)
        self._heal_agent_nodes(self._fresh_node_runs(workflow_run.id))
        self._check_workflow_complete(workflow_run.id)

    def _heal_agent_nodes(self, node_runs: List[WorkflowNodeRun]) -> None:
        """Crash windows around an agent node's dispatch/completion:
        AgentRun finished but the node run never recorded it (replayed
        through ``on_agent_run_complete``), or the node was claimed and its
        AgentRun created but the queue job never enqueued (enqueued now --
        the unique (job_type, payload_ref) constraint keeps it single)."""
        for node_run in node_runs:
            if not (node_run.agent_run_id and node_run.status == WorkflowNodeRunStatus.RUNNING):
                continue
            agent_run: Optional[AgentRun] = self.db.execute(
                select(AgentRun).where(AgentRun.id == node_run.agent_run_id).execution_options(populate_existing=True)
            ).scalar_one_or_none()
            if agent_run is None:
                continue
            if agent_run.status in (AgentRunStatus.COMPLETED, AgentRunStatus.FAILED, AgentRunStatus.STOPPED):
                self.on_agent_run_complete(agent_run.id)
            elif agent_run.status == AgentRunStatus.CREATED:
                self._ensure_agent_job_enqueued(agent_run.id)

    def _ensure_agent_job_enqueued(self, agent_run_id: str) -> None:
        already = self.db.query(JobQueue).filter(
            JobQueue.job_type == JobType.AGENT_RUN, JobQueue.payload_ref == agent_run_id
        ).first()
        if already is not None:
            return
        try:
            self.job_queue_repo.enqueue(self.db, job_type=JobType.AGENT_RUN, payload_ref=agent_run_id)
        except IntegrityError:
            self.db.rollback()  # a concurrent sweep/dispatcher enqueued it first

    # ===== Private helpers =====

    def _schedule_ready_nodes(self, workflow_run_id: str) -> None:
        """Identify and dispatch ready nodes.

        Ready = all upstream dependencies COMPLETED.
        Sequential enforcement: fail closed if multiple nodes simultaneously ready.
        """
        workflow_run: Optional[WorkflowRun] = self._fresh_run(workflow_run_id)
        if not workflow_run:
            return

        # Only a RUNNING run schedules work: a run that is waiting on a human,
        # cancelling or terminal must never dispatch a node (e.g. an approve
        # that raced a cancellation).
        if workflow_run.status != WorkflowRunStatus.RUNNING:
            return

        ready: List[WorkflowNodeRun] = self._find_ready_nodes(workflow_run_id)

        # Sequential-only: multiple ready = parallel (unsupported in MA7.2)
        if len(ready) > 1:
            failed = self._cas_run(workflow_run_id, WorkflowRunStatus.RUNNING, status=WorkflowRunStatus.FAILED)
            self.db.commit()
            if failed:
                self._emit_event(
                    workflow_run.task_run_id,
                    "workflow.error",
                    workflow_run_id=workflow_run_id,
                    error="Multiple nodes ready: parallel execution unsupported in MA7.2",
                )
            return

        for node_run in ready:
            self._dispatch_node_for_execution(node_run)

        # A TERMINAL node completes synchronously inside
        # _dispatch_node_for_execution above (no AgentRun/job involved), so
        # this is the one place on the normal completion path where "the
        # last node just finished" can be noticed and the WorkflowRun
        # itself promoted out of RUNNING. Idempotent and a no-op unless
        # every node run is now terminal (see _check_workflow_complete).
        self._check_workflow_complete(workflow_run_id)

    def _find_ready_nodes(self, workflow_run_id: str) -> List[WorkflowNodeRun]:
        """Find PENDING nodes whose incoming dependencies are ALL COMPLETED
        (an entry node, with no incoming edge, is ready).

        Decided from CURRENT committed state -- both the node runs and the
        edges are read with ``populate_existing`` -- never from whatever
        this session happened to load earlier: with several processes
        completing nodes concurrently, a stale "not COMPLETED yet" would
        stall a downstream node, and a stale "still PENDING" could re-offer a
        node another process already claimed. Returned in node_key order so
        dispatch order is deterministic.
        """
        run = self._fresh_run(workflow_run_id)
        if run is None:
            return []

        node_runs = self._fresh_node_runs(workflow_run_id)
        status_by_node = {nr.workflow_node_id: nr.status for nr in node_runs}
        keys: Dict[str, str] = {
            node_id: node_key
            for node_id, node_key in self.db.execute(
                select(WorkflowNode.id, WorkflowNode.node_key).where(
                    WorkflowNode.workflow_version_id == run.workflow_version_id
                )
            ).all()
        }
        sources_of: Dict[str, List[str]] = {}
        for edge in self.db.execute(
            select(WorkflowEdge)
            .where(WorkflowEdge.workflow_version_id == run.workflow_version_id)
            .execution_options(populate_existing=True)
        ).scalars():
            sources_of.setdefault(edge.to_node_id, []).append(edge.from_node_id)

        ready: List[WorkflowNodeRun] = []
        for node_run in node_runs:
            if node_run.status != WorkflowNodeRunStatus.PENDING:
                continue
            sources = sources_of.get(node_run.workflow_node_id, [])
            # A missing source node run counts as "not COMPLETED".
            if all(status_by_node.get(source) == WorkflowNodeRunStatus.COMPLETED for source in sources):
                ready.append(node_run)

        ready.sort(key=lambda nr: (keys.get(nr.workflow_node_id, ""), nr.id))
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
            completed = self._cas_node_run(
                node_run.id,
                WorkflowNodeRunStatus.PENDING,
                status=WorkflowNodeRunStatus.COMPLETED,
                ended_at=datetime.now(timezone.utc),
            )
            self.db.commit()

            if completed:  # only the dispatcher that made the change records it
                self._emit_event(
                    workflow_run.task_run_id,
                    "workflow.node.completed",
                    workflow_run_id=workflow_run.id,
                    workflow_node_run_id=node_run.id,
                )
            return

        # HUMAN_APPROVAL: a durable wait for a human, never an execution.
        if node.node_type == WorkflowNodeType.HUMAN_APPROVAL:
            self._dispatch_human_approval(node_run, node, workflow_run)
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
        sources = self._upstream_sources(node_run)
        upstream_evidence = self._upstream_evidence(sources)

        if upstream_evidence or sources:
            agent_run.input_context_json = {
                # ``kind`` lets AgentExecutionService render the upstream
                # artifacts' actual content into this agent's prompt
                # (MA7.3b); the ids remain the immutable lineage record.
                "kind": "workflow_upstream",
                # Canonical order: source node_key ascending; each artifact id
                # appears once. ``upstream`` labels every artifact with the
                # node(s) that produced it (MA7.4a).
                "upstream_artifact_ids": [entry["artifact_id"] for entry in upstream_evidence],
                "upstream_node_run_ids": [nr.id for _key, nr in sources],
                "upstream": upstream_evidence,
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

    def _upstream_sources(self, node_run: WorkflowNodeRun) -> List[Tuple[str, WorkflowNodeRun]]:
        """The COMPLETED source node runs of ``node_run``'s incoming edges, as
        ``(source node_key, source node run)`` pairs in CANONICAL order:
        ``node_key`` ascending (unique and immutable within a workflow
        version, so identical across runs and restarts), ``node_run.id`` only
        as a tie-break. Never edge-insertion or database order. Read from
        current committed state."""
        from_ids = [
            row[0]
            for row in self.db.execute(
                select(WorkflowEdge.from_node_id).where(WorkflowEdge.to_node_id == node_run.workflow_node_id)
            ).all()
        ]
        if not from_ids:
            return []
        rows = self.db.execute(
            select(WorkflowNode.node_key, WorkflowNodeRun)
            .join(WorkflowNodeRun, WorkflowNodeRun.workflow_node_id == WorkflowNode.id)
            .where(
                WorkflowNode.id.in_(from_ids),
                WorkflowNodeRun.workflow_run_id == node_run.workflow_run_id,
                WorkflowNodeRun.status == WorkflowNodeRunStatus.COMPLETED,
            )
            .execution_options(populate_existing=True)
        ).all()
        return sorted(((key, nr) for key, nr in rows), key=lambda pair: (pair[0], pair[1].id))

    @staticmethod
    def _upstream_evidence(sources: List[Tuple[str, WorkflowNodeRun]]) -> List[Dict[str, Any]]:
        """Immutable, labelled upstream evidence for a downstream node: one
        entry per DISTINCT artifact id, in canonical (source node_key
        ascending) order. When several sources yield the same artifact
        (e.g. a diamond, or a pass-through) the first source in canonical
        order is the entry's ``node_key``/``node_run_id`` and every source is
        listed in ``source_node_keys`` -- nothing is silently dropped, and
        the result never depends on edge order."""
        by_artifact: Dict[str, Dict[str, Any]] = {}
        for key, source in sources:
            artifact_id = source.output_snapshot_ref
            if not artifact_id:
                continue
            entry = by_artifact.get(artifact_id)
            if entry is None:
                by_artifact[artifact_id] = {
                    "artifact_id": artifact_id,
                    "node_key": key,
                    "node_run_id": source.id,
                    "source_node_keys": [key],
                }
            elif key not in entry["source_node_keys"]:
                entry["source_node_keys"].append(key)
        return list(by_artifact.values())

    def _collect_upstream_artifacts(self, node_run: WorkflowNodeRun) -> List[str]:
        """Immutable upstream artifact ids: canonical order, de-duplicated."""
        return [entry["artifact_id"] for entry in self._upstream_evidence(self._upstream_sources(node_run))]

    def _get_upstream_node_runs(self, node_run: WorkflowNodeRun) -> List[WorkflowNodeRun]:
        """All upstream COMPLETED node runs, in canonical (node_key) order."""
        return [source for _key, source in self._upstream_sources(node_run)]

    def _check_workflow_complete(self, workflow_run_id: str) -> None:
        """Finalize a run whose nodes have all reached a terminal status.

        Decided from CURRENT committed state, and applied as a
        compare-and-swap on the status that was observed, so it is idempotent
        and safe to call from any number of processes: exactly one caller
        makes the transition and records its evidence; the rest are no-ops.
        This is also the finalize-on-drain step -- a CANCELLING run becomes
        CANCELLED the moment its last in-flight node is terminal (completed
        siblings keep their results); a failed node makes the run FAILED
        (failure outranks cancellation).
        """
        workflow_run: Optional[WorkflowRun] = self._fresh_run(workflow_run_id)
        if not workflow_run:
            return

        observed = workflow_run.status
        if observed not in (WorkflowRunStatus.RUNNING, WorkflowRunStatus.CANCELLING):
            return  # not active, or already terminal

        statuses = [nr.status for nr in self._fresh_node_runs(workflow_run_id)]
        if any(
            status
            not in (
                WorkflowNodeRunStatus.COMPLETED,
                WorkflowNodeRunStatus.FAILED,
                WorkflowNodeRunStatus.CANCELLED,
            )
            for status in statuses
        ):
            return  # something is still in flight

        if WorkflowNodeRunStatus.FAILED in statuses:
            target, event, summary = WorkflowRunStatus.FAILED, "workflow.failed", None
        elif observed == WorkflowRunStatus.CANCELLING:
            target, event, summary = WorkflowRunStatus.CANCELLED, "workflow.cancelled", "cancellation complete"
        else:
            target, event, summary = WorkflowRunStatus.COMPLETED, "workflow.completed", None

        won = self._cas_run(workflow_run_id, observed, status=target, ended_at=_now())
        self.db.commit()
        if won:
            extra = {"decision_summary": summary} if summary else {}
            self._emit_event(workflow_run.task_run_id, event, workflow_run_id=workflow_run_id, **extra)
