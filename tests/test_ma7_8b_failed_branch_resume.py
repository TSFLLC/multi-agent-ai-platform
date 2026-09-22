"""MA7.8B -- failed-branch resume completion.

Reproduces what the staging acceptance of f0ac447f exposed: after the
interrupted reviewers were retried, ``eval_test``'s evaluator returned invalid
JSON (the EvaluationRun FAILED) and fail-fast cancelled the two reviewers that
were still in flight -- leaving a FAILED run nobody could resume: a failed
EVALUATION node had no retry path, and fail-fast-cancelled siblings were never
dispatched again.

The graph mirrors f0ac447f's workflow: Planner -> Engineer -> {code, security,
test} -> each its own evaluation -> ``final`` (fan-in of all six) -> Human
Approval -> TERMINAL. Real engine, queue, Worker, MA6 and MA3; only the
provider is faked. Every database, artifact and log is disposable.
"""

from types import SimpleNamespace

from sqlalchemy import func, select

from app.db.enums import (
    AgentRunStatus,
    ApprovalStatus,
    EvaluationRunStatus,
    JobType,
    ModelCallStatus,
    WorkflowNodeRunStatus,
    WorkflowRunStatus,
)
from app.models.artifacts_eval import Artifact
from app.models.evaluation_runs import EvaluationCriterionResult, EvaluationRun
from app.models.execution import JobQueue, ModelCall
from app.models.governance import Approval
from app.models.observability import ExecutionEvent
from app.models.tasks import AgentRun, AgentRunAttempt, TaskRun
from app.models.workflow import WorkflowNodeRun, WorkflowRun
from app.providers.base import ProviderAuthenticationError
from app.services.workflow_execution_service import WorkflowExecutionService
from tests.ma7_3b_support import AGENT, APPROVAL, TERMINAL, start
from tests.ma7_4b_support import (
    EVALUATION,
    RoleProvider,
    build_graph,
    evaluator_json,
    install_provider,
    make_evaluation_setup,
    new_worker,
    running_worker,
    wait_until,
)
from tests.test_ma7_5a_evaluation_node import CRITERIA, in_threads
from tests.test_ma7_5b_parallel_evaluations import drain
from tests.test_ma7_6b_control_room_api import by_key, detail
from tests.test_ma7_6b_retry_recovery import node_run_row

REVIEWERS = ("code", "security", "test")
EVALS = {"code": "eval_code", "security": "eval_security", "test": "eval_test"}
BAD_JSON = "not json {"


# =============================================================================
# harness
# =============================================================================


def build_shape(db, project, monkeypatch, label):
    setups = {
        node: make_evaluation_setup(db, project, f"{label}-{node}", CRITERIA, role=node) for node in EVALS.values()
    }
    nodes = [
        ("planner", AGENT),
        ("engineer", AGENT),
        ("code", AGENT),
        ("security", AGENT),
        ("test", AGENT),
        ("eval_code", EVALUATION),
        ("eval_security", EVALUATION),
        ("eval_test", EVALUATION),
        ("final", AGENT),
        ("gate", APPROVAL),
        ("result", TERMINAL),
    ]
    edges = [("planner", "engineer")]
    for reviewer in REVIEWERS:
        edges += [("engineer", reviewer), (reviewer, EVALS[reviewer]), (reviewer, "final"), (EVALS[reviewer], "final")]
    edges += [("final", "gate"), ("gate", "result")]
    graph = build_graph(db, project, nodes, edges, label=label, real=True, evaluations=setups)
    role_of = dict(graph.role_of)
    for setup in setups.values():
        role_of.update(setup.role_of)

    bad_once = {}  # role -> remaining invalid responses

    def text_for(role):
        if bad_once.get(role):
            bad_once[role] -= 1
            return BAD_JSON
        if role in setups:
            return evaluator_json(CRITERIA)
        return f"OUTPUT-OF-{role}"

    provider = install_provider(monkeypatch, RoleProvider(role_of, text=text_for))
    return SimpleNamespace(built=graph.built, provider=provider, bad_once=bad_once, run=None)


def go(db, harness):
    harness.run = start(db, harness.built)


def row(session_factory, harness, key, iteration=None):
    return node_run_row(session_factory, harness.run.id, key, iteration=iteration)


def run_status(session_factory, harness):
    session = session_factory()
    try:
        return session.get(WorkflowRun, harness.run.id).status
    finally:
        session.close()


def fetch(session_factory, model, **filters):
    session = session_factory()
    try:
        return session.query(model).filter_by(**filters).all()
    finally:
        session.close()


def count(session_factory, model, *conditions):
    session = session_factory()
    try:
        return session.execute(select(func.count()).select_from(model).where(*conditions)).scalar_one()
    finally:
        session.close()


def events(session_factory, harness, event_type):
    return count(
        session_factory,
        ExecutionEvent,
        ExecutionEvent.event_type == event_type,
        ExecutionEvent.task_run_id == harness.run.task_run_id,
    )


def iterations(session_factory, harness, key):
    session = session_factory()
    try:
        run = session.get(WorkflowRun, harness.run.id)
        from app.models.workflow import WorkflowNode

        node = session.query(WorkflowNode).filter_by(workflow_version_id=run.workflow_version_id, node_key=key).one()
        return [
            (nr.iteration, nr.status)
            for nr in session.query(WorkflowNodeRun)
            .filter_by(workflow_run_id=run.id, workflow_node_id=node.id)
            .order_by(WorkflowNodeRun.iteration)
        ]
    finally:
        session.close()


def retry(client, auth_headers, harness, key, session_factory, body=None):
    return client.post(
        f"/workflow-runs/{harness.run.id}/nodes/{row(session_factory, harness, key).id}/retry",
        headers=auth_headers,
        json=body or {},
    )


def evaluation_of(session_factory, harness, key, iteration):
    agent_run_id = row(session_factory, harness, key, iteration=iteration).agent_run_id
    runs = fetch(session_factory, EvaluationRun, evaluator_agent_run_id=agent_run_id)
    return runs[0] if runs else None


def f0ac447f_shape(db, session_factory, harness):
    """eval_test's evaluator returns invalid JSON while code and security are
    still in their provider calls; fail-fast then cancels both AFTER their
    calls returned -- exactly the staging run's end state."""
    harness.bad_once["eval_test"] = 1
    go(db, harness)

    def hold_until_eval_test_failed(_request):
        assert wait_until(
            lambda: (row(session_factory, harness, "eval_test") or SimpleNamespace(status=None)).status
            == WorkflowNodeRunStatus.FAILED,
            timeout=20,
        ), "eval_test never failed"

    harness.provider.hooks["code"] = hold_until_eval_test_failed
    harness.provider.hooks["security"] = hold_until_eval_test_failed
    with running_worker(session_factory, concurrency=3):
        assert wait_until(lambda: run_status(session_factory, harness) == WorkflowRunStatus.FAILED, timeout=30)
    drain(new_worker(session_factory))
    harness.provider.hooks.clear()

    for reviewer in ("code", "security"):
        cancelled = row(session_factory, harness, reviewer)
        assert cancelled.status == WorkflowNodeRunStatus.CANCELLED
        agent_run = fetch(session_factory, AgentRun, id=cancelled.agent_run_id)[0]
        assert agent_run.status == AgentRunStatus.STOPPED and agent_run.cancellation_requested_by is None
        assert harness.provider.count(reviewer) == 1  # its call happened; the output was kept, unused
    assert row(session_factory, harness, "test").status == WorkflowNodeRunStatus.COMPLETED
    assert row(session_factory, harness, "eval_test").status == WorkflowNodeRunStatus.FAILED
    for key in ("eval_code", "eval_security", "final", "gate"):
        assert row(session_factory, harness, key).status == WorkflowNodeRunStatus.PENDING


def assert_reaches_gate_once(session_factory, harness):
    assert run_status(session_factory, harness) == WorkflowRunStatus.NODE_WAITING_FOR_APPROVAL
    gate = row(session_factory, harness, "gate")
    assert gate.status == WorkflowNodeRunStatus.WAITING_FOR_APPROVAL
    assert [a.status for a in fetch(session_factory, Approval, scope_ref_id=gate.id)] == [ApprovalStatus.PENDING]
    assert harness.provider.count("final") == 1
    assert harness.provider.count("planner") == 1 and harness.provider.count("engineer") == 1


# =============================================================================
# 1, 3, 5, 7, 10, 11, 12: the f0ac447f end state, recovered by one explicit retry
# =============================================================================


def test_failed_evaluation_retry_revives_fail_fast_cancelled_siblings_and_reaches_the_gate(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch
):
    harness = build_shape(db, bootstrap.project, monkeypatch, "shape")
    f0ac447f_shape(db, session_factory, harness)
    old_eval = evaluation_of(session_factory, harness, "eval_test", 0)
    assert old_eval.status == EvaluationRunStatus.FAILED

    # what the Control Room offers: only eval_test, retried as configured
    nodes = by_key(detail(client, auth_headers, harness.run.id))
    assert nodes["eval_test"]["retry_mode"] == "as_configured"
    assert nodes["code"]["retry_mode"] is None and nodes["security"]["retry_mode"] is None

    response = retry(client, auth_headers, harness, "eval_test", session_factory)
    assert response.status_code == 200, response.text
    assert run_status(session_factory, harness) == WorkflowRunStatus.RUNNING
    assert events(session_factory, harness, "workflow.node.revived") == 2
    for reviewer in ("code", "security"):
        assert [it for it, _ in iterations(session_factory, harness, reviewer)] == [0, 1]

    drain(new_worker(session_factory))

    # reviewers re-run only because their cancellation was lifted; test never re-runs
    assert harness.provider.count("code") == 2 and harness.provider.count("security") == 2
    assert harness.provider.count("test") == 1
    assert harness.provider.count("eval_test") == 2
    assert harness.provider.count("eval_code") == 1 and harness.provider.count("eval_security") == 1
    for reviewer in ("code", "security"):
        assert iterations(session_factory, harness, reviewer) == [
            (0, WorkflowNodeRunStatus.CANCELLED),
            (1, WorkflowNodeRunStatus.COMPLETED),
        ]
    assert iterations(session_factory, harness, "eval_test") == [
        (0, WorkflowNodeRunStatus.FAILED),
        (1, WorkflowNodeRunStatus.COMPLETED),
    ]
    # the failed EvaluationRun is untouched history; findings exist only on the new one
    old_after = evaluation_of(session_factory, harness, "eval_test", 0)
    assert old_after.id == old_eval.id and old_after.status == EvaluationRunStatus.FAILED
    assert old_after.failure_reason == old_eval.failure_reason
    assert count(session_factory, EvaluationCriterionResult, EvaluationCriterionResult.evaluation_run_id == old_eval.id) == 0
    new_eval = evaluation_of(session_factory, harness, "eval_test", 1)
    assert new_eval.status == EvaluationRunStatus.COMPLETED
    assert new_eval.evaluation_definition_version_id == old_eval.evaluation_definition_version_id
    assert count(session_factory, EvaluationCriterionResult, EvaluationCriterionResult.evaluation_run_id == new_eval.id) == len(CRITERIA)

    assert_reaches_gate_once(session_factory, harness)


def test_failed_evaluation_with_cancelled_sibling_evaluations_recovers_all_of_them(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch
):
    """7: sequential worker -- eval_code fails first; its two sibling
    evaluations, still queued, are stopped by fail-fast before any provider
    call. One retry of eval_code revives both and the run completes its fan-in."""
    harness = build_shape(db, bootstrap.project, monkeypatch, "seq")
    harness.bad_once["eval_code"] = 1
    go(db, harness)
    drain(new_worker(session_factory))
    assert run_status(session_factory, harness) == WorkflowRunStatus.FAILED
    for key in ("eval_security", "eval_test"):
        assert row(session_factory, harness, key).status == WorkflowNodeRunStatus.CANCELLED
        assert harness.provider.count(key) == 0  # stopped before its call

    assert retry(client, auth_headers, harness, "eval_code", session_factory).status_code == 200
    drain(new_worker(session_factory))

    for key in EVALS.values():
        assert row(session_factory, harness, key).status == WorkflowNodeRunStatus.COMPLETED
    for reviewer in REVIEWERS:
        assert harness.provider.count(reviewer) == 1  # completed nodes never re-run
    assert_reaches_gate_once(session_factory, harness)


# =============================================================================
# 2: non-eligible evaluation failures stay non-retryable
# =============================================================================


def test_an_integrity_failure_of_an_evaluation_is_not_retryable(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch
):
    harness = build_shape(db, bootstrap.project, monkeypatch, "integrity")
    go(db, harness)

    def tamper_with_the_subject(_request):
        session = session_factory()
        try:
            subject = session.get(Artifact, row(session_factory, harness, "code").output_snapshot_ref)
            with open(subject.storage_ref, "a", encoding="utf-8") as handle:
                handle.write("\ntampered")
        finally:
            session.close()

    harness.provider.hooks["eval_code"] = tamper_with_the_subject
    drain(new_worker(session_factory))
    assert row(session_factory, harness, "eval_code").status == WorkflowNodeRunStatus.FAILED
    assert by_key(detail(client, auth_headers, harness.run.id))["eval_code"]["retry_mode"] is None

    response = retry(client, auth_headers, harness, "eval_code", session_factory)
    assert response.status_code == 400 and response.json()["error"]["code"] == "retry_not_allowed"
    assert "integrity" in response.json()["error"]["message"]
    assert run_status(session_factory, harness) == WorkflowRunStatus.FAILED


def test_a_non_recoverable_evaluator_failure_is_not_retryable(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch
):
    harness = build_shape(db, bootstrap.project, monkeypatch, "auth")

    def rejected(_request):
        raise ProviderAuthenticationError("bad key")

    harness.provider.hooks["eval_code"] = rejected
    go(db, harness)
    drain(new_worker(session_factory))
    assert row(session_factory, harness, "eval_code").status == WorkflowNodeRunStatus.FAILED

    response = retry(client, auth_headers, harness, "eval_code", session_factory)
    assert response.status_code == 400 and response.json()["error"]["code"] == "retry_not_allowed"
    replacement = retry(
        client, auth_headers, harness, "eval_code", session_factory, body={"replacement_provider_model_id": "x"}
    )
    assert replacement.status_code == 400
    assert run_status(session_factory, harness) == WorkflowRunStatus.FAILED


# =============================================================================
# 4: a person's cancellation is never revived (and never leaves a stalled run)
# =============================================================================


def test_a_workflow_a_person_also_cancelled_is_not_resumed(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch
):
    harness = build_shape(db, bootstrap.project, monkeypatch, "usercancel")
    harness.bad_once["eval_code"] = 1
    go(db, harness)
    worker = new_worker(session_factory)
    for _ in range(6):  # planner, engineer, 3 reviewers, eval_code (fails; run still draining)
        assert worker.run_once()
    assert row(session_factory, harness, "eval_code").status == WorkflowNodeRunStatus.FAILED
    assert client.post(f"/workflow-runs/{harness.run.id}/cancel", headers=auth_headers).status_code in (200, 202)
    drain(worker)
    assert run_status(session_factory, harness) == WorkflowRunStatus.FAILED  # failure outranks cancellation

    response = retry(client, auth_headers, harness, "eval_code", session_factory)
    assert response.status_code == 400 and "cannot be resumed" in response.json()["error"]["message"]
    assert by_key(detail(client, auth_headers, harness.run.id))["eval_code"]["retry_mode"] is None
    for key in ("eval_security", "eval_test"):
        assert [it for it, _ in iterations(session_factory, harness, key)] == [0]
    assert events(session_factory, harness, "workflow.node.revived") == 0


def test_a_sibling_whose_task_run_a_person_cancelled_is_not_revived(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch
):
    harness = build_shape(db, bootstrap.project, monkeypatch, "taskcancel")
    harness.bad_once["eval_code"] = 1
    go(db, harness)
    worker = new_worker(session_factory)
    for _ in range(5):  # planner, engineer, 3 reviewers; the three evaluations are now queued
        assert worker.run_once()
    security_eval = row(session_factory, harness, "eval_security")
    task_run = fetch(session_factory, AgentRun, id=security_eval.agent_run_id)[0].task_run_id
    task_id = fetch(session_factory, TaskRun, id=task_run)[0].task_id
    cancelled = client.post(f"/tasks/{task_id}/runs/{task_run}/cancel", headers=auth_headers)
    assert cancelled.status_code == 200, cancelled.text
    drain(worker)
    assert run_status(session_factory, harness) == WorkflowRunStatus.FAILED
    assert row(session_factory, harness, "eval_security").status == WorkflowNodeRunStatus.CANCELLED

    response = retry(client, auth_headers, harness, "eval_code", session_factory)
    assert response.status_code == 400 and "cannot be resumed" in response.json()["error"]["message"]
    assert [it for it, _ in iterations(session_factory, harness, "eval_security")] == [0]
    assert [it for it, _ in iterations(session_factory, harness, "eval_code")] == [0]


# =============================================================================
# 6: a cancelled attempt whose provider call has no recorded outcome is never revived
# =============================================================================


def test_a_cancelled_sibling_with_an_unresolved_provider_call_blocks_resume(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch
):
    harness = build_shape(db, bootstrap.project, monkeypatch, "ambiguous")
    f0ac447f_shape(db, session_factory, harness)
    # disposable-DB fixture: the durable shape of a call whose outcome was never recorded
    session = session_factory()
    try:
        call = session.query(ModelCall).filter_by(agent_run_id=row(session_factory, harness, "code").agent_run_id).one()
        call.status = ModelCallStatus.RUNNING
        session.commit()
    finally:
        session.close()

    response = retry(client, auth_headers, harness, "eval_test", session_factory)
    assert response.status_code == 400 and "no recorded outcome" in response.json()["error"]["message"]
    drain(new_worker(session_factory))
    assert harness.provider.count("code") == 1  # nothing replayed
    assert run_status(session_factory, harness) == WorkflowRunStatus.FAILED


# =============================================================================
# 8, 9: idempotent and concurrency-safe
# =============================================================================


def test_repeated_recovery_requests_change_nothing(client, db, session_factory, auth_headers, bootstrap, monkeypatch):
    harness = build_shape(db, bootstrap.project, monkeypatch, "repeat")
    f0ac447f_shape(db, session_factory, harness)
    original = row(session_factory, harness, "eval_test", iteration=0)
    assert retry(client, auth_headers, harness, "eval_test", session_factory).status_code == 200

    jobs_before = count(session_factory, JobQueue, JobQueue.job_type == JobType.AGENT_RUN)
    again = client.post(
        f"/workflow-runs/{harness.run.id}/nodes/{original.id}/retry", headers=auth_headers, json={}
    )
    assert again.status_code == 400  # superseded: nothing new
    assert retry(client, auth_headers, harness, "eval_test", session_factory).status_code == 400  # running, not failed
    session = session_factory()
    try:
        assert WorkflowExecutionService(session)._resume_failed_run(harness.run.id) == (False, [])
    finally:
        session.close()
    assert count(session_factory, JobQueue, JobQueue.job_type == JobType.AGENT_RUN) == jobs_before
    for key in ("code", "security", "eval_test"):
        assert [it for it, _ in iterations(session_factory, harness, key)] == [0, 1]
    assert events(session_factory, harness, "workflow.node.revived") == 2


def test_concurrent_recovery_requests_resume_exactly_once(db, session_factory, bootstrap, monkeypatch):
    harness = build_shape(db, bootstrap.project, monkeypatch, "race")
    f0ac447f_shape(db, session_factory, harness)
    failed_id = row(session_factory, harness, "eval_test").id

    def attempt(_i):
        session = session_factory()
        try:
            return WorkflowExecutionService(session).retry_failed_agent_node(failed_id, None).id
        except Exception as exc:  # the losers: Conflict / RetryNotAllowed
            return type(exc).__name__
        finally:
            session.close()

    results = in_threads(6, attempt)
    assert sum(1 for r in results if r not in ("ConflictError", "RetryNotAllowedError")) == 1, results

    for key in ("code", "security", "eval_test"):
        assert [it for it, _ in iterations(session_factory, harness, key)] == [0, 1]
    assert events(session_factory, harness, "workflow.node.revived") == 2
    assert run_status(session_factory, harness) == WorkflowRunStatus.RUNNING

    drain(new_worker(session_factory))
    payloads = [j.payload_ref for j in fetch(session_factory, JobQueue)]
    assert len(payloads) == len(set(payloads))
    assert_reaches_gate_once(session_factory, harness)


# =============================================================================
# 12 (Human Approval): a gate the failure closed is never revived
# =============================================================================


def test_a_human_approval_gate_closed_by_the_failure_is_never_revived(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch
):
    """The MA7.5B shape (gate directly after the reviewers and evaluations):
    a gate that was WAITING when a branch failed is closed by fail-fast
    (Approval EXPIRED, reason workflow_failed; node CANCELLED). Resuming would
    need that gate back, and a gate is never revived -- so it blocks resume,
    and its Approval is never re-requested or decided."""
    from tests.test_ma7_5b_parallel_evaluations import build_parallel, go as go_parallel

    harness = build_parallel(db, bootstrap.project, monkeypatch, "gateclosed", downstream="post")
    go_parallel(db, harness)
    drain(new_worker(session_factory))
    gate = node_run_row(session_factory, harness.run.id, "gate")
    assert gate.status == WorkflowNodeRunStatus.WAITING_FOR_APPROVAL

    # disposable-DB fixture: a sibling branch fails while the gate waits (fail-fast closes the gate)
    session = session_factory()
    try:
        engine = WorkflowExecutionService(session)
        code = node_run_row(session_factory, harness.run.id, "code")
        session.query(WorkflowNodeRun).filter_by(id=code.id).update({"status": WorkflowNodeRunStatus.FAILED})
        session.commit()
        engine._advance_run(harness.run.id)
    finally:
        session.close()
    assert node_run_row(session_factory, harness.run.id, "gate").status == WorkflowNodeRunStatus.CANCELLED
    assert run_status(session_factory, harness) == WorkflowRunStatus.FAILED

    session = session_factory()
    try:
        plan = WorkflowExecutionService(session)._fail_fast_cancellation_plan(session.get(WorkflowRun, harness.run.id))
        assert [key for key, _why in plan.blocking] == ["gate"] and plan.revivable == []
    finally:
        session.close()
    approvals = fetch(session_factory, Approval, scope_ref_id=gate.id)
    assert [a.status for a in approvals] == [ApprovalStatus.EXPIRED]  # never re-requested, never approved


# =============================================================================
# 13: MA8 exclusion unchanged (the MA7.8 suite proves it end to end)
# =============================================================================


def test_recovery_attempts_are_ordinary_model_evidence(db, session_factory, bootstrap, monkeypatch):
    """The revived and retried calls are normal SUCCESS calls; no
    worker_interrupted or failure is fabricated for the cancelled ones."""
    harness = build_shape(db, bootstrap.project, monkeypatch, "evidence")
    f0ac447f_shape(db, session_factory, harness)
    session = session_factory()
    try:
        WorkflowExecutionService(session).retry_failed_agent_node(row(session_factory, harness, "eval_test").id, None)
    finally:
        session.close()
    drain(new_worker(session_factory))
    calls = fetch(session_factory, ModelCall)
    assert {c.status for c in calls} == {ModelCallStatus.SUCCESS}
    assert all((c.error or {}).get("category") is None for c in calls)
    stopped = [
        a for a in fetch(session_factory, AgentRunAttempt) if (a.error or {}).get("category") == "cancelled"
    ]
    assert len(stopped) == 2  # the two fail-fast cancellations, kept as history
