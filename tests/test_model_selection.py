"""Model selection — Section 10/14, MA2.

Manual selection must work for MA2; FREE_ONLY/PREFER_FREE/ANY auto
policies must be representable (not evaluated by any router yet — that's
MA8). Agent != Model: model_policy is a JSON preference/constraint on the
Agent Version, never a stored FK to a specific model (ADR-1).
"""

import pytest
from pydantic import ValidationError

from app.db.enums import ModelSelectionMode, RouterFreePolicy
from app.schemas.agents import ModelPolicy
from app.services.agent_registry_service import AgentRegistryService
from tests.conftest import make_project


def test_manual_policy_requires_a_model_id():
    with pytest.raises(ValidationError):
        ModelPolicy(mode=ModelSelectionMode.MANUAL)


def test_manual_policy_valid_with_model_id():
    policy = ModelPolicy(mode=ModelSelectionMode.MANUAL, manual_provider_model_id="pm-123")
    assert policy.mode == ModelSelectionMode.MANUAL


def test_auto_policy_requires_a_free_policy_value():
    with pytest.raises(ValidationError):
        ModelPolicy(mode=ModelSelectionMode.AUTO)


@pytest.mark.parametrize(
    "policy_value", [RouterFreePolicy.FREE_ONLY, RouterFreePolicy.PREFER_FREE, RouterFreePolicy.ANY]
)
def test_auto_policy_supports_all_three_contracts(policy_value):
    policy = ModelPolicy(mode=ModelSelectionMode.AUTO, auto_policy=policy_value)
    assert policy.auto_policy == policy_value


def test_agent_version_stores_manual_model_policy(db):
    project = make_project(db)
    svc = AgentRegistryService(db)
    agent = svc.create_agent(project_id=project.id, name="X", role="x")

    policy = ModelPolicy(mode=ModelSelectionMode.MANUAL, manual_provider_model_id="pm-abc")
    version = svc.create_agent_version(
        agent_id=agent.id, name="X", role="x", model_policy=policy.model_dump()
    )

    db.refresh(version)
    assert version.model_policy["mode"] == "manual"
    assert version.model_policy["manual_provider_model_id"] == "pm-abc"


def test_agent_version_stores_auto_free_only_policy(db):
    project = make_project(db)
    svc = AgentRegistryService(db)
    agent = svc.create_agent(project_id=project.id, name="X", role="x")

    policy = ModelPolicy(mode=ModelSelectionMode.AUTO, auto_policy=RouterFreePolicy.FREE_ONLY)
    version = svc.create_agent_version(
        agent_id=agent.id, name="X", role="x", model_policy=policy.model_dump()
    )

    db.refresh(version)
    assert version.model_policy["mode"] == "auto"
    assert version.model_policy["auto_policy"] == "free_only"


def test_agent_version_without_model_policy_is_allowed(db):
    """Manual-before-autonomous: an Agent Version with no model_policy at
    all is valid — model selection can be deferred to run time (MA3)."""
    project = make_project(db)
    svc = AgentRegistryService(db)
    agent = svc.create_agent(project_id=project.id, name="X", role="x")
    version = svc.create_agent_version(agent_id=agent.id, name="X", role="x")
    assert version.model_policy is None


def test_model_policy_via_api(client, auth_headers, bootstrap):
    create = client.post(
        "/agents", headers=auth_headers, json={"project_id": bootstrap.project.id, "name": "X", "role": "x"}
    )
    agent_id = create.json()["id"]

    resp = client.post(
        f"/agents/{agent_id}/versions",
        headers=auth_headers,
        json={
            "name": "X",
            "role": "x",
            "model_policy": {"mode": "auto", "auto_policy": "prefer_free"},
        },
    )
    assert resp.status_code == 201
    assert resp.json()["model_policy"] == {
        "mode": "auto",
        "manual_provider_model_id": None,
        "auto_policy": "prefer_free",
    }


def test_model_policy_via_api_rejects_invalid_manual_policy(client, auth_headers, bootstrap):
    create = client.post(
        "/agents", headers=auth_headers, json={"project_id": bootstrap.project.id, "name": "X", "role": "x"}
    )
    agent_id = create.json()["id"]

    resp = client.post(
        f"/agents/{agent_id}/versions",
        headers=auth_headers,
        json={"name": "X", "role": "x", "model_policy": {"mode": "manual"}},
    )
    assert resp.status_code == 422
