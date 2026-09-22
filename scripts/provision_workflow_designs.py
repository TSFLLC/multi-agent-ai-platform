"""Reusable Workflow Design provisioning — MA7.7D.

Creates two standard, saved software-development workflow architectures
through the application's own HTTP API only -- never by writing database
rows directly (same Owner instruction scripts/provision_staging.py and
scripts/provision_agent_responsibilities.py already follow):

- "Simple Development" (5 nodes): Planner -> Software Engineer -> Final
  Reviewer -> Human Approval -> Complete. For routine/lower-risk work
  where a full specialist review would unnecessarily increase latency
  and token consumption.

- "Full Software Development" (11 nodes): Planner -> Software Engineer
  fans out to Test Engineer / Security Reviewer / Code Reviewer, each
  followed by its own EVALUATION node (eval_test / eval_security /
  eval_code -- MA6's existing EVALUATION node type, Section
  app.db.enums.WorkflowNodeType.EVALUATION: "one MA6 agent-evaluator run
  over the output of exactly one upstream AGENT node", never a new node
  kind), all three converging (AND-join -- WorkflowExecutionService
  dispatches a node only once ALL its incoming sources are COMPLETED)
  into Final Reviewer -> Human Approval -> Complete. For complex,
  security-sensitive, high-value, or release-quality work.

Both templates bind each AGENT node to whichever Agent Version is
CURRENTLY ACTIVE for that role at provisioning time (never a hardcoded
version number) -- Agent != Model, and Agent-version-in-use != workflow-
template-authorship-time (Section 12, ADR-1): publishing a later Agent
version (e.g. v2 responsibilities, see provision_agent_responsibilities)
does not require re-authoring these templates.

Each EVALUATION node's evaluator is the SAME upstream Agent that
produced the evidence it evaluates (e.g. Test Engineer evaluates its own
testing evidence) -- a minimal, explicitly-a-starting-point default, the
same spirit as the original starter Agent prompts being "deliberately
not tuned" (app.starter_agents). This satisfies the Owner instruction to
add no new specialized evaluator Agents in this slice: zero new Agents
are created here, only three minimal starter Evaluation Definitions
(rubric data, authored through MA6's own existing, unmodified API --
never a redesign of MA6 Evaluation) so the EVALUATION nodes have
something valid to point at.

No node hard-codes a specific Model or provider -- each AGENT node's
config carries an AUTO/prefer_free model policy (Agent != Model, ADR-1:
a policy/preference, never a stored model id), which is what
WorkflowValidator's publish-time check requires every AGENT node to
resolve to SOME model policy (from the config or the Agent Version); a
manual, environment-specific model pick remains the operator's to make
per node/run, never baked into the template.

Idempotent: safe to re-run. A Workflow already existing by name is left
completely untouched (not re-versioned, not re-published) -- once
created, a workflow is the operator's to evolve via the normal Workflow
Studio (draft edit / clone-to-new-version), never silently overwritten
by this script.

Usage::

    MAP_API_BASE_URL=http://127.0.0.1:8000 \\
    MAP_AUTH_TOKEN=<the bearer token> \\
    python -m scripts.provision_workflow_designs
"""

import logging
import os
import sys
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("scripts.provision_workflow_designs")

# app.bootstrap.LOCAL_PROJECT_ID -- V1 is single-project.
LOCAL_PROJECT_ID = "00000000-0000-0000-0000-000000000003"

_APPROVAL_GROUP = "owners"  # matches frontend/assets/js/workflowGraph.js's DEFAULT_APPROVAL_LABEL

# Publish-time validation requires every AGENT node to resolve to SOME
# model (WorkflowValidator: "AGENT node X has no model") -- either from
# the Agent Version's own model_policy or a per-node override. The
# starter Agents carry no model_policy (Section 13/14's manual-selection
# default), and a reusable, portable TEMPLATE must never hardcode one
# specific provider/model id (that varies per environment's Provider/
# Model registry, and would violate Agent != Model, ADR-1) -- so every
# AGENT node here uses the existing AUTO/RouterFreePolicy mechanism
# (app.db.enums.RouterFreePolicy.PREFER_FREE) instead of a manual pick.
# Not MA8 routing: this policy is already a supported, validated
# ModelPolicy shape today (app.schemas.agents.ModelPolicy) -- MA8 is the
# (not-yet-built) intelligent scorer that would evaluate it live.
_AUTO_PREFER_FREE = {"mode": "auto", "auto_policy": "prefer_free"}

_EVAL_DEFINITIONS = {
    "test_engineer": (
        "Test Verification",
        (
            "Starter rubric for verifying Test Engineer evidence -- deliberately minimal, "
            "an operator starting point (same spirit as the original starter Agent prompts)."
        ),
        [
            {
                "key": "tests_cover_acceptance_criteria",
                "label": "Tests cover the Planner's acceptance criteria",
                "method_hint": "agent_judge",
            },
            {
                "key": "execution_evidence_is_real",
                "label": "Reported pass/fail evidence reflects real execution, never fabricated",
                "method_hint": "agent_judge",
            },
        ],
    ),
    "security_reviewer": (
        "Security Verification",
        (
            "Starter rubric for verifying Security Reviewer evidence -- deliberately minimal, "
            "an operator starting point."
        ),
        [
            {
                "key": "findings_grounded_in_evidence",
                "label": "Findings are grounded in supplied evidence, not invented",
                "method_hint": "agent_judge",
            },
            {
                "key": "severity_classified",
                "label": "Each finding is classified by severity with rationale",
                "method_hint": "agent_judge",
            },
        ],
    ),
    "code_reviewer": (
        "Code Quality Verification",
        (
            "Starter rubric for verifying Code Reviewer evidence -- deliberately minimal, "
            "an operator starting point."
        ),
        [
            {
                "key": "aligns_with_acceptance_criteria",
                "label": "Review addresses the Planner's acceptance criteria",
                "method_hint": "agent_judge",
            },
            {
                "key": "no_unsupported_verdict",
                "label": "No bare pass/fail without supporting findings",
                "method_hint": "agent_judge",
            },
        ],
    ),
}


class ProvisioningError(RuntimeError):
    pass


def _client(base_url: str, token: str) -> httpx.Client:
    return httpx.Client(base_url=base_url, headers={"Authorization": f"Bearer {token}"}, timeout=30.0)


def _agent_version_ids_by_role(client: httpx.Client) -> Dict[str, str]:
    resp = client.get("/agents", params={"project_id": LOCAL_PROJECT_ID})
    resp.raise_for_status()
    agents: List[Dict[str, Any]] = resp.json()

    result: Dict[str, str] = {}
    for agent in agents:
        versions_resp = client.get(f"/agents/{agent['id']}/versions")
        versions_resp.raise_for_status()
        versions: List[Dict[str, Any]] = versions_resp.json()
        active = next((v for v in versions if v["status"] == "active"), None)
        if active is not None:
            result[agent["role"]] = active["id"]
    return result


def _require_roles(agent_version_ids: Dict[str, str], roles: List[str]) -> None:
    missing = [r for r in roles if r not in agent_version_ids]
    if missing:
        raise ProvisioningError(
            f"No ACTIVE Agent Version found for role(s) {missing} -- starter Agents must be "
            "bootstrapped (and published) before provisioning workflow designs."
        )


def _find_evaluation_definition(client: httpx.Client, name: str) -> Optional[Dict[str, Any]]:
    resp = client.get("/evaluation-definitions", params={"project_id": LOCAL_PROJECT_ID})
    resp.raise_for_status()
    for d in resp.json():
        if d["name"] == name:
            return d
    return None


def _ensure_evaluation_definition_version(client: httpx.Client, role: str) -> str:
    """Returns an ACTIVE EvaluationDefinitionVersion id for ``role``,
    creating the (minimal, starter) definition and its v1 if it does not
    already exist. Idempotent by definition name."""
    name, description, criteria = _EVAL_DEFINITIONS[role]
    definition = _find_evaluation_definition(client, name)
    if definition is None:
        create_resp = client.post(
            "/evaluation-definitions",
            json={"project_id": LOCAL_PROJECT_ID, "name": name, "description": description},
        )
        if create_resp.status_code != 201:
            raise ProvisioningError(
                f"POST /evaluation-definitions failed for {name!r}: {create_resp.status_code} {create_resp.text}"
            )
        definition = create_resp.json()
        logger.info("evaluation_definition_created name=%s id=%s", name, definition["id"])
    else:
        logger.info("evaluation_definition_already_exists name=%s id=%s", name, definition["id"])

    versions_resp = client.get(f"/evaluation-definitions/{definition['id']}/versions")
    versions_resp.raise_for_status()
    versions = versions_resp.json()
    active = next((v for v in versions if v["status"] == "active"), None)
    if active is not None:
        return active["id"]

    version_resp = client.post(
        f"/evaluation-definitions/{definition['id']}/versions",
        json={"description": description, "criteria": criteria},
    )
    if version_resp.status_code != 201:
        raise ProvisioningError(
            f"POST evaluation-definition versions failed for {name!r}: {version_resp.status_code} {version_resp.text}"
        )
    version_number = version_resp.json()["version"]
    publish_resp = client.post(
        f"/evaluation-definitions/{definition['id']}/versions/{version_number}/publish"
    )
    if publish_resp.status_code != 200:
        raise ProvisioningError(
            f"POST evaluation-definition publish failed for {name!r}: {publish_resp.status_code} {publish_resp.text}"
        )
    published = publish_resp.json()
    logger.info("evaluation_definition_version_published name=%s version=%s", name, published["version"])
    return published["id"]


def _find_workflow(client: httpx.Client, name: str) -> Optional[Dict[str, Any]]:
    resp = client.get("/workflows", params={"project_id": LOCAL_PROJECT_ID})
    resp.raise_for_status()
    for w in resp.json():
        if w["name"] == name:
            return w
    return None


def _add_node(
    client: httpx.Client,
    workflow_id: str,
    version: int,
    node_key: str,
    node_type: str,
    config: Optional[dict] = None,
) -> str:
    resp = client.post(
        f"/workflows/{workflow_id}/versions/{version}/nodes",
        json={"node_key": node_key, "node_type": node_type, "config": config},
    )
    if resp.status_code != 201:
        raise ProvisioningError(f"add_node {node_key!r} failed: {resp.status_code} {resp.text}")
    return resp.json()["id"]


def _add_edge(
    client: httpx.Client, workflow_id: str, version: int, from_node_id: str, to_node_id: str
) -> None:
    resp = client.post(
        f"/workflows/{workflow_id}/versions/{version}/edges",
        params={"from_node_id": from_node_id, "to_node_id": to_node_id},
    )
    if resp.status_code != 201:
        raise ProvisioningError(
            f"add_edge {from_node_id}->{to_node_id} failed: {resp.status_code} {resp.text}"
        )


def _create_and_publish_workflow(
    client: httpx.Client,
    *,
    name: str,
    nodes: List[Dict[str, Any]],
    edges: List[tuple],
) -> Dict[str, Any]:
    create_resp = client.post("/workflows", json={"project_id": LOCAL_PROJECT_ID, "name": name})
    if create_resp.status_code != 201:
        raise ProvisioningError(
            f"POST /workflows failed for {name!r}: {create_resp.status_code} {create_resp.text}"
        )
    workflow = create_resp.json()
    workflow_id = workflow["id"]

    # create_workflow already creates v1 DRAFT server-side per
    # WorkflowDefinitionService.create_workflow -- confirm via list.
    versions_resp = client.get(f"/workflows/{workflow_id}/versions")
    versions_resp.raise_for_status()
    version_number = versions_resp.json()[0]["version"]

    node_ids: Dict[str, str] = {}
    for node in nodes:
        node_ids[node["key"]] = _add_node(
            client, workflow_id, version_number, node["key"], node["type"], node.get("config")
        )
    for from_key, to_key in edges:
        _add_edge(client, workflow_id, version_number, node_ids[from_key], node_ids[to_key])

    publish_resp = client.post(f"/workflows/{workflow_id}/versions/{version_number}/publish")
    if publish_resp.status_code != 200:
        raise ProvisioningError(
            f"publish failed for workflow {name!r}: {publish_resp.status_code} {publish_resp.text}"
        )
    logger.info("workflow_published name=%s id=%s version=%s", name, workflow_id, version_number)
    return {"id": workflow_id, "version": version_number}


def _simple_development_nodes(agent_versions: Dict[str, str]) -> List[Dict[str, Any]]:
    return [
        {
            "key": "planner",
            "type": "agent",
            "config": {
                "agent_version_id": agent_versions["planner"],
                "model_policy_override": _AUTO_PREFER_FREE,
            },
        },
        {
            "key": "software_engineer",
            "type": "agent",
            "config": {
                "agent_version_id": agent_versions["software_engineer"],
                "model_policy_override": _AUTO_PREFER_FREE,
            },
        },
        {
            "key": "final_reviewer",
            "type": "agent",
            "config": {
                "agent_version_id": agent_versions["final_reviewer"],
                "model_policy_override": _AUTO_PREFER_FREE,
            },
        },
        {"key": "human_approval", "type": "human_approval", "config": {"approval_group": _APPROVAL_GROUP}},
        {"key": "complete", "type": "terminal", "config": None},
    ]


_SIMPLE_DEVELOPMENT_EDGES = [
    ("planner", "software_engineer"),
    ("software_engineer", "final_reviewer"),
    ("final_reviewer", "human_approval"),
    ("human_approval", "complete"),
]


def _full_software_development_nodes(
    agent_versions: Dict[str, str], eval_versions: Dict[str, str]
) -> List[Dict[str, Any]]:
    return [
        {
            "key": "planner",
            "type": "agent",
            "config": {
                "agent_version_id": agent_versions["planner"],
                "model_policy_override": _AUTO_PREFER_FREE,
            },
        },
        {
            "key": "software_engineer",
            "type": "agent",
            "config": {
                "agent_version_id": agent_versions["software_engineer"],
                "model_policy_override": _AUTO_PREFER_FREE,
            },
        },
        {
            "key": "test_engineer",
            "type": "agent",
            "config": {
                "agent_version_id": agent_versions["test_engineer"],
                "model_policy_override": _AUTO_PREFER_FREE,
            },
        },
        {
            "key": "eval_test",
            "type": "evaluation",
            "config": {
                "evaluation_definition_version_id": eval_versions["test_engineer"],
                "evaluator_agent_version_id": agent_versions["test_engineer"],
            },
        },
        {
            "key": "security_reviewer",
            "type": "agent",
            "config": {
                "agent_version_id": agent_versions["security_reviewer"],
                "model_policy_override": _AUTO_PREFER_FREE,
            },
        },
        {
            "key": "eval_security",
            "type": "evaluation",
            "config": {
                "evaluation_definition_version_id": eval_versions["security_reviewer"],
                "evaluator_agent_version_id": agent_versions["security_reviewer"],
            },
        },
        {
            "key": "code_reviewer",
            "type": "agent",
            "config": {
                "agent_version_id": agent_versions["code_reviewer"],
                "model_policy_override": _AUTO_PREFER_FREE,
            },
        },
        {
            "key": "eval_code",
            "type": "evaluation",
            "config": {
                "evaluation_definition_version_id": eval_versions["code_reviewer"],
                "evaluator_agent_version_id": agent_versions["code_reviewer"],
            },
        },
        {
            "key": "final_reviewer",
            "type": "agent",
            "config": {
                "agent_version_id": agent_versions["final_reviewer"],
                "model_policy_override": _AUTO_PREFER_FREE,
            },
        },
        {"key": "human_approval", "type": "human_approval", "config": {"approval_group": _APPROVAL_GROUP}},
        {"key": "complete", "type": "terminal", "config": None},
    ]


_FULL_SOFTWARE_DEVELOPMENT_EDGES = [
    ("planner", "software_engineer"),
    ("software_engineer", "test_engineer"),
    ("software_engineer", "security_reviewer"),
    ("software_engineer", "code_reviewer"),
    ("test_engineer", "eval_test"),
    ("security_reviewer", "eval_security"),
    ("code_reviewer", "eval_code"),
    ("eval_test", "final_reviewer"),
    ("eval_security", "final_reviewer"),
    ("eval_code", "final_reviewer"),
    ("final_reviewer", "human_approval"),
    ("human_approval", "complete"),
]


def provision(base_url: str, token: str) -> Dict[str, Dict[str, Any]]:
    results: Dict[str, Dict[str, Any]] = {}
    with _client(base_url, token) as client:
        agent_versions = _agent_version_ids_by_role(client)
        _require_roles(agent_versions, ["planner", "software_engineer", "final_reviewer"])

        existing = _find_workflow(client, "Simple Development")
        if existing is not None:
            logger.info(
                "workflow_already_exists name=%s id=%s -- skipping", "Simple Development", existing["id"]
            )
            results["simple_development"] = {"id": existing["id"], "skipped": True}
        else:
            created = _create_and_publish_workflow(
                client,
                name="Simple Development",
                nodes=_simple_development_nodes(agent_versions),
                edges=_SIMPLE_DEVELOPMENT_EDGES,
            )
            results["simple_development"] = {**created, "skipped": False}

        existing_full = _find_workflow(client, "Full Software Development")
        if existing_full is not None:
            logger.info(
                "workflow_already_exists name=%s id=%s -- skipping",
                "Full Software Development",
                existing_full["id"],
            )
            results["full_software_development"] = {"id": existing_full["id"], "skipped": True}
        else:
            _require_roles(agent_versions, ["test_engineer", "security_reviewer", "code_reviewer"])
            eval_versions = {
                role: _ensure_evaluation_definition_version(client, role)
                for role in ("test_engineer", "security_reviewer", "code_reviewer")
            }
            created_full = _create_and_publish_workflow(
                client,
                name="Full Software Development",
                nodes=_full_software_development_nodes(agent_versions, eval_versions),
                edges=_FULL_SOFTWARE_DEVELOPMENT_EDGES,
            )
            results["full_software_development"] = {**created_full, "skipped": False}

    return results


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    base_url = os.environ.get("MAP_API_BASE_URL")
    token = os.environ.get("MAP_AUTH_TOKEN")
    if not base_url or not token:
        print(
            "MAP_API_BASE_URL and MAP_AUTH_TOKEN must both be set in the environment.\n"
            "See the module docstring (python -m pydoc scripts.provision_workflow_designs) for usage.",
            file=sys.stderr,
        )
        return 2

    try:
        provision(base_url, token)
    except (ProvisioningError, httpx.HTTPError) as exc:
        logger.error("provisioning_failed error=%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
