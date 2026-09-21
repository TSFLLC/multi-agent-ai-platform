"""MA7.6A -- the backend contracts behind the Workflow Studio.

Every test drives the REAL HTTP API the way the Studio does (create workflow,
add nodes/edges, configure with PATCH, validate, publish, clone, start with a
Task); nothing here is a private service call unless it is proving atomicity.

A. Dry-run validation: the same validator publish uses, changes nothing.
B. Structured publish errors (``error.detail.issues``).
C. PATCH node configuration (DRAFT only).
D. Publish-time AGENT validation (exists, project, ACTIVE, model resolvable).
E. Evaluation / Human Approval configuration through validate.
F. Clone an existing version into a new DRAFT (atomic; source untouched).
G. Start from a Task: parent TaskRun only, exact version binding, idempotency.
H. Authorization: cross-project, VIEWER, foreign Task, foreign registry rows.
I. The whole Studio flow end to end on the example DAG.
"""

import pytest

from app.db.enums import (
    JobType,
    ModelStatus,
    ProjectRole,
    TaskRunStatus,
    VersionStatus,
    WorkflowNodeType,
    WorkflowRunStatus,
)
from app.models.execution import JobQueue
from app.models.identity import Project, ProjectMembership
from app.models.providers import Model
from app.models.tasks import AgentRun, Task, TaskRun
from app.models.workflow import WorkflowEdge, WorkflowNode, WorkflowNodeRun, WorkflowRun, WorkflowVersion
from app.services.workflow_definition_service import WorkflowDefinitionService
from tests.conftest import (
    make_agent,
    make_model,
    make_provider,
    make_provider_model,
    make_runnable_agent_version,
    make_task,
)
from tests.ma7_4b_support import make_evaluation_setup

CRITERIA = ("correctness", "security")


# -- helpers ---------------------------------------------------------------------------------


def rows(session_factory, model, *criteria):
    session = session_factory()
    try:
        return session.query(model).filter(*criteria).all()
    finally:
        session.close()


def vurl(workflow_id, version, tail=""):
    return f"/workflows/{workflow_id}/versions/{version}{tail}"


class Studio:
    """The calls the Workflow Studio makes, over HTTP, as the local user."""

    def __init__(self, client, headers, project):
        self.client, self.headers, self.project = client, headers, project

    def create(self, name="studio-wf"):
        response = self.client.post("/workflows", headers=self.headers, json={"project_id": self.project.id, "name": name})
        assert response.status_code == 201, response.text
        return response.json()["id"]

    def node(self, workflow_id, version, key, node_type, config=None):
        body = {"node_key": key, "node_type": node_type}
        if config is not None:
            body["config"] = config
        response = self.client.post(vurl(workflow_id, version, "/nodes"), headers=self.headers, json=body)
        assert response.status_code == 201, response.text
        return response.json()["id"]

    def edge(self, workflow_id, version, source_id, target_id):
        response = self.client.post(
            vurl(workflow_id, version, "/edges"),
            headers=self.headers,
            params={"from_node_id": source_id, "to_node_id": target_id},
        )
        assert response.status_code == 201, response.text
        return response.json()["id"]

    def patch(self, workflow_id, version, node_id, config):
        return self.client.patch(
            vurl(workflow_id, version, f"/nodes/{node_id}"), headers=self.headers, json={"config": config}
        )

    def validate(self, workflow_id, version):
        return self.client.post(vurl(workflow_id, version, "/validate"), headers=self.headers)

    def publish(self, workflow_id, version):
        return self.client.post(vurl(workflow_id, version, "/publish"), headers=self.headers)

    def clone(self, workflow_id, version):
        return self.client.post(vurl(workflow_id, version, "/clone"), headers=self.headers)

    def graph(self, workflow_id, version):
        response = self.client.get(vurl(workflow_id, version, "/graph"), headers=self.headers)
        assert response.status_code == 200, response.text
        return response.json()

    def start(self, workflow_id, version, body, headers=None):
        return self.client.post(vurl(workflow_id, version, "/runs"), headers=headers or self.headers, json=body)


@pytest.fixture()
def studio(client, auth_headers, bootstrap):
    return Studio(client, auth_headers, bootstrap.project)


def manual_model(db, label):
    provider = make_provider(db)
    model = make_model(db, canonical_model_id=f"test/studio-{label}")
    provider_model = make_provider_model(db, model=model, provider=provider)
    db.commit()
    return provider_model


def agent_config(av, provider_model=None):
    config = {"agent_version_id": av.id}
    if provider_model is not None:
        config["model_policy_override"] = {"mode": "manual", "manual_provider_model_id": provider_model.id}
    return config


def runnable(db, project, name):
    av = make_runnable_agent_version(db, make_agent(db, project, name))
    db.commit()
    return av


def simple_valid(db, studio, bootstrap, label="v"):
    """agent -> complete, publishable. Returns (workflow_id, agent_node_id, terminal_node_id, agent_version)."""
    workflow_id = studio.create(f"simple-{label}")
    av = runnable(db, bootstrap.project, f"{label}-agent")
    agent = studio.node(workflow_id, 1, "worker", "agent", agent_config(av))
    done = studio.node(workflow_id, 1, "done", "terminal")
    studio.edge(workflow_id, 1, agent, done)
    return workflow_id, agent, done, av


def issues_of(response):
    assert response.status_code == 200, response.text
    return response.json()["issues"]


def publish_issues(response):
    assert response.status_code == 400, response.text
    return response.json()["error"]["detail"]["issues"]


def foreign_project(db, bootstrap, name="foreign", member=False, role=ProjectRole.OWNER):
    project = Project(org_id=bootstrap.organization.id, name=name)
    db.add(project)
    db.flush()
    if member:
        db.add(ProjectMembership(project_id=project.id, user_id=bootstrap.user.id, role=role))
    db.commit()
    return project


# =============================================================================
# A. Dry-run validation
# =============================================================================


def test_validate_a_valid_draft_reports_valid_and_changes_nothing(db, session_factory, studio, bootstrap):
    workflow_id, *_ = simple_valid(db, studio, bootstrap, "a1")
    before = (len(rows(session_factory, WorkflowVersion)), len(rows(session_factory, WorkflowNode)))

    response = studio.validate(workflow_id, 1)

    assert response.status_code == 200
    assert response.json() == {"valid": True, "issues": []}
    version = rows(session_factory, WorkflowVersion, WorkflowVersion.workflow_id == workflow_id)[0]
    assert version.status == VersionStatus.DRAFT and version.published_at is None  # a dry run never publishes
    assert (len(rows(session_factory, WorkflowVersion)), len(rows(session_factory, WorkflowNode))) == before


def test_validate_reports_each_problem_of_an_invalid_draft_individually(db, studio, bootstrap):
    workflow_id = studio.create("a2")
    lone = studio.node(workflow_id, 1, "lonely", "agent", {})  # no agent version
    approval = studio.node(workflow_id, 1, "gate", "human_approval", {})  # no approval_group
    studio.edge(workflow_id, 1, lone, approval)  # and no terminal node at all

    issues = issues_of(studio.validate(workflow_id, 1))

    assert isinstance(issues, list) and len(issues) >= 2
    assert any("AGENT node lonely requires config.agent_version_id" in i for i in issues)
    assert any("HUMAN_APPROVAL node gate requires config.approval_group" in i for i in issues)
    response = studio.validate(workflow_id, 1)
    assert response.json()["valid"] is False


@pytest.mark.parametrize(
    "shape,fragment",
    [
        ("cycle", "Cycle detected"),
        ("empty", "Workflow has no nodes"),
        ("disconnected", "not connected"),
    ],
)
def test_validate_reports_structural_problems(db, studio, bootstrap, shape, fragment):
    workflow_id = studio.create(f"a3-{shape}")
    av = runnable(db, bootstrap.project, f"a3-{shape}")
    if shape == "cycle":
        a = studio.node(workflow_id, 1, "a", "agent", agent_config(av))
        b = studio.node(workflow_id, 1, "b", "agent", agent_config(av))
        entry = studio.node(workflow_id, 1, "entry", "agent", agent_config(av))
        done = studio.node(workflow_id, 1, "done", "terminal")
        studio.edge(workflow_id, 1, entry, a)
        studio.edge(workflow_id, 1, a, b)
        studio.edge(workflow_id, 1, b, a)
        studio.edge(workflow_id, 1, b, done)
    elif shape == "disconnected":
        a = studio.node(workflow_id, 1, "a", "agent", agent_config(av))
        done = studio.node(workflow_id, 1, "done", "terminal")
        studio.edge(workflow_id, 1, a, done)
        studio.node(workflow_id, 1, "island", "agent", agent_config(av))
    assert any(fragment in issue for issue in issues_of(studio.validate(workflow_id, 1)))


def test_validate_refuses_a_non_draft_version_and_an_unknown_one(db, studio, bootstrap):
    workflow_id, *_ = simple_valid(db, studio, bootstrap, "a4")
    assert studio.publish(workflow_id, 1).status_code == 200

    active = studio.validate(workflow_id, 1)
    assert active.status_code == 409 and "Only DRAFT" in active.json()["error"]["message"]
    assert studio.validate(workflow_id, 99).status_code == 404


def test_validate_is_read_only_for_a_viewer_but_cannot_publish(db, studio, bootstrap):
    workflow_id, *_ = simple_valid(db, studio, bootstrap, "a5")
    bootstrap.membership.role = ProjectRole.VIEWER
    db.commit()

    assert studio.validate(workflow_id, 1).status_code == 200  # project READ suffices
    assert studio.publish(workflow_id, 1).status_code == 403


# =============================================================================
# B. Structured publish errors
# =============================================================================


def test_publish_failure_keeps_issues_structured_in_the_error_envelope(db, studio, bootstrap):
    workflow_id = studio.create("b1")
    lone = studio.node(workflow_id, 1, "lonely", "agent", {})
    done = studio.node(workflow_id, 1, "done", "terminal")
    studio.edge(workflow_id, 1, lone, done)

    response = studio.publish(workflow_id, 1)

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "workflow_validation_failed"
    assert isinstance(error["detail"]["issues"], list) and error["detail"]["issues"]
    assert any("AGENT node lonely requires config.agent_version_id" in i for i in error["detail"]["issues"])
    assert "{" not in error["message"] and "[" not in error["message"]  # never a stringified dict/list
    assert error["message"].startswith("Workflow validation failed (")
    # the same issues a dry run reports
    assert issues_of(studio.validate(workflow_id, 1)) == error["detail"]["issues"]


def test_publish_other_errors_keep_their_existing_shape(db, studio, bootstrap):
    workflow_id, *_ = simple_valid(db, studio, bootstrap, "b2")
    assert studio.publish(workflow_id, 1).status_code == 200
    again = studio.publish(workflow_id, 1)
    assert again.status_code == 400 and "already" in again.json()["error"]["message"]
    assert studio.publish(workflow_id, 42).status_code == 404


# =============================================================================
# C. PATCH node configuration
# =============================================================================


def test_patch_updates_a_drafts_node_config_and_nothing_else(db, session_factory, studio, bootstrap):
    workflow_id, agent_id, _done, av = simple_valid(db, studio, bootstrap, "c1")
    model = manual_model(db, "c1")
    new_config = agent_config(av, model)

    response = studio.patch(workflow_id, 1, agent_id, new_config)

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == agent_id and body["node_key"] == "worker" and body["node_type"] == "agent"
    assert body["config"] == new_config
    node = rows(session_factory, WorkflowNode, WorkflowNode.id == agent_id)[0]
    assert node.config == new_config and node.node_key == "worker" and node.node_type == WorkflowNodeType.AGENT
    read_back = next(n for n in studio.graph(workflow_id, 1)["nodes"] if n["id"] == agent_id)
    assert read_back["config"] == new_config


def test_patch_without_a_config_changes_nothing_and_an_empty_one_clears_it(db, studio, bootstrap):
    workflow_id, agent_id, _done, av = simple_valid(db, studio, bootstrap, "c2")
    kept = studio.client.patch(vurl(workflow_id, 1, f"/nodes/{agent_id}"), headers=studio.headers, json={})
    assert kept.status_code == 200 and kept.json()["config"] == agent_config(av)
    cleared = studio.patch(workflow_id, 1, agent_id, {})
    assert cleared.status_code == 200 and cleared.json()["config"] == {}


def test_patch_is_not_validated_until_validate_or_publish(db, studio, bootstrap):
    workflow_id, agent_id, *_ = simple_valid(db, studio, bootstrap, "c3")
    assert studio.patch(workflow_id, 1, agent_id, {"agent_version_id": "does-not-exist"}).status_code == 200
    assert any("unknown agent version" in i for i in issues_of(studio.validate(workflow_id, 1)))


def test_patch_rejects_an_active_version_an_unknown_node_and_a_node_of_another_version(db, studio, bootstrap):
    workflow_id, agent_id, _done, av = simple_valid(db, studio, bootstrap, "c4")
    other_wf = studio.create("c4-other")
    foreign_node = studio.node(other_wf, 1, "n", "agent", agent_config(av))
    assert studio.patch(workflow_id, 1, "nope", {}).status_code == 404
    assert studio.patch(workflow_id, 1, foreign_node, {}).status_code == 404  # node of ANOTHER workflow's version
    assert studio.publish(workflow_id, 1).status_code == 200

    response = studio.patch(workflow_id, 1, agent_id, {"agent_version_id": av.id})
    assert response.status_code == 400 and "published" in response.json()["error"]["message"]


def test_every_edit_of_an_active_version_is_rejected(db, studio, bootstrap):
    workflow_id, agent_id, done_id, _av = simple_valid(db, studio, bootstrap, "c5")
    assert studio.publish(workflow_id, 1).status_code == 200
    add = studio.client.post(
        vurl(workflow_id, 1, "/nodes"), headers=studio.headers, json={"node_key": "extra", "node_type": "terminal"}
    )
    edge = studio.client.post(
        vurl(workflow_id, 1, "/edges"), headers=studio.headers, params={"from_node_id": agent_id, "to_node_id": done_id}
    )
    delete = studio.client.delete(vurl(workflow_id, 1, f"/nodes/{agent_id}"), headers=studio.headers)
    assert (add.status_code, edge.status_code, delete.status_code) == (400, 400, 400)
    assert studio.patch(workflow_id, 1, agent_id, {}).status_code == 400


# =============================================================================
# D. Publish-time AGENT validation
# =============================================================================


def agent_workflow(db, studio, bootstrap, label, av, override=None, config=None):
    workflow_id = studio.create(f"d-{label}")
    node = studio.node(workflow_id, 1, "worker", "agent", config if config is not None else agent_config(av, override))
    done = studio.node(workflow_id, 1, "done", "terminal")
    studio.edge(workflow_id, 1, node, done)
    return workflow_id


def test_an_active_agent_version_with_its_own_model_policy_is_publishable(db, studio, bootstrap):
    av = runnable(db, bootstrap.project, "d1")
    workflow_id = agent_workflow(db, studio, bootstrap, "d1", av)
    assert issues_of(studio.validate(workflow_id, 1)) == []
    assert studio.publish(workflow_id, 1).status_code == 200


def test_an_explicit_manual_model_override_makes_a_policyless_agent_publishable(db, studio, bootstrap):
    av = make_runnable_agent_version(db, make_agent(db, bootstrap.project, "d2"))
    av.model_policy = None  # e.g. a starter Agent: no model of its own
    db.commit()
    workflow_id = agent_workflow(db, studio, bootstrap, "d2", av)
    assert any("has no model" in i for i in issues_of(studio.validate(workflow_id, 1)))  # guaranteed runtime failure

    node = studio.graph(workflow_id, 1)["nodes"]
    worker = next(n["id"] for n in node if n["key"] == "worker")
    assert studio.patch(workflow_id, 1, worker, agent_config(av, manual_model(db, "d2"))).status_code == 200
    assert issues_of(studio.validate(workflow_id, 1)) == []
    assert studio.publish(workflow_id, 1).status_code == 200


def test_a_non_active_agent_version_cannot_be_published(db, studio, bootstrap):
    av = runnable(db, bootstrap.project, "d3")
    av.status = VersionStatus.DRAFT
    db.commit()
    workflow_id = agent_workflow(db, studio, bootstrap, "d3", av)
    issues = publish_issues(studio.publish(workflow_id, 1))
    assert any("not ACTIVE" in i and "status=draft" in i for i in issues)


@pytest.mark.parametrize("status", [VersionStatus.DEPRECATED, VersionStatus.RETIRED])
def test_a_deprecated_or_retired_agent_version_cannot_be_published(db, studio, bootstrap, status):
    av = runnable(db, bootstrap.project, f"d4-{status.value}")
    av.status = status
    db.commit()
    workflow_id = agent_workflow(db, studio, bootstrap, f"d4-{status.value}", av)
    assert any("not ACTIVE" in i for i in publish_issues(studio.publish(workflow_id, 1)))


def test_an_agent_version_from_another_project_is_rejected(db, studio, bootstrap):
    other = foreign_project(db, bootstrap, "d5-other")
    foreign_av = runnable(db, other, "d5-foreign")
    workflow_id = agent_workflow(db, studio, bootstrap, "d5", foreign_av)
    assert any("different project" in i for i in publish_issues(studio.publish(workflow_id, 1)))


def test_an_unknown_agent_version_is_rejected(db, studio, bootstrap):
    workflow_id = agent_workflow(db, studio, bootstrap, "d6", None, config={"agent_version_id": "missing"})
    assert any("unknown agent version" in i for i in publish_issues(studio.publish(workflow_id, 1)))


@pytest.mark.parametrize(
    "override,fragment",
    [
        ({}, "must be a non-empty object"),
        ({"mode": "manual"}, "requires manual_provider_model_id"),
        ({"mode": "manual", "manual_provider_model_id": "nope"}, "unknown provider model"),
        ({"mode": "bogus"}, "mode must be 'manual' or 'auto'"),
        ({"mode": "manual", "manual_provider_model_id": "x", "extra": 1}, "unsupported key"),
        ({"mode": "auto", "auto_policy": "not-a-policy"}, "requires auto_policy"),
    ],
)
def test_a_malformed_agent_model_override_is_rejected(db, studio, bootstrap, override, fragment):
    av = runnable(db, bootstrap.project, "d7")
    workflow_id = agent_workflow(
        db, studio, bootstrap, "d7", av, config={"agent_version_id": av.id, "model_policy_override": override}
    )
    issues = publish_issues(studio.publish(workflow_id, 1))
    assert any(fragment in i and "AGENT node worker model_policy_override" in i for i in issues), issues


@pytest.mark.parametrize("status", [ModelStatus.DEPRECATED, ModelStatus.UNAVAILABLE])
def test_an_override_naming_a_model_that_is_no_longer_active_is_rejected(db, studio, bootstrap, status):
    av = make_runnable_agent_version(db, make_agent(db, bootstrap.project, f"d8-{status.value}"))
    av.model_policy = None
    provider_model = manual_model(db, f"d8-{status.value}")
    db.get(Model, provider_model.model_id).status = status
    db.commit()
    workflow_id = agent_workflow(db, studio, bootstrap, f"d8-{status.value}", av, override=provider_model)
    assert any("not active" in i for i in publish_issues(studio.publish(workflow_id, 1)))


def test_an_agent_versions_own_unusable_policy_counts_as_no_model(db, studio, bootstrap):
    av = make_runnable_agent_version(db, make_agent(db, bootstrap.project, "d9"))
    av.model_policy = {"mode": "manual", "manual_provider_model_id": "vanished"}
    db.commit()
    workflow_id = agent_workflow(db, studio, bootstrap, "d9", av)
    assert any("agent version model_policy" in i and "unknown provider model" in i for i in issues_of(studio.validate(workflow_id, 1)))


# =============================================================================
# E. Evaluation and Human Approval configuration
# =============================================================================


def evaluation_workflow(db, studio, bootstrap, label, config_for=None, approval_config=None):
    """``approval_config=None`` means the valid default; ``{}`` is passed through as-is."""
    """agent -> evaluation -> approval -> complete."""
    setup = make_evaluation_setup(db, bootstrap.project, f"e-{label}", CRITERIA)
    db.commit()
    av = runnable(db, bootstrap.project, f"e-{label}")
    workflow_id = studio.create(f"e-{label}")
    agent = studio.node(workflow_id, 1, "produce", "agent", agent_config(av))
    config = {
        "evaluation_definition_version_id": setup.version.id,
        "evaluator_agent_version_id": setup.agent_version.id,
    }
    if config_for:
        config = config_for(config, setup)
    evaluation = studio.node(workflow_id, 1, "check", "evaluation", config)
    gate_config = {"approval_group": "owners"} if approval_config is None else approval_config
    gate = studio.node(workflow_id, 1, "gate", "human_approval", gate_config)
    done = studio.node(workflow_id, 1, "done", "terminal")
    for a, b in ((agent, evaluation), (evaluation, gate), (gate, done)):
        studio.edge(workflow_id, 1, a, b)
    return workflow_id, setup


def test_a_valid_evaluation_and_approval_workflow_validates_and_publishes(db, studio, bootstrap):
    workflow_id, _setup = evaluation_workflow(db, studio, bootstrap, "ok")
    assert issues_of(studio.validate(workflow_id, 1)) == []
    assert studio.publish(workflow_id, 1).status_code == 200


def test_an_evaluation_override_is_optional_and_when_given_must_be_valid(db, studio, bootstrap):
    model = manual_model(db, "e-override")
    workflow_id, _ = evaluation_workflow(
        db,
        studio,
        bootstrap,
        "ov",
        config_for=lambda base, setup: {
            **base,
            "evaluator_model_policy_override": {"mode": "manual", "manual_provider_model_id": model.id},
        },
    )
    assert issues_of(studio.validate(workflow_id, 1)) == []
    bad_wf, _ = evaluation_workflow(
        db,
        studio,
        bootstrap,
        "badov",
        config_for=lambda base, setup: {**base, "evaluator_model_policy_override": {"mode": "manual"}},
    )
    assert any("evaluator_model_policy_override" in i for i in issues_of(studio.validate(bad_wf, 1)))


@pytest.mark.parametrize(
    "mutate,fragment",
    [
        (lambda base, s: {k: v for k, v in base.items() if k != "evaluation_definition_version_id"}, "requires config.evaluation_definition_version_id"),
        (lambda base, s: {k: v for k, v in base.items() if k != "evaluator_agent_version_id"}, "requires config.evaluator_agent_version_id"),
        (lambda base, s: {**base, "score": 1}, "evidence only"),
        (lambda base, s: {**base, "auto_approve": True}, "evidence only"),
        (lambda base, s: {**base, "surprise": 1}, "unsupported configuration key"),
    ],
)
def test_evaluation_configuration_is_an_allow_list(db, studio, bootstrap, mutate, fragment):
    workflow_id, _ = evaluation_workflow(db, studio, bootstrap, "al", config_for=mutate)
    assert any(fragment in i for i in issues_of(studio.validate(workflow_id, 1)))


def test_only_active_evaluation_dependencies_validate(db, studio, bootstrap):
    workflow_id, setup = evaluation_workflow(db, studio, bootstrap, "dep")
    setup.version.status = VersionStatus.DEPRECATED
    db.commit()
    assert any("EVALUATION node check" in i for i in issues_of(studio.validate(workflow_id, 1)))
    setup.version.status = VersionStatus.ACTIVE
    setup.agent_version.status = VersionStatus.DRAFT
    db.commit()
    assert any("EVALUATION node check" in i for i in issues_of(studio.validate(workflow_id, 1)))


def test_an_evaluation_needs_exactly_one_agent_parent(db, studio, bootstrap):
    setup = make_evaluation_setup(db, bootstrap.project, "e-parents", CRITERIA)
    db.commit()
    av = runnable(db, bootstrap.project, "e-parents")
    workflow_id = studio.create("e-parents")
    a = studio.node(workflow_id, 1, "a", "agent", agent_config(av))
    b = studio.node(workflow_id, 1, "b", "agent", agent_config(av))
    check = studio.node(
        workflow_id,
        1,
        "check",
        "evaluation",
        {"evaluation_definition_version_id": setup.version.id, "evaluator_agent_version_id": setup.agent_version.id},
    )
    done = studio.node(workflow_id, 1, "done", "terminal")
    studio.edge(workflow_id, 1, a, b)
    studio.edge(workflow_id, 1, a, check)
    studio.edge(workflow_id, 1, b, check)  # two parents
    studio.edge(workflow_id, 1, check, done)
    assert any("exactly one incoming edge" in i for i in issues_of(studio.validate(workflow_id, 1)))


def test_evaluation_definitions_and_evaluators_from_another_project_are_rejected(db, studio, bootstrap):
    other = foreign_project(db, bootstrap, "e-other")
    foreign_setup = make_evaluation_setup(db, other, "e-foreign", CRITERIA)
    db.commit()
    workflow_id, _ = evaluation_workflow(
        db,
        studio,
        bootstrap,
        "xp",
        config_for=lambda base, s: {
            "evaluation_definition_version_id": foreign_setup.version.id,
            "evaluator_agent_version_id": foreign_setup.agent_version.id,
        },
    )
    issues = issues_of(studio.validate(workflow_id, 1))
    assert any("different project" in i for i in issues), issues


@pytest.mark.parametrize(
    "config,fragment",
    [
        ({}, "requires config.approval_group"),
        ({"approval_group": ""}, "requires config.approval_group"),
        ({"approval_group": "owners", "auto_approve": True}, "automatic or timed approval"),
        ({"approval_group": "owners", "timeout_seconds": 60}, "automatic or timed approval"),
        ({"approval_group": "owners", "approver_agent_version_id": "x"}, "automatic or timed approval"),
        ({"approval_group": "owners", "model_policy_override": {"mode": "auto"}}, "automatic or timed approval"),
        ({"approval_group": "owners", "default_decision": "approve"}, "automatic or timed approval"),
    ],
)
def test_human_approval_can_only_ever_be_an_explicit_human_decision(db, studio, bootstrap, config, fragment):
    workflow_id, _ = evaluation_workflow(db, studio, bootstrap, "ha", approval_config=config)
    assert any(fragment in i for i in issues_of(studio.validate(workflow_id, 1)))


def test_a_json_body_on_the_edge_route_is_read_as_a_condition_and_blocks_publish(db, studio, bootstrap):
    """Why the Studio sends NO body when connecting: on this route a JSON body is the edge's
    ``condition``. Even an empty ``{}`` is a (non-null) condition, and conditional routing is
    not supported, so the workflow could never be published."""
    workflow_id = studio.create("e-body")
    av = runnable(db, bootstrap.project, "e-body")
    a = studio.node(workflow_id, 1, "a", "agent", agent_config(av))
    done = studio.node(workflow_id, 1, "done", "terminal")

    with_body = studio.client.post(
        vurl(workflow_id, 1, "/edges"),
        headers=studio.headers,
        params={"from_node_id": a, "to_node_id": done},
        json={},
    )
    assert with_body.status_code == 201
    issues = issues_of(studio.validate(workflow_id, 1))
    assert any("has a condition" in i and "a -> done" in i for i in issues), issues

    edge_id = with_body.json()["id"]
    assert studio.client.delete(vurl(workflow_id, 1, f"/edges/{edge_id}"), headers=studio.headers).status_code == 200
    studio.edge(workflow_id, 1, a, done)  # the Studio's way: query parameters, no body
    assert issues_of(studio.validate(workflow_id, 1)) == []


# =============================================================================
# F. Clone a version into a new DRAFT
# =============================================================================


def graph_by_key(studio, workflow_id, version):
    graph = studio.graph(workflow_id, version)
    key_of = {n["id"]: n["key"] for n in graph["nodes"]}
    nodes = {n["key"]: (n["type"], n["config"], n["max_iterations"]) for n in graph["nodes"]}
    edges = sorted((key_of[e["from"]], key_of[e["to"]], e["condition"]) for e in graph["edges"])
    return nodes, edges


def example_dag(db, studio, bootstrap, label="ex"):
    """The MA7.6A example: planner -> engineer -> {test, security, code} ->
    one evaluation each -> ONE human approval (six parents) -> complete."""
    project = bootstrap.project
    agents = {key: runnable(db, project, f"{label}-{key}") for key in ("planner", "engineer", "test", "security", "code")}
    override_model = manual_model(db, f"{label}-ov")
    setups = {key: make_evaluation_setup(db, project, f"{label}-{key}", CRITERIA) for key in ("test", "security", "code")}
    db.commit()
    workflow_id = studio.create(f"{label}-example")
    ids = {}
    for key in ("planner", "engineer", "test", "security", "code"):
        # mix both ways of giving an Agent a model: its own policy, and a node-level override
        override = override_model if key in ("security",) else None
        ids[key] = studio.node(workflow_id, 1, key, "agent", agent_config(agents[key], override))
    for key in ("test", "security", "code"):
        ids[f"eval_{key}"] = studio.node(
            workflow_id,
            1,
            f"eval_{key}",
            "evaluation",
            {
                "evaluation_definition_version_id": setups[key].version.id,
                "evaluator_agent_version_id": setups[key].agent_version.id,
            },
        )
    ids["approval"] = studio.node(workflow_id, 1, "approval", "human_approval", {"approval_group": "owners"})
    ids["complete"] = studio.node(workflow_id, 1, "complete", "terminal")
    studio.edge(workflow_id, 1, ids["planner"], ids["engineer"])
    for key in ("test", "security", "code"):
        studio.edge(workflow_id, 1, ids["engineer"], ids[key])
        studio.edge(workflow_id, 1, ids[key], ids[f"eval_{key}"])
        studio.edge(workflow_id, 1, ids[key], ids["approval"])
        studio.edge(workflow_id, 1, ids[f"eval_{key}"], ids["approval"])
    studio.edge(workflow_id, 1, ids["approval"], ids["complete"])
    return workflow_id, ids, agents, setups


def test_clone_creates_an_identical_draft_and_leaves_the_source_untouched(db, session_factory, studio, bootstrap):
    workflow_id, ids, *_ = example_dag(db, studio, bootstrap, "f1")
    assert studio.publish(workflow_id, 1).status_code == 200
    source_before = graph_by_key(studio, workflow_id, 1)
    source_rows_before = (
        sorted(n.id for n in rows(session_factory, WorkflowNode)),
        sorted(e.id for e in rows(session_factory, WorkflowEdge)),
    )

    response = studio.clone(workflow_id, 1)

    assert response.status_code == 201
    body = response.json()
    assert body["version"] == 2 and body["status"] == "draft" and body["workflow_id"] == workflow_id
    clone_graph = graph_by_key(studio, workflow_id, 2)
    assert clone_graph == source_before  # same keys, types, configs, max_iterations and edges (by key)
    assert len(clone_graph[0]) == 10 and len(clone_graph[1]) == 14  # 10 nodes; 1 + 3*4 + 1 edges
    # source is byte-for-byte what it was: same rows, still ACTIVE
    assert graph_by_key(studio, workflow_id, 1) == source_before
    source_version = rows(session_factory, WorkflowVersion, WorkflowVersion.workflow_id == workflow_id, WorkflowVersion.version == 1)[0]
    assert source_version.status == VersionStatus.ACTIVE
    new_node_ids = {n["id"] for n in studio.graph(workflow_id, 2)["nodes"]}
    assert not new_node_ids & set(ids.values())  # new rows, not shared ones (edges remapped)
    old_node_ids = {n["id"] for n in studio.graph(workflow_id, 1)["nodes"]}
    assert old_node_ids == set(ids.values())
    assert set(source_rows_before[0]) <= {n.id for n in rows(session_factory, WorkflowNode)}
    assert set(source_rows_before[1]) <= {e.id for e in rows(session_factory, WorkflowEdge)}


def test_a_clone_is_a_normal_editable_draft_that_can_be_changed_and_published(db, studio, bootstrap):
    workflow_id, *_ = example_dag(db, studio, bootstrap, "f2")
    assert studio.publish(workflow_id, 1).status_code == 200
    assert studio.clone(workflow_id, 1).status_code == 201
    assert issues_of(studio.validate(workflow_id, 2)) == []

    node = next(n for n in studio.graph(workflow_id, 2)["nodes"] if n["key"] == "approval")
    assert studio.patch(workflow_id, 2, node["id"], {"approval_group": "leads"}).status_code == 200
    assert studio.publish(workflow_id, 2).status_code == 200

    approvals_v1 = next(n for n in studio.graph(workflow_id, 1)["nodes"] if n["key"] == "approval")
    assert approvals_v1["config"] == {"approval_group": "owners"}  # editing the copy never touches the original
    assert node["config"] == {"approval_group": "owners"}  # (the pre-edit read of the copy)


def test_clone_numbers_versions_monotonically_and_can_copy_any_version(db, studio, bootstrap):
    workflow_id, *_ = simple_valid(db, studio, bootstrap, "f3")
    assert studio.publish(workflow_id, 1).status_code == 200
    assert studio.clone(workflow_id, 1).json()["version"] == 2
    assert studio.clone(workflow_id, 1).json()["version"] == 3  # source 1 again -> next number, never a reused one
    assert studio.clone(workflow_id, 2).json()["version"] == 4  # a DRAFT can be copied too
    versions = [v["version"] for v in studio.client.get(f"/workflows/{workflow_id}/versions", headers=studio.headers).json()]
    assert sorted(versions) == [1, 2, 3, 4]
    assert studio.clone(workflow_id, 99).status_code == 404


def test_clone_preserves_config_max_iterations_and_conditions_verbatim(db, session_factory, studio, bootstrap):
    workflow_id = studio.create("f4")
    service = WorkflowDefinitionService(db)
    a = service.add_node(workflow_id, 1, "a", WorkflowNodeType.AGENT, config={"k": {"nested": [1, 2]}}, timeout_seconds=77)
    b = service.add_node(workflow_id, 1, "b", WorkflowNodeType.REPAIR_LOOP, config=None, max_iterations=3)
    service.add_edge(workflow_id, 1, a.id, b.id, condition={"when": "x"})

    assert studio.clone(workflow_id, 1).status_code == 201

    nodes = {n.node_key: n for n in rows(session_factory, WorkflowNode) if n.workflow_version_id != a.workflow_version_id}
    assert nodes["a"].config == {"k": {"nested": [1, 2]}} and nodes["a"].timeout_seconds == 77
    assert nodes["b"].max_iterations == 3 and nodes["b"].config is None
    copied_edges = [e for e in rows(session_factory, WorkflowEdge) if e.workflow_version_id != a.workflow_version_id]
    assert len(copied_edges) == 1 and copied_edges[0].condition == {"when": "x"}
    # the copy's config is a separate object: mutating it cannot alias the source
    assert nodes["a"].id != a.id


def test_clone_never_rebinds_an_existing_run(db, session_factory, studio, bootstrap):
    workflow_id, *_ = simple_valid(db, studio, bootstrap, "f5")
    assert studio.publish(workflow_id, 1).status_code == 200
    task = make_task(db, bootstrap.project)
    db.commit()
    run = studio.start(workflow_id, 1, {"task_id": task.id}).json()

    assert studio.clone(workflow_id, 1).status_code == 201

    persisted = rows(session_factory, WorkflowRun, WorkflowRun.id == run["id"])[0]
    v1 = rows(session_factory, WorkflowVersion, WorkflowVersion.workflow_id == workflow_id, WorkflowVersion.version == 1)[0]
    assert persisted.workflow_version_id == v1.id
    assert len(rows(session_factory, WorkflowRun)) == 1 and len(rows(session_factory, WorkflowNodeRun)) == 2


def test_a_failed_clone_leaves_no_partial_version(db, session_factory, studio, bootstrap, monkeypatch):
    workflow_id, *_ = example_dag(db, studio, bootstrap, "f6")
    assert studio.publish(workflow_id, 1).status_code == 200
    before = (
        len(rows(session_factory, WorkflowVersion)),
        len(rows(session_factory, WorkflowNode)),
        len(rows(session_factory, WorkflowEdge)),
    )
    import app.services.workflow_definition_service as service_module

    real_edge = service_module.WorkflowEdge
    created = []

    def failing_edge(**kwargs):
        created.append(kwargs)
        if len(created) == 5:  # after every node and four edges were already copied
            raise RuntimeError("disk full")
        return real_edge(**kwargs)

    monkeypatch.setattr(service_module, "WorkflowEdge", failing_edge)
    with pytest.raises(RuntimeError, match="disk full"):
        WorkflowDefinitionService(db).clone_version(workflow_id, 1)
    monkeypatch.setattr(service_module, "WorkflowEdge", real_edge)

    assert len(created) == 5
    assert (
        len(rows(session_factory, WorkflowVersion)),
        len(rows(session_factory, WorkflowNode)),
        len(rows(session_factory, WorkflowEdge)),
    ) == before  # the new version, ten node copies and four edge copies were ALL rolled back
    assert studio.clone(workflow_id, 1).json()["version"] == 2  # and the version number was not burned


def test_a_concurrent_clone_conflict_is_a_409_and_leaves_nothing(db, session_factory, studio, bootstrap, monkeypatch):
    workflow_id, *_ = simple_valid(db, studio, bootstrap, "f7")
    assert studio.publish(workflow_id, 1).status_code == 200
    service = WorkflowDefinitionService(db)
    real_latest = service.get_latest_version
    calls = {"n": 0}

    def stale_latest(wid):
        calls["n"] += 1
        return real_latest(wid) if calls["n"] > 1 else None  # a racer computed "next = 1" -- already taken

    monkeypatch.setattr(service, "get_latest_version", stale_latest)
    from app.errors import ConflictError

    with pytest.raises(ConflictError, match="created concurrently"):
        service.clone_version(workflow_id, 1)
    assert len(rows(session_factory, WorkflowVersion, WorkflowVersion.workflow_id == workflow_id)) == 1
    assert len(rows(session_factory, WorkflowNode)) == 2


# =============================================================================
# G. Start from a Task
# =============================================================================


def active_simple(db, studio, bootstrap, label):
    workflow_id, _agent, _done, _av = simple_valid(db, studio, bootstrap, label)
    assert studio.publish(workflow_id, 1).status_code == 200
    return workflow_id


def test_start_with_task_id_creates_the_parent_task_run_only(db, session_factory, studio, bootstrap):
    workflow_id = active_simple(db, studio, bootstrap, "g1")
    created = studio.client.post(
        "/tasks",
        headers=studio.headers,
        json={"project_id": bootstrap.project.id, "title": "Ship it", "description": "Do the thing", "execution_mode": "workflow"},
    )
    assert created.status_code == 201
    task_id = created.json()["id"]

    response = studio.start(workflow_id, 1, {"task_id": task_id})

    assert response.status_code == 201, response.text
    run = response.json()
    assert run["status"] in ("running", "created") and run["workflow_version_id"]
    version = rows(session_factory, WorkflowVersion, WorkflowVersion.workflow_id == workflow_id, WorkflowVersion.version == 1)[0]
    assert run["workflow_version_id"] == version.id  # exact version binding

    parent = rows(session_factory, TaskRun, TaskRun.id == run["task_run_id"])[0]
    assert parent.task_id == task_id and parent.workflow_version_id == version.id
    assert parent.config_snapshot["task_title"] == "Ship it" and parent.config_snapshot["workflow_version"] == 1
    # NO AgentRun and NO job belong to the parent: only the entry node's own Agent Run (and its job) exist
    assert rows(session_factory, AgentRun, AgentRun.task_run_id == parent.id) == []
    agent_runs = rows(session_factory, AgentRun)
    assert len(agent_runs) == 1 and agent_runs[0].task_run_id != parent.id  # the entry node's, in ITS own TaskRun
    jobs = rows(session_factory, JobQueue)
    assert [(j.job_type, j.payload_ref) for j in jobs] == [(JobType.AGENT_RUN, agent_runs[0].id)]
    node_task_runs = rows(session_factory, TaskRun, TaskRun.id != parent.id)
    assert len(node_task_runs) == 1 and node_task_runs[0].task_id == task_id  # the assignment reaches the node


def test_start_binds_exactly_the_requested_version_when_several_are_active(db, session_factory, studio, bootstrap):
    workflow_id = active_simple(db, studio, bootstrap, "g2")
    assert studio.clone(workflow_id, 1).status_code == 201
    assert studio.publish(workflow_id, 2).status_code == 200  # v1 and v2 are both ACTIVE now
    t1, t2 = make_task(db, bootstrap.project), make_task(db, bootstrap.project)
    db.commit()

    r1 = studio.start(workflow_id, 1, {"task_id": t1.id}).json()
    r2 = studio.start(workflow_id, 2, {"task_id": t2.id}).json()

    by_number = {
        v.version: v.id for v in rows(session_factory, WorkflowVersion, WorkflowVersion.workflow_id == workflow_id)
    }
    assert r1["workflow_version_id"] == by_number[1] and r2["workflow_version_id"] == by_number[2]
    assert r1["workflow_version_id"] != r2["workflow_version_id"]


def test_start_accepts_exactly_one_of_task_run_id_or_task_id(db, studio, bootstrap):
    workflow_id = active_simple(db, studio, bootstrap, "g3")
    task = make_task(db, bootstrap.project)
    parent = TaskRun(task_id=task.id, status=TaskRunStatus.CREATED)
    db.add(parent)
    db.commit()

    both = studio.start(workflow_id, 1, {"task_id": task.id, "task_run_id": parent.id})
    neither = studio.start(workflow_id, 1, {})
    assert both.status_code == 400 and "exactly one" in both.json()["error"]["message"]
    assert neither.status_code == 400
    assert studio.start(workflow_id, 1, {"task_run_id": parent.id}).status_code == 201  # the MA7.2 contract is unchanged


def test_a_refused_start_leaves_no_parent_task_run_behind(db, session_factory, studio, bootstrap):
    workflow_id, *_ = simple_valid(db, studio, bootstrap, "g4")  # still a DRAFT
    task = make_task(db, bootstrap.project)
    db.commit()
    before = (len(rows(session_factory, TaskRun)), len(rows(session_factory, WorkflowRun)))

    response = studio.start(workflow_id, 1, {"task_id": task.id})

    assert response.status_code == 400 and "ACTIVE" in response.json()["error"]["message"]
    assert (len(rows(session_factory, TaskRun)), len(rows(session_factory, WorkflowRun))) == before
    assert rows(session_factory, AgentRun) == [] and rows(session_factory, JobQueue) == []


def test_evaluation_dependencies_deprecated_after_publish_refuse_the_start_cleanly(db, session_factory, studio, bootstrap):
    workflow_id, setup = evaluation_workflow(db, studio, bootstrap, "g5")
    assert studio.publish(workflow_id, 1).status_code == 200
    setup.version.status = VersionStatus.DEPRECATED
    task = make_task(db, bootstrap.project)
    db.commit()
    before = len(rows(session_factory, TaskRun))

    response = studio.start(workflow_id, 1, {"task_id": task.id})

    assert response.status_code == 400 and "no longer valid" in response.json()["error"]["message"]
    assert len(rows(session_factory, TaskRun)) == before and rows(session_factory, WorkflowRun) == []


def test_a_retried_start_with_the_same_idempotency_key_returns_the_same_run(db, session_factory, studio, bootstrap):
    workflow_id = active_simple(db, studio, bootstrap, "g6")
    task = make_task(db, bootstrap.project)
    db.commit()
    headers = {**studio.headers, "Idempotency-Key": "studio-start-1"}

    first = studio.start(workflow_id, 1, {"task_id": task.id}, headers=headers)
    second = studio.start(workflow_id, 1, {"task_id": task.id}, headers=headers)

    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert len(rows(session_factory, WorkflowRun)) == 1
    assert len([t for t in rows(session_factory, TaskRun) if (t.config_snapshot or {}).get("task_title")]) == 1  # ONE parent
    assert len(rows(session_factory, AgentRun)) == 1


def test_an_idempotency_key_cannot_be_replayed_against_a_different_version(db, studio, bootstrap):
    workflow_id = active_simple(db, studio, bootstrap, "g7")
    assert studio.clone(workflow_id, 1).status_code == 201
    assert studio.publish(workflow_id, 2).status_code == 200
    task = make_task(db, bootstrap.project)
    db.commit()
    headers = {**studio.headers, "Idempotency-Key": "studio-start-shared"}

    assert studio.start(workflow_id, 1, {"task_id": task.id}, headers=headers).status_code == 201
    replay = studio.start(workflow_id, 2, {"task_id": task.id}, headers=headers)
    assert replay.status_code == 409 and "different request" in replay.json()["error"]["message"]


def test_a_failed_start_does_not_burn_its_idempotency_key(db, session_factory, studio, bootstrap):
    workflow_id, *_ = simple_valid(db, studio, bootstrap, "g8")  # DRAFT: the first attempt is refused
    task = make_task(db, bootstrap.project)
    db.commit()
    headers = {**studio.headers, "Idempotency-Key": "studio-start-retry"}
    assert studio.start(workflow_id, 1, {"task_id": task.id}, headers=headers).status_code == 400

    assert studio.publish(workflow_id, 1).status_code == 200
    retry = studio.start(workflow_id, 1, {"task_id": task.id}, headers=headers)
    assert retry.status_code == 201 and len(rows(session_factory, WorkflowRun)) == 1


def test_standalone_task_execution_is_unchanged_by_the_workflow_start(db, session_factory, studio, bootstrap):
    """POST /tasks/{id}/runs still creates its AgentRun and job, exactly as before."""
    av = runnable(db, bootstrap.project, "g9")
    task = make_task(db, bootstrap.project)
    db.commit()
    response = studio.client.post(f"/tasks/{task.id}/runs", headers=studio.headers, json={"agent_version_id": av.id})
    assert response.status_code == 201
    assert len(rows(session_factory, AgentRun)) == 1
    assert [j.job_type for j in rows(session_factory, JobQueue)] == [JobType.AGENT_RUN]


# =============================================================================
# H. Authorization
# =============================================================================


def foreign_workflow(db, bootstrap, name="hostile"):
    other = foreign_project(db, bootstrap, name)
    service = WorkflowDefinitionService(db)
    workflow = service.create_workflow(other.id, name)
    av = runnable(db, other, name)
    a = service.add_node(workflow.id, 1, "a", WorkflowNodeType.AGENT, config=agent_config(av))
    done = service.add_node(workflow.id, 1, "done", WorkflowNodeType.TERMINAL)
    service.add_edge(workflow.id, 1, a.id, done.id)
    return workflow, a, other


def test_a_workflow_in_a_project_without_membership_is_denied_on_every_studio_route(db, studio, bootstrap):
    workflow, node, other = foreign_workflow(db, bootstrap, "h1")
    task = make_task(db, other)
    db.commit()
    wid = workflow.id
    calls = [
        studio.client.get(f"/workflows/{wid}", headers=studio.headers),
        studio.client.get(f"/workflows?project_id={other.id}", headers=studio.headers),
        studio.client.get(f"/workflows/{wid}/versions", headers=studio.headers),
        studio.client.get(vurl(wid, 1, "/graph"), headers=studio.headers),
        studio.validate(wid, 1),
        studio.publish(wid, 1),
        studio.clone(wid, 1),
        studio.patch(wid, 1, node.id, {}),
        studio.start(wid, 1, {"task_id": task.id}),
    ]
    assert [c.status_code for c in calls] == [403] * len(calls)


def test_nothing_changes_after_a_denied_studio_mutation(db, session_factory, studio, bootstrap):
    workflow, node, _other = foreign_workflow(db, bootstrap, "h2")
    original_config = dict(node.config)
    before = (len(rows(session_factory, WorkflowVersion)), len(rows(session_factory, WorkflowNode)), len(rows(session_factory, TaskRun)))
    studio.clone(workflow.id, 1)
    studio.patch(workflow.id, 1, node.id, {"x": 1})
    after = (len(rows(session_factory, WorkflowVersion)), len(rows(session_factory, WorkflowNode)), len(rows(session_factory, TaskRun)))
    assert after == before
    assert rows(session_factory, WorkflowNode, WorkflowNode.id == node.id)[0].config == original_config


def test_a_viewer_can_read_and_validate_but_cannot_mutate_publish_clone_or_start(db, studio, bootstrap):
    workflow_id = active_simple(db, studio, bootstrap, "h3")
    draft_id, draft_agent, _done, _av = simple_valid(db, studio, bootstrap, "h3-draft")
    task = make_task(db, bootstrap.project)
    bootstrap.membership.role = ProjectRole.VIEWER
    db.commit()

    reads = [
        studio.client.get(f"/workflows?project_id={bootstrap.project.id}", headers=studio.headers),
        studio.client.get(f"/workflows/{workflow_id}/versions", headers=studio.headers),
        studio.client.get(vurl(workflow_id, 1, "/graph"), headers=studio.headers),
        studio.validate(draft_id, 1),
    ]
    assert [r.status_code for r in reads] == [200] * 4

    denied = [
        studio.client.post("/workflows", headers=studio.headers, json={"project_id": bootstrap.project.id, "name": "nope"}),
        studio.client.post(vurl(draft_id, 1, "/nodes"), headers=studio.headers, json={"node_key": "n", "node_type": "terminal"}),
        studio.patch(draft_id, 1, draft_agent, {}),
        studio.client.delete(vurl(draft_id, 1, f"/nodes/{draft_agent}"), headers=studio.headers),
        studio.publish(draft_id, 1),
        studio.clone(workflow_id, 1),
        studio.start(workflow_id, 1, {"task_id": task.id}),
        studio.client.post(
            "/tasks", headers=studio.headers, json={"project_id": bootstrap.project.id, "title": "t", "execution_mode": "workflow"}
        ),
    ]
    assert [d.status_code for d in denied] == [403] * len(denied)


def test_unauthenticated_studio_calls_are_401(db, client, studio, bootstrap):
    workflow_id, agent_id, *_ = simple_valid(db, studio, bootstrap, "h4")
    calls = [
        client.post(vurl(workflow_id, 1, "/validate")),
        client.post(vurl(workflow_id, 1, "/clone")),
        client.patch(vurl(workflow_id, 1, f"/nodes/{agent_id}"), json={"config": {}}),
        client.post(vurl(workflow_id, 1, "/runs"), json={"task_id": "x"}),
    ]
    assert [c.status_code for c in calls] == [401] * 4


def test_a_task_from_a_project_the_user_cannot_access_is_rejected_before_anything_is_created(db, session_factory, studio, bootstrap):
    workflow_id = active_simple(db, studio, bootstrap, "h5")
    other = foreign_project(db, bootstrap, "h5-other")  # no membership
    foreign_task = make_task(db, other)
    db.commit()
    before = (len(rows(session_factory, TaskRun)), len(rows(session_factory, WorkflowRun)))

    response = studio.start(workflow_id, 1, {"task_id": foreign_task.id})

    assert response.status_code == 403
    assert (len(rows(session_factory, TaskRun)), len(rows(session_factory, WorkflowRun))) == before


def test_a_task_from_another_project_the_user_belongs_to_is_rejected_as_a_mismatch(db, session_factory, studio, bootstrap):
    workflow_id = active_simple(db, studio, bootstrap, "h6")
    sibling = foreign_project(db, bootstrap, "h6-sibling", member=True)
    sibling_task = make_task(db, sibling)
    db.commit()
    before = len(rows(session_factory, TaskRun))

    response = studio.start(workflow_id, 1, {"task_id": sibling_task.id})

    assert response.status_code == 400 and "same project" in response.json()["error"]["message"]
    assert len(rows(session_factory, TaskRun)) == before and rows(session_factory, WorkflowRun) == []
    assert studio.start(workflow_id, 1, {"task_id": "no-such-task"}).status_code == 404


def test_the_route_itself_refuses_a_task_from_another_project_before_the_engine_is_asked(db, studio, bootstrap, monkeypatch):
    """The router checks the project match on its own (the engine repeats it as defence in depth)."""
    from app.services.workflow_execution_service import WorkflowExecutionService

    workflow_id = active_simple(db, studio, bootstrap, "h6b")
    sibling = foreign_project(db, bootstrap, "h6b-sibling", member=True)
    sibling_task = make_task(db, sibling)
    db.commit()

    def engine_must_not_be_called(*args, **kwargs):
        raise AssertionError("the route should have refused before starting anything")

    monkeypatch.setattr(WorkflowExecutionService, "start_workflow_run_from_task", engine_must_not_be_called)
    response = studio.start(workflow_id, 1, {"task_id": sibling_task.id})
    assert response.status_code == 400 and "same project" in response.json()["error"]["message"]


def test_the_engine_refuses_a_task_from_another_project_on_its_own(db, session_factory, studio, bootstrap):
    """Defence in depth: called directly (no route), the engine still proves ownership and creates nothing."""
    from app.services.workflow_execution_service import WorkflowExecutionError, WorkflowExecutionService

    workflow_id = active_simple(db, studio, bootstrap, "h6c")
    sibling = foreign_project(db, bootstrap, "h6c-sibling", member=True)
    sibling_task = make_task(db, sibling)
    db.commit()
    version = rows(session_factory, WorkflowVersion, WorkflowVersion.workflow_id == workflow_id)[0]
    before = (len(rows(session_factory, TaskRun)), len(rows(session_factory, WorkflowRun)))

    with pytest.raises(WorkflowExecutionError, match="same project"):
        WorkflowExecutionService(db).start_workflow_run_from_task(version.id, sibling_task.id)
    with pytest.raises(WorkflowExecutionError, match="ownership"):
        WorkflowExecutionService(db).start_workflow_run_from_task(version.id, "no-such-task")

    assert (len(rows(session_factory, TaskRun)), len(rows(session_factory, WorkflowRun))) == before


def test_a_task_run_from_another_project_is_still_refused_by_the_existing_contract(db, studio, bootstrap):
    workflow_id = active_simple(db, studio, bootstrap, "h7")
    other = foreign_project(db, bootstrap, "h7-other", member=True)
    foreign_run = TaskRun(task_id=make_task(db, other).id, status=TaskRunStatus.CREATED)
    db.add(foreign_run)
    db.commit()
    response = studio.start(workflow_id, 1, {"task_run_id": foreign_run.id})
    assert response.status_code == 400 and "same project" in response.json()["error"]["message"]


def test_user_controlled_names_round_trip_as_plain_data(db, studio, bootstrap):
    """Keys and names are stored and returned verbatim (as JSON strings); rendering safely is the
    frontend's job -- the API must not alter or interpret them."""
    hostile = '<img src=x onerror="alert(1)">'
    workflow_id = studio.create(hostile)
    node_id = studio.node(workflow_id, 1, hostile, "terminal")
    listed = studio.client.get(f"/workflows?project_id={bootstrap.project.id}", headers=studio.headers).json()
    assert [w["name"] for w in listed] == [hostile]
    assert studio.graph(workflow_id, 1)["nodes"][0] == {
        "id": node_id, "key": hostile, "type": "terminal", "config": None, "max_iterations": None,
    }


# =============================================================================
# I. The whole Studio flow on the example DAG
# =============================================================================


def test_the_studio_flow_end_to_end_on_the_example_dag(db, session_factory, studio, bootstrap):
    """create -> build -> validate -> publish -> edit as new version -> original unchanged ->
    publish the copy -> run BOTH exact versions from assignments."""
    workflow_id, ids, agents, _setups = example_dag(db, studio, bootstrap, "z")

    # a fresh, fully configured draft is valid; the six-parent gate is allowed
    assert issues_of(studio.validate(workflow_id, 1)) == []
    gate_parents = [e for e in studio.graph(workflow_id, 1)["edges"] if e["to"] == ids["approval"]]
    assert len(gate_parents) == 6
    assert studio.publish(workflow_id, 1).status_code == 200
    listed = studio.client.get(f"/workflows?project_id={bootstrap.project.id}", headers=studio.headers).json()
    assert [(w["name"], w["current_status"]) for w in listed] == [("z-example", "active")]

    # edit as a new version: v2 is a DRAFT copy; v1 is unchanged
    cloned = studio.clone(workflow_id, 1)
    assert cloned.status_code == 201 and cloned.json()["version"] == 2
    v1_before = graph_by_key(studio, workflow_id, 1)
    v2 = graph_by_key(studio, workflow_id, 2)
    assert v2 == v1_before
    versions = {v["version"]: v["status"] for v in studio.client.get(f"/workflows/{workflow_id}/versions", headers=studio.headers).json()}
    assert versions[1] == "active" and versions[2] == "draft"

    # change v2 (drop the override on security, give Code one), publish it
    security = next(n for n in studio.graph(workflow_id, 2)["nodes"] if n["key"] == "security")
    assert studio.patch(workflow_id, 2, security["id"], agent_config(agents["security"])).status_code == 200
    assert studio.publish(workflow_id, 2).status_code == 200
    assert graph_by_key(studio, workflow_id, 1) == v1_before  # still exactly as published

    # run each exact version from an assignment
    runs = {}
    for version in (1, 2):
        task = studio.client.post(
            "/tasks",
            headers=studio.headers,
            json={"project_id": bootstrap.project.id, "title": f"assignment v{version}", "execution_mode": "workflow"},
        ).json()
        started = studio.start(workflow_id, version, {"task_id": task["id"]}, headers={**studio.headers, "Idempotency-Key": f"e2e-{version}"})
        assert started.status_code == 201, started.text
        runs[version] = started.json()
    by_number = {v.version: v.id for v in rows(session_factory, WorkflowVersion, WorkflowVersion.workflow_id == workflow_id)}
    assert {v: r["workflow_version_id"] for v, r in runs.items()} == by_number
    assert all(r["status"] == WorkflowRunStatus.RUNNING.value for r in runs.values())
    # each run got one node run per graph node and exactly ONE parent TaskRun / entry AgentRun
    for run in runs.values():
        assert len(rows(session_factory, WorkflowNodeRun, WorkflowNodeRun.workflow_run_id == run["id"])) == 10
    assert len(rows(session_factory, AgentRun)) == 2  # one entry node (planner) per run
    assert len([t for t in rows(session_factory, TaskRun) if (t.config_snapshot or {}).get("task_title")]) == 2
    assert len(rows(session_factory, Task)) >= 2
