"""AgentExecutionService — MA3 core orchestration.

Every test here uses a fake ProviderAdapter (no real network, no
OpenRouter dependency) injected via ``adapter_factory`` — the deterministic
suite must never require Internet. The one live OpenRouter execution is a
separate, explicit operator smoke test.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional

from app.db.enums import (
    AgentRunAttemptStatus,
    AgentRunStatus,
    ModelCallStatus,
    RouterFreePolicy,
    TaskRunStatus,
    VersionStatus,
)
from app.models.artifacts_eval import Artifact
from app.models.execution import ModelCall
from app.models.observability import ExecutionEvent
from app.models.tasks import AgentRunAttempt
from app.providers.base import (
    InvokeRequest,
    InvokeResponse,
    ProviderAuthenticationError,
    ProviderConnectionError,
    ProviderInvalidResponseError,
    ProviderTimeoutError,
)
from app.services.execution_service import AgentExecutionService
from tests.conftest import (
    make_agent,
    make_agent_run,
    make_agent_version,
    make_budget,
    make_model,
    make_project,
    make_provider,
    make_provider_model,
    make_task,
    make_task_run,
)


@dataclass
class Scenario:
    project: Any
    task: Any
    task_run: Any
    agent: Any
    agent_version: Any
    agent_run: Any


def _build_scenario(db, *, model_policy: Dict[str, Any], budget=None) -> Scenario:
    project = make_project(db)
    task = make_task(db, project=project)
    task_run = make_task_run(db, task=task)
    if budget is not None:
        task_run.budget_id = budget.id
    agent = make_agent(db, project=project)
    agent_version = make_agent_version(db, agent=agent, status=VersionStatus.ACTIVE)
    agent_version.model_policy = model_policy
    agent_run = make_agent_run(db, task_run=task_run, agent_version=agent_version)
    db.commit()
    return Scenario(
        project=project,
        task=task,
        task_run=task_run,
        agent=agent,
        agent_version=agent_version,
        agent_run=agent_run,
    )


def _manual(pm) -> Dict[str, Any]:
    return {"mode": "manual", "manual_provider_model_id": pm.id}


def _auto(policy: RouterFreePolicy) -> Dict[str, Any]:
    return {"mode": "auto", "auto_policy": policy.value}


@dataclass
class FakeAdapter:
    responses: List[Any] = field(default_factory=list)
    calls: List[InvokeRequest] = field(default_factory=list)
    on_invoke: Optional[Any] = None

    def invoke(self, request: InvokeRequest) -> InvokeResponse:
        self.calls.append(request)
        if self.on_invoke is not None:
            self.on_invoke()
        outcome = self.responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _factory_for(adapter: FakeAdapter):
    return lambda db, provider: adapter


def _events(db, task_run_id) -> List[str]:
    from sqlalchemy import select

    stmt = (
        select(ExecutionEvent.event_type)
        .where(ExecutionEvent.task_run_id == task_run_id)
        .order_by(ExecutionEvent.sequence_number)
    )
    return [row[0] for row in db.execute(stmt).all()]


# -- success paths -----------------------------------------------------------


def test_execute_success_free_model_records_verified_zero_cost(db):
    model = make_model(db, canonical_model_id="free/model")
    provider = make_provider(db)
    pm = make_provider_model(
        db, model=model, provider=provider, cost_input_per_mtok=Decimal(0), cost_output_per_mtok=Decimal(0)
    )
    db.commit()
    scenario = _build_scenario(db, model_policy=_manual(pm))

    adapter = FakeAdapter(
        responses=[InvokeResponse(text="MA3_EXECUTION_OK", tokens_in=10, tokens_out=2, latency_ms=42)]
    )
    service = AgentExecutionService(db, adapter_factory=_factory_for(adapter))
    service.execute(scenario.agent_run.id, worker_id="test-worker")

    db.refresh(scenario.agent_run)
    db.refresh(scenario.task_run)
    assert scenario.agent_run.status == AgentRunStatus.COMPLETED
    assert scenario.task_run.status == TaskRunStatus.COMPLETED

    model_call = db.query(ModelCall).filter_by(agent_run_id=scenario.agent_run.id).one()
    assert model_call.status == ModelCallStatus.SUCCESS
    assert model_call.cost_amount == Decimal(0)
    assert model_call.cost_is_estimated is False  # a verified free model, not an estimate

    artifact = db.query(Artifact).filter_by(agent_run_id=scenario.agent_run.id).one()
    assert artifact.size_bytes == len(b"MA3_EXECUTION_OK")
    assert len(artifact.content_hash) == 64
    with open(artifact.storage_ref, "r", encoding="utf-8") as f:
        assert f.read() == "MA3_EXECUTION_OK"


def test_execute_success_paid_model_computes_cost_from_tokens(db):
    model = make_model(db, canonical_model_id="paid/model")
    provider = make_provider(db)
    pm = make_provider_model(
        db,
        model=model,
        provider=provider,
        cost_input_per_mtok=Decimal("3.00"),
        cost_output_per_mtok=Decimal("6.00"),
    )
    db.commit()
    scenario = _build_scenario(db, model_policy=_manual(pm))

    adapter = FakeAdapter(
        responses=[InvokeResponse(text="done", tokens_in=1_000_000, tokens_out=1_000_000, latency_ms=10)]
    )
    service = AgentExecutionService(db, adapter_factory=_factory_for(adapter))
    service.execute(scenario.agent_run.id, worker_id="test-worker")

    model_call = db.query(ModelCall).filter_by(agent_run_id=scenario.agent_run.id).one()
    assert model_call.cost_amount == Decimal("9.00")  # 1M in @ $3 + 1M out @ $6
    assert model_call.cost_is_estimated is True


def test_execute_unknown_pricing_never_fabricates_zero_cost(db):
    model = make_model(db, canonical_model_id="unknown/model")
    provider = make_provider(db)
    pm = make_provider_model(
        db, model=model, provider=provider, cost_input_per_mtok=None, cost_output_per_mtok=None
    )
    db.commit()
    scenario = _build_scenario(db, model_policy=_manual(pm))

    adapter = FakeAdapter(responses=[InvokeResponse(text="done", tokens_in=5, tokens_out=5, latency_ms=1)])
    service = AgentExecutionService(db, adapter_factory=_factory_for(adapter))
    service.execute(scenario.agent_run.id, worker_id="test-worker")

    model_call = db.query(ModelCall).filter_by(agent_run_id=scenario.agent_run.id).one()
    assert model_call.cost_amount is not None
    assert model_call.cost_amount > Decimal(0)
    assert model_call.cost_is_estimated is True


def test_execute_auto_free_only_selects_free_model(db):
    provider = make_provider(db)
    paid_pm = make_provider_model(
        db,
        model=make_model(db, canonical_model_id="paid/x"),
        provider=provider,
        cost_input_per_mtok=Decimal("3.00"),
        cost_output_per_mtok=Decimal("6.00"),
    )
    free_pm = make_provider_model(
        db,
        model=make_model(db, canonical_model_id="free/x"),
        provider=provider,
        cost_input_per_mtok=Decimal(0),
        cost_output_per_mtok=Decimal(0),
    )
    db.commit()
    scenario = _build_scenario(db, model_policy=_auto(RouterFreePolicy.FREE_ONLY))

    adapter = FakeAdapter(responses=[InvokeResponse(text="ok", tokens_in=1, tokens_out=1)])
    service = AgentExecutionService(db, adapter_factory=_factory_for(adapter))
    service.execute(scenario.agent_run.id, worker_id="test-worker")

    db.refresh(scenario.agent_run)
    assert scenario.agent_run.model_id == free_pm.model_id
    assert scenario.agent_run.model_id != paid_pm.model_id


# -- failure paths (non-retryable) -------------------------------------------


def test_execute_free_only_no_eligible_model_fails_clearly_without_invoking(db):
    provider = make_provider(db)
    make_provider_model(
        db,
        model=make_model(db, canonical_model_id="paid/only"),
        provider=provider,
        cost_input_per_mtok=Decimal("1.00"),
        cost_output_per_mtok=Decimal("1.00"),
    )
    db.commit()
    scenario = _build_scenario(db, model_policy=_auto(RouterFreePolicy.FREE_ONLY))

    adapter = FakeAdapter(responses=[])
    service = AgentExecutionService(db, adapter_factory=_factory_for(adapter))
    service.execute(scenario.agent_run.id, worker_id="test-worker")

    db.refresh(scenario.agent_run)
    db.refresh(scenario.task_run)
    assert scenario.agent_run.status == AgentRunStatus.FAILED
    assert scenario.task_run.status == TaskRunStatus.FAILED
    assert adapter.calls == []  # never even attempted to invoke


def test_execute_provider_authentication_failure_is_terminal_not_retried(db):
    model = make_model(db, canonical_model_id="x/y")
    pm = make_provider_model(db, model=model, cost_input_per_mtok=Decimal(1), cost_output_per_mtok=Decimal(1))
    db.commit()
    scenario = _build_scenario(db, model_policy=_manual(pm))

    adapter = FakeAdapter(responses=[ProviderAuthenticationError("bad key")])
    service = AgentExecutionService(db, adapter_factory=_factory_for(adapter))
    service.execute(scenario.agent_run.id, worker_id="test-worker")

    db.refresh(scenario.agent_run)
    db.refresh(scenario.task_run)
    assert scenario.agent_run.status == AgentRunStatus.FAILED
    assert scenario.task_run.status == TaskRunStatus.FAILED
    assert len(adapter.calls) == 1  # not retried — auth failures are permanent

    attempts = db.query(AgentRunAttempt).filter_by(agent_run_id=scenario.agent_run.id).all()
    assert len(attempts) == 1
    assert attempts[0].status == AgentRunAttemptStatus.FAILED
    assert attempts[0].error["category"] == "provider_authentication_error"


def test_execute_malformed_response_is_terminal_not_retried(db):
    model = make_model(db, canonical_model_id="x/y")
    pm = make_provider_model(db, model=model, cost_input_per_mtok=Decimal(1), cost_output_per_mtok=Decimal(1))
    db.commit()
    scenario = _build_scenario(db, model_policy=_manual(pm))

    adapter = FakeAdapter(responses=[ProviderInvalidResponseError("no choices")])
    service = AgentExecutionService(db, adapter_factory=_factory_for(adapter))
    service.execute(scenario.agent_run.id, worker_id="test-worker")

    db.refresh(scenario.agent_run)
    assert scenario.agent_run.status == AgentRunStatus.FAILED
    assert len(adapter.calls) == 1


def test_execute_budget_exceeded_blocks_before_invocation(db):
    model = make_model(db, canonical_model_id="x/y")
    pm = make_provider_model(db, model=model, cost_input_per_mtok=Decimal(1), cost_output_per_mtok=Decimal(1))
    # settings.budget_estimate_tokens_in/out (2000/500) @ $1/$1 per mtok
    # estimate to 0.0025 — a limit well below that guarantees the
    # reservation alone pushes past the 100% hard threshold.
    budget = make_budget(db, limit_amount="0.0001")
    db.commit()
    scenario = _build_scenario(db, model_policy=_manual(pm), budget=budget)

    adapter = FakeAdapter(responses=[])
    service = AgentExecutionService(db, adapter_factory=_factory_for(adapter))
    service.execute(scenario.agent_run.id, worker_id="test-worker")

    db.refresh(scenario.agent_run)
    db.refresh(scenario.task_run)
    assert scenario.agent_run.status == AgentRunStatus.FAILED
    assert scenario.task_run.status == TaskRunStatus.FAILED
    assert adapter.calls == []


# -- retryable failures --------------------------------------------------------


def test_execute_timeout_retries_then_succeeds(db):
    model = make_model(db, canonical_model_id="x/y")
    pm = make_provider_model(db, model=model, cost_input_per_mtok=Decimal(0), cost_output_per_mtok=Decimal(0))
    db.commit()
    scenario = _build_scenario(db, model_policy=_manual(pm))

    adapter = FakeAdapter(
        responses=[ProviderTimeoutError("slow"), InvokeResponse(text="ok", tokens_in=1, tokens_out=1)]
    )
    service = AgentExecutionService(db, adapter_factory=_factory_for(adapter))
    service.execute(scenario.agent_run.id, worker_id="test-worker")

    db.refresh(scenario.agent_run)
    assert scenario.agent_run.status == AgentRunStatus.COMPLETED
    assert len(adapter.calls) == 2

    attempts = (
        db.query(AgentRunAttempt)
        .filter_by(agent_run_id=scenario.agent_run.id)
        .order_by(AgentRunAttempt.attempt_number)
        .all()
    )
    assert len(attempts) == 2
    assert attempts[0].status == AgentRunAttemptStatus.FAILED
    assert attempts[1].status == AgentRunAttemptStatus.COMPLETED


def test_execute_timeout_exhausts_attempts_and_fails(db):
    model = make_model(db, canonical_model_id="x/y")
    pm = make_provider_model(db, model=model, cost_input_per_mtok=Decimal(0), cost_output_per_mtok=Decimal(0))
    db.commit()
    scenario = _build_scenario(db, model_policy=_manual(pm))

    adapter = FakeAdapter(responses=[ProviderConnectionError("down"), ProviderConnectionError("still down")])
    service = AgentExecutionService(db, adapter_factory=_factory_for(adapter))
    service.execute(scenario.agent_run.id, worker_id="test-worker")

    db.refresh(scenario.agent_run)
    db.refresh(scenario.task_run)
    assert scenario.agent_run.status == AgentRunStatus.FAILED
    assert scenario.task_run.status == TaskRunStatus.FAILED
    assert len(adapter.calls) == 2  # bounded — settings.max_agent_run_attempts


# -- cancellation ---------------------------------------------------------------


def test_execute_cancellation_before_start_never_invokes(db):
    from datetime import datetime, timezone

    model = make_model(db, canonical_model_id="x/y")
    pm = make_provider_model(db, model=model, cost_input_per_mtok=Decimal(0), cost_output_per_mtok=Decimal(0))
    db.commit()
    scenario = _build_scenario(db, model_policy=_manual(pm))
    scenario.task_run.cancellation_requested_at = datetime.now(timezone.utc)
    db.commit()

    adapter = FakeAdapter(responses=[])
    service = AgentExecutionService(db, adapter_factory=_factory_for(adapter))
    service.execute(scenario.agent_run.id, worker_id="test-worker")

    db.refresh(scenario.agent_run)
    db.refresh(scenario.task_run)
    assert scenario.agent_run.status == AgentRunStatus.STOPPED
    assert scenario.task_run.status == TaskRunStatus.CANCELLED
    assert adapter.calls == []


def test_execute_cancellation_observed_after_in_flight_call_preserves_result(db):
    """A cancellation requested while the provider call was already
    in-flight must not discard the real result it produced — the run
    still ends CANCELLED, but the artifact/usage that already happened is
    recorded, never silently dropped."""
    from datetime import datetime, timezone

    model = make_model(db, canonical_model_id="x/y")
    pm = make_provider_model(db, model=model, cost_input_per_mtok=Decimal(0), cost_output_per_mtok=Decimal(0))
    db.commit()
    scenario = _build_scenario(db, model_policy=_manual(pm))

    def _cancel_mid_flight():
        scenario.task_run.cancellation_requested_at = datetime.now(timezone.utc)
        db.commit()

    adapter = FakeAdapter(
        responses=[InvokeResponse(text="done before cancel", tokens_in=1, tokens_out=1)],
        on_invoke=_cancel_mid_flight,
    )
    service = AgentExecutionService(db, adapter_factory=_factory_for(adapter))
    service.execute(scenario.agent_run.id, worker_id="test-worker")

    db.refresh(scenario.task_run)
    assert scenario.task_run.status == TaskRunStatus.CANCELLED
    artifact = db.query(Artifact).filter_by(agent_run_id=scenario.agent_run.id).one()
    assert artifact is not None
    model_call = db.query(ModelCall).filter_by(agent_run_id=scenario.agent_run.id).one()
    assert model_call.status == ModelCallStatus.SUCCESS


# -- Flight Recorder narration --------------------------------------------------


def test_execute_success_emits_expected_event_sequence(db):
    model = make_model(db, canonical_model_id="x/y")
    pm = make_provider_model(db, model=model, cost_input_per_mtok=Decimal(0), cost_output_per_mtok=Decimal(0))
    db.commit()
    scenario = _build_scenario(db, model_policy=_manual(pm))

    adapter = FakeAdapter(responses=[InvokeResponse(text="ok", tokens_in=1, tokens_out=1)])
    service = AgentExecutionService(db, adapter_factory=_factory_for(adapter))
    service.execute(scenario.agent_run.id, worker_id="test-worker")

    event_types = _events(db, scenario.task_run.id)
    expected_in_order = [
        "agent_run_attempt.started",
        "agent_run.model_selected",
        "agent_run.provider_model_snapshot_selected",
        "agent_run.prompt_assembled",
        "model_call.started",
        "model_call.completed",
        "artifact.created",
        "agent_run.completed",
        "task_run.verifying",
        "task_run.completed",
    ]
    assert event_types == expected_in_order


def test_execute_failure_emits_failure_events(db):
    model = make_model(db, canonical_model_id="x/y")
    pm = make_provider_model(db, model=model, cost_input_per_mtok=Decimal(1), cost_output_per_mtok=Decimal(1))
    db.commit()
    scenario = _build_scenario(db, model_policy=_manual(pm))

    adapter = FakeAdapter(responses=[ProviderAuthenticationError("bad key")])
    service = AgentExecutionService(db, adapter_factory=_factory_for(adapter))
    service.execute(scenario.agent_run.id, worker_id="test-worker")

    event_types = _events(db, scenario.task_run.id)
    assert "model_call.failed" in event_types
    assert "agent_run.failed" in event_types
    assert "task_run.failed" in event_types


# -- last-resort durability net -------------------------------------------------


def test_execute_unexpected_internal_error_still_finalizes_run(db, monkeypatch):
    model = make_model(db, canonical_model_id="x/y")
    pm = make_provider_model(db, model=model, cost_input_per_mtok=Decimal(0), cost_output_per_mtok=Decimal(0))
    db.commit()
    scenario = _build_scenario(db, model_policy=_manual(pm))

    def _boom(**kwargs):
        raise RuntimeError("unexpected bug")

    monkeypatch.setattr("app.services.execution_service.build_prompt", _boom)

    adapter = FakeAdapter(responses=[])
    service = AgentExecutionService(db, adapter_factory=_factory_for(adapter))
    service.execute(scenario.agent_run.id, worker_id="test-worker")

    db.refresh(scenario.agent_run)
    db.refresh(scenario.task_run)
    assert scenario.agent_run.status == AgentRunStatus.FAILED
    assert scenario.task_run.status == TaskRunStatus.FAILED

    attempts = db.query(AgentRunAttempt).filter_by(agent_run_id=scenario.agent_run.id).all()
    assert attempts[0].status == AgentRunAttemptStatus.FAILED
    assert attempts[0].error["category"] == "internal_error"
