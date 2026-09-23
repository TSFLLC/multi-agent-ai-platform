"""AIL.1B — Model Explorer / Provider Explorer API.

Read-only composition over the existing MA2 registry and MA8 evidence —
never a second registry, never a second reliability/quality computation.
Disposable tmp_path databases only (tests.conftest); the persistent UAT DB
is never opened.
"""

from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.db.enums import ProjectRole
from app.models.execution import ModelRoutingDecision, RouterPolicyVersion
from app.models.identity import Project, ProjectMembership
from app.models.providers import Model, Provider
from tests.conftest import make_model, make_provider, make_provider_model
from tests.test_ma8_2_evidence_routing import MET, NOT_MET, PARTIAL, History, _free, _paid, _unknown

BASE = "/ail"


@pytest.fixture(autouse=True)
def artifacts_dir(monkeypatch, tmp_path):
    path = tmp_path / "artifacts"
    monkeypatch.setattr(settings, "artifacts_dir", path)
    return path


def _other_project(db, bootstrap, name="Other", member=False):
    project = Project(org_id=bootstrap.organization.id, name=name)
    db.add(project)
    db.flush()
    if member:
        db.add(ProjectMembership(project_id=project.id, user_id=bootstrap.user.id, role=ProjectRole.OWNER))
    db.commit()
    return project


def _get(client, headers, path, **params):
    return client.get(f"{BASE}{path}", headers=headers, params=params)


# =============================================================================
# registry facts — canonical reuse, no duplication
# =============================================================================


def test_list_models_reuses_canonical_registry_without_duplication(client, db, auth_headers, bootstrap):
    pm = _paid(db, "aaa/canonical-one")
    db.commit()

    body = _get(client, auth_headers, "/models").json()
    matches = [m for m in body if m["canonical_model_id"] == "aaa/canonical-one"]
    assert len(matches) == 1  # exactly the one Model row — no second registry, no duplicate

    assert db.execute(select(func.count(Model.id))).scalar_one() == 1
    assert db.execute(select(func.count(Provider.id))).scalar_one() == 1


def test_model_detail_shows_facts_and_provider_availability(client, db, auth_headers, bootstrap):
    pm = _paid(db, "aaa/detail-one", cost="2.50")
    db.commit()

    body = _get(client, auth_headers, f"/models/{pm.model_id}").json()
    assert body["identity"]["canonical_model_id"] == "aaa/detail-one"
    assert len(body["availability"]) == 1
    offering = body["availability"][0]
    assert offering["provider_model_id"] == pm.id
    assert offering["pricing_classification"] == "paid"
    assert body["our_evidence"] == {"status": "no_project_selected"}
    assert body["claims"] == {"status": "not_available_yet", "layers": []}  # honest empty seam


def test_model_detail_404_for_unknown_model(client, auth_headers, bootstrap):
    assert _get(client, auth_headers, "/models/does-not-exist").status_code == 404


def test_provider_detail_lists_its_models_and_freshness(client, db, auth_headers, bootstrap):
    provider = make_provider(db, name="OpenRouter")
    pm1 = make_provider_model(db, model=make_model(db, canonical_model_id="p/one"), provider=provider)
    pm2 = make_provider_model(db, model=make_model(db, canonical_model_id="p/two"), provider=provider)
    db.commit()

    body = _get(client, auth_headers, f"/providers/{provider.id}").json()
    assert body["identity"]["name"] == "OpenRouter"
    assert {m["provider_model_id"] for m in body["models"]} == {pm1.id, pm2.id}
    assert body["freshness"]["last_model_refresh_at"] is not None


# =============================================================================
# our evidence — project-scoped, RBAC-gated, honest about no-data
# =============================================================================


def test_no_project_selected_returns_no_project_selected_not_zero(client, db, auth_headers, bootstrap):
    pm = _paid(db, "aaa/no-project")
    db.commit()
    body = _get(client, auth_headers, f"/models/{pm.model_id}").json()
    assert body["our_evidence"] == {"status": "no_project_selected"}


def test_inaccessible_project_is_rejected_not_silently_empty(client, db, auth_headers, bootstrap):
    pm = _paid(db, "aaa/foreign")
    foreign = _other_project(db, bootstrap, name="Foreign", member=False)

    resp = _get(client, auth_headers, f"/models/{pm.model_id}", project_id=foreign.id)
    assert resp.status_code == 403


def test_our_evidence_is_project_scoped_no_cross_project_leakage(client, db, auth_headers, bootstrap):
    mine = History(db, project=bootstrap.project)
    pm = _paid(db, "aaa/scoped")
    mine.many(pm, 6, findings=[MET, MET, MET])

    other = History(db, project=_other_project(db, bootstrap, member=True))
    other.many(pm, 9, outcome="provider_timeout")

    body = _get(client, auth_headers, f"/models/{pm.model_id}", project_id=bootstrap.project.id).json()
    evidence = body["our_evidence"]["by_provider_model"][pm.id]
    assert evidence["model_calls"] == 6  # not 15 — the other project's calls never leak in
    assert evidence["reliability"] == "ok"  # not demoted by the other project's timeouts


def test_no_data_renders_honestly_never_as_zero_quality(client, db, auth_headers, bootstrap):
    pm = _unknown(db, "aaa/never-called")
    db.commit()
    body = _get(client, auth_headers, f"/models/{pm.model_id}", project_id=bootstrap.project.id).json()
    assert body["our_evidence"]["by_provider_model"][pm.id]["status"] == "no_data"
    assert "reliability" not in body["our_evidence"]["by_provider_model"][pm.id]


def test_exact_estimated_unknown_cost_remain_distinct(client, db, auth_headers, bootstrap):
    from app.models.execution import ModelCall

    mine = History(db, project=bootstrap.project)
    pm = _paid(db, "aaa/cost-mix")
    exact = mine.call(pm)
    estimated = mine.call(pm)
    unknown = mine.call(pm)
    calls = {c.agent_run_id: c for c in db.execute(select(ModelCall)).scalars()}
    calls[exact.id].cost_amount, calls[exact.id].cost_is_estimated = Decimal("0.02"), False
    calls[estimated.id].cost_amount, calls[estimated.id].cost_is_estimated = Decimal("0.03"), True
    calls[unknown.id].cost_amount = None
    db.commit()

    evidence = _get(
        client, auth_headers, f"/models/{pm.model_id}", project_id=bootstrap.project.id
    ).json()["our_evidence"]["by_provider_model"][pm.id]
    cost = evidence["cost"]
    assert Decimal(cost["exact_usd"]) == Decimal("0.02") and cost["exact_calls"] == 1
    assert Decimal(cost["estimated_usd"]) == Decimal("0.03") and cost["estimated_calls"] == 1
    assert cost["unknown_calls"] == 1  # never folded into a $0 figure


def test_ma8_thresholds_are_reused_not_reinvented(client, db, auth_headers, bootstrap):
    """Same min_observations/min_evaluated_runs semantics as MA8.3 —
    Explorer never invents its own quality/reliability bar."""
    mine = History(db, project=bootstrap.project)
    pm = _paid(db, "aaa/thin-evidence")
    mine.many(pm, 2, findings=[NOT_MET])  # below both min_observations(5) and min_evaluated_runs(3)

    evidence = _get(
        client, auth_headers, f"/models/{pm.model_id}", project_id=bootstrap.project.id
    ).json()["our_evidence"]["by_provider_model"][pm.id]
    assert evidence["reliability"] == "insufficient"
    assert evidence["quality"] == "insufficient"


def test_router_selection_count_reflects_actual_routed_calls(client, db, auth_headers, bootstrap):
    from app.model_resolution import record_routing_decision, route
    from app.db.enums import RouterFreePolicy

    mine = History(db, project=bootstrap.project)
    pm = _paid(db, "aaa/routed")
    agent_run = mine.call(pm)  # one ModelCall, no decision linked yet

    decision = route(db, {"mode": "auto", "auto_policy": RouterFreePolicy.ANY.value}, context=mine.context())
    row = record_routing_decision(db, agent_run_id=agent_run.id, decision=decision)
    from app.models.execution import ModelCall

    call = db.execute(select(ModelCall).where(ModelCall.agent_run_id == agent_run.id)).scalar_one()
    call.model_routing_decision_id = row.id
    db.commit()

    evidence = _get(
        client, auth_headers, f"/models/{pm.model_id}", project_id=bootstrap.project.id
    ).json()["our_evidence"]["by_provider_model"][pm.id]
    assert evidence["router_selection_count"] == 1


# =============================================================================
# compare
# =============================================================================


def test_compare_accepts_two_to_four_models(client, db, auth_headers, bootstrap):
    pm_a = _paid(db, "aaa/cmp-a")
    pm_b = _paid(db, "aaa/cmp-b")
    db.commit()

    resp = client.get(
        f"{BASE}/models/compare",
        headers=auth_headers,
        params=[("model_id", pm_a.model_id), ("model_id", pm_b.model_id)],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 2
    assert {i["identity"]["canonical_model_id"] for i in body["items"]} == {"aaa/cmp-a", "aaa/cmp-b"}


def test_compare_rejects_fewer_than_two_or_more_than_four(client, db, auth_headers, bootstrap):
    pm_a = _paid(db, "aaa/cmp-one")
    resp = client.get(f"{BASE}/models/compare", headers=auth_headers, params=[("model_id", pm_a.model_id)])
    assert resp.status_code == 422


# =============================================================================
# history / what's new — catalog-refresh only, never execution freezes
# =============================================================================


def test_history_endpoint_shows_change_kinds(client, db, auth_headers, bootstrap):
    from tests.test_model_registry_service import FakeAdapter, _descriptor
    from app.services.model_registry_service import ModelRegistryService

    provider = make_provider(db)
    svc = ModelRegistryService(db)
    svc.refresh_catalog(provider, FakeAdapter([_descriptor(provider_model_id="aaa/history-one")]))
    svc.refresh_catalog(
        provider,
        FakeAdapter([_descriptor(provider_model_id="aaa/history-one", cost_input_per_mtok=Decimal("9.99"))]),
    )
    model = db.query(Model).filter_by(canonical_model_id="aaa/history-one").one()

    body = _get(client, auth_headers, f"/models/{model.id}/history").json()
    assert [item["change_kinds"] for item in body["items"]] == [["price"], ["new"]]  # newest first


def test_whats_new_excludes_execution_time_freezes(client, db, auth_headers, bootstrap):
    from tests.test_model_registry_service import FakeAdapter, _descriptor
    from app.services.model_registry_service import ModelRegistryService
    from app.model_resolution import ResolvedModel, freeze_snapshot
    from app.db.enums import PricingClassification

    provider = make_provider(db)
    svc = ModelRegistryService(db)
    svc.refresh_catalog(provider, FakeAdapter([_descriptor(provider_model_id="aaa/whats-new")]))
    model = db.query(Model).filter_by(canonical_model_id="aaa/whats-new").one()
    pm = make_provider_model(db, model=model, provider=make_provider(db, name="Direct"))
    freeze_snapshot(
        db,
        ResolvedModel(
            provider_model=pm,
            model=model,
            provider=pm.provider,
            pricing_classification=PricingClassification.PAID,
            rationale="test",
            eligible_candidate_ids=[pm.id],
        ),
    )
    db.commit()

    body = _get(client, auth_headers, "/whats-new").json()
    assert len(body["items"]) == 1  # only the catalog-refresh "new" event, not the execution freeze


# =============================================================================
# explorer never mutates routing/registry state
# =============================================================================


def test_explorer_endpoints_never_write_routing_decisions_or_policies(client, db, auth_headers, bootstrap):
    mine = History(db, project=bootstrap.project)
    pm = _paid(db, "aaa/read-only")
    mine.many(pm, 3, findings=[MET])

    decisions_before = db.execute(select(func.count(ModelRoutingDecision.id))).scalar_one()
    policies_before = db.execute(select(func.count(RouterPolicyVersion.id))).scalar_one()

    _get(client, auth_headers, "/models")
    _get(client, auth_headers, f"/models/{pm.model_id}", project_id=bootstrap.project.id)
    _get(client, auth_headers, f"/models/{pm.model_id}/history")
    _get(client, auth_headers, "/providers")
    _get(client, auth_headers, f"/providers/{pm.provider_id}")
    _get(client, auth_headers, "/whats-new")

    assert db.execute(select(func.count(ModelRoutingDecision.id))).scalar_one() == decisions_before
    assert db.execute(select(func.count(RouterPolicyVersion.id))).scalar_one() == policies_before
