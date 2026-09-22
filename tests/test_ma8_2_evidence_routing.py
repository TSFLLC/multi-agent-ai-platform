"""MA8.2 — Intelligent Evidence Routing.

Evidence (app.routing_evidence) may only reorder candidates that MA8.1's
eligibility/policy gate (app.model_resolution) already admitted, only for
AUTO routing, only from the routing run's own project and exact Agent role,
and only once a candidate has the configured minimum evidence. Everything
else — including any failure to load evidence — is exactly MA8.1.

History is written directly as the authoritative rows the platform already
keeps (model_calls, evaluation_runs, evaluation_criterion_results).
Disposable ``tmp_path`` databases only; no real provider is called.
"""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.config import settings
from app.db.enums import (
    AgentRunStatus,
    EvaluationFinding,
    EvaluationMethod,
    EvaluationRunStatus,
    ModelCallStatus,
    ModelSelectionMode,
    RouterFreePolicy,
    VersionStatus,
    WorkflowNodeRunStatus,
)
from app.model_resolution import (
    EVIDENCE_APPLIED,
    EVIDENCE_DISABLED,
    EVIDENCE_INSUFFICIENT,
    EVIDENCE_UNAVAILABLE,
    ROUTING_STRATEGY,
    route,
)
from app.models.agents import Agent, AgentVersion
from app.models.evaluation_definitions import EvaluationCriterion
from app.models.evaluation_runs import EvaluationCriterionResult, EvaluationRun
from app.models.execution import ModelCall, ModelRoutingDecision, RouterPolicyVersion
from app.models.tasks import AgentRun
from app.providers.base import InvokeResponse, ProviderTimeoutError
from app.routing_evidence import (
    DEFAULT_V1_CONFIG,
    EVIDENCE_STRATEGY_V1,
    INSUFFICIENT,
    QUALITY_MIXED,
    QUALITY_NEGATIVE,
    QUALITY_POSITIVE,
    RELIABILITY_DEMOTED,
    RELIABILITY_OK,
    EvidenceConfig,
    RoutingContext,
    load_evidence,
)
from app.secrets_store import get_secret_store
from app.services.execution_service import AgentExecutionService
from tests.conftest import (
    make_artifact_with_content,
    make_evaluation_definition_version,
    make_model,
    make_project,
    make_provider,
    make_provider_model,
    make_task,
    make_task_run,
)
from tests.ma7_4b_support import new_worker
from tests.test_execution_service import FakeAdapter, _factory_for
from tests.test_ma7_5a_evaluation_node import build_harness, evaluation_run_of, run_to_waiting_gate
from tests.test_ma7_5b_parallel_evaluations import drain
from tests.test_ma7_6b_retry_recovery import add_replacement_model, failed_flagship, node_run_row

ENGINEER = "software_engineer"
PLANNER = "planner"
REAL_DB = settings.database_path


@pytest.fixture(autouse=True)
def artifacts_dir(monkeypatch, tmp_path):
    path = tmp_path / "artifacts"
    monkeypatch.setattr(settings, "artifacts_dir", path)
    return path


# -- catalog ---------------------------------------------------------------------


def _pm(db, canonical, cost_in, cost_out):
    return make_provider_model(
        db,
        model=make_model(db, canonical_model_id=canonical),
        provider=make_provider(db),
        cost_input_per_mtok=None if cost_in is None else Decimal(cost_in),
        cost_output_per_mtok=None if cost_out is None else Decimal(cost_out),
    )


def _free(db, canonical):
    return _pm(db, canonical, "0", "0")


def _paid(db, canonical, cost="1.00"):
    return _pm(db, canonical, cost, cost)


def _unknown(db, canonical):
    return _pm(db, canonical, None, None)


def _activate(db, **overrides):
    row = RouterPolicyVersion(version=1, scoring_config={**DEFAULT_V1_CONFIG, **overrides}, status="active")
    db.add(row)
    db.commit()
    return row


def _auto(policy):
    return {"mode": "auto", "auto_policy": policy.value}


# -- history -----------------------------------------------------------------------


class History:
    """Writes the same rows real executions/evaluations produce."""

    def __init__(self, db, project=None):
        self.db = db
        self.project = project or make_project(db)
        self.task = make_task(db, project=self.project)
        self._versions = {}
        self._criteria = None
        self._definition_version = None

    def context(self, role=ENGINEER):
        return RoutingContext(project_id=self.project.id, agent_role=role)

    def agent_version(self, role):
        if role not in self._versions:
            agent = Agent(project_id=self.project.id, name=f"{role} agent", role=role)
            self.db.add(agent)
            self.db.flush()
            version = AgentVersion(
                agent_id=agent.id, version=1, name=agent.name, role=role, status=VersionStatus.ACTIVE
            )
            self.db.add(version)
            self.db.flush()
            self._versions[role] = version
        return self._versions[role]

    def _criterion(self, index):
        if self._definition_version is None:
            self._definition_version = make_evaluation_definition_version(
                self.db, status=VersionStatus.ACTIVE
            )
            self._criteria = list(self._definition_version.criteria)
        while len(self._criteria) <= index:
            criterion = EvaluationCriterion(
                evaluation_definition_version_id=self._definition_version.id,
                key=f"c{len(self._criteria)}",
                label=f"Criterion {len(self._criteria)}",
                order_index=len(self._criteria),
            )
            self.db.add(criterion)
            self.db.flush()
            self._criteria.append(criterion)
        return self._criteria[index]

    def call(
        self,
        pm,
        *,
        role=ENGINEER,
        outcome="success",
        days_ago=1,
        findings=None,
        latency_ms=100,
    ):
        """One Agent Run with one ModelCall. ``outcome``: success, a provider
        error category, or 'timeout'. ``findings`` evaluates the run's
        artifact (a COMPLETED Evaluation Run with one result per finding)."""
        task_run = make_task_run(self.db, task=self.task)
        agent_run = AgentRun(
            task_run_id=task_run.id,
            agent_version_id=self.agent_version(role).id,
            status=AgentRunStatus.COMPLETED if outcome == "success" else AgentRunStatus.FAILED,
        )
        self.db.add(agent_run)
        self.db.flush()
        started = datetime.now(timezone.utc) - timedelta(days=days_ago)
        call = ModelCall(
            agent_run_id=agent_run.id,
            model_id=pm.model_id,
            provider_id=pm.provider_id,
            provider_model_id=pm.id,
            started_at=started,
            completed_at=started,
        )
        if outcome == "success":
            call.status = ModelCallStatus.SUCCESS
            call.latency_ms, call.tokens_in, call.tokens_out = latency_ms, 100, 50
            call.cost_amount, call.cost_is_estimated = Decimal("0.01"), True
        elif outcome == "timeout":
            call.status = ModelCallStatus.TIMEOUT
            call.error = {"category": "provider_timeout", "message": "slow"}
        else:
            call.status = ModelCallStatus.ERROR
            call.error = {"category": outcome, "message": "no choices"}
        self.db.add(call)
        self.db.flush()
        if findings:
            self.evaluate(agent_run, findings)
        self.db.commit()
        return agent_run

    def evaluate(self, agent_run, findings, *, status=EvaluationRunStatus.COMPLETED, content="artifact"):
        digest = hashlib.sha256(content.encode()).hexdigest()
        from app.models.artifacts_eval import Artifact

        artifact = Artifact(
            agent_run_id=agent_run.id, type="report", storage_ref="unused", content_hash=digest
        )
        self.db.add(artifact)
        self.db.flush()
        self._criterion(len(findings) - 1)
        run = EvaluationRun(
            subject_agent_run_id=agent_run.id,
            subject_artifact_id=artifact.id,
            subject_artifact_content_hash=digest,
            evaluation_definition_version_id=self._definition_version.id,
            method=EvaluationMethod.DETERMINISTIC,
            status=status,
        )
        self.db.add(run)
        self.db.flush()
        for index, finding in enumerate(findings):
            criterion = self._criteria[index]
            self.db.add(
                EvaluationCriterionResult(
                    evaluation_run_id=run.id,
                    evaluation_criterion_id=criterion.id,
                    criterion_key=criterion.key,
                    order_index=index,
                    finding=finding,
                    rationale="RATIONALE-TEXT-MUST-NOT-LEAK",
                )
            )
        self.db.flush()
        return run

    def many(self, pm, n, **kwargs):
        for _ in range(n):
            self.call(pm, **kwargs)


MET, PARTIAL, NOT_MET, NA = (
    EvaluationFinding.MET,
    EvaluationFinding.PARTIAL,
    EvaluationFinding.NOT_MET,
    EvaluationFinding.NOT_APPLICABLE,
)


def _route(db, history, policy, role=ENGINEER):
    return route(db, _auto(policy), context=history.context(role))


def _profiles(db, history, pms, role=ENGINEER, **overrides):
    config = EvidenceConfig.from_json({**DEFAULT_V1_CONFIG, **overrides})
    return load_evidence(
        db, context=history.context(role), provider_model_ids=[p.id for p in pms], config=config
    )


# =============================================================================
# fallback / cold start / thresholds
# =============================================================================


def test_insufficient_history_falls_back_to_ma8_1_order(db):
    a, b = _paid(db, "aaa/cheap"), _paid(db, "bbb/other")
    history = History(db)
    history.many(b, 4, findings=[MET])  # below both minimums: 5 calls / 5 evaluated runs
    _activate(db, min_evaluated_runs=5)
    decision = _route(db, history, RouterFreePolicy.ANY)
    assert decision.selected.provider_model.id == a.id
    assert decision.evidence["status"] == EVIDENCE_INSUFFICIENT
    assert decision.strategy == ROUTING_STRATEGY
    assert "MA8.1 order was used" in decision.reason


def test_minimum_evidence_threshold(db):
    a, b = _paid(db, "aaa/flaky"), _paid(db, "bbb/steady")
    history = History(db)
    history.many(a, 4, outcome="provider_invalid_response")
    _activate(db)
    below = _route(db, history, RouterFreePolicy.ANY)
    assert below.evidence["status"] == EVIDENCE_INSUFFICIENT and below.selected.provider_model.id == a.id

    history.call(a, outcome="provider_invalid_response")  # 5th observation reaches min_observations
    at = _route(db, history, RouterFreePolicy.ANY)
    assert at.evidence["status"] == EVIDENCE_APPLIED
    assert at.selected.provider_model.id == b.id
    assert at.evidence["evidence_changed_selection"] is True


def test_new_models_remain_eligible_during_cold_start(db):
    old = _paid(db, "aaa/known-bad")
    new = _paid(db, "zzz/brand-new")
    history = History(db)
    history.many(old, 3, findings=[NOT_MET])
    _activate(db)
    decision = _route(db, history, RouterFreePolicy.ANY)
    # The new model has no history at all: neutral, eligible, and preferred
    # over a sufficiently-evidenced negative one.
    assert decision.selected.provider_model.id == new.id
    assert [v.provider_model_id for v in decision.eligible] == [new.id, old.id]


def test_history_window_bounds_the_evidence(db):
    a, b = _paid(db, "aaa/was-flaky"), _paid(db, "bbb/other")
    history = History(db)
    history.many(a, 6, outcome="provider_timeout", days_ago=40)
    history.many(a, 6, outcome="provider_timeout", days_ago=10)
    assert _profiles(db, history, [a])[a.id].provider_failures == 6  # 30-day default window
    assert _profiles(db, history, [a], history_window_days=7)[a.id].observations == 0

    _activate(db, history_window_days=7)
    decision = _route(db, history, RouterFreePolicy.ANY)
    assert decision.selected.provider_model.id == a.id
    assert decision.evidence["status"] == EVIDENCE_INSUFFICIENT
    assert b.id != decision.selected.provider_model.id


def test_evidence_query_failure_falls_back_safely(db, monkeypatch):
    a, b = _paid(db, "aaa/one"), _paid(db, "bbb/two")
    history = History(db)
    history.many(a, 6, outcome="provider_timeout")
    _activate(db)

    def boom(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr("app.model_resolution.load_evidence", boom)
    decision = _route(db, history, RouterFreePolicy.ANY)
    assert decision.selected.provider_model.id == a.id  # MA8.1 pick, evidence ignored
    assert decision.evidence["status"] == EVIDENCE_UNAVAILABLE
    assert decision.evidence["error"] == "RuntimeError" and "database is locked" not in json.dumps(
        decision.evidence
    )
    assert decision.strategy == ROUTING_STRATEGY
    assert b.id in [v.provider_model_id for v in decision.eligible]


def test_unsupported_active_policy_config_falls_back_safely(db):
    a = _paid(db, "aaa/one")
    history = History(db)
    db.add(RouterPolicyVersion(version=1, scoring_config={"strategy": "future-v9"}, status="active"))
    db.commit()
    decision = _route(db, history, RouterFreePolicy.ANY)
    assert decision.selected.provider_model.id == a.id
    assert decision.evidence["status"] == EVIDENCE_UNAVAILABLE


def test_ma8_1_behavior_is_unchanged_when_evidence_routing_is_not_enabled(db):
    _free(db, "zzz/free")
    _paid(db, "aaa/paid")
    history = History(db)
    history.many(_paid(db, "bbb/other"), 6, outcome="provider_timeout")
    for policy in RouterFreePolicy:
        plain = route(db, _auto(policy))
        with_context = _route(db, history, policy)
        assert plain.evidence is None and with_context.evidence == {"status": EVIDENCE_DISABLED}
        assert [v.provider_model_id for v in with_context.eligible] == [
            v.provider_model_id for v in plain.eligible
        ]
        assert with_context.reason == plain.reason
        assert with_context.strategy == plain.strategy == ROUTING_STRATEGY
        assert with_context.router_policy_version_id is None


def test_inactive_policy_keeps_ma8_1_until_activated_and_deactivation_restores_it(db):
    """The migration seeds v1 INACTIVE: routing stays MA8.1 while history keeps
    accumulating; explicit activation turns evidence routing on, deactivation
    turns it off again. MANUAL is unaffected throughout."""
    flaky, steady = _paid(db, "aaa/flaky"), _paid(db, "bbb/steady")
    history = History(db)
    history.many(flaky, 6, outcome="provider_timeout")
    row = RouterPolicyVersion(version=1, scoring_config=dict(DEFAULT_V1_CONFIG), status="inactive")
    db.add(row)
    db.commit()
    manual = {"mode": "manual", "manual_provider_model_id": flaky.id}
    baseline = route(db, _auto(RouterFreePolicy.ANY))

    def check_ma8_1():
        decision = _route(db, history, RouterFreePolicy.ANY)
        assert decision.selected.provider_model.id == flaky.id  # the MA8.1 pick
        assert (
            decision.evidence == {"status": EVIDENCE_DISABLED} and decision.router_policy_version_id is None
        )
        assert decision.reason == baseline.reason and decision.strategy == ROUTING_STRATEGY

    check_ma8_1()
    # History is still there to learn from while the policy is inactive.
    assert _profiles(db, history, [flaky])[flaky.id].provider_failures == 6

    row.status = "active"
    db.commit()
    active = _route(db, history, RouterFreePolicy.ANY)
    assert active.selected.provider_model.id == steady.id
    assert active.evidence["status"] == EVIDENCE_APPLIED and active.router_policy_version_id == row.id
    assert active.strategy == EVIDENCE_STRATEGY_V1

    row.status = "inactive"
    db.commit()
    check_ma8_1()

    for status in ("inactive", "active"):
        row.status = status
        db.commit()
        decision = route(db, manual, context=history.context())
        assert decision.selected.provider_model.id == flaky.id and decision.evidence is None


# =============================================================================
# policy boundaries / manual
# =============================================================================


def test_manual_selection_ignores_evidence(db):
    bad = _paid(db, "aaa/bad")
    _paid(db, "bbb/good")
    history = History(db)
    history.many(bad, 6, outcome="provider_timeout")
    _activate(db)
    decision = route(db, {"mode": "manual", "manual_provider_model_id": bad.id}, context=history.context())
    assert decision.selection_mode == ModelSelectionMode.MANUAL
    assert decision.selected.provider_model.id == bad.id
    assert decision.evidence is None and decision.router_policy_version_id is None


def test_free_only_cannot_be_overridden_by_evidence(db):
    free = _free(db, "zzz/free")
    paid = _paid(db, "aaa/paid-excellent")
    history = History(db)
    history.many(paid, 6, findings=[MET, MET])
    history.many(free, 3, findings=[PARTIAL])
    _activate(db)
    decision = _route(db, history, RouterFreePolicy.FREE_ONLY)
    assert decision.selected.provider_model.id == free.id
    assert paid.id in [v.provider_model_id for v in decision.excluded]


def test_prefer_free_keeps_free_ahead_of_better_evidenced_paid(db):
    free_a, free_b = _free(db, "aaa/free"), _free(db, "bbb/free")
    paid = _paid(db, "ccc/paid-excellent")
    history = History(db)
    history.many(paid, 6, findings=[MET])
    history.many(free_a, 3, findings=[NOT_MET])  # negative, but still FREE
    _activate(db)
    decision = _route(db, history, RouterFreePolicy.PREFER_FREE)
    order = [v.provider_model_id for v in decision.eligible]
    assert order == [free_b.id, free_a.id, paid.id]  # evidence reorders WITHIN free; paid stays after
    assert decision.free_preference_satisfied is True and decision.fallback_used is False


def test_prefer_free_paid_fallback_is_evidence_ordered_only_among_paid(db):
    cheap = _paid(db, "aaa/cheap", "0.10")
    pricier = _paid(db, "bbb/pricier", "5.00")
    history = History(db)
    history.many(cheap, 3, findings=[NOT_MET])
    history.many(pricier, 3, findings=[MET])
    _activate(db)
    decision = _route(db, history, RouterFreePolicy.PREFER_FREE)
    assert decision.selected.provider_model.id == pricier.id
    assert decision.fallback_used is True  # still the explicit PREFER_FREE paid fallback


def test_any_uses_evidence_ordering_but_unknown_stays_last_resort(db):
    cheap = _paid(db, "aaa/cheap", "0.10")
    pricier = _paid(db, "bbb/pricier", "5.00")
    unknown = _unknown(db, "ccc/unknown-excellent")
    history = History(db)
    history.many(cheap, 3, findings=[NOT_MET])
    history.many(pricier, 3, findings=[MET])
    history.many(unknown, 6, findings=[MET])
    _activate(db)
    decision = _route(db, history, RouterFreePolicy.ANY)
    assert [v.provider_model_id for v in decision.eligible] == [pricier.id, cheap.id, unknown.id]
    assert decision.strategy == EVIDENCE_STRATEGY_V1  # cheapest did not simply win


# =============================================================================
# role / project scope
# =============================================================================


def test_evidence_is_role_specific_planner_history_is_not_engineer_evidence(db):
    a, b = _paid(db, "aaa/one"), _paid(db, "bbb/two")
    history = History(db)
    history.many(a, 6, role=PLANNER, outcome="provider_timeout")
    history.many(b, 3, role=PLANNER, findings=[MET])
    _activate(db)
    planner = _route(db, history, RouterFreePolicy.ANY, role=PLANNER)
    engineer = _route(db, history, RouterFreePolicy.ANY, role=ENGINEER)
    assert planner.selected.provider_model.id == b.id and planner.evidence["status"] == EVIDENCE_APPLIED
    assert (
        engineer.selected.provider_model.id == a.id and engineer.evidence["status"] == EVIDENCE_INSUFFICIENT
    )
    assert planner.evidence["agent_role"] == PLANNER and engineer.evidence["agent_role"] == ENGINEER


def test_evidence_never_crosses_projects(db):
    a, b = _paid(db, "aaa/one"), _paid(db, "bbb/two")
    other = History(db, make_project(db, name="Other"))
    other.many(a, 6, outcome="provider_timeout")
    mine = History(db)
    _activate(db)
    decision = _route(db, mine, RouterFreePolicy.ANY)
    assert decision.selected.provider_model.id == a.id
    assert decision.evidence["status"] == EVIDENCE_INSUFFICIENT and decision.evidence["profiles"] == []
    assert b.id != decision.selected.provider_model.id


# =============================================================================
# quality evidence
# =============================================================================


def test_met_is_positive_partial_is_distinct_and_not_met_is_negative(db):
    met, partial, not_met = _paid(db, "a/met"), _paid(db, "b/partial"), _paid(db, "c/not-met")
    history = History(db)
    history.many(met, 3, findings=[MET, MET])
    history.many(partial, 3, findings=[PARTIAL, PARTIAL])
    history.many(not_met, 3, findings=[NOT_MET, MET])
    config = EvidenceConfig.from_json(DEFAULT_V1_CONFIG)
    profiles = _profiles(db, history, [met, partial, not_met])
    assert profiles[met.id].quality(config) == QUALITY_POSITIVE
    assert profiles[partial.id].quality(config) == QUALITY_MIXED
    assert profiles[not_met.id].quality(config) == QUALITY_NEGATIVE
    assert (profiles[partial.id].met, profiles[partial.id].partial) == (0, 6)

    _activate(db)
    order = [v.provider_model_id for v in _route(db, history, RouterFreePolicy.ANY).eligible]
    assert order == [met.id, partial.id, not_met.id]


def test_not_applicable_is_not_counted_as_failure(db):
    only_na, met_and_na = _paid(db, "a/na"), _paid(db, "b/met-na")
    history = History(db)
    history.many(only_na, 3, findings=[NA, NA])
    history.many(met_and_na, 3, findings=[MET, NA, NA, NA])
    config = EvidenceConfig.from_json(DEFAULT_V1_CONFIG)
    profiles = _profiles(db, history, [only_na, met_and_na])
    assert profiles[only_na.id].quality(config) == INSUFFICIENT  # never checked, not NOT_MET
    assert profiles[only_na.id].not_met == 0 and profiles[only_na.id].not_applicable == 6
    assert profiles[met_and_na.id].quality(config) == QUALITY_POSITIVE  # NA does not dilute MET


def test_incomplete_evaluation_runs_are_not_evidence(db):
    pm = _paid(db, "a/one")
    history = History(db)
    for status in (EvaluationRunStatus.FAILED, EvaluationRunStatus.CANCELLED, EvaluationRunStatus.RUNNING):
        run = history.call(pm)
        history.evaluate(run, [NOT_MET], status=status)
    db.commit()
    assert _profiles(db, history, [pm])[pm.id].evaluated_runs == 0


# =============================================================================
# reliability evidence
# =============================================================================


def test_provider_failure_is_reliability_evidence_not_quality_evidence(db):
    flaky = _paid(db, "a/flaky")
    history = History(db)
    history.many(flaky, 4, outcome="provider_invalid_response")
    history.many(flaky, 2, findings=[MET])
    history.call(flaky, outcome="provider_authentication_error")  # credential problem: not counted
    config = EvidenceConfig.from_json(DEFAULT_V1_CONFIG)
    profile = _profiles(db, history, [flaky])[flaky.id]
    assert (profile.provider_failures, profile.completed, profile.other_failures) == (4, 2, 1)
    assert profile.reliability(config) == RELIABILITY_DEMOTED
    assert profile.not_met == 0 and profile.quality(config) == INSUFFICIENT  # quality untouched


def test_one_isolated_failure_does_not_eliminate_a_model(db):
    mostly_ok, other = _paid(db, "aaa/mostly-ok"), _paid(db, "bbb/other")
    history = History(db)
    history.call(mostly_ok, outcome="provider_invalid_response")
    history.many(mostly_ok, 5)
    _activate(db)
    config = EvidenceConfig.from_json(DEFAULT_V1_CONFIG)
    assert _profiles(db, history, [mostly_ok])[mostly_ok.id].reliability(config) == RELIABILITY_OK
    decision = _route(db, history, RouterFreePolicy.ANY)
    assert decision.selected.provider_model.id == mostly_ok.id
    assert other.id in [v.provider_model_id for v in decision.eligible]

    # Even a demoted model stays eligible — it is ordered later, never excluded.
    history.many(mostly_ok, 6, outcome="provider_timeout")
    demoted = _route(db, history, RouterFreePolicy.ANY)
    assert demoted.selected.provider_model.id == other.id
    assert mostly_ok.id in [v.provider_model_id for v in demoted.eligible]


# =============================================================================
# decision record / explanation / execution integration
# =============================================================================


def _decisions(db, agent_run_id):
    stmt = select(ModelRoutingDecision).where(ModelRoutingDecision.agent_run_id == agent_run_id)
    return list(db.execute(stmt.order_by(ModelRoutingDecision.created_at)).scalars().all())


def _pending_run(history, policy, role=ENGINEER):
    run = AgentRun(
        task_run_id=make_task_run(history.db, task=history.task).id,
        agent_version_id=history.agent_version(role).id,
    )
    history.agent_version(role).model_policy = policy
    history.db.add(run)
    history.db.commit()
    return run


def test_execution_records_strategy_version_and_factual_explanation(db):
    cheap, good = _paid(db, "aaa/cheap", "1.00"), _paid(db, "bbb/good", "1.00")
    history = History(db)
    history.many(cheap, 6, outcome="provider_timeout")
    history.many(good, 3, findings=[MET, MET], latency_ms=250)
    policy_row = _activate(db)
    run = _pending_run(history, _auto(RouterFreePolicy.ANY))
    adapter = FakeAdapter(
        responses=[InvokeResponse(text="ok", tokens_in=1_000_000, tokens_out=500_000, latency_ms=9)]
    )
    AgentExecutionService(db, adapter_factory=_factory_for(adapter)).execute(run.id, worker_id="w")

    assert adapter.calls[0].provider_model_id == "bbb/good"
    (row,) = _decisions(db, run.id)
    assert row.routing_strategy == EVIDENCE_STRATEGY_V1
    assert row.router_policy_version_id == policy_row.id
    evidence = row.details["evidence"]
    assert evidence["status"] == EVIDENCE_APPLIED and evidence["router_policy_version"] == 1
    assert evidence["config"]["min_observations"] == 5
    assert evidence["deterministic_pick_provider_model_id"] == cheap.id
    profiles = {p["provider_model_id"]: p for p in evidence["profiles"]}
    assert profiles[cheap.id]["provider_failures"] == 6 and profiles[cheap.id]["reliability"] == "demoted"
    assert profiles[good.id]["findings"]["met"] == 6 and profiles[good.id]["median_latency_ms"] == 250

    text = row.rationale
    assert "ma8.2-evidence-v1" in text and "role='software_engineer'" in text
    assert "3 observed call(s) (3 completed, 0 provider failure(s))" in text
    assert "3 evaluated run(s) (MET 6, PARTIAL 0, NOT_MET 0" in text
    assert "would have selected 'aaa/cheap'" in text and "6 provider failure(s)" in text
    for claim in ("best", "highest quality", "most reliable"):
        assert claim not in text.lower()

    # token/cost accounting is exactly MA3's: 1M in * $1 + 0.5M out * $1, estimated
    (call,) = db.execute(select(ModelCall).where(ModelCall.agent_run_id == run.id)).scalars().all()
    assert call.cost_amount == Decimal("1.50") and call.cost_is_estimated is True
    assert (call.tokens_in, call.tokens_out, call.latency_ms) == (1_000_000, 500_000, 9)
    assert call.model_routing_decision_id == row.id


def test_explanation_contains_no_secrets_prompts_or_artifact_content(db, tmp_path):
    pm = _paid(db, "aaa/one")
    history = History(db)
    history.task.description = "PROMPT-TEXT-MUST-NOT-LEAK"
    for _ in range(3):
        run = history.call(pm)
        make_artifact_with_content(db, tmp_path, agent_run=run, content="ARTIFACT-TEXT-MUST-NOT-LEAK")
        history.evaluate(run, [MET], content="ARTIFACT-TEXT-MUST-NOT-LEAK")
    get_secret_store().set_secret("secret-ref", "sk-or-v1-SECRET-MUST-NOT-LEAK")
    db.commit()
    _activate(db)
    run = _pending_run(history, _auto(RouterFreePolicy.ANY))
    AgentExecutionService(
        db, adapter_factory=_factory_for(FakeAdapter(responses=[InvokeResponse(text="x")]))
    ).execute(run.id, worker_id="w")
    (row,) = _decisions(db, run.id)
    assert row.details["evidence"]["status"] == EVIDENCE_APPLIED
    blob = json.dumps([row.details, row.eligible_candidates, row.rationale])
    for forbidden in ("MUST-NOT-LEAK", "secret-ref", "storage_ref"):
        assert forbidden not in blob


def test_retries_remain_separate_observations(db):
    pm = _paid(db, "aaa/one")
    history = History(db)
    _activate(db)
    run = _pending_run(history, {"mode": "manual", "manual_provider_model_id": pm.id})
    adapter = FakeAdapter(
        responses=[ProviderTimeoutError("slow"), InvokeResponse(text="ok", tokens_in=1, tokens_out=1)]
    )
    AgentExecutionService(db, adapter_factory=_factory_for(adapter)).execute(run.id, worker_id="w")
    assert len(_decisions(db, run.id)) == 2  # one decision per attempt
    profile = _profiles(db, history, [pm])[pm.id]
    assert (profile.observations, profile.completed, profile.provider_failures) == (2, 1, 1)


def test_tests_use_a_disposable_database(db):
    assert str(REAL_DB) not in str(db.get_bind().url)
    assert "multi_agent_platform.db" not in str(db.get_bind().url)


# =============================================================================
# MA7 recovery + evaluation nodes with evidence routing active
# =============================================================================


def test_evaluation_node_evaluator_uses_the_same_router_with_evidence_active(
    db, session_factory, bootstrap, monkeypatch
):
    _activate(db)
    harness = build_harness(db, bootstrap.project, monkeypatch, "m82e", override_model=True)
    run_to_waiting_gate(db, session_factory, harness)
    evaluation = evaluation_run_of(session_factory)
    (row,) = _decisions(db, evaluation.evaluator_agent_run_id)
    assert row.selection_mode == ModelSelectionMode.MANUAL  # the frozen override, not evidence
    assert row.details["selected_provider_model_id"] == harness.setup.override["manual_provider_model_id"]
    assert row.router_policy_version_id is None and row.details["evidence"] is None


def test_failed_node_manual_replacement_stays_authoritative_with_evidence_active(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch
):
    harness = failed_flagship(db, session_factory, monkeypatch, bootstrap, "m82r")
    _activate(db)
    original = node_run_row(session_factory, harness.run.id, "code")
    replacement = add_replacement_model(db, harness, "code", "m82r")
    response = client.post(
        f"/workflow-runs/{harness.run.id}/nodes/{original.id}/retry",
        headers=auth_headers,
        json={"replacement_provider_model_id": replacement.id},
    )
    assert response.status_code == 200, response.text
    drain(new_worker(session_factory))

    retried = node_run_row(session_factory, harness.run.id, "code")
    assert retried.status == WorkflowNodeRunStatus.COMPLETED and retried.workflow_run_id == harness.run.id
    (row,) = _decisions(db, retried.agent_run_id)
    assert row.selection_mode == ModelSelectionMode.MANUAL
    assert row.details["selected_provider_model_id"] == replacement.id
    assert row.details["evidence"] is None and row.router_policy_version_id is None
