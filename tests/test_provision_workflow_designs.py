"""scripts.provision_workflow_designs -- idempotent creation of the two
reusable workflow designs, MA7.7D. Uses httpx.MockTransport (same pattern
as test_provision_staging.py / test_provision_agent_responsibilities.py)
so these run offline, never against a live instance.
"""

import json

import httpx
import pytest

from scripts import provision_workflow_designs as provision_mod

_ROLES = [
    "planner",
    "software_engineer",
    "test_engineer",
    "security_reviewer",
    "code_reviewer",
    "final_reviewer",
]


def _mock_client(handler):
    transport = httpx.MockTransport(handler)
    return httpx.Client(transport=transport, base_url="https://instance.example")


class FakeBackend:
    """In-memory stand-in for the parts of the HTTP API
    provision_workflow_designs.py calls, so the test asserts on the
    *shape* of what gets built (node/edge counts, evaluator wiring,
    idempotency) rather than hand-writing dozens of endpoint branches."""

    def __init__(self, *, existing_workflows=None):
        self.agents = [{"id": f"agent-{r}", "role": r, "name": r} for r in _ROLES]
        self.agent_versions = {
            f"agent-{r}": [{"id": f"v1-{r}", "version": 1, "status": "active"}] for r in _ROLES
        }
        self.eval_definitions = []
        self.eval_versions = {}
        self.workflows = list(existing_workflows or [])
        self.workflow_versions = {}
        self.nodes = {}
        self.edges = {}
        self.next_id = 0

    def _id(self, prefix):
        self.next_id += 1
        return f"{prefix}-{self.next_id}"

    def handler(self, request):
        method, path = request.method, request.url.path
        params = dict(request.url.params)

        if path == "/agents" and method == "GET":
            return httpx.Response(200, json=self.agents)
        if path.endswith("/versions") and path.startswith("/agents/") and method == "GET":
            agent_id = path.split("/")[2]
            return httpx.Response(200, json=self.agent_versions.get(agent_id, []))

        if path == "/evaluation-definitions" and method == "GET":
            return httpx.Response(200, json=self.eval_definitions)
        if path == "/evaluation-definitions" and method == "POST":
            body = json.loads(request.content)
            definition = {"id": self._id("evaldef"), "name": body["name"]}
            self.eval_definitions.append(definition)
            self.eval_versions[definition["id"]] = []
            return httpx.Response(201, json=definition)
        if path.startswith("/evaluation-definitions/") and path.endswith("/versions") and method == "GET":
            def_id = path.split("/")[2]
            return httpx.Response(200, json=self.eval_versions.get(def_id, []))
        if path.startswith("/evaluation-definitions/") and path.endswith("/versions") and method == "POST":
            def_id = path.split("/")[2]
            version = {
                "id": self._id("evalver"),
                "version": len(self.eval_versions[def_id]) + 1,
                "status": "draft",
            }
            self.eval_versions[def_id].append(version)
            return httpx.Response(201, json=version)
        if "/versions/" in path and path.endswith("/publish") and path.startswith("/evaluation-definitions/"):
            def_id = path.split("/")[2]
            version_number = int(path.split("/")[4])
            for v in self.eval_versions[def_id]:
                if v["version"] == version_number:
                    v["status"] = "active"
                    return httpx.Response(200, json=v)
            raise AssertionError("version not found")

        if path == "/workflows" and method == "GET":
            return httpx.Response(200, json=self.workflows)
        if path == "/workflows" and method == "POST":
            body = json.loads(request.content)
            workflow = {"id": self._id("wf"), "name": body["name"]}
            self.workflows.append(workflow)
            self.workflow_versions[workflow["id"]] = [{"version": 1, "status": "draft"}]
            self.nodes[workflow["id"]] = {}
            self.edges[workflow["id"]] = []
            return httpx.Response(201, json=workflow)
        parts = path.split("/")
        if len(parts) == 4 and parts[1] == "workflows" and parts[3] == "versions" and method == "GET":
            workflow_id = parts[2]
            return httpx.Response(200, json=self.workflow_versions[workflow_id])
        if "/versions/" in path and path.endswith("/nodes") and method == "POST":
            workflow_id = path.split("/")[2]
            body = json.loads(request.content)
            node = {"id": self._id("node"), "node_key": body["node_key"], "node_type": body["node_type"]}
            self.nodes[workflow_id][node["id"]] = {**node, "config": body.get("config")}
            return httpx.Response(201, json=node)
        if "/versions/" in path and path.endswith("/edges") and method == "POST":
            workflow_id = path.split("/")[2]
            edge = {"id": self._id("edge"), "from": params["from_node_id"], "to": params["to_node_id"]}
            self.edges[workflow_id].append(edge)
            return httpx.Response(201, json=edge)
        if "/versions/" in path and path.endswith("/publish") and path.startswith("/workflows/"):
            workflow_id = path.split("/")[2]
            version_number = int(path.split("/")[4])
            for v in self.workflow_versions[workflow_id]:
                if v["version"] == version_number:
                    v["status"] = "active"
                    return httpx.Response(200, json=v)
            raise AssertionError("workflow version not found")

        raise AssertionError(f"unexpected request: {method} {path} params={params}")


def test_provision_creates_both_designs_with_correct_topology(monkeypatch):
    backend = FakeBackend()
    monkeypatch.setattr(provision_mod, "_client", lambda base_url, token: _mock_client(backend.handler))

    results = provision_mod.provision("https://instance.example", "token")

    assert results["simple_development"]["skipped"] is False
    assert results["full_software_development"]["skipped"] is False

    simple_id = results["simple_development"]["id"]
    assert len(backend.nodes[simple_id]) == 5
    assert len(backend.edges[simple_id]) == 4
    node_types = {n["node_key"]: n["node_type"] for n in backend.nodes[simple_id].values()}
    assert node_types == {
        "planner": "agent",
        "software_engineer": "agent",
        "final_reviewer": "agent",
        "human_approval": "human_approval",
        "complete": "terminal",
    }

    full_id = results["full_software_development"]["id"]
    assert len(backend.nodes[full_id]) == 11
    assert len(backend.edges[full_id]) == 12
    full_node_types = {n["node_key"]: n["node_type"] for n in backend.nodes[full_id].values()}
    assert full_node_types["eval_test"] == "evaluation"
    assert full_node_types["eval_security"] == "evaluation"
    assert full_node_types["eval_code"] == "evaluation"

    # Final Reviewer has 3 incoming edges (the AND-join fan-in).
    final_reviewer_node_id = next(
        n["id"] for n in backend.nodes[full_id].values() if n["node_key"] == "final_reviewer"
    )
    incoming = [e for e in backend.edges[full_id] if e["to"] == final_reviewer_node_id]
    assert len(incoming) == 3

    # Both workflow versions were published (ACTIVE).
    assert backend.workflow_versions[simple_id][0]["status"] == "active"
    assert backend.workflow_versions[full_id][0]["status"] == "active"

    # Three starter Evaluation Definitions were created and published.
    assert len(backend.eval_definitions) == 3
    for versions in backend.eval_versions.values():
        assert versions[0]["status"] == "active"


def test_evaluation_nodes_reuse_the_upstream_agent_as_evaluator_never_a_new_agent(monkeypatch):
    """K's constraint: no new specialized evaluator Agents in this slice."""
    backend = FakeBackend()
    monkeypatch.setattr(provision_mod, "_client", lambda base_url, token: _mock_client(backend.handler))

    results = provision_mod.provision("https://instance.example", "token")
    full_id = results["full_software_development"]["id"]

    by_key = {n["node_key"]: n for n in backend.nodes[full_id].values()}
    assert by_key["eval_test"]["config"]["evaluator_agent_version_id"] == "v1-test_engineer"
    assert by_key["eval_security"]["config"]["evaluator_agent_version_id"] == "v1-security_reviewer"
    assert by_key["eval_code"]["config"]["evaluator_agent_version_id"] == "v1-code_reviewer"
    # No agent-creation endpoint was ever called -- FakeBackend has none,
    # and the run above completed without hitting the AssertionError
    # catch-all, so this holds structurally.


def test_provision_is_idempotent_when_workflows_already_exist(monkeypatch):
    backend = FakeBackend(
        existing_workflows=[
            {"id": "existing-simple", "name": "Simple Development"},
            {"id": "existing-full", "name": "Full Software Development"},
        ]
    )
    monkeypatch.setattr(provision_mod, "_client", lambda base_url, token: _mock_client(backend.handler))

    results = provision_mod.provision("https://instance.example", "token")

    assert results["simple_development"] == {"id": "existing-simple", "skipped": True}
    assert results["full_software_development"] == {"id": "existing-full", "skipped": True}
    # Nothing was created underneath either existing workflow.
    assert backend.nodes == {}
    assert backend.eval_definitions == []


class _NoLifespanClientProxy:
    """Adapts the shared ``client`` fixture's already-configured TestClient
    (dependency_overrides applied, lifespan never triggered) to the
    context-manager shape ``provision()`` expects, without ever calling
    TestClient's OWN ``__enter__``/``__exit__`` -- those fire the app's real
    startup lifespan (app.main's ``db = SessionLocal()`` bypasses
    dependency_overrides entirely and would touch the real production
    engine), which is exactly what the ``client`` fixture's own docstring
    says to avoid."""

    def __init__(self, inner, token):
        self._inner = inner
        self._headers = {"Authorization": f"Bearer {token}"}

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def get(self, path, **kwargs):
        kwargs.setdefault("headers", self._headers)
        return self._inner.get(path, **kwargs)

    def post(self, path, **kwargs):
        kwargs.setdefault("headers", self._headers)
        return self._inner.post(path, **kwargs)


def test_provision_workflow_designs_end_to_end_against_real_api(
    client, db, auth_token, bootstrap, monkeypatch
):
    """Exercises the real app -- FastAPI routers, WorkflowDefinitionService,
    and WorkflowValidator -- not the FakeBackend stand-in, so this is the
    one test that actually proves the 11-node fan-out/fan-in DAG and the
    EVALUATION nodes' config pass real publish-time validation."""
    from app.starter_agents import ensure_starter_agents
    from scripts import provision_workflow_designs as provision_mod

    ensure_starter_agents(db, project_id=bootstrap.project.id)
    db.commit()

    monkeypatch.setattr(
        provision_mod, "_client", lambda base_url, token: _NoLifespanClientProxy(client, token)
    )

    results = provision_mod.provision("http://testserver", auth_token)

    assert results["simple_development"]["skipped"] is False
    assert results["full_software_development"]["skipped"] is False

    auth = {"Authorization": f"Bearer {auth_token}"}
    simple_versions = client.get(
        f"/workflows/{results['simple_development']['id']}/versions", headers=auth
    ).json()
    assert simple_versions[0]["status"] == "active"
    full_versions = client.get(
        f"/workflows/{results['full_software_development']['id']}/versions", headers=auth
    ).json()
    assert full_versions[0]["status"] == "active"

    # Idempotent re-run: no duplicate workflow created, nothing re-published.
    second = provision_mod.provision("http://testserver", auth_token)
    assert second["simple_development"]["skipped"] is True
    assert second["full_software_development"]["skipped"] is True
    assert second["simple_development"]["id"] == results["simple_development"]["id"]
    assert second["full_software_development"]["id"] == results["full_software_development"]["id"]


def test_provision_raises_when_required_agent_role_missing(monkeypatch):
    backend = FakeBackend()
    backend.agents = [a for a in backend.agents if a["role"] != "final_reviewer"]
    monkeypatch.setattr(provision_mod, "_client", lambda base_url, token: _mock_client(backend.handler))

    with pytest.raises(provision_mod.ProvisioningError):
        provision_mod.provision("https://instance.example", "token")


def _client_from_backend(backend):
    return _mock_client(backend.handler)


def test_agent_version_selection_picks_latest_active_when_v1_and_v2_both_active(monkeypatch):
    """publish_agent_version never deprecates the version it supersedes
    (agent_registry_service.py), so v1 and v2 can both be status=="active"
    at once -- selection must resolve to v2, the highest version number,
    not whichever happens to come first in the API response."""
    backend = FakeBackend()
    backend.agent_versions["agent-planner"] = [
        {"id": "v1-planner", "version": 1, "status": "active"},
        {"id": "v2-planner", "version": 2, "status": "active"},
    ]

    with _client_from_backend(backend) as client:
        ids = provision_mod._agent_version_ids_by_role(client)

    assert ids["planner"] == "v2-planner"


def test_agent_version_selection_does_not_depend_on_api_response_ordering(monkeypatch):
    """Same v1/v2-both-active case as above, but with the API returning
    the higher version first -- selection must still land on v2 by
    version number, never by list position."""
    backend = FakeBackend()
    backend.agent_versions["agent-planner"] = [
        {"id": "v2-planner", "version": 2, "status": "active"},
        {"id": "v1-planner", "version": 1, "status": "active"},
    ]

    with _client_from_backend(backend) as client:
        ids = provision_mod._agent_version_ids_by_role(client)

    assert ids["planner"] == "v2-planner"


def test_agent_version_selection_uses_v1_when_only_v1_exists(monkeypatch):
    """final_reviewer's real-world case: no v2 has been published, so the
    lone v1 is selected."""
    backend = FakeBackend()
    assert backend.agent_versions["agent-final_reviewer"] == [
        {"id": "v1-final_reviewer", "version": 1, "status": "active"}
    ]

    with _client_from_backend(backend) as client:
        ids = provision_mod._agent_version_ids_by_role(client)

    assert ids["final_reviewer"] == "v1-final_reviewer"


def test_provision_binds_workflow_nodes_to_latest_active_agent_version(monkeypatch):
    """End-to-end through provision(): when a role has both v1 and v2
    active, the AGENT/EVALUATION nodes built for both workflow designs
    must reference v2, not v1."""
    backend = FakeBackend()
    for role in _ROLES:
        if role == "final_reviewer":
            continue
        backend.agent_versions[f"agent-{role}"] = [
            {"id": f"v1-{role}", "version": 1, "status": "active"},
            {"id": f"v2-{role}", "version": 2, "status": "active"},
        ]
    monkeypatch.setattr(provision_mod, "_client", lambda base_url, token: _mock_client(backend.handler))

    results = provision_mod.provision("https://instance.example", "token")

    simple_id = results["simple_development"]["id"]
    simple_by_key = {n["node_key"]: n for n in backend.nodes[simple_id].values()}
    assert simple_by_key["planner"]["config"]["agent_version_id"] == "v2-planner"
    assert simple_by_key["software_engineer"]["config"]["agent_version_id"] == "v2-software_engineer"
    assert simple_by_key["final_reviewer"]["config"]["agent_version_id"] == "v1-final_reviewer"

    full_id = results["full_software_development"]["id"]
    full_by_key = {n["node_key"]: n for n in backend.nodes[full_id].values()}
    assert full_by_key["test_engineer"]["config"]["agent_version_id"] == "v2-test_engineer"
    assert full_by_key["security_reviewer"]["config"]["agent_version_id"] == "v2-security_reviewer"
    assert full_by_key["code_reviewer"]["config"]["agent_version_id"] == "v2-code_reviewer"
    assert full_by_key["eval_test"]["config"]["evaluator_agent_version_id"] == "v2-test_engineer"
    assert full_by_key["eval_security"]["config"]["evaluator_agent_version_id"] == "v2-security_reviewer"
    assert full_by_key["eval_code"]["config"]["evaluator_agent_version_id"] == "v2-code_reviewer"


def test_every_executable_node_gets_a_usable_auto_prefer_free_model_policy_and_nothing_is_hardcoded(monkeypatch):
    """MA7.7D provisioning correction: an AGENT node's model_policy_override
    was already set to AUTO/prefer_free, but an EVALUATION node's
    evaluator_model_policy_override was missing entirely -- so an evaluator
    Agent Version with no default model_policy (true of every starter
    Agent) had nothing to resolve at run time, even though publish never
    required it (evaluator_model_policy_override is validated only when
    present). Every executable node (agent + evaluation) must now carry the
    same AUTO/prefer_free policy, and no node may name a specific model id
    or provider (Agent != Model, ADR-1: a template never hardcodes one)."""
    backend = FakeBackend()
    monkeypatch.setattr(provision_mod, "_client", lambda base_url, token: _mock_client(backend.handler))

    results = provision_mod.provision("https://instance.example", "token")

    auto_prefer_free = {"mode": "auto", "auto_policy": "prefer_free"}
    for key in ("simple_development", "full_software_development"):
        workflow_id = results[key]["id"]
        for node in backend.nodes[workflow_id].values():
            config = node["config"] or {}
            if node["node_type"] == "agent":
                assert config.get("model_policy_override") == auto_prefer_free, node["node_key"]
            elif node["node_type"] == "evaluation":
                assert config.get("evaluator_model_policy_override") == auto_prefer_free, node["node_key"]
            # No node anywhere names a specific model: the only model-shaped
            # value in any config is the AUTO/prefer_free policy itself.
            assert "manual_provider_model_id" not in json.dumps(config)


def test_provision_workflow_designs_end_to_end_validates_with_zero_issues_no_model_step_required(
    client, db, auth_token, bootstrap, monkeypatch
):
    """Exercises the REAL WorkflowValidator (not FakeBackend): both freshly
    provisioned designs must validate with zero issues immediately after
    provisioning -- proving the operator never has to open a second
    (v2/v3) workflow version just to assign models before running them."""
    from app.starter_agents import ensure_starter_agents
    from scripts import provision_workflow_designs as provision_mod

    ensure_starter_agents(db, project_id=bootstrap.project.id)
    db.commit()

    monkeypatch.setattr(
        provision_mod, "_client", lambda base_url, token: _NoLifespanClientProxy(client, token)
    )

    results = provision_mod.provision("http://testserver", auth_token)
    auth = {"Authorization": f"Bearer {auth_token}"}

    # Both versions already published by provision() -- clone each back to a
    # DRAFT (the only way to re-run /validate) and confirm zero issues, i.e.
    # nothing about model configuration would have blocked the original publish.
    for key in ("simple_development", "full_software_development"):
        workflow_id = results[key]["id"]
        cloned = client.post(f"/workflows/{workflow_id}/versions/1/clone", headers=auth)
        assert cloned.status_code == 201, cloned.text
        draft_version = cloned.json()["version"]
        validation = client.post(f"/workflows/{workflow_id}/versions/{draft_version}/validate", headers=auth)
        assert validation.status_code == 200, validation.text
        assert validation.json() == {"valid": True, "issues": []}, (key, validation.json())


def test_provision_is_idempotent_when_workflows_already_exist_and_v2_is_active(monkeypatch):
    """The fix must not disturb existing idempotent-skip behavior: an
    already-existing workflow is still left untouched even when a role
    now has an active v2 available."""
    backend = FakeBackend(
        existing_workflows=[
            {"id": "existing-simple", "name": "Simple Development"},
            {"id": "existing-full", "name": "Full Software Development"},
        ]
    )
    backend.agent_versions["agent-planner"] = [
        {"id": "v1-planner", "version": 1, "status": "active"},
        {"id": "v2-planner", "version": 2, "status": "active"},
    ]
    monkeypatch.setattr(provision_mod, "_client", lambda base_url, token: _mock_client(backend.handler))

    results = provision_mod.provision("https://instance.example", "token")

    assert results["simple_development"] == {"id": "existing-simple", "skipped": True}
    assert results["full_software_development"] == {"id": "existing-full", "skipped": True}
    assert backend.nodes == {}
    assert backend.eval_definitions == []
