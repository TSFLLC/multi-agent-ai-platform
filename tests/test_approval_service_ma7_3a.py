"""MA7.3a — ApprovalService (Approval Core).

Service-level behavior: request/idempotency/uniqueness, fingerprint, scope ->
project lookup, decision (CAS) semantics, immutable provenance, audit
atomicity, concurrency. HTTP-level behavior is in test_approval_api_ma7_3a.py.
All tests use disposable temp DBs (conftest fixtures) -- never the real one.
"""

import ast
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, OperationalError

from app.authz import ProjectAction
from app.db.enums import (
    ApprovalScope,
    ApprovalStatus,
    ProjectRole,
    VersionStatus,
    WorkflowNodeRunStatus,
    WorkflowNodeType,
)
from app.errors import (
    ConflictError,
    FingerprintMismatchError,
    ForbiddenError,
    InvalidStateTransitionError,
    NotFoundError,
)
from app.models.governance import Approval
from app.models.identity import Project, ProjectMembership
from app.models.observability import AuditEvent
from app.models.workflow import Workflow, WorkflowNode, WorkflowNodeRun, WorkflowRun, WorkflowVersion
from app.services.approval_service import ApprovalService, compute_action_fingerprint
from tests.conftest import make_task, make_task_run

APP_DIR = Path(__file__).resolve().parent.parent / "app"

PAYLOAD = {"node_key": "gate", "approval_group": "eng-leads", "upstream_artifact_ids": ["a-1"]}


def make_workflow_node_run(db, project, node_key="gate"):
    """A minimal HUMAN_APPROVAL WorkflowNodeRun in ``project`` -- built from
    rows directly (no WorkflowExecutionService; MA7.3a does not integrate
    HUMAN_APPROVAL into the engine)."""
    workflow = Workflow(project_id=project.id, name=f"wf-{node_key}")
    db.add(workflow)
    db.flush()
    version = WorkflowVersion(workflow_id=workflow.id, version=1, status=VersionStatus.ACTIVE)
    db.add(version)
    db.flush()
    node = WorkflowNode(
        workflow_version_id=version.id,
        node_key=node_key,
        node_type=WorkflowNodeType.HUMAN_APPROVAL,
        config={"approval_group": "eng-leads"},
    )
    db.add(node)
    task = make_task(db, project)
    task_run = make_task_run(db, task)
    run = WorkflowRun(task_run_id=task_run.id, workflow_version_id=version.id)
    db.add(run)
    db.flush()
    node_run = WorkflowNodeRun(
        workflow_run_id=run.id,
        workflow_node_id=node.id,
        iteration=0,
        status=WorkflowNodeRunStatus.WAITING_FOR_APPROVAL,
    )
    db.add(node_run)
    db.commit()
    return node_run


def request_workflow_approval(db, node_run, payload=None, operation_type="workflow_node_approval", **kwargs):
    return ApprovalService(db).request_approval(
        scope=ApprovalScope.WORKFLOW_NODE_RUN,
        scope_ref_id=node_run.id,
        operation_type=operation_type,
        action_payload=payload if payload is not None else PAYLOAD,
        **kwargs,
    )


@pytest.fixture()
def node_run(db, bootstrap):
    return make_workflow_node_run(db, bootstrap.project)


@pytest.fixture()
def pending(db, node_run):
    return request_workflow_approval(db, node_run)


# --- fingerprint ----------------------------------------------------------


def test_fingerprint_is_deterministic_64_hex_and_order_independent():
    a = compute_action_fingerprint({"x": 1, "y": [1, 2], "z": {"k": "v"}})
    b = compute_action_fingerprint({"z": {"k": "v"}, "y": [1, 2], "x": 1})
    assert a == b
    assert len(a) == 64 and all(c in "0123456789abcdef" for c in a)


def test_fingerprint_changes_when_any_bound_value_changes():
    base = compute_action_fingerprint(PAYLOAD)
    assert compute_action_fingerprint({**PAYLOAD, "upstream_artifact_ids": ["a-2"]}) != base
    assert compute_action_fingerprint({**PAYLOAD, "approval_group": "other"}) != base


def test_fingerprint_rejects_non_json_values_instead_of_stringifying_them():
    with pytest.raises(TypeError):
        compute_action_fingerprint({"when": datetime.now(timezone.utc)})


# --- request --------------------------------------------------------------


def test_request_creates_pending_approval_with_stored_fingerprint(db, node_run):
    approval = request_workflow_approval(db, node_run, bound_artifact_id=None)
    assert approval.status == ApprovalStatus.PENDING
    assert approval.scope == ApprovalScope.WORKFLOW_NODE_RUN
    assert approval.scope_ref_id == node_run.id
    assert approval.action_fingerprint == compute_action_fingerprint(PAYLOAD)
    assert approval.requested_by is None  # system-created
    assert approval.resolved_by is None and approval.resolved_at is None
    assert approval.resolution_note is None
    assert approval.expires_at is None  # no timeout semantics exist


def test_request_is_idempotent_and_never_creates_a_duplicate(db, node_run):
    first = request_workflow_approval(db, node_run)
    second = request_workflow_approval(db, node_run)
    assert second.id == first.id
    assert db.query(Approval).count() == 1


def test_request_with_different_fingerprint_for_same_node_run_is_a_conflict(db, node_run):
    first = request_workflow_approval(db, node_run)
    with pytest.raises(ConflictError):
        request_workflow_approval(db, node_run, payload={**PAYLOAD, "upstream_artifact_ids": ["other"]})
    db.rollback()
    assert db.query(Approval).count() == 1
    db.refresh(first)
    assert first.action_fingerprint == compute_action_fingerprint(PAYLOAD)


def test_different_operation_types_for_one_node_run_are_distinct_approvals(db, node_run):
    a = request_workflow_approval(db, node_run, operation_type="op-a")
    b = request_workflow_approval(db, node_run, operation_type="op-b")
    assert a.id != b.id


def test_request_commit_false_only_flushes(db, session_factory, node_run):
    approval = request_workflow_approval(db, node_run, commit=False)
    other = session_factory()
    try:
        assert other.query(Approval).count() == 0  # not visible until the caller commits
    finally:
        other.close()
    db.commit()
    assert db.get(Approval, approval.id) is not None


def test_request_rejects_unsupported_scope_and_missing_reference(db, node_run):
    service = ApprovalService(db)
    with pytest.raises(ValueError):
        service.request_approval(
            scope=ApprovalScope.TASK_RUN, scope_ref_id=node_run.id, operation_type="op", action_payload={}
        )
    with pytest.raises(ValueError):
        service.request_approval(
            scope=ApprovalScope.WORKFLOW_NODE_RUN,
            scope_ref_id="does-not-exist",
            operation_type="op",
            action_payload={},
        )
    with pytest.raises(ValueError):
        service.request_approval(
            scope=ApprovalScope.WORKFLOW_NODE_RUN,
            scope_ref_id=node_run.id,
            operation_type="",
            action_payload={},
        )
    assert db.query(Approval).count() == 0


def test_database_rejects_a_second_workflow_node_approval_even_via_raw_sql(db, pending):
    """The partial unique index is the backstop behind the service's own
    idempotency -- a writer that bypasses ApprovalService still cannot
    create a duplicate workflow-node approval."""
    with pytest.raises(IntegrityError):
        db.execute(
            text(
                "INSERT INTO approvals (id, scope, scope_ref_id, operation_type, action_fingerprint, "
                "status, requested_at) VALUES ('dup', 'workflow_node_run', :ref, :op, 'f', 'pending', "
                "CURRENT_TIMESTAMP)"
            ),
            {"ref": pending.scope_ref_id, "op": pending.operation_type},
        )
        db.commit()
    db.rollback()


def test_uniqueness_is_scoped_to_workflow_node_run_only(db, pending):
    """Other scopes may legitimately re-request for the same reference."""
    for row_id in ("t1", "t2"):
        db.execute(
            text(
                "INSERT INTO approvals (id, scope, scope_ref_id, operation_type, action_fingerprint, "
                "status, requested_at) VALUES (:id, 'task_run', 'same-ref', 'op', 'f', 'pending', "
                "CURRENT_TIMESTAMP)"
            ),
            {"id": row_id},
        )
    db.commit()
    assert db.query(Approval).filter(Approval.scope == ApprovalScope.TASK_RUN).count() == 2


def test_concurrent_requests_create_exactly_one_approval(session_factory, node_run):
    barrier = threading.Barrier(6)

    def attempt(_):
        session = session_factory()
        try:
            barrier.wait(timeout=10)
            return request_workflow_approval(session, node_run).id
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=6) as pool:
        ids = list(pool.map(attempt, range(6)))

    assert len(set(ids)) == 1
    check = session_factory()
    try:
        assert check.query(Approval).count() == 1
    finally:
        check.close()


# --- scope -> owning project ---------------------------------------------


def test_owning_project_is_resolved_through_the_workflow_node_run_chain(db, bootstrap, pending):
    assert ApprovalService(db).owning_project_id(pending) == bootstrap.project.id


def test_owning_project_fails_closed_for_unsupported_scope_and_dangling_reference(db, pending):
    service = ApprovalService(db)
    unsupported = Approval(
        scope=ApprovalScope.TASK_RUN,
        scope_ref_id="x",
        operation_type="op",
        action_fingerprint="f" * 64,
        status=ApprovalStatus.PENDING,
    )
    dangling = Approval(
        scope=ApprovalScope.WORKFLOW_NODE_RUN,
        scope_ref_id="no-such-node-run",
        operation_type="op",
        action_fingerprint="f" * 64,
        status=ApprovalStatus.PENDING,
    )
    with pytest.raises(ForbiddenError):
        service.owning_project_id(unsupported)
    with pytest.raises(ForbiddenError):
        service.owning_project_id(dangling)


# --- read / list ----------------------------------------------------------


def test_get_missing_approval_is_not_found(db):
    with pytest.raises(NotFoundError):
        ApprovalService(db).get("nope")


def test_get_for_user_requires_project_membership(db, bootstrap):
    other_project = Project(org_id=bootstrap.organization.id, name="unshared")
    db.add(other_project)
    db.flush()
    foreign = request_workflow_approval(db, make_workflow_node_run(db, other_project))
    with pytest.raises(ForbiddenError):
        ApprovalService(db).get_for_user(foreign.id, user=bootstrap.user, action=ProjectAction.READ)


def test_list_is_scoped_to_the_requested_project_and_filters_by_status(db, bootstrap, pending):
    other_project = Project(org_id=bootstrap.organization.id, name="other")
    db.add(other_project)
    db.flush()
    db.add(ProjectMembership(project_id=other_project.id, user_id=bootstrap.user.id, role=ProjectRole.OWNER))
    db.commit()
    foreign = request_workflow_approval(db, make_workflow_node_run(db, other_project))

    service = ApprovalService(db)
    mine = service.list_for_user(user=bootstrap.user, project_id=bootstrap.project.id)
    theirs = service.list_for_user(user=bootstrap.user, project_id=other_project.id)
    assert [a.id for a in mine] == [pending.id]
    assert [a.id for a in theirs] == [foreign.id]

    service.resolve(
        pending.id, user=bootstrap.user, approve=True, action_fingerprint=pending.action_fingerprint
    )
    assert service.list_for_user(user=bootstrap.user, project_id=bootstrap.project.id) == []
    approved = service.list_for_user(
        user=bootstrap.user, project_id=bootstrap.project.id, status=ApprovalStatus.APPROVED
    )
    assert [a.id for a in approved] == [pending.id]
    everything = service.list_for_user(user=bootstrap.user, project_id=bootstrap.project.id, status=None)
    assert [a.id for a in everything] == [pending.id]


def test_list_requires_read_on_the_project(db, bootstrap):
    other_project = Project(org_id=bootstrap.organization.id, name="no-membership")
    db.add(other_project)
    db.commit()
    with pytest.raises(ForbiddenError):
        ApprovalService(db).list_for_user(user=bootstrap.user, project_id=other_project.id)


# --- resolve: approve / reject / provenance -------------------------------


def test_approve_records_immutable_decision_provenance(db, bootstrap, pending):
    resolved = ApprovalService(db).resolve(
        pending.id,
        user=bootstrap.user,
        approve=True,
        action_fingerprint=pending.action_fingerprint,
        note="  looks correct  ",
    )
    assert resolved.status == ApprovalStatus.APPROVED
    assert resolved.resolved_by == bootstrap.user.id
    assert resolved.resolved_at is not None
    assert resolved.resolution_note == "looks correct"
    assert resolved.action_fingerprint == pending.action_fingerprint  # untouched

    fresh = db.execute(
        select(Approval).where(Approval.id == pending.id).execution_options(populate_existing=True)
    ).scalar_one()
    assert (fresh.status, fresh.resolved_by, fresh.resolution_note) == (
        ApprovalStatus.APPROVED,
        bootstrap.user.id,
        "looks correct",
    )


def test_reject_records_provenance_and_blank_note_is_stored_as_null(db, bootstrap, pending):
    resolved = ApprovalService(db).resolve(
        pending.id,
        user=bootstrap.user,
        approve=False,
        action_fingerprint=pending.action_fingerprint,
        note="  ",
    )
    assert resolved.status == ApprovalStatus.REJECTED
    assert resolved.resolved_by == bootstrap.user.id
    assert resolved.resolution_note is None


def test_decision_writes_one_audit_event_in_the_same_transaction(db, bootstrap, pending):
    ApprovalService(db).resolve(
        pending.id, user=bootstrap.user, approve=True, action_fingerprint=pending.action_fingerprint
    )
    events = db.query(AuditEvent).all()
    assert len(events) == 1
    event = events[0]
    assert event.event_type == "approval.approved"
    assert event.actor_user_id == bootstrap.user.id
    assert event.target_ref == pending.id
    assert event.org_id == bootstrap.organization.id
    assert event.detail["decision"] == "approved"
    assert event.detail["action_fingerprint"] == pending.action_fingerprint


def test_a_failing_audit_write_rolls_the_decision_back(db, session_factory, bootstrap, pending, monkeypatch):
    """Decision and audit commit together or not at all -- provenance can
    never be recorded on the row without its audit event."""
    from app.services.audit_service import AuditService

    def boom(self, **kwargs):
        raise RuntimeError("audit store down")

    monkeypatch.setattr(AuditService, "record", boom)
    with pytest.raises(RuntimeError):
        ApprovalService(db).resolve(
            pending.id, user=bootstrap.user, approve=True, action_fingerprint=pending.action_fingerprint
        )
    db.rollback()

    check = session_factory()
    try:
        row = check.get(Approval, pending.id)
        assert row.status == ApprovalStatus.PENDING
        assert row.resolved_by is None and row.resolved_at is None
    finally:
        check.close()


def test_resolve_missing_approval_is_not_found(db, bootstrap):
    with pytest.raises(NotFoundError):
        ApprovalService(db).resolve("nope", user=bootstrap.user, approve=True, action_fingerprint="f")


# --- fingerprint validation ----------------------------------------------


def test_stale_or_wrong_fingerprint_is_rejected_and_changes_nothing(db, bootstrap, pending):
    with pytest.raises(FingerprintMismatchError) as exc:
        ApprovalService(db).resolve(
            pending.id, user=bootstrap.user, approve=True, action_fingerprint="0" * 64
        )
    assert exc.value.status_code == 409
    assert exc.value.code == "fingerprint_mismatch"
    assert exc.value.detail == {"expected_action_fingerprint": pending.action_fingerprint}

    db.rollback()
    fresh = db.get(Approval, pending.id)
    db.refresh(fresh)
    assert fresh.status == ApprovalStatus.PENDING
    assert db.query(AuditEvent).count() == 0


def test_non_ascii_fingerprint_is_a_mismatch_not_a_crash(db, bootstrap, pending):
    with pytest.raises(FingerprintMismatchError):
        ApprovalService(db).resolve(
            pending.id, user=bootstrap.user, approve=True, action_fingerprint="é" * 64
        )


def test_fingerprint_is_verified_on_replay_too(db, bootstrap, pending):
    service = ApprovalService(db)
    service.resolve(
        pending.id, user=bootstrap.user, approve=True, action_fingerprint=pending.action_fingerprint
    )
    with pytest.raises(FingerprintMismatchError):
        service.resolve(pending.id, user=bootstrap.user, approve=True, action_fingerprint="0" * 64)


# --- idempotency / conflict ----------------------------------------------


def test_same_decision_replay_is_idempotent_and_keeps_original_provenance(db, bootstrap, pending):
    service = ApprovalService(db)
    first = service.resolve(
        pending.id,
        user=bootstrap.user,
        approve=True,
        action_fingerprint=pending.action_fingerprint,
        note="first",
    )
    first_resolved_at = first.resolved_at

    again = service.resolve(
        pending.id,
        user=bootstrap.user,
        approve=True,
        action_fingerprint=pending.action_fingerprint,
        note="second attempt, must not overwrite",
    )
    assert again.status == ApprovalStatus.APPROVED
    assert again.resolved_at == first_resolved_at
    assert again.resolution_note == "first"
    assert db.query(AuditEvent).count() == 1  # no second audit event


@pytest.mark.parametrize("first_approve", [True, False])
def test_opposite_decision_after_resolution_is_a_409_and_changes_nothing(
    db, bootstrap, pending, first_approve
):
    service = ApprovalService(db)
    first = service.resolve(
        pending.id,
        user=bootstrap.user,
        approve=first_approve,
        action_fingerprint=pending.action_fingerprint,
        note="original",
    )
    snapshot = (first.status, first.resolved_by, first.resolved_at, first.resolution_note)

    with pytest.raises(InvalidStateTransitionError) as exc:
        service.resolve(
            pending.id,
            user=bootstrap.user,
            approve=not first_approve,
            action_fingerprint=pending.action_fingerprint,
            note="attempt to flip",
        )
    assert exc.value.status_code == 409
    assert exc.value.detail["status"] == first.status.value

    db.rollback()
    fresh = db.execute(
        select(Approval).where(Approval.id == pending.id).execution_options(populate_existing=True)
    ).scalar_one()
    assert (fresh.status, fresh.resolved_by, fresh.resolved_at, fresh.resolution_note) == snapshot
    assert db.query(AuditEvent).count() == 1


def test_a_decision_on_an_expired_approval_is_a_409(db, bootstrap, pending):
    db.execute(text("UPDATE approvals SET status='expired' WHERE id=:id"), {"id": pending.id})
    db.commit()
    for approve in (True, False):
        with pytest.raises(InvalidStateTransitionError):
            ApprovalService(db).resolve(
                pending.id,
                user=bootstrap.user,
                approve=approve,
                action_fingerprint=pending.action_fingerprint,
            )
    db.rollback()
    row = db.execute(
        select(Approval).where(Approval.id == pending.id).execution_options(populate_existing=True)
    ).scalar_one()
    assert row.status == ApprovalStatus.EXPIRED and row.resolved_by is None


# --- authorization inside the service ------------------------------------


@pytest.mark.parametrize(
    "role,allowed",
    [
        (ProjectRole.VIEWER, False),
        (ProjectRole.MEMBER, True),
        (ProjectRole.ADMIN, True),
        (ProjectRole.OWNER, True),
    ],
)
def test_resolve_requires_modify_on_the_owning_project(db, bootstrap, role, allowed):
    project = Project(org_id=bootstrap.organization.id, name=f"proj-{role.value}")
    db.add(project)
    db.flush()
    db.add(ProjectMembership(project_id=project.id, user_id=bootstrap.user.id, role=role))
    db.commit()
    approval = request_workflow_approval(db, make_workflow_node_run(db, project))
    service = ApprovalService(db)

    # READ is available to every role.
    assert service.get_for_user(approval.id, user=bootstrap.user, action=ProjectAction.READ).id == approval.id

    if allowed:
        resolved = service.resolve(
            approval.id, user=bootstrap.user, approve=True, action_fingerprint=approval.action_fingerprint
        )
        assert resolved.status == ApprovalStatus.APPROVED
    else:
        with pytest.raises(ForbiddenError):
            service.resolve(
                approval.id, user=bootstrap.user, approve=True, action_fingerprint=approval.action_fingerprint
            )
        db.rollback()
        row = db.execute(
            select(Approval).where(Approval.id == approval.id).execution_options(populate_existing=True)
        ).scalar_one()
        assert row.status == ApprovalStatus.PENDING


def test_cross_project_user_cannot_resolve_and_nothing_changes(db, bootstrap):
    foreign_project = Project(org_id=bootstrap.organization.id, name="foreign")
    db.add(foreign_project)
    db.commit()
    approval = request_workflow_approval(db, make_workflow_node_run(db, foreign_project))

    with pytest.raises(ForbiddenError):
        ApprovalService(db).resolve(
            approval.id, user=bootstrap.user, approve=True, action_fingerprint=approval.action_fingerprint
        )
    db.rollback()
    row = db.execute(
        select(Approval).where(Approval.id == approval.id).execution_options(populate_existing=True)
    ).scalar_one()
    assert row.status == ApprovalStatus.PENDING
    assert db.query(AuditEvent).count() == 0


def test_unauthorized_caller_does_not_learn_the_expected_fingerprint(db, bootstrap):
    """Authorization is checked before the fingerprint, so a caller without
    MODIFY never sees ``expected_action_fingerprint`` in an error."""
    foreign_project = Project(org_id=bootstrap.organization.id, name="foreign2")
    db.add(foreign_project)
    db.commit()
    approval = request_workflow_approval(db, make_workflow_node_run(db, foreign_project))
    with pytest.raises(ForbiddenError) as exc:
        ApprovalService(db).resolve(
            approval.id, user=bootstrap.user, approve=True, action_fingerprint="wrong"
        )
    assert exc.value.detail is None


# --- concurrency ----------------------------------------------------------


def _run_concurrently(session_factory, user, approval, decisions):
    """One thread + session per decision, released together by a barrier.
    Returns ("ok", status) or ("err", exception-class-name) per decision."""
    barrier = threading.Barrier(len(decisions))

    def attempt(approve):
        session = session_factory()
        try:
            barrier.wait(timeout=10)
            resolved = ApprovalService(session).resolve(
                approval.id, user=user, approve=approve, action_fingerprint=approval.action_fingerprint
            )
            return ("ok", resolved.status)
        except Exception as exc:  # noqa: BLE001 -- the outcome itself is what is asserted
            return ("err", type(exc).__name__)
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=len(decisions)) as pool:
        return list(pool.map(attempt, decisions))


def test_concurrent_same_decision_all_succeed_with_exactly_one_write(session_factory, bootstrap, pending):
    outcomes = _run_concurrently(session_factory, bootstrap.user, pending, [True] * 6)
    assert outcomes == [("ok", ApprovalStatus.APPROVED)] * 6

    check = session_factory()
    try:
        assert check.query(AuditEvent).count() == 1  # only the CAS winner audited
        assert check.get(Approval, pending.id).status == ApprovalStatus.APPROVED
    finally:
        check.close()


def test_concurrent_approve_and_reject_exactly_one_wins(session_factory, bootstrap, pending):
    outcomes = _run_concurrently(session_factory, bootstrap.user, pending, [True, False, True, False])

    check = session_factory()
    try:
        final = check.get(Approval, pending.id)
        winner_status = final.status
        assert winner_status in (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED)
        assert check.query(AuditEvent).count() == 1
        assert final.resolved_by == bootstrap.user.id
    finally:
        check.close()

    # Every caller either got the winning decision back (its own or an
    # idempotent replay of it) or a 409 -- never the losing decision.
    for kind, value in outcomes:
        if kind == "ok":
            assert value == winner_status
        else:
            assert value == "InvalidStateTransitionError"
    assert any(kind == "ok" and value == winner_status for kind, value in outcomes)
    assert any(kind == "err" for kind, _ in outcomes)


def test_concurrent_decision_and_flip_never_change_the_winning_row(session_factory, bootstrap, pending):
    _run_concurrently(session_factory, bootstrap.user, pending, [False, True, False, True, False, True])
    check = session_factory()
    try:
        row = check.get(Approval, pending.id)
        first = (row.status, row.resolved_at, row.resolved_by)
    finally:
        check.close()

    later = session_factory()
    try:
        with pytest.raises(InvalidStateTransitionError):
            ApprovalService(later).resolve(
                pending.id,
                user=bootstrap.user,
                approve=row.status != ApprovalStatus.APPROVED,
                action_fingerprint=pending.action_fingerprint,
            )
        later.rollback()
        again = later.get(Approval, pending.id)
        assert (again.status, again.resolved_at, again.resolved_by) == first
    finally:
        later.close()


def test_sqlite_busy_while_still_pending_is_a_retryable_409_not_a_500(db, bootstrap, pending, monkeypatch):
    def locked(self, *args, **kwargs):
        raise OperationalError("UPDATE approvals", {}, Exception("database is locked"))

    monkeypatch.setattr(ApprovalService, "_compare_and_swap_decision", locked)
    with pytest.raises(ConflictError):
        ApprovalService(db).resolve(
            pending.id, user=bootstrap.user, approve=True, action_fingerprint=pending.action_fingerprint
        )


def test_sqlite_busy_after_another_writer_resolved_becomes_replay_or_conflict(
    db, session_factory, bootstrap, pending, monkeypatch
):
    original = ApprovalService._compare_and_swap_decision

    def lose_to_other_writer(self, approval, **kwargs):
        # A different writer commits the same decision first (through the
        # real, unpatched CAS), then this caller's own write hits SQLITE_BUSY.
        other = session_factory()
        try:
            assert original(ApprovalService(other), approval, **kwargs) is True
        finally:
            other.close()
        raise OperationalError("UPDATE approvals", {}, Exception("database is locked"))

    monkeypatch.setattr(ApprovalService, "_compare_and_swap_decision", lose_to_other_writer)
    replay = ApprovalService(db).resolve(
        pending.id, user=bootstrap.user, approve=True, action_fingerprint=pending.action_fingerprint
    )
    assert replay.status == ApprovalStatus.APPROVED
    with pytest.raises(InvalidStateTransitionError):
        ApprovalService(db).resolve(
            pending.id, user=bootstrap.user, approve=False, action_fingerprint=pending.action_fingerprint
        )


# --- structural guards: no automatic / agent / timeout approval -----------


def _app_python_files():
    return [p for p in APP_DIR.rglob("*.py") if "__pycache__" not in p.parts]


def test_only_approval_service_assigns_a_decision_status():
    """APPROVED/REJECTED are referenced in application code only by the
    service that owns the decision (plus the enum and its API schema) -- no
    worker, execution service or workflow code can approve/reject."""
    allowed = {
        APP_DIR / "services" / "approval_service.py",
        APP_DIR / "db" / "enums.py",
    }
    offenders = []
    for path in _app_python_files():
        if path in allowed:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "ApprovalStatus"
                and node.attr in ("APPROVED", "REJECTED")
            ):
                offenders.append(f"{path.relative_to(APP_DIR)}:{node.lineno}")
    assert offenders == []


def test_worker_and_agent_execution_never_import_the_approval_service():
    for name in ("worker.py", "services/execution_service.py", "services/review_orchestration_service.py"):
        source = (APP_DIR / name).read_text(encoding="utf-8")
        assert "approval_service" not in source, name


def test_resolve_signature_requires_an_authenticated_user_and_a_fingerprint():
    import inspect

    params = inspect.signature(ApprovalService.resolve).parameters
    for required in ("user", "approve", "action_fingerprint"):
        assert params[required].default is inspect.Parameter.empty, required


def test_no_timeout_or_expiry_is_set_or_evaluated_by_the_service(db, bootstrap, pending):
    service = ApprovalService(db)
    assert pending.expires_at is None
    resolved = service.resolve(
        pending.id, user=bootstrap.user, approve=True, action_fingerprint=pending.action_fingerprint
    )
    assert resolved.expires_at is None
    source = (APP_DIR / "services" / "approval_service.py").read_text(encoding="utf-8")
    assert "ApprovalStatus.EXPIRED" not in source
    assert "expires_at" not in source
