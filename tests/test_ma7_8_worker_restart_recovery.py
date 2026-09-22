"""MA7.8 -- worker-restart recovery.

Reproduces the staging incident (workflow run f0ac447f): a container restart
killed three parallel reviewer Agent Runs mid provider call. Their jobs were
reclaimed after the lease expired, ``execute`` re-inserted attempt #1, hit the
UNIQUE (agent_run_id, attempt_number) constraint, the jobs were marked FAILED
and the Agent Runs stayed RUNNING forever -- which reconciliation never healed.

A process death is simulated with ``SimulatedWorkerDeath``, a BaseException
raised inside the fake provider (or just before it): no handler in the worker
or the execution service catches it, so durable state is left exactly as a
killed container leaves it (attempt RUNNING, ModelCall RUNNING, job LEASED).

Every database, artifact and log is a disposable/scratch location.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.db.enums import (
    AgentRunAttemptStatus,
    AgentRunStatus,
    ApprovalStatus,
    EvaluationRunStatus,
    JobQueueStatus,
    JobType,
    ModelCallStatus,
    TaskRunStatus,
    WorkflowNodeRunStatus,
    WorkflowRunStatus,
)
from app.models.artifacts_eval import Artifact
from app.models.evaluation_runs import EvaluationRun
from app.models.execution import JobQueue, ModelCall
from app.models.observability import ExecutionEvent
from app.models.tasks import AgentRun, AgentRunAttempt, TaskRun
from app.models.workflow import WorkflowRun
from app.providers.base import InvokeResponse, ProviderConnectionError
from app.routing_evidence import DEFAULT_V1_CONFIG, EvidenceConfig, RoutingContext, load_evidence
from app.services.execution_service import WORKER_INTERRUPTED, AgentExecutionService
from app.services.model_intelligence_service import ModelIntelligenceService
from app.services.workflow_execution_service import WorkflowExecutionService
from tests.conftest import make_model, make_provider, make_provider_model
from tests.ma7_4b_support import new_worker
from tests.test_execution_service import FakeAdapter, _build_scenario, _factory_for, _manual
from tests.test_ma7_5a_evaluation_node import in_threads
from tests.test_ma7_5b_parallel_evaluations import EVALS, REVIEWERS, build_parallel, drain, go
from tests.test_ma7_6b_control_room_api import by_key, detail
from tests.test_ma7_6b_retry_recovery import node_run_row


class SimulatedWorkerDeath(BaseException):
    """Stands in for the container being killed (SIGKILL): not an Exception,
    so nothing between the provider and the test catches it."""


def die(_request=None):
    raise SimulatedWorkerDeath()


# =============================================================================
# helpers
# =============================================================================


def free_pm(db, label="free/model"):
    pm = make_provider_model(
        db,
        model=make_model(db, canonical_model_id=label),
        provider=make_provider(db),
        cost_input_per_mtok=Decimal(0),
        cost_output_per_mtok=Decimal(0),
    )
    db.commit()
    return pm


def ok(text="RECOVERED"):
    return InvokeResponse(text=text, tokens_in=7, tokens_out=3, latency_ms=5)


def crash_during_provider_call(session_factory, agent_run_id):
    """A worker dies while the provider call is in flight."""
    session = session_factory()
    try:
        adapter = FakeAdapter(on_invoke=die)
        with pytest.raises(SimulatedWorkerDeath):
            AgentExecutionService(session, adapter_factory=_factory_for(adapter)).execute(
                agent_run_id, worker_id="dead-worker"
            )
        assert len(adapter.calls) == 1
    finally:
        session.close()


def crash_before_provider_call(session_factory, agent_run_id):
    """A worker dies after its attempt started, before any ModelCall exists."""
    session = session_factory()
    try:
        adapter = FakeAdapter()
        service = AgentExecutionService(session, adapter_factory=_factory_for(adapter))
        service._route_model = lambda ctx: die()
        with pytest.raises(SimulatedWorkerDeath):
            service.execute(agent_run_id, worker_id="dead-worker")
        assert adapter.calls == []
    finally:
        session.close()


def recover(session_factory, agent_run_id, adapter):
    session = session_factory()
    try:
        AgentExecutionService(session, adapter_factory=_factory_for(adapter)).execute(
            agent_run_id, worker_id="new-worker"
        )
    finally:
        session.close()


def attempts_of(db, agent_run_id):
    db.expire_all()
    return (
        db.query(AgentRunAttempt)
        .filter_by(agent_run_id=agent_run_id)
        .order_by(AgentRunAttempt.attempt_number)
        .all()
    )


def events_of(db, event_type, *, agent_run_id=None, task_run_id=None):
    stmt = select(func.count()).select_from(ExecutionEvent).where(ExecutionEvent.event_type == event_type)
    if agent_run_id:
        stmt = stmt.where(ExecutionEvent.agent_run_id == agent_run_id)
    if task_run_id:
        stmt = stmt.where(ExecutionEvent.task_run_id == task_run_id)
    return db.execute(stmt).scalar_one()


def category(error):
    return (error or {}).get("category")


def assert_interrupted(db, agent_run_id):
    """The whole durable chain of one interrupted Agent Run."""
    db.expire_all()
    agent_run = db.get(AgentRun, agent_run_id)
    assert agent_run.status == AgentRunStatus.FAILED
    assert db.get(TaskRun, agent_run.task_run_id).status == TaskRunStatus.FAILED
    attempts = attempts_of(db, agent_run_id)
    assert [a.status for a in attempts] == [AgentRunAttemptStatus.FAILED] * len(attempts)
    assert category(attempts[-1].error) == WORKER_INTERRUPTED
    # the interrupted attempt's own call(s); earlier attempts keep their real history
    for call in db.query(ModelCall).filter_by(agent_run_attempt_id=attempts[-1].id).all():
        assert call.status in (ModelCallStatus.ERROR, ModelCallStatus.SUCCESS)
        if call.status == ModelCallStatus.ERROR:
            assert category(call.error) == WORKER_INTERRUPTED
            assert call.cost_amount is None  # unknown -- never fabricated
    assert events_of(db, "agent_run.interrupted", agent_run_id=agent_run_id) == 1


# -- workflow-level helpers --------------------------------------------------------------------


def jobs_for(session_factory, agent_run_ids):
    session = session_factory()
    try:
        return {
            j.payload_ref: j
            for j in session.query(JobQueue)
            .filter(JobQueue.job_type == JobType.AGENT_RUN, JobQueue.payload_ref.in_(agent_run_ids))
            .all()
        }
    finally:
        session.close()


def set_jobs(session_factory, agent_run_ids, **values):
    session = session_factory()
    try:
        for job in session.query(JobQueue).filter(JobQueue.payload_ref.in_(agent_run_ids)).all():
            for key, value in values.items():
                setattr(job, key, value)
        session.commit()
    finally:
        session.close()


def expire_leases(session_factory, agent_run_ids):
    set_jobs(session_factory, agent_run_ids, lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=5))


def staging_orphan_shape(session_factory, agent_run_ids):
    """Exactly what the pre-MA7.8 reclaim left on staging: job FAILED after a
    second claim (fencing 2, 2 tries) while the Agent Run stays RUNNING."""
    set_jobs(session_factory, agent_run_ids, status=JobQueueStatus.FAILED, fencing_token=2, attempt_count=2)


def reviewer_agent_runs(session_factory, harness):
    return {r: node_run_row(session_factory, harness.run.id, r).agent_run_id for r in REVIEWERS}


def restart_during_fan_out(db, session_factory, harness):
    """Planner and Engineer complete; then the worker dies inside each of the
    three reviewers' provider calls (the f0ac447f moment)."""
    go(db, harness)
    worker = new_worker(session_factory)
    assert worker.run_once() and worker.run_once()  # planner, engineer
    for reviewer in REVIEWERS:
        harness.provider.hooks[reviewer] = die
    for _ in REVIEWERS:
        with pytest.raises(SimulatedWorkerDeath):
            worker.run_once()
    harness.provider.hooks.clear()
    ids = reviewer_agent_runs(session_factory, harness)
    session = session_factory()
    try:
        for agent_run_id in ids.values():
            assert session.get(AgentRun, agent_run_id).status == AgentRunStatus.RUNNING
            call = session.query(ModelCall).filter_by(agent_run_id=agent_run_id).one()
            assert call.status == ModelCallStatus.RUNNING
    finally:
        session.close()
    assert {j.status for j in jobs_for(session_factory, ids.values()).values()} == {JobQueueStatus.LEASED}
    return ids


def run_status(session_factory, harness):
    session = session_factory()
    try:
        return session.get(WorkflowRun, harness.run.id).status
    finally:
        session.close()


def reconcile(session_factory):
    session = session_factory()
    try:
        return WorkflowExecutionService(session).reconcile_active_runs()
    finally:
        session.close()


def retry(client, auth_headers, harness, node_key, session_factory, body=None):
    node_run = node_run_row(session_factory, harness.run.id, node_key)
    return client.post(
        f"/workflow-runs/{harness.run.id}/nodes/{node_run.id}/retry", headers=auth_headers, json=body or {}
    )


# =============================================================================
# 1-3. the recovery contract, at the execution-service boundary
# =============================================================================


def test_reclaimed_in_flight_provider_call_is_never_replayed(db, session_factory):
    """CASE 4: attempt RUNNING + ModelCall RUNNING. The reclaiming worker
    must NOT call the provider; the run converges FAILED worker_interrupted."""
    scenario = _build_scenario(db, model_policy=_manual(free_pm(db)))
    crash_during_provider_call(session_factory, scenario.agent_run.id)

    adapter = FakeAdapter(responses=[ok()])
    recover(session_factory, scenario.agent_run.id, adapter)

    assert adapter.calls == []  # the ambiguous call is not repeated
    assert_interrupted(db, scenario.agent_run.id)
    assert [a.attempt_number for a in attempts_of(db, scenario.agent_run.id)] == [1]
    assert db.query(Artifact).filter_by(agent_run_id=scenario.agent_run.id).count() == 0


def test_reclaimed_successful_but_unfinalized_call_is_not_replayed(db, session_factory):
    """CASE 4, other half: the provider answered and the ModelCall is
    SUCCESS, but the worker died before the run was finalized. The real call
    stays recorded as SUCCESS; nothing is re-invoked."""
    scenario = _build_scenario(db, model_policy=_manual(free_pm(db)))
    session = session_factory()
    try:
        service = AgentExecutionService(session, adapter_factory=_factory_for(FakeAdapter(responses=[ok()])))
        service._create_artifact = lambda ctx, text: die()
        with pytest.raises(SimulatedWorkerDeath):
            service.execute(scenario.agent_run.id, worker_id="dead-worker")
    finally:
        session.close()

    adapter = FakeAdapter(responses=[ok()])
    recover(session_factory, scenario.agent_run.id, adapter)

    assert adapter.calls == []
    assert_interrupted(db, scenario.agent_run.id)
    call = db.query(ModelCall).filter_by(agent_run_id=scenario.agent_run.id).one()
    assert call.status == ModelCallStatus.SUCCESS and call.tokens_in == 7  # real evidence kept


def test_interruption_before_any_provider_call_continues_with_the_next_attempt(db, session_factory):
    """CASE 3: attempt #1 RUNNING, no ModelCall. #1 is kept as FAILED
    worker_interrupted and attempt #2 (never #1 again) completes."""
    scenario = _build_scenario(db, model_policy=_manual(free_pm(db)))
    crash_before_provider_call(session_factory, scenario.agent_run.id)

    adapter = FakeAdapter(responses=[ok()])
    recover(session_factory, scenario.agent_run.id, adapter)

    assert len(adapter.calls) == 1
    attempts = attempts_of(db, scenario.agent_run.id)
    assert [(a.attempt_number, a.status) for a in attempts] == [
        (1, AgentRunAttemptStatus.FAILED),
        (2, AgentRunAttemptStatus.COMPLETED),
    ]
    assert category(attempts[0].error) == WORKER_INTERRUPTED
    assert db.get(AgentRun, scenario.agent_run.id).status == AgentRunStatus.COMPLETED
    assert events_of(db, "agent_run_attempt.interrupted", agent_run_id=scenario.agent_run.id) == 1


def test_interruption_before_provider_call_respects_the_attempt_limit(db, session_factory):
    """The interrupted attempt was already the last one allowed: no new
    attempt, no provider call -- FAILED worker_interrupted."""
    scenario = _build_scenario(db, model_policy=_manual(free_pm(db)))
    session = session_factory()
    try:
        service = AgentExecutionService(
            session, adapter_factory=_factory_for(FakeAdapter(responses=[ProviderConnectionError("down")]))
        )
        real_route = service._route_model
        routed = {"n": 0}

        def route_then_die_on_second_attempt(ctx):
            routed["n"] += 1
            if routed["n"] == 2:
                die()
            return real_route(ctx)

        service._route_model = route_then_die_on_second_attempt
        with pytest.raises(SimulatedWorkerDeath):
            service.execute(scenario.agent_run.id, worker_id="dead-worker")
    finally:
        session.close()

    adapter = FakeAdapter(responses=[ok()])
    recover(session_factory, scenario.agent_run.id, adapter)

    assert adapter.calls == []
    assert_interrupted(db, scenario.agent_run.id)
    attempts = attempts_of(db, scenario.agent_run.id)
    assert [a.attempt_number for a in attempts] == [1, 2]
    assert category(attempts[0].error) == "provider_connection_error"  # history untouched


def test_reclaimed_already_completed_agent_run_is_propagated_not_rerun(db, session_factory, bootstrap, monkeypatch):
    """CASE 1, through the real Worker: the Agent Run COMPLETED but the
    worker died before propagating it and completing its job. The reclaim
    calls no provider, the node completes, downstream proceeds, job DONE."""
    harness = build_parallel(db, bootstrap.project, monkeypatch, "done")
    go(db, harness)
    worker = new_worker(session_factory)
    real = worker._reconcile_workflow_node
    state = {"died": False}

    def die_once(session, agent_run_id):
        if not state["died"]:
            state["died"] = True
            die()
        return real(session, agent_run_id)

    worker._reconcile_workflow_node = die_once
    with pytest.raises(SimulatedWorkerDeath):
        worker.run_once()  # planner completes, then the worker dies

    planner = node_run_row(session_factory, harness.run.id, "planner")
    assert planner.status == WorkflowNodeRunStatus.RUNNING
    expire_leases(session_factory, [planner.agent_run_id])

    worker2 = new_worker(session_factory)
    assert worker2.run_once()  # the reclaim

    assert harness.provider.count("planner") == 1
    assert node_run_row(session_factory, harness.run.id, "planner").status == WorkflowNodeRunStatus.COMPLETED
    assert jobs_for(session_factory, [planner.agent_run_id])[planner.agent_run_id].status == JobQueueStatus.DONE
    assert node_run_row(session_factory, harness.run.id, "engineer").status == WorkflowNodeRunStatus.RUNNING


# =============================================================================
# 4-9. the staging incident, end to end through engine + queue + Worker
# =============================================================================


def test_reclaim_after_restart_converges_instead_of_crashing(db, session_factory, bootstrap, monkeypatch):
    """The first half of f0ac447f with MA7.8: the expired leases are
    reclaimed by a new worker; no provider call, no IntegrityError, jobs DONE,
    the three nodes FAILED worker_interrupted and the run FAILED."""
    harness = build_parallel(db, bootstrap.project, monkeypatch, "reclaim")
    ids = restart_during_fan_out(db, session_factory, harness)
    expire_leases(session_factory, ids.values())

    drain(new_worker(session_factory))

    assert all(harness.provider.count(r) == 1 for r in REVIEWERS)
    for agent_run_id in ids.values():
        assert_interrupted(db, agent_run_id)
    assert {j.status for j in jobs_for(session_factory, ids.values()).values()} == {JobQueueStatus.DONE}
    assert run_status(session_factory, harness) == WorkflowRunStatus.FAILED


def test_exact_staging_orphan_shape_converges_on_reconciliation(db, session_factory, bootstrap, monkeypatch):
    """4 + 5: job FAILED + AgentRun RUNNING (the persisted f0ac447f state).
    Reconciliation finalizes each exactly once, idempotently, with no
    provider call, and the run ends FAILED -- never RUNNING forever."""
    harness = build_parallel(db, bootstrap.project, monkeypatch, "orphan")
    ids = restart_during_fan_out(db, session_factory, harness)
    staging_orphan_shape(session_factory, ids.values())

    assert reconcile(session_factory) == 1

    for reviewer, agent_run_id in ids.items():
        assert_interrupted(db, agent_run_id)
        assert node_run_row(session_factory, harness.run.id, reviewer).status == WorkflowNodeRunStatus.FAILED
    assert run_status(session_factory, harness) == WorkflowRunStatus.FAILED
    assert all(harness.provider.count(r) == 1 for r in REVIEWERS)
    # the evaluations downstream never ran; completed upstream untouched
    for key in EVALS.values():
        assert node_run_row(session_factory, harness.run.id, key).status == WorkflowNodeRunStatus.PENDING
    for key in ("planner", "engineer"):
        assert node_run_row(session_factory, harness.run.id, key).status == WorkflowNodeRunStatus.COMPLETED

    # 5. idempotent: a second (and third) sweep changes nothing
    reconcile(session_factory)
    reconcile(session_factory)
    db.expire_all()
    for agent_run_id in ids.values():
        assert events_of(db, "agent_run.interrupted", agent_run_id=agent_run_id) == 1
    assert events_of(db, "workflow.failed", task_run_id=harness.run.task_run_id) == 1


def test_concurrent_reconciliation_and_reclaim_never_duplicate_execution(db, session_factory, bootstrap, monkeypatch):
    """6: one orphan (job FAILED, reconciliation's) and two expired leases
    (the claim path's), with sweeps and workers racing on threads."""
    harness = build_parallel(db, bootstrap.project, monkeypatch, "race")
    ids = restart_during_fan_out(db, session_factory, harness)
    staging_orphan_shape(session_factory, [ids["code"]])
    expire_leases(session_factory, [ids["security"], ids["test"]])

    def work(i):
        if i % 2 == 0:
            return reconcile(session_factory)
        return new_worker(session_factory).run_once()

    in_threads(6, work)
    drain(new_worker(session_factory))
    reconcile(session_factory)

    assert all(harness.provider.count(r) == 1 for r in REVIEWERS)
    for agent_run_id in ids.values():
        assert_interrupted(db, agent_run_id)
        assert len(attempts_of(db, agent_run_id)) == 1
    assert run_status(session_factory, harness) == WorkflowRunStatus.FAILED
    assert events_of(db, "workflow.failed", task_run_id=harness.run.task_run_id) == 1


def test_a_live_unexpired_lease_is_left_alone(db, session_factory, bootstrap, monkeypatch):
    """7: the old worker may still be alive (Railway overlaps containers
    during a deploy) -- a LEASED job whose lease has not expired is never
    touched by reconciliation, and cannot be claimed."""
    harness = build_parallel(db, bootstrap.project, monkeypatch, "live")
    ids = restart_during_fan_out(db, session_factory, harness)

    reconcile(session_factory)
    assert new_worker(session_factory).run_once() is False

    session = session_factory()
    try:
        for agent_run_id in ids.values():
            assert session.get(AgentRun, agent_run_id).status == AgentRunStatus.RUNNING
            assert events_of(session, "agent_run.interrupted", agent_run_id=agent_run_id) == 0
    finally:
        session.close()
    assert run_status(session_factory, harness) == WorkflowRunStatus.RUNNING


def test_three_interrupted_siblings_are_each_retried_without_rerunning_completed_nodes(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch
):
    """8 + 9 + 11: from the exact staging shape, the operator retries each
    interrupted reviewer (no replacement model -- the model was never at
    fault). The first two retries queue; the third resumes the run. Planner
    and Engineer never run again, history is preserved, and the run stops at
    the Human Approval gate, which is never decided automatically."""
    harness = build_parallel(db, bootstrap.project, monkeypatch, "resume")
    ids = restart_during_fan_out(db, session_factory, harness)
    staging_orphan_shape(session_factory, ids.values())
    reconcile(session_factory)
    assert run_status(session_factory, harness) == WorkflowRunStatus.FAILED
    # what the Control Room shows (and keys its "Retry this step" offer on)
    nodes = by_key(detail(client, auth_headers, harness.run.id))
    for reviewer in REVIEWERS:
        assert nodes[reviewer]["failure"]["category"] == WORKER_INTERRUPTED

    first = retry(client, auth_headers, harness, "code", session_factory)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "pending"
    assert run_status(session_factory, harness) == WorkflowRunStatus.FAILED  # queued, not resumed
    assert retry(client, auth_headers, harness, "security", session_factory).status_code == 200
    assert run_status(session_factory, harness) == WorkflowRunStatus.FAILED
    last = retry(client, auth_headers, harness, "test", session_factory)
    assert last.status_code == 200, last.text
    assert run_status(session_factory, harness) == WorkflowRunStatus.RUNNING

    drain(new_worker(session_factory))

    assert harness.provider.count("planner") == 1 and harness.provider.count("engineer") == 1
    for reviewer in REVIEWERS:
        assert harness.provider.count(reviewer) == 2  # the interrupted call + the explicit retry
        original = node_run_row(session_factory, harness.run.id, reviewer, iteration=0)
        latest = node_run_row(session_factory, harness.run.id, reviewer)
        assert original.status == WorkflowNodeRunStatus.FAILED and original.agent_run_id == ids[reviewer]
        assert latest.iteration == 1 and latest.status == WorkflowNodeRunStatus.COMPLETED
        assert_interrupted(db, ids[reviewer])  # history untouched
    for key in EVALS.values():
        assert node_run_row(session_factory, harness.run.id, key).status == WorkflowNodeRunStatus.COMPLETED

    assert run_status(session_factory, harness) == WorkflowRunStatus.NODE_WAITING_FOR_APPROVAL
    gate = node_run_row(session_factory, harness.run.id, "gate")
    assert gate.status == WorkflowNodeRunStatus.WAITING_FOR_APPROVAL
    from app.models.governance import Approval

    session = session_factory()
    try:
        approvals = session.query(Approval).filter(Approval.scope_ref_id == gate.id).all()
        assert [a.status for a in approvals] == [ApprovalStatus.PENDING]
    finally:
        session.close()


def test_a_genuinely_failed_node_still_requires_a_replacement_model(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch
):
    """The no-replacement retry is ONLY for worker interruption: a node that
    failed on its own (a provider error) keeps the MA7.6B contract."""
    from app.providers.base import ProviderInvalidResponseError

    harness = build_parallel(db, bootstrap.project, monkeypatch, "genuine")

    def bad(_request):
        raise ProviderInvalidResponseError("boom")

    harness.provider.hooks["code"] = bad
    go(db, harness)
    drain(new_worker(session_factory))
    assert run_status(session_factory, harness) == WorkflowRunStatus.FAILED

    response = retry(client, auth_headers, harness, "code", session_factory)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "retry_not_allowed"


# =============================================================================
# 10. EVALUATION nodes
# =============================================================================


def interrupt_evaluator(db, session_factory, harness, eval_key):
    """Reviewers complete; the worker dies inside ``eval_key``'s evaluator
    provider call while the other two evaluations complete normally."""
    go(db, harness)
    harness.provider.hooks[eval_key] = die
    worker = new_worker(session_factory)
    while True:
        try:
            if not worker.run_once():
                break
        except SimulatedWorkerDeath:
            pass
    harness.provider.hooks.clear()
    agent_run_id = node_run_row(session_factory, harness.run.id, eval_key).agent_run_id
    staging_orphan_shape(session_factory, [agent_run_id])
    return agent_run_id


def test_interrupted_evaluator_fails_safely_and_can_be_retried(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch
):
    harness = build_parallel(db, bootstrap.project, monkeypatch, "evalint")
    evaluator = interrupt_evaluator(db, session_factory, harness, "eval_code")

    reconcile(session_factory)

    assert_interrupted(db, evaluator)
    db.expire_all()
    evaluation = db.query(EvaluationRun).filter_by(evaluator_agent_run_id=evaluator).one()
    assert evaluation.status == EvaluationRunStatus.FAILED
    assert node_run_row(session_factory, harness.run.id, "eval_code").status == WorkflowNodeRunStatus.FAILED
    assert run_status(session_factory, harness) == WorkflowRunStatus.FAILED
    assert harness.provider.count("eval_code") == 1
    assert by_key(detail(client, auth_headers, harness.run.id))["eval_code"]["failure"]["category"] == (
        WORKER_INTERRUPTED
    )

    # a replacement model is not accepted for an EVALUATION node
    rejected = retry(
        client, auth_headers, harness, "eval_code", session_factory, body={"replacement_provider_model_id": "x"}
    )
    assert rejected.status_code == 400 and rejected.json()["error"]["code"] == "retry_not_allowed"

    response = retry(client, auth_headers, harness, "eval_code", session_factory)
    assert response.status_code == 200, response.text
    assert run_status(session_factory, harness) == WorkflowRunStatus.RUNNING
    drain(new_worker(session_factory))

    assert harness.provider.count("eval_code") == 2
    for reviewer in REVIEWERS:
        assert harness.provider.count(reviewer) == 1  # the subject is never re-produced
    latest = node_run_row(session_factory, harness.run.id, "eval_code")
    assert latest.iteration == 1 and latest.status == WorkflowNodeRunStatus.COMPLETED
    db.expire_all()
    assert db.query(EvaluationRun).filter_by(evaluator_agent_run_id=evaluator).one().status == (
        EvaluationRunStatus.FAILED
    )  # the interrupted evaluation stays as history
    assert run_status(session_factory, harness) == WorkflowRunStatus.NODE_WAITING_FOR_APPROVAL


def test_a_genuinely_failed_evaluation_is_still_not_retryable(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch
):
    harness = build_parallel(db, bootstrap.project, monkeypatch, "evalbad", texts={"eval_code": "not json"})
    go(db, harness)
    drain(new_worker(session_factory))
    assert node_run_row(session_factory, harness.run.id, "eval_code").status == WorkflowNodeRunStatus.FAILED

    response = retry(client, auth_headers, harness, "eval_code", session_factory)
    assert response.status_code == 400 and response.json()["error"]["code"] == "retry_not_allowed"


# =============================================================================
# 12. zombie / late writers
# =============================================================================


def test_a_late_provider_response_cannot_complete_an_interrupted_run(db, session_factory):
    """The old worker was only presumed dead: while its provider call was in
    flight, recovery finalized the run as interrupted. When the response
    finally arrives it must not turn the run COMPLETED or create output."""
    scenario = _build_scenario(db, model_policy=_manual(free_pm(db)))

    def recovery_happens_meanwhile():
        session = session_factory()
        try:
            assert AgentExecutionService(session).finalize_interrupted(scenario.agent_run.id, reason="test")
        finally:
            session.close()

    zombie = FakeAdapter(responses=[ok("LATE")], on_invoke=recovery_happens_meanwhile)
    session = session_factory()
    try:
        AgentExecutionService(session, adapter_factory=_factory_for(zombie)).execute(
            scenario.agent_run.id, worker_id="zombie"
        )
    finally:
        session.close()

    assert_interrupted(db, scenario.agent_run.id)
    assert db.query(Artifact).filter_by(agent_run_id=scenario.agent_run.id).count() == 0
    assert events_of(db, "agent_run.completed", agent_run_id=scenario.agent_run.id) == 0


def test_a_superseded_worker_never_starts_its_provider_call(db, session_factory):
    """The old worker stalls before its provider call; meanwhile a new worker
    recovers the run (attempt #1 interrupted before any call) and completes
    it with attempt #2. The stalled worker then resumes -- and must not call
    the provider. Exactly one provider call in total."""
    scenario = _build_scenario(db, model_policy=_manual(free_pm(db)))
    fresh = FakeAdapter(responses=[ok()])
    zombie = FakeAdapter(responses=[ok("ZOMBIE")])
    session = session_factory()
    try:
        stalled = AgentExecutionService(session, adapter_factory=_factory_for(zombie))
        real = stalled._build_extra_context

        def meanwhile_the_run_is_recovered(agent_run, resolved=None):
            recover(session_factory, scenario.agent_run.id, fresh)
            return real(agent_run, resolved=resolved)

        stalled._build_extra_context = meanwhile_the_run_is_recovered
        stalled.execute(scenario.agent_run.id, worker_id="zombie")
    finally:
        session.close()

    assert zombie.calls == [] and len(fresh.calls) == 1
    db.expire_all()
    assert db.get(AgentRun, scenario.agent_run.id).status == AgentRunStatus.COMPLETED
    assert [(a.attempt_number, a.status) for a in attempts_of(db, scenario.agent_run.id)] == [
        (1, AgentRunAttemptStatus.FAILED),
        (2, AgentRunAttemptStatus.COMPLETED),
    ]


# =============================================================================
# 13. MA8 evidence isolation
# =============================================================================


def test_worker_interruption_is_not_provider_or_quality_evidence(db, session_factory):
    pm = free_pm(db)
    scenario = _build_scenario(db, model_policy=_manual(pm))
    crash_during_provider_call(session_factory, scenario.agent_run.id)
    recover(session_factory, scenario.agent_run.id, FakeAdapter(responses=[ok()]))
    db.expire_all()

    context = RoutingContext(project_id=scenario.project.id, agent_role=scenario.agent_version.role)
    profile = load_evidence(
        db, context=context, provider_model_ids=[pm.id], config=EvidenceConfig.from_json(DEFAULT_V1_CONFIG)
    )[pm.id]
    assert profile.provider_failures == 0
    assert profile.observations == 0  # not a reliability observation either way
    assert profile.provider_failure_rate is None
    assert profile.other_failures == 1
    assert profile.evaluated_runs == 0 and (profile.met, profile.partial, profile.not_met) == (0, 0, 0)

    summary = ModelIntelligenceService(db).summary(scenario.project.id)
    assert summary["reliability_issues"] == 0
    assert summary["failures_by_category"] == {WORKER_INTERRUPTED: 1}
