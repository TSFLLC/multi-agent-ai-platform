"""MA8.1 — Router Foundation.

``app.model_resolution.route`` is the one router every Agent Run goes
through. These tests pin its policy semantics (MANUAL / FREE_ONLY /
PREFER_FREE / ANY), eligibility rules, deterministic tie-break, the
structured decision it returns, and the ``model_routing_decisions`` audit
row ``AgentExecutionService`` writes per attempt — including for workflow
EVALUATION-node evaluators and the MA7.6B failed-node replacement retry.

Disposable ``tmp_path`` databases only (tests.conftest); nothing here
touches data/multi_agent_platform.db. No real provider is called.
"""

import json
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.config import settings
from app.db.enums import (
    AgentRunStatus,
    HealthStatus,
    ModelCallStatus,
    ModelSelectionMode,
    ModelStatus,
    PricingClassification,
    RouterFreePolicy,
    WorkflowNodeRunStatus,
)
from app.model_resolution import (
    ROUTING_STRATEGY,
    ExclusionReason,
    ModelUnavailableError,
    NoEligibleModelError,
    record_routing_decision,
    resolve_auto,
    route,
)
from app.models.execution import ModelCall, ModelRoutingDecision
from app.models.governance import SecretReference
from app.providers.base import InvokeResponse, ProviderTimeoutError
from app.secrets_store import get_secret_store
from app.services.execution_service import AgentExecutionService
from tests.conftest import make_model, make_provider, make_provider_model
from tests.ma7_4b_support import new_worker
from tests.test_execution_service import FakeAdapter, _auto, _build_scenario, _factory_for, _manual
from tests.test_ma7_5a_evaluation_node import (
    build_harness,
    evaluation_run_of,
    run_to_waiting_gate,
)
from tests.test_ma7_5b_parallel_evaluations import drain
from tests.test_ma7_6b_retry_recovery import add_replacement_model, failed_flagship, node_run_row


@pytest.fixture(autouse=True)
def artifacts_dir(monkeypatch, tmp_path):
    """Every run in this module writes its artifacts under tmp_path, never data/."""
    path = tmp_path / "artifacts"
    monkeypatch.setattr(settings, "artifacts_dir", path)
    return path


def _pm(db, canonical, cost_in, cost_out, *, provider=None, **kwargs):
    return make_provider_model(
        db,
        model=make_model(db, canonical_model_id=canonical),
        provider=provider or make_provider(db),
        cost_input_per_mtok=None if cost_in is None else Decimal(cost_in),
        cost_output_per_mtok=None if cost_out is None else Decimal(cost_out),
        **kwargs,
    )


def _free(db, canonical="free/model", **kw):
    return _pm(db, canonical, "0", "0", **kw)


def _paid(db, canonical="paid/model", cost_in="3.00", cost_out="6.00", **kw):
    return _pm(db, canonical, cost_in, cost_out, **kw)


def _unknown(db, canonical="unknown/model", **kw):
    return _pm(db, canonical, None, None, **kw)


def _manual_policy(pm_id):
    return {"mode": "manual", "manual_provider_model_id": pm_id}


def _auto_policy(policy):
    return {"mode": "auto", "auto_policy": policy.value}


def _decisions(db, agent_run_id):
    stmt = (
        select(ModelRoutingDecision)
        .where(ModelRoutingDecision.agent_run_id == agent_run_id)
        .order_by(ModelRoutingDecision.created_at)
    )
    return list(db.execute(stmt).scalars().all())


def _ok(text="done"):
    return InvokeResponse(text=text, tokens_in=10, tokens_out=20, latency_ms=5)


# =============================================================================
# MANUAL
# =============================================================================


def test_manual_valid_model_is_preserved_even_when_a_free_auto_pick_exists(db):
    paid = _paid(db)
    _free(db)
    db.commit()
    decision = route(db, _manual_policy(paid.id))
    assert decision.selection_mode == ModelSelectionMode.MANUAL
    assert decision.selected.provider_model.id == paid.id
    assert decision.selected.pricing_classification == PricingClassification.PAID
    assert decision.requested_policy is None and decision.fallback_used is None
    assert [v.provider_model_id for v in decision.eligible] == [paid.id]


@pytest.mark.parametrize(
    "breakage, code",
    [
        ("missing", ExclusionReason.MODEL_NOT_FOUND),
        ("model_inactive", ExclusionReason.MODEL_INACTIVE),
        ("provider_down", ExclusionReason.PROVIDER_UNAVAILABLE),
        ("offering_down", ExclusionReason.PROVIDER_MODEL_UNAVAILABLE),
    ],
)
def test_manual_ineligible_model_fails_with_reason_and_never_substitutes(db, breakage, code):
    _free(db, "free/always-there")  # an eligible alternative the router must NOT fall back to
    target = _paid(db, "paid/target")
    if breakage == "model_inactive":
        target.model.status = ModelStatus.DEPRECATED
    elif breakage == "provider_down":
        target.provider.health_status = HealthStatus.DOWN
    elif breakage == "offering_down":
        target.availability_status = HealthStatus.DOWN
    db.commit()
    requested = "does-not-exist" if breakage == "missing" else target.id

    with pytest.raises(ModelUnavailableError) as info:
        route(db, _manual_policy(requested))

    assert info.value.code == code
    decision = info.value.decision
    assert decision.selected is None and decision.eligible == []
    assert decision.requested_provider_model_id == requested
    assert decision.excluded[0].exclusion_reasons[0] == code


def test_manual_policy_without_a_model_id_fails_clearly(db):
    _free(db)
    db.commit()
    with pytest.raises(ModelUnavailableError) as info:
        route(db, {"mode": "manual"})
    assert info.value.code == ExclusionReason.MANUAL_MODEL_INVALID


@pytest.mark.parametrize(
    "policy", [{"mode": "auto"}, {"mode": "auto", "auto_policy": "cheapest"}, {"mode": "x"}]
)
def test_malformed_policy_is_a_categorized_routing_failure(db, policy):
    _free(db)
    db.commit()
    with pytest.raises(ModelUnavailableError) as info:
        route(db, policy)
    assert info.value.code == ExclusionReason.POLICY_INVALID


# =============================================================================
# FREE_ONLY / PREFER_FREE / ANY
# =============================================================================


def test_free_only_selects_only_free_and_records_why_others_were_excluded(db):
    free = _free(db)
    paid = _paid(db)
    unknown = _unknown(db)
    db.commit()
    decision = route(db, _auto_policy(RouterFreePolicy.FREE_ONLY))
    assert decision.selected.provider_model.id == free.id
    assert [v.provider_model_id for v in decision.eligible] == [free.id]
    reasons = {v.provider_model_id: v.exclusion_reasons for v in decision.excluded}
    assert reasons == {paid.id: (ExclusionReason.NOT_FREE,), unknown.id: (ExclusionReason.PRICING_UNKNOWN,)}
    assert decision.free_preference_satisfied is True and decision.fallback_used is None


def test_free_only_never_selects_paid(db):
    _paid(db, "paid/a", "0.01", "0.01")
    _paid(db, "paid/b")
    db.commit()
    with pytest.raises(NoEligibleModelError) as info:
        route(db, _auto_policy(RouterFreePolicy.FREE_ONLY))
    assert info.value.code == ExclusionReason.NO_ELIGIBLE_MODEL
    assert info.value.decision.exclusion_counts() == {"NOT_FREE": 2}


def test_free_only_does_not_treat_unknown_as_free(db):
    _unknown(db)
    # half-known pricing (output missing) is UNKNOWN too, not FREE
    _pm(db, "half/known", "0", None)
    db.commit()
    with pytest.raises(NoEligibleModelError) as info:
        route(db, _auto_policy(RouterFreePolicy.FREE_ONLY))
    assert info.value.decision.exclusion_counts() == {"PRICING_UNKNOWN": 2}


def test_prefer_free_selects_an_eligible_free_model_when_available(db):
    _paid(db, "aaa/paid-cheap", "0.01", "0.01")
    free = _free(db, "zzz/free")
    db.commit()
    decision = route(db, _auto_policy(RouterFreePolicy.PREFER_FREE))
    assert decision.selected.provider_model.id == free.id
    assert decision.fallback_used is False and decision.free_preference_satisfied is True
    assert [v.canonical_model_id for v in decision.eligible] == ["zzz/free", "aaa/paid-cheap"]


def test_prefer_free_falls_back_to_paid_explicitly_but_never_to_unknown(db):
    """Frozen MA3 PREFER_FREE semantics: PAID is the permitted fallback;
    UNKNOWN is never eligible. MA8.1 makes the fallback explicit."""
    paid = _paid(db)
    _unknown(db)
    inactive_free = _free(db, "free/inactive")
    inactive_free.model.status = ModelStatus.UNAVAILABLE
    db.commit()
    decision = route(db, _auto_policy(RouterFreePolicy.PREFER_FREE))
    assert decision.selected.provider_model.id == paid.id
    assert decision.fallback_used is True and decision.free_preference_satisfied is False
    assert "fallback" in decision.reason
    assert decision.exclusion_counts() == {"MODEL_INACTIVE": 1, "PRICING_UNKNOWN": 1}


def test_any_can_select_regardless_of_classification(db):
    paid = _paid(db)
    db.commit()
    assert route(db, _auto_policy(RouterFreePolicy.ANY)).selected.provider_model.id == paid.id

    only_unknown = _unknown(db, "u/only")
    paid.model.status = ModelStatus.DEPRECATED
    db.commit()
    decision = route(db, _auto_policy(RouterFreePolicy.ANY))
    assert decision.selected.provider_model.id == only_unknown.id
    assert decision.free_preference_satisfied is None and decision.fallback_used is None


# =============================================================================
# eligibility
# =============================================================================


def test_down_provider_is_excluded(db):
    down = make_provider(db, name="Down Provider")
    down.health_status = HealthStatus.DOWN
    _free(db, "free/on-down", provider=down)
    live = _free(db, "free/on-live")
    db.commit()
    decision = route(db, _auto_policy(RouterFreePolicy.ANY))
    assert decision.selected.provider_model.id == live.id
    assert decision.exclusion_counts() == {"PROVIDER_UNAVAILABLE": 1}


def test_degraded_provider_stays_eligible(db):
    degraded = make_provider(db, name="Degraded")
    degraded.health_status = HealthStatus.DEGRADED
    pm = _free(db, provider=degraded)
    db.commit()
    assert route(db, _auto_policy(RouterFreePolicy.FREE_ONLY)).selected.provider_model.id == pm.id


def test_inactive_model_is_excluded(db):
    for status in (ModelStatus.DEPRECATED, ModelStatus.UNAVAILABLE):
        _free(db, f"free/{status.value}").model.status = status
    live = _paid(db)
    db.commit()
    decision = route(db, _auto_policy(RouterFreePolicy.ANY))
    assert decision.selected.provider_model.id == live.id
    assert decision.exclusion_counts() == {"MODEL_INACTIVE": 2}


def test_unavailable_provider_offering_is_excluded(db):
    _free(db, "free/offering-down", availability_status=HealthStatus.DOWN)
    live = _paid(db)
    db.commit()
    decision = route(db, _auto_policy(RouterFreePolicy.PREFER_FREE))
    assert decision.selected.provider_model.id == live.id
    assert decision.exclusion_counts() == {"PROVIDER_MODEL_UNAVAILABLE": 1}


def test_missing_model_is_excluded_rather_than_guessed(db):
    """A manual id that is not in the catalog fails; an empty catalog fails
    auto routing — neither invents a model."""
    with pytest.raises(ModelUnavailableError) as manual:
        route(db, _manual_policy("gone"))
    assert manual.value.code == ExclusionReason.MODEL_NOT_FOUND
    with pytest.raises(NoEligibleModelError) as auto:
        route(db, _auto_policy(RouterFreePolicy.ANY))
    assert auto.value.decision.excluded == [] and auto.value.decision.eligible == []


# =============================================================================
# determinism
# =============================================================================


def test_selection_is_deterministic_and_follows_the_documented_tie_break(db):
    b = make_provider(db, name="B-provider")
    a = make_provider(db, name="A-provider")
    shared = make_model(db, canonical_model_id="same/model")
    on_b = make_provider_model(
        db, model=shared, provider=b, cost_input_per_mtok=Decimal(0), cost_output_per_mtok=Decimal(0)
    )
    on_a = make_provider_model(
        db, model=shared, provider=a, cost_input_per_mtok=Decimal(0), cost_output_per_mtok=Decimal(0)
    )
    _free(db, "zzz/free")
    _paid(db, "aaa/paid", "0.10", "0.10")
    _paid(db, "aab/paid", "0.05", "9.00")
    db.commit()

    first = route(db, _auto_policy(RouterFreePolicy.ANY))
    assert first.selected.provider_model.id == on_a.id  # same model on two providers: provider name decides
    assert [v.provider_model_id for v in first.eligible[:2]] == [on_a.id, on_b.id]
    assert [v.canonical_model_id for v in first.eligible[2:]] == ["zzz/free", "aab/paid", "aaa/paid"]
    for _ in range(5):
        again = route(db, _auto_policy(RouterFreePolicy.ANY))
        assert [v.provider_model_id for v in again.eligible] == [v.provider_model_id for v in first.eligible]
    assert resolve_auto(db, RouterFreePolicy.ANY).provider_model.id == on_a.id  # MA3 wrapper agrees


# =============================================================================
# decision record
# =============================================================================


def test_decision_record_identifies_selection_policy_and_strategy(db):
    free = _free(db)
    _paid(db)
    run = _build_scenario(db, model_policy=_auto(RouterFreePolicy.PREFER_FREE)).agent_run
    decision = route(db, _auto_policy(RouterFreePolicy.PREFER_FREE))
    row = record_routing_decision(db, agent_run_id=run.id, decision=decision)
    db.commit()
    db.refresh(row)

    assert row.selection_mode == ModelSelectionMode.AUTO
    assert row.requested_policy == "prefer_free"
    assert row.routing_strategy == ROUTING_STRATEGY
    assert row.eligible_candidates[0]["provider_model_id"] == free.id
    assert row.details["selected_provider_model_id"] == free.id
    assert row.details["selected_pricing_classification"] == "free"
    assert row.details["outcome"] == "selected"
    assert row.details["eligible_count"] == 2 and row.details["excluded_count"] == 0
    assert row.details["free_preference_satisfied"] is True and row.details["fallback_used"] is False
    assert "free/model" in row.rationale and "prefer_free" in row.rationale
    for claim in ("best", "highest quality", "most reliable"):
        assert claim not in row.rationale.lower()


def test_no_secrets_appear_in_decision_records(db):
    secret_value = "sk-or-v1-TOPSECRET-should-never-leak"
    scenario = _build_scenario(db, model_policy=_auto(RouterFreePolicy.ANY))
    run = scenario.agent_run
    pm = _free(db)
    pm.provider.base_url = "https://user:pass@example.invalid/api"
    ref = "provider-key-ref-123"
    get_secret_store().set_secret(ref, secret_value)
    db.add(
        SecretReference(
            project_id=scenario.project.id,
            provider_id=pm.provider.id,
            name="openrouter",
            secret_store_ref=ref,
        )
    )
    db.commit()

    adapter = FakeAdapter(responses=[_ok()])
    AgentExecutionService(db, adapter_factory=_factory_for(adapter)).execute(run.id, worker_id="w")
    (row,) = _decisions(db, run.id)
    blob = json.dumps(
        [row.eligible_candidates, row.details, row.rationale, row.requested_policy, row.routing_strategy]
    )
    for forbidden in (secret_value, ref, "user:pass", "example.invalid"):
        assert forbidden not in blob


# =============================================================================
# integration with the existing execution path
# =============================================================================


def test_agent_execution_receives_the_routed_model_and_links_the_decision(db):
    _paid(db, "aaa/paid", "0.01", "0.01")
    free = _free(db, "zzz/free")
    scenario = _build_scenario(db, model_policy=_auto(RouterFreePolicy.PREFER_FREE))
    adapter = FakeAdapter(responses=[_ok()])
    AgentExecutionService(db, adapter_factory=_factory_for(adapter)).execute(
        scenario.agent_run.id, worker_id="w"
    )

    db.refresh(scenario.agent_run)
    assert scenario.agent_run.status == AgentRunStatus.COMPLETED
    assert adapter.calls[0].provider_model_id == "zzz/free"
    assert scenario.agent_run.model_id == free.model_id and scenario.agent_run.provider_id == free.provider_id

    (row,) = _decisions(db, scenario.agent_run.id)
    (call,) = (
        db.execute(select(ModelCall).where(ModelCall.agent_run_id == scenario.agent_run.id)).scalars().all()
    )
    assert call.model_routing_decision_id == row.id
    assert row.selected_provider_model_snapshot_id == scenario.agent_run.provider_model_snapshot_id
    assert row.details["agent_run_attempt_id"] == call.agent_run_attempt_id


def test_run_level_override_beats_agent_version_policy(db):
    version_pm = _paid(db, "paid/version-choice")
    override_pm = _paid(db, "paid/override-choice", "9", "9")
    scenario = _build_scenario(db, model_policy=_manual(version_pm))
    scenario.agent_run.model_policy_override_json = _manual_policy(override_pm.id)
    db.commit()
    adapter = FakeAdapter(responses=[_ok()])
    AgentExecutionService(db, adapter_factory=_factory_for(adapter)).execute(
        scenario.agent_run.id, worker_id="w"
    )
    assert adapter.calls[0].provider_model_id == "paid/override-choice"
    (row,) = _decisions(db, scenario.agent_run.id)
    assert row.selection_mode == ModelSelectionMode.MANUAL
    assert row.details["requested_provider_model_id"] == override_pm.id


def test_failed_routing_is_audited_and_fails_the_run_without_any_provider_call(db):
    _paid(db)
    scenario = _build_scenario(db, model_policy=_auto(RouterFreePolicy.FREE_ONLY))
    adapter = FakeAdapter(responses=[])
    AgentExecutionService(db, adapter_factory=_factory_for(adapter)).execute(
        scenario.agent_run.id, worker_id="w"
    )
    db.refresh(scenario.agent_run)
    assert scenario.agent_run.status == AgentRunStatus.FAILED and adapter.calls == []
    (row,) = _decisions(db, scenario.agent_run.id)
    assert row.selected_provider_model_snapshot_id is None
    assert row.details["outcome"] == "failed" and row.details["failure_code"] == "NO_ELIGIBLE_MODEL"
    assert row.details["exclusion_counts"] == {"NOT_FREE": 1}


def test_each_retryable_attempt_gets_its_own_decision_and_accounting_is_unchanged(db):
    pm = _paid(db, "paid/model", "1.00", "2.00")
    scenario = _build_scenario(db, model_policy=_manual(pm))
    adapter = FakeAdapter(
        responses=[
            ProviderTimeoutError("slow"),
            InvokeResponse(text="ok", tokens_in=1_000_000, tokens_out=500_000, latency_ms=42),
        ]
    )
    AgentExecutionService(db, adapter_factory=_factory_for(adapter)).execute(
        scenario.agent_run.id, worker_id="w"
    )
    rows = _decisions(db, scenario.agent_run.id)
    assert len(rows) == 2 and len({r.details["agent_run_attempt_id"] for r in rows}) == 2
    calls = (
        db.execute(select(ModelCall).where(ModelCall.agent_run_id == scenario.agent_run.id)).scalars().all()
    )
    by_status = {c.status: c for c in calls}
    ok = by_status[ModelCallStatus.SUCCESS]
    # Same MA3 arithmetic as before MA8.1: 1M in * $1 + 0.5M out * $2, estimated.
    assert ok.tokens_in == 1_000_000 and ok.tokens_out == 500_000 and ok.latency_ms == 42
    assert ok.cost_amount == Decimal("2.00") and ok.cost_is_estimated is True
    assert {c.model_routing_decision_id for c in calls} == {r.id for r in rows}


# =============================================================================
# workflow EVALUATION node + MA7.6B failed-node retry
# =============================================================================


def test_evaluation_node_evaluator_uses_the_same_router(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir
):
    harness = build_harness(db, bootstrap.project, monkeypatch, "m81e", override_model=True)
    run_to_waiting_gate(db, session_factory, harness)
    evaluation = evaluation_run_of(session_factory)
    rows = _decisions(db, evaluation.evaluator_agent_run_id)
    assert len(rows) == 1
    assert rows[0].selection_mode == ModelSelectionMode.MANUAL
    assert rows[0].details["selected_provider_model_id"] == harness.setup.override["manual_provider_model_id"]
    assert rows[0].routing_strategy == ROUTING_STRATEGY


def test_failed_node_replacement_model_is_authoritative_on_retry(
    client,
    db,
    session_factory,
    auth_headers,
    bootstrap,
    monkeypatch,
    artifacts_dir,
):
    harness = failed_flagship(db, session_factory, monkeypatch, bootstrap, "m81r")
    original = node_run_row(session_factory, harness.run.id, "code")
    original_rows = _decisions(db, original.agent_run_id)
    original_snapshot = [(r.id, r.details["selected_provider_model_id"]) for r in original_rows]
    assert original_rows and all(r.selection_mode == ModelSelectionMode.MANUAL for r in original_rows)

    replacement = add_replacement_model(db, harness, "code", "m81r")
    response = client.post(
        f"/workflow-runs/{harness.run.id}/nodes/{original.id}/retry",
        headers=auth_headers,
        json={"replacement_provider_model_id": replacement.id},
    )
    assert response.status_code == 200, response.text
    drain(new_worker(session_factory))

    retried = node_run_row(session_factory, harness.run.id, "code")
    assert retried.iteration == 1 and retried.status == WorkflowNodeRunStatus.COMPLETED
    (row,) = _decisions(db, retried.agent_run_id)
    assert row.selection_mode == ModelSelectionMode.MANUAL
    assert row.details["requested_provider_model_id"] == replacement.id
    assert row.details["selected_provider_model_id"] == replacement.id
    assert retried.workflow_run_id == harness.run.id  # the same WorkflowRun continues
    # The failed attempt's decisions are untouched history.
    db.expire_all()
    assert [
        (r.id, r.details["selected_provider_model_id"]) for r in _decisions(db, original.agent_run_id)
    ] == (original_snapshot)
