"""MA8.3 — Model Intelligence API.

Project-scoped, bounded read views over model_calls / evaluation runs /
model_routing_decisions, and the platform-admin evidence-routing on/off
control over router_policy_versions. Disposable ``tmp_path`` databases only
(tests.conftest); the persistent UAT DB is never opened.
"""

import json
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import sessionmaker

from alembic import command
from app.config import settings
from app.db.enums import (
    AgentRunStatus,
    ModelCallStatus,
    ModelStatus,
    OrgRole,
    ProjectRole,
    RouterFreePolicy,
)
from app.db.session import build_engine
from app.model_resolution import record_routing_decision, route
from app.models.execution import ModelCall, ModelRoutingDecision, RouterPolicyVersion
from app.models.identity import Project, ProjectMembership
from app.models.tasks import AgentRun
from app.routing_evidence import DEFAULT_V1_CONFIG, EVIDENCE_STRATEGY_V1, load_active_policy
from app.secrets_store import get_secret_store
from app.services.model_intelligence_service import ModelIntelligenceService
from tests.conftest import make_task_run
from tests.test_ma8_2_evidence_routing import MET, NOT_MET, PARTIAL, History, _free, _paid, _unknown
from tests.test_migration_ma8_1 import REAL_DB, _cfg

BASE = "/model-intelligence"


@pytest.fixture(autouse=True)
def artifacts_dir(monkeypatch, tmp_path):
    path = tmp_path / "artifacts"
    monkeypatch.setattr(settings, "artifacts_dir", path)
    return path


def _v1(db, status="inactive"):
    row = RouterPolicyVersion(version=1, scoring_config=dict(DEFAULT_V1_CONFIG), status=status)
    db.add(row)
    db.commit()
    return row


def _other_project(db, bootstrap, name="Other", member=False):
    project = Project(org_id=bootstrap.organization.id, name=name)
    db.add(project)
    db.flush()
    if member:
        db.add(ProjectMembership(project_id=project.id, user_id=bootstrap.user.id, role=ProjectRole.OWNER))
    db.commit()
    return project


def _get(client, headers, path, project_id, **params):
    params = {"project_id": project_id, **params}
    return client.get(f"{BASE}{path}", headers=headers, params=params)


def _decide(db, history, policy, role="software_engineer"):
    """A real routing decision (MA8.1 or MA8.2 depending on the policy
    state), persisted for a real Agent Run in ``history``'s project."""
    run = AgentRun(
        task_run_id=make_task_run(db, task=history.task).id,
        agent_version_id=history.agent_version(role).id,
        status=AgentRunStatus.COMPLETED,
    )
    db.add(run)
    db.flush()
    decision = route(db, policy, context=history.context(role))
    row = record_routing_decision(db, agent_run_id=run.id, decision=decision)
    db.commit()
    return row


AUTO_ANY = {"mode": "auto", "auto_policy": RouterFreePolicy.ANY.value}


# =============================================================================
# evidence policy status / control
# =============================================================================


def test_evidence_policy_reports_inactive_after_migration(tmp_path, monkeypatch):
    db_path = tmp_path / "ma83.db"
    monkeypatch.setattr(settings, "database_path", db_path)
    assert Path(settings.database_path).resolve() != REAL_DB.resolve()
    engine = build_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        command.upgrade(_cfg(), "head")
        session = sessionmaker(bind=engine)()
        try:
            status = ModelIntelligenceService(session).policy_status()
            assert status["installed"] is True and status["enabled"] is False
            assert status["version"] == 1 and status["strategy"] == EVIDENCE_STRATEGY_V1
            assert (
                status["history_window_days"],
                status["min_observations"],
                status["min_evaluated_runs"],
            ) == (
                30,
                5,
                3,
            )
            assert status["active_versions"] == 0
        finally:
            session.close()
    finally:
        engine.dispose()


def test_policy_status_when_nothing_is_installed(client, auth_headers, bootstrap):
    body = client.get(f"{BASE}/evidence-policy", headers=auth_headers).json()
    assert body == {"installed": False, "enabled": False, "active_versions": 0}
    refused = client.post(f"{BASE}/evidence-policy", headers=auth_headers, json={"enabled": True})
    assert refused.status_code == 409  # never creates a policy version


def test_admin_can_enable_and_disable_without_touching_history(client, db, auth_headers, bootstrap):
    row = _v1(db)
    history = History(db, project=bootstrap.project)
    a = _paid(db, "aaa/one")
    history.many(a, 3, findings=[MET])
    _decide(db, history, AUTO_ANY)

    def counts():
        return tuple(
            db.execute(select(func.count()).select_from(t)).scalar_one()
            for t in (ModelCall, ModelRoutingDecision, RouterPolicyVersion)
        )

    before = counts()
    on = client.post(f"{BASE}/evidence-policy", headers=auth_headers, json={"enabled": True})
    assert on.status_code == 200 and on.json()["enabled"] is True and on.json()["active_versions"] == 1
    db.expire_all()
    assert db.get(RouterPolicyVersion, row.id).status == "active"
    assert load_active_policy(db).router_policy_version_id == row.id

    again = client.post(f"{BASE}/evidence-policy", headers=auth_headers, json={"enabled": True})
    assert again.status_code == 200 and again.json()["active_versions"] == 1  # idempotent

    off = client.post(f"{BASE}/evidence-policy", headers=auth_headers, json={"enabled": False})
    assert off.status_code == 200 and off.json()["enabled"] is False and off.json()["active_versions"] == 0
    db.expire_all()
    assert load_active_policy(db) is None
    assert counts() == before  # no history deleted, no policy version created or removed

    audit = db.execute(text("SELECT event_type FROM audit_events ORDER BY occurred_at")).scalars().all()
    assert audit.count("router_policy.evidence_routing_enabled") == 1
    assert audit.count("router_policy.evidence_routing_disabled") == 1


@pytest.mark.parametrize("role", [OrgRole.MEMBER, OrgRole.VIEWER])
def test_non_admins_cannot_change_evidence_routing(client, db, auth_headers, bootstrap, role):
    row = _v1(db)
    bootstrap.user.role = role
    db.commit()
    response = client.post(f"{BASE}/evidence-policy", headers=auth_headers, json={"enabled": True})
    assert response.status_code == 403
    db.expire_all()
    assert db.get(RouterPolicyVersion, row.id).status == "inactive"
    assert (
        client.get(f"{BASE}/evidence-policy", headers=auth_headers).json()["enabled"] is False
    )  # still readable


def test_enabling_leaves_exactly_the_evidence_policy_active(client, db, auth_headers, bootstrap):
    v1 = _v1(db)
    legacy = RouterPolicyVersion(version=2, scoring_config={"strategy": "unsupported"}, status="active")
    db.add(legacy)
    db.commit()
    body = client.post(f"{BASE}/evidence-policy", headers=auth_headers, json={"enabled": True}).json()
    assert body["active_versions"] == 1 and body["router_policy_version_id"] == v1.id
    db.expire_all()
    assert db.get(RouterPolicyVersion, legacy.id).status == "inactive"
    assert db.execute(select(func.count(RouterPolicyVersion.id))).scalar_one() == 2


def test_manual_routing_is_unaffected_by_activation(client, db, auth_headers, bootstrap):
    _v1(db)
    history = History(db, project=bootstrap.project)
    flaky = _paid(db, "aaa/flaky")
    _paid(db, "bbb/steady")
    history.many(flaky, 6, outcome="provider_timeout")
    manual = {"mode": "manual", "manual_provider_model_id": flaky.id}
    before = route(db, manual, context=history.context())
    client.post(f"{BASE}/evidence-policy", headers=auth_headers, json={"enabled": True})
    db.expire_all()
    after = route(db, manual, context=history.context())
    assert before.selected.provider_model.id == after.selected.provider_model.id == flaky.id
    assert after.evidence is None and after.reason == before.reason
    assert (
        route(db, AUTO_ANY, context=history.context()).selected.provider_model.id != flaky.id
    )  # AUTO did change


# =============================================================================
# summary / roles / issues
# =============================================================================


def test_summary_is_project_scoped_and_cost_semantics_stay_distinct(client, db, auth_headers, bootstrap):
    mine = History(db, project=bootstrap.project)
    pm = _paid(db, "aaa/one")
    exact = mine.call(pm)
    estimated = mine.call(pm)
    unknown = mine.call(pm)
    mine.call(pm, outcome="provider_invalid_response")
    mine.call(pm, outcome="provider_authentication_error")
    calls = {c.agent_run_id: c for c in db.execute(select(ModelCall)).scalars()}
    calls[exact.id].cost_amount, calls[exact.id].cost_is_estimated = Decimal("0.02"), False
    calls[estimated.id].cost_amount, calls[estimated.id].cost_is_estimated = Decimal("0.03"), True
    calls[unknown.id].cost_amount = None
    db.commit()
    other = History(db, project=_other_project(db, bootstrap, member=True))
    other.many(pm, 7, outcome="provider_timeout")

    body = _get(client, auth_headers, "/summary", bootstrap.project.id).json()
    assert body["window_days"] == 30
    assert body["model_calls"] == 5 and body["completed_calls"] == 3 and body["failed_calls"] == 2
    assert body["reliability_issues"] == 1
    assert body["failures_by_category"] == {
        "provider_authentication_error": 1,
        "provider_invalid_response": 1,
    }
    assert body["tokens_in"] == 300 and body["tokens_out"] == 150 and body["tokens_total"] == 450
    cost = body["cost"]
    assert Decimal(cost["exact_usd"]) == Decimal("0.02") and cost["exact_calls"] == 1
    assert Decimal(cost["estimated_usd"]) == Decimal("0.03") and cost["estimated_calls"] == 1
    assert cost["unknown_calls"] == 1  # never folded into a $0 figure
    assert body["agent_runs"] == 5 and body["evidence_policy"]["installed"] is False


def test_summary_denies_projects_without_membership(client, db, auth_headers, bootstrap):
    foreign = _other_project(db, bootstrap, name="Foreign")
    for path in ("/summary", "/roles", "/issues", "/routing-decisions"):
        assert _get(client, auth_headers, path, foreign.id).status_code == 403, path


def test_role_evidence_is_role_specific_project_scoped_and_honest_about_insufficiency(
    client, db, auth_headers, bootstrap
):
    mine = History(db, project=bootstrap.project)
    good, thin = _paid(db, "aaa/good"), _free(db, "bbb/thin")
    mine.many(good, 3, role="planner", findings=[MET, MET, PARTIAL])
    mine.many(good, 3, role="planner")
    mine.many(good, 2, role="software_engineer", findings=[NOT_MET])
    mine.call(thin, role="software_engineer")
    History(db, project=_other_project(db, bootstrap, member=True)).many(
        good, 9, role="planner", findings=[NOT_MET]
    )

    body = _get(client, auth_headers, "/roles", bootstrap.project.id).json()
    assert body["thresholds"]["min_observations"] == 5 and body["thresholds"]["min_evaluated_runs"] == 3
    rows = {(e["agent_role"], e["canonical_model_id"]): e for e in body["entries"]}
    assert set(rows) == {
        ("planner", "aaa/good"),
        ("software_engineer", "aaa/good"),
        ("software_engineer", "bbb/thin"),
    }

    planner = rows[("planner", "aaa/good")]
    assert (planner["model_calls"], planner["completed_calls"], planner["evaluated_runs"]) == (6, 6, 3)
    assert planner["findings"] == {"met": 6, "partial": 3, "not_met": 0, "not_applicable": 0}
    assert planner["reliability"] == "ok" and planner["quality"] == "mixed"  # 6/9 MET is below the 70% line

    engineer = rows[("software_engineer", "aaa/good")]
    assert (
        engineer["findings"]["not_met"] == 2 and engineer["quality"] == "insufficient"
    )  # 2 < 3 evaluated runs
    assert engineer["reliability"] == "insufficient"  # 2 < 5 calls: "Not enough history yet"
    assert rows[("software_engineer", "bbb/thin")]["evaluated_runs"] == 0


def test_recent_issues_show_categories_not_raw_provider_payloads(client, db, auth_headers, bootstrap):
    mine = History(db, project=bootstrap.project)
    pm = _paid(db, "nex-agi/flaky")
    run = mine.call(pm, outcome="provider_invalid_response")
    call = db.execute(select(ModelCall).where(ModelCall.agent_run_id == run.id)).scalar_one()
    call.error = {"category": "provider_invalid_response", "message": "RAW-PROVIDER-PAYLOAD {choices: []}"}
    db.commit()
    mine.call(pm, outcome="provider_authentication_error")
    mine.call(pm)

    issues = _get(client, auth_headers, "/issues", bootstrap.project.id).json()
    assert [i["kind"] for i in issues] == ["configuration", "reliability"]
    assert issues[1]["label"] == "Provider returned an invalid or empty response"
    assert (
        issues[1]["canonical_model_id"] == "nex-agi/flaky" and issues[1]["agent_role"] == "software_engineer"
    )
    assert "RAW-PROVIDER-PAYLOAD" not in json.dumps(issues)
    assert len(_get(client, auth_headers, "/issues", bootstrap.project.id, limit=1).json()) == 1
    assert _get(client, auth_headers, "/issues", bootstrap.project.id, limit=1000).status_code == 422


# =============================================================================
# routing decisions
# =============================================================================


def test_recent_routing_decisions_are_bounded_and_paginated(client, db, auth_headers, bootstrap):
    history = History(db, project=bootstrap.project)
    _paid(db, "aaa/one")
    for _ in range(27):
        _decide(db, history, AUTO_ANY)
    first = _get(client, auth_headers, "/routing-decisions", bootstrap.project.id).json()
    assert len(first["items"]) == 25 and first["has_more"] is True and first["limit"] == 25
    rest = _get(client, auth_headers, "/routing-decisions", bootstrap.project.id, offset=25).json()
    assert len(rest["items"]) == 2 and rest["has_more"] is False
    assert (
        _get(client, auth_headers, "/routing-decisions", bootstrap.project.id, limit=101).status_code == 422
    )


def test_ma8_1_only_and_manual_decisions_display_correctly(client, db, auth_headers, bootstrap):
    history = History(db, project=bootstrap.project)
    free = _free(db, "zzz/free")
    paid = _paid(db, "aaa/paid")
    _unknown(db, "ccc/unknown")
    auto = _decide(db, history, {"mode": "auto", "auto_policy": "free_only"})
    manual = _decide(db, history, {"mode": "manual", "manual_provider_model_id": paid.id})

    listing = {
        i["id"]: i
        for i in _get(client, auth_headers, "/routing-decisions", bootstrap.project.id).json()["items"]
    }
    assert (
        listing[auto.id]["selection_mode"] == "auto" and listing[auto.id]["requested_policy"] == "free_only"
    )
    assert listing[auto.id]["routing_strategy"] == "ma8.1-deterministic-v1"
    assert (
        listing[auto.id]["evidence_status"] == "disabled"
        and listing[auto.id]["evidence_changed_selection"] is None
    )
    assert listing[manual.id]["is_manual"] is True and listing[manual.id]["requested_policy"] is None

    detail = _get(client, auth_headers, f"/routing-decisions/{auto.id}", bootstrap.project.id).json()
    assert (
        detail["selected_canonical_model_id"] == "zzz/free"
        and detail["selected_provider_model_id"] == free.id
    )
    assert detail["free_preference_satisfied"] is True and detail["eligible_count"] == 1
    assert detail["excluded_count"] == 2 and detail["exclusion_counts"] == {
        "NOT_FREE": 1,
        "PRICING_UNKNOWN": 1,
    }
    assert {c["canonical_model_id"]: c["exclusion_reasons"] for c in detail["excluded_candidates"]} == {
        "aaa/paid": ["NOT_FREE"],
        "ccc/unknown": ["PRICING_UNKNOWN"],
    }
    assert detail["evidence"]["status"] == "disabled" and detail["evidence"]["profiles"] == []

    manual_detail = _get(client, auth_headers, f"/routing-decisions/{manual.id}", bootstrap.project.id).json()
    assert manual_detail["is_manual"] is True and manual_detail["evidence"] is None
    assert manual_detail["requested_provider_model_id"] == paid.id


def test_evidence_decision_detail_shows_decision_time_evidence_and_changed_flag(
    client, db, auth_headers, bootstrap
):
    history = History(db, project=bootstrap.project)
    cheap, good = _paid(db, "aaa/cheap"), _paid(db, "bbb/good")
    history.many(cheap, 6, outcome="provider_timeout")
    history.many(good, 3, findings=[MET])
    _v1(db, status="active")
    changed = _decide(db, history, AUTO_ANY)
    history.many(cheap, 30)  # now cheap is reliable (6/36 failures) and first again by MA8.1
    history.many(cheap, 3, findings=[MET])
    unchanged = _decide(db, history, AUTO_ANY)

    # The catalog changes afterwards; the detail must still show decision-time data.
    good.model.status = ModelStatus.UNAVAILABLE
    good.cost_input_per_mtok = None
    db.commit()

    detail = _get(client, auth_headers, f"/routing-decisions/{changed.id}", bootstrap.project.id).json()
    assert detail["routing_strategy"] == EVIDENCE_STRATEGY_V1
    assert (
        detail["selected_canonical_model_id"] == "bbb/good"
        and detail["selected_pricing_classification"] == "paid"
    )
    evidence = detail["evidence"]
    assert evidence["status"] == "applied" and evidence["router_policy_version"] == 1
    assert evidence["config"]["history_window_days"] == 30 and evidence["config"]["min_evaluated_runs"] == 3
    assert evidence["deterministic_pick_canonical_model_id"] == "aaa/cheap"
    assert evidence["evidence_changed_selection"] is True
    profiles = {p["canonical_model_id"]: p for p in evidence["profiles"]}
    assert (
        profiles["aaa/cheap"]["provider_failures"] == 6 and profiles["aaa/cheap"]["reliability"] == "demoted"
    )
    assert profiles["bbb/good"]["findings"]["met"] == 3 and profiles["bbb/good"]["median_latency_ms"] == 100
    assert "would have selected 'aaa/cheap'" in detail["rationale"]

    same = _get(client, auth_headers, f"/routing-decisions/{unchanged.id}", bootstrap.project.id).json()
    assert same["evidence"]["evidence_changed_selection"] is False
    assert same["selected_canonical_model_id"] == same["evidence"]["deterministic_pick_canonical_model_id"]


def test_decisions_filter_by_agent_run_and_never_cross_projects(client, db, auth_headers, bootstrap):
    history = History(db, project=bootstrap.project)
    _paid(db, "aaa/one")
    mine = _decide(db, history, AUTO_ANY)
    _decide(db, history, AUTO_ANY)
    other_project = _other_project(db, bootstrap, member=False)
    theirs = _decide(db, History(db, project=other_project), AUTO_ANY)

    filtered = _get(
        client, auth_headers, "/routing-decisions", bootstrap.project.id, agent_run_id=mine.agent_run_id
    ).json()["items"]
    assert [i["id"] for i in filtered] == [mine.id]
    listed = _get(client, auth_headers, "/routing-decisions", bootstrap.project.id).json()["items"]
    assert theirs.id not in {i["id"] for i in listed}
    # Another project's decision is simply not found through my project.
    assert (
        _get(client, auth_headers, f"/routing-decisions/{theirs.id}", bootstrap.project.id).status_code == 404
    )


def test_payloads_contain_no_prompts_artifacts_or_secrets(client, db, auth_headers, bootstrap, tmp_path):
    from tests.conftest import make_artifact_with_content

    history = History(db, project=bootstrap.project)
    history.task.description = "PROMPT-TEXT-MUST-NOT-LEAK"
    pm = _paid(db, "aaa/one")
    pm.provider.base_url = "https://user:pass@provider.invalid"
    for _ in range(3):
        run = history.call(pm)
        make_artifact_with_content(db, tmp_path, agent_run=run, content="ARTIFACT-TEXT-MUST-NOT-LEAK")
        history.evaluate(run, [MET], content="ARTIFACT-TEXT-MUST-NOT-LEAK")
    get_secret_store().set_secret("secret-ref", "sk-or-v1-SECRET-MUST-NOT-LEAK")
    _v1(db, status="active")
    decision = _decide(db, history, AUTO_ANY)

    payloads = [
        _get(client, auth_headers, path, bootstrap.project.id).json()
        for path in (
            "/summary",
            "/roles",
            "/issues",
            "/routing-decisions",
            f"/routing-decisions/{decision.id}",
        )
    ]
    payloads.append(client.get(f"{BASE}/evidence-policy", headers=auth_headers).json())
    blob = json.dumps(payloads)
    for forbidden in (
        "MUST-NOT-LEAK",
        "RATIONALE-TEXT",
        "secret-ref",
        "user:pass",
        "provider.invalid",
        "storage_ref",
    ):
        assert forbidden not in blob, forbidden


def test_dashboard_tests_use_a_disposable_database(db):
    assert "multi_agent_platform.db" not in str(db.get_bind().url)
    assert ModelCallStatus.SUCCESS  # imported enum sanity
