"""Approval Service — Section 11, 23. Creates, reads, lists and resolves
Approval records. PR merge is V1's initial consequential-action acceptance
case (Owner decision); real protected-branch merge stays disabled until Git
Tool safety/approval testing completes.

MA7.3a (Approval Core): the one approval mechanism (spec 23.5 -- "one
approval system, not two"). Nothing here resolves an approval on its own:
the only path from PENDING to APPROVED/REJECTED is ``resolve``, which
requires an authenticated ``User`` with MODIFY access to the owning
project, and there is no timeout/expiry/auto-approval logic anywhere in this
module. Scope-specific side effects (e.g. advancing a Workflow Node Run once
its approval resolves) live in the owning engine, not here: the one hook is
``_apply_scope_effect``/``_finish``, which only fire for the workflow
human-approval gate (``WORKFLOW_HUMAN_APPROVAL_OPERATION``) and delegate to
``WorkflowExecutionService`` (MA7.3b).

Only ``ApprovalScope.WORKFLOW_NODE_RUN`` has a project-ownership lookup so
far. ``Approval`` carries no ``project_id`` of its own, so an approval whose
owning project cannot be established is unreachable through this service
(fail closed) rather than readable/resolvable by anyone.
"""

import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from typing import Any, List, Mapping, Optional, cast

from sqlalchemy import select, text, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import OperationalError

from app.authz import ProjectAction, check_project_access
from app.db.enums import ApprovalScope, ApprovalStatus
from app.db.mixins import new_uuid
from app.errors import (
    ConflictError,
    FingerprintMismatchError,
    ForbiddenError,
    InvalidStateTransitionError,
    NotFoundError,
)
from app.models.governance import Approval
from app.models.identity import Project, User
from app.models.workflow import Workflow, WorkflowNodeRun, WorkflowRun, WorkflowVersion
from app.services.audit_service import AuditService
from app.services.base import BaseService

logger = logging.getLogger("app.services.approval_service")

# The operation the workflow engine's HUMAN_APPROVAL node requests (MA7.3b).
# Only approvals with this operation_type advance/fail/cancel a Workflow Node
# Run; any other approval on a node run is a plain approval record.
WORKFLOW_HUMAN_APPROVAL_OPERATION = "workflow.human_approval"

_SUPPORTED_REQUEST_SCOPES = (ApprovalScope.WORKFLOW_NODE_RUN,)
_WORKFLOW_NODE_RUN_UNIQUE_WHERE = text("scope = 'workflow_node_run'")
_MAX_OPERATION_TYPE_LENGTH = 120


def compute_action_fingerprint(action_payload: Mapping[str, Any]) -> str:
    """sha256 (hex, 64 chars) over the canonical JSON of the exact action
    being approved (Section 24.4 #15). Canonical = sorted keys, no
    whitespace, ASCII-escaped; a value that is not plain JSON raises
    ``TypeError`` rather than being silently stringified, so two payloads
    can never fingerprint equal by accident."""
    canonical = json.dumps(action_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ApprovalService(BaseService):
    # -- creation ----------------------------------------------------------

    def request_approval(
        self,
        *,
        scope: ApprovalScope,
        scope_ref_id: str,
        operation_type: str,
        action_payload: Mapping[str, Any],
        requested_by: Optional[str] = None,
        bound_artifact_id: Optional[str] = None,
        policy_rule_id: Optional[str] = None,
        commit: bool = True,
    ) -> Approval:
        """Creates a PENDING Approval, computing and storing its
        ``action_fingerprint`` from ``action_payload`` (Section 24.4 #15).

        Idempotent per (scope, scope_ref_id, operation_type): a repeat
        request returns the existing row instead of a duplicate -- enforced
        atomically by the partial unique index (INSERT .. ON CONFLICT DO
        NOTHING), never a check-then-insert race. A repeat with a
        *different* fingerprint is a ``ConflictError``: the existing
        approval was requested for a different action and is never silently
        reused or overwritten.

        ``commit=False`` flushes only, so a caller (MA7.3b's node dispatch)
        can make the request part of its own transaction. ``requested_by``
        is ``None`` for a system-created request.
        """
        if scope not in _SUPPORTED_REQUEST_SCOPES:
            raise ValueError(f"Approval scope {scope.value!r} is not supported yet.")
        if not operation_type or len(operation_type) > _MAX_OPERATION_TYPE_LENGTH:
            raise ValueError(f"operation_type must be 1..{_MAX_OPERATION_TYPE_LENGTH} characters.")
        if self.db.get(WorkflowNodeRun, scope_ref_id) is None:
            raise ValueError(f"Workflow node run {scope_ref_id} not found.")

        fingerprint = compute_action_fingerprint(action_payload)
        approval_id = new_uuid()
        inserted = cast(
            CursorResult,
            self.db.execute(
                sqlite_insert(Approval)
                .values(
                    id=approval_id,
                    scope=scope,
                    scope_ref_id=scope_ref_id,
                    operation_type=operation_type,
                    policy_rule_id=policy_rule_id,
                    action_fingerprint=fingerprint,
                    bound_artifact_id=bound_artifact_id,
                    status=ApprovalStatus.PENDING,
                    requested_by=requested_by,
                    requested_at=_utcnow(),
                )
                .on_conflict_do_nothing(
                    index_elements=["scope", "scope_ref_id", "operation_type"],
                    index_where=_WORKFLOW_NODE_RUN_UNIQUE_WHERE,
                )
            ),
        )

        if inserted.rowcount == 1:
            approval = self.get(approval_id)
        else:
            approval = self._read_existing(scope, scope_ref_id, operation_type)
            if approval.action_fingerprint != fingerprint:
                raise ConflictError(
                    f"An approval for {scope.value} {scope_ref_id} / {operation_type!r} already exists "
                    "for a different action."
                )

        if commit:
            self.db.commit()
            self.db.refresh(approval)
        else:
            self.db.flush()
        return approval

    # -- read --------------------------------------------------------------

    def get(self, approval_id: str) -> Approval:
        approval = self._read_fresh(approval_id)
        if approval is None:
            raise NotFoundError(f"Approval {approval_id} not found.")
        return approval

    def find(self, scope: ApprovalScope, scope_ref_id: str, operation_type: str) -> Optional[Approval]:
        """The approval for (scope, reference, operation), or ``None``."""
        return self.db.execute(
            select(Approval)
            .where(
                Approval.scope == scope,
                Approval.scope_ref_id == scope_ref_id,
                Approval.operation_type == operation_type,
            )
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()

    def owning_project_id(self, approval: Approval) -> str:
        """Scope -> owning project. Fail closed: an unsupported scope, or a
        reference that no longer resolves, raises ``ForbiddenError`` -- an
        approval whose project cannot be established is never visible or
        decidable."""
        if approval.scope != ApprovalScope.WORKFLOW_NODE_RUN:
            raise ForbiddenError(f"Approval scope {approval.scope.value!r} has no supported project lookup.")
        project_id = self.db.execute(
            select(Workflow.project_id)
            .select_from(WorkflowNodeRun)
            .join(WorkflowRun, WorkflowRun.id == WorkflowNodeRun.workflow_run_id)
            .join(WorkflowVersion, WorkflowVersion.id == WorkflowRun.workflow_version_id)
            .join(Workflow, Workflow.id == WorkflowVersion.workflow_id)
            .where(WorkflowNodeRun.id == approval.scope_ref_id)
        ).scalar_one_or_none()
        if project_id is None:
            raise ForbiddenError("The owning project of this approval could not be established.")
        return project_id

    def get_for_user(self, approval_id: str, *, user: User, action: ProjectAction) -> Approval:
        """Resource-then-authorize, same order as every other router: 404
        when the approval does not exist, then the owning project's
        membership/role check (401 is the auth dependency's, upstream)."""
        approval = self.get(approval_id)
        check_project_access(self.db, user=user, project_id=self.owning_project_id(approval), action=action)
        return approval

    def list_for_user(
        self,
        *,
        user: User,
        project_id: str,
        status: Optional[ApprovalStatus] = ApprovalStatus.PENDING,
        limit: int = 100,
    ) -> List[Approval]:
        """READ on ``project_id``, then only that project's approvals
        (newest first). ``status=None`` lists every status."""
        check_project_access(self.db, user=user, project_id=project_id, action=ProjectAction.READ)
        stmt = (
            select(Approval)
            .join(WorkflowNodeRun, WorkflowNodeRun.id == Approval.scope_ref_id)
            .join(WorkflowRun, WorkflowRun.id == WorkflowNodeRun.workflow_run_id)
            .join(WorkflowVersion, WorkflowVersion.id == WorkflowRun.workflow_version_id)
            .join(Workflow, Workflow.id == WorkflowVersion.workflow_id)
            .where(Approval.scope == ApprovalScope.WORKFLOW_NODE_RUN, Workflow.project_id == project_id)
            .order_by(Approval.requested_at.desc(), Approval.id)
            .limit(limit)
            .execution_options(populate_existing=True)
        )
        if status is not None:
            stmt = stmt.where(Approval.status == status)
        return list(self.db.execute(stmt).scalars().all())

    # -- decision ----------------------------------------------------------

    def resolve(
        self,
        approval_id: str,
        *,
        user: User,
        approve: bool,
        action_fingerprint: str,
        note: Optional[str] = None,
    ) -> Approval:
        """Records a human decision. Order of checks: approval exists (404)
        -> owning project established + MODIFY access (403) -> echoed
        fingerprint matches (409 ``fingerprint_mismatch``) -> state.

        * PENDING -> APPROVED/REJECTED, atomically: one compare-and-swap
          ``UPDATE .. WHERE status = 'pending'`` writes ``status``,
          ``resolved_by``, ``resolved_at`` and ``resolution_note`` together
          with the audit event, in a single transaction. Exactly one of any
          number of concurrent callers wins the swap.
        * Same decision again -> idempotent replay: the existing resolved
          row is returned untouched (original resolver/time/note kept; no
          new audit event).
        * Any other already-resolved state (opposite decision, EXPIRED) ->
          409 ``invalid_state_transition``; the resolved row is never
          modified.

        The resolver is always a real, authenticated ``User`` -- there is no
        system/agent/timeout path to a decision.
        """
        approval = self.get(approval_id)
        project_id = self.owning_project_id(approval)
        check_project_access(self.db, user=user, project_id=project_id, action=ProjectAction.MODIFY)

        if not hmac.compare_digest(
            action_fingerprint.encode("utf-8"), approval.action_fingerprint.encode("utf-8")
        ):
            raise FingerprintMismatchError(
                "The echoed action_fingerprint does not match this approval's action -- refusing to "
                "record a decision on an action that changed since it was displayed.",
                detail={"expected_action_fingerprint": approval.action_fingerprint},
            )

        decision = ApprovalStatus.APPROVED if approve else ApprovalStatus.REJECTED
        if approval.status != ApprovalStatus.PENDING:
            return self._finish(self._replay_or_conflict(approval, decision), user, decided_now=False)

        cleaned_note = (note or "").strip() or None
        try:
            won = self._compare_and_swap_decision(
                approval, decision=decision, user=user, project_id=project_id, note=cleaned_note
            )
        except OperationalError:
            # e.g. SQLITE_BUSY that outlasted busy_timeout under contention.
            self.db.rollback()
            current = self._read_fresh(approval_id)
            if current is None or current.status == ApprovalStatus.PENDING:
                raise ConflictError(
                    f"Approval {approval_id} is being resolved concurrently; retry the request."
                )
            return self._finish(self._replay_or_conflict(current, decision), user, decided_now=False)

        current = self._read_fresh(approval_id)
        if current is None:
            raise NotFoundError(f"Approval {approval_id} not found.")
        if won:
            return self._finish(current, user, decided_now=True)
        return self._finish(self._replay_or_conflict(current, decision), user, decided_now=False)

    # -- internals ---------------------------------------------------------

    def _compare_and_swap_decision(
        self,
        approval: Approval,
        *,
        decision: ApprovalStatus,
        user: User,
        project_id: str,
        note: Optional[str],
    ) -> bool:
        """The single write path that can resolve an approval. Returns
        ``True`` if this call won the swap (and committed decision + audit
        together); ``False`` if another writer already resolved it (nothing
        written, transaction released)."""
        swapped = cast(
            CursorResult,
            self.db.execute(
                update(Approval)
                .where(Approval.id == approval.id, Approval.status == ApprovalStatus.PENDING)
                .values(
                    status=decision,
                    resolved_by=user.id,
                    resolved_at=_utcnow(),
                    resolution_note=note,
                )
                .execution_options(synchronize_session=False)
            ),
        )
        if swapped.rowcount != 1:
            self.db.rollback()
            return False

        try:
            project = self.db.get(Project, project_id)
            if project is not None:
                AuditService(self.db).record(
                    org_id=project.org_id,
                    event_type=f"approval.{decision.value}",
                    actor_user_id=user.id,
                    target_ref=approval.id,
                    detail={
                        "scope": approval.scope.value,
                        "scope_ref_id": approval.scope_ref_id,
                        "operation_type": approval.operation_type,
                        "action_fingerprint": approval.action_fingerprint,
                        "decision": decision.value,
                        "has_resolution_note": note is not None,
                    },
                    commit=False,
                )
            self._apply_scope_effect(approval, decision)
            self.db.commit()
        except Exception:
            # Decision, audit event and any scope effect commit together or
            # not at all.
            self.db.rollback()
            raise
        return True

    def expire_pending_for_cancellation(
        self,
        *,
        scope: ApprovalScope,
        scope_ref_id: str,
        operation_type: str,
        cancelled_by: Optional[str],
        reason: str,
        commit: bool = True,
    ) -> Optional[Approval]:
        """Closes a still-PENDING approval whose subject was cancelled (V1
        decision: EXPIRED, never REJECTED -- a cancellation is not a human
        decision, and ``ApprovalStatus`` has no CANCELLED). Same
        compare-and-swap discipline as ``resolve``: only a PENDING row is
        touched, so a decision that already won is never overwritten.
        Returns the expired approval, or ``None`` if there was no pending
        approval to close. ``resolved_by`` preserves the cancelling user's
        identity (``None`` for a system cancel) and ``resolution_note``
        carries ``reason``; the audit event ``approval.cancelled`` records
        the same. ``commit=False`` leaves the transaction to the caller.
        """
        swapped = cast(
            CursorResult,
            self.db.execute(
                update(Approval)
                .where(
                    Approval.scope == scope,
                    Approval.scope_ref_id == scope_ref_id,
                    Approval.operation_type == operation_type,
                    Approval.status == ApprovalStatus.PENDING,
                )
                .values(
                    status=ApprovalStatus.EXPIRED,
                    resolved_by=cancelled_by,
                    resolved_at=_utcnow(),
                    resolution_note=reason,
                )
                .execution_options(synchronize_session=False)
            ),
        )
        if swapped.rowcount != 1:
            return None

        approval = self._read_existing(scope, scope_ref_id, operation_type)
        project = self.db.get(Project, self.owning_project_id(approval))
        if project is not None:
            AuditService(self.db).record(
                org_id=project.org_id,
                event_type="approval.cancelled",
                actor_user_id=cancelled_by,
                target_ref=approval.id,
                detail={
                    "scope": approval.scope.value,
                    "scope_ref_id": approval.scope_ref_id,
                    "operation_type": approval.operation_type,
                    "action_fingerprint": approval.action_fingerprint,
                    "reason": reason,
                },
                commit=False,
            )
        if commit:
            self.db.commit()
            self.db.refresh(approval)
        else:
            self.db.flush()
        return approval

    def _apply_scope_effect(self, approval: Approval, decision: ApprovalStatus) -> None:
        """Runs INSIDE the decision transaction (after the CAS and the audit
        write, before the commit): a workflow human-approval gate's node/run
        transition commits atomically with the decision, or -- if the
        workflow is no longer waiting on it -- raises and rolls the whole
        decision back."""
        if (
            approval.scope == ApprovalScope.WORKFLOW_NODE_RUN
            and approval.operation_type == WORKFLOW_HUMAN_APPROVAL_OPERATION
        ):
            from app.services.workflow_execution_service import WorkflowExecutionService

            WorkflowExecutionService(self.db).apply_approval_decision(approval, decision)

    def _finish(self, approval: Approval, user: User, *, decided_now: bool) -> Approval:
        """After the decision is durable: hand the (already-committed) result
        to the owning engine so it can emit evidence and resume/heal the run.
        Never raises -- the decision is committed; reconciliation repairs
        anything this misses."""
        if (
            approval.scope == ApprovalScope.WORKFLOW_NODE_RUN
            and approval.operation_type == WORKFLOW_HUMAN_APPROVAL_OPERATION
        ):
            try:
                from app.services.workflow_execution_service import WorkflowExecutionService

                WorkflowExecutionService(self.db).after_approval_resolved(
                    approval.id, actor_user_id=user.id, decided_now=decided_now
                )
            except Exception:
                logger.exception("approval_post_commit_resume_failed approval_id=%s", approval.id)
                self.db.rollback()
        return approval

    def _replay_or_conflict(self, approval: Approval, decision: ApprovalStatus) -> Approval:
        if approval.status == decision:
            return approval
        detail = {
            "status": approval.status.value,
            "resolved_at": approval.resolved_at.isoformat() if approval.resolved_at else None,
        }
        raise InvalidStateTransitionError(
            f"Approval {approval.id} is already {approval.status.value!r}; it cannot be changed to "
            f"{decision.value!r}.",
            detail=detail,
        )

    def _read_fresh(self, approval_id: str) -> Optional[Approval]:
        # populate_existing: a session with expire_on_commit=False must not
        # hand back a stale identity-mapped row after another writer's swap.
        return self.db.execute(
            select(Approval).where(Approval.id == approval_id).execution_options(populate_existing=True)
        ).scalar_one_or_none()

    def _read_existing(self, scope: ApprovalScope, scope_ref_id: str, operation_type: str) -> Approval:
        return self.db.execute(
            select(Approval)
            .where(
                Approval.scope == scope,
                Approval.scope_ref_id == scope_ref_id,
                Approval.operation_type == operation_type,
            )
            .execution_options(populate_existing=True)
        ).scalar_one()
