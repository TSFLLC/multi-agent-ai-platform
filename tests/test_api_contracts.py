"""API contract shape — Section 25. MA0 freezes the OpenAPI contract only;
every state-changing/business-logic endpoint is a stub returning 501, not
working behavior (Section 31 MA0 non-goals: "no execution logic")."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

EXPECTED_PATHS = [
    "/projects",
    "/agents",
    "/agents/{agent_id}/versions",
    "/agents/{agent_id}/versions/{version}/publish",
    "/agents/{agent_id}/prompt-versions",
    "/tools",
    "/models",
    "/providers",
    "/router/simulate",
    "/tasks",
    "/tasks/{task_id}/runs",
    "/tasks/{task_id}/runs/reviewed",
    "/tasks/{task_id}/runs/{run_id}/review",
    "/tasks/{task_id}/runs/{run_id}/cancel",
    "/tasks/{task_id}/runs/{run_id}/events",
    "/tasks/{task_id}/runs/{run_id}/stream",
    "/agent-runs/{agent_run_id}",
    "/agent-runs/{agent_run_id}/stop",
    "/agent-runs/{agent_run_id}/retry",
    "/agent-runs/{agent_run_id}/attempts",
    "/workflows",
    "/workflow-runs/{workflow_run_id}",
    "/comparisons",
    "/comparisons/{comparison_id}/select-winner",
    "/evaluations",
    "/approvals",
    "/approvals/{approval_id}/resolve",
    "/audit-events",
    "/usage",
    "/costs/budgets",
    "/budgets",
    "/events",
    "/events/stream",
]


def test_health_endpoint_works():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["phase"] == "MA4"


def test_openapi_schema_generates():
    schema = client.get("/openapi.json").json()
    assert schema["info"]["title"].startswith("Multi-Agent AI Platform")
    for path in EXPECTED_PATHS:
        assert path in schema["paths"], f"missing contract path: {path}"


def test_stub_endpoints_return_not_implemented_not_a_crash():
    """Section K: every error response uses the {"error": {code, message}}
    envelope (app.errors), including the still-501 contract stubs.
    /agents became a real, authenticated endpoint in MA2 — /tools remains
    an MA0-style stub (Tool Bus is out of scope through at least MA9)."""
    resp = client.get("/tools")
    assert resp.status_code == 501
    body = resp.json()
    assert body["error"]["code"] == "not_implemented"
    assert "message" in body["error"]


def test_approval_resolve_declares_409_fingerprint_mismatch_response():
    schema = client.get("/openapi.json").json()
    op = schema["paths"]["/approvals/{approval_id}/resolve"]["post"]
    assert "409" in op["responses"]


def test_idempotency_key_header_declared_on_state_changing_posts():
    schema = client.get("/openapi.json").json()
    create_task_op = schema["paths"]["/tasks"]["post"]
    header_names = [p["name"] for p in create_task_op.get("parameters", []) if p["in"] == "header"]
    assert "Idempotency-Key" in header_names


def test_app_binds_to_loopback_by_default():
    from app.config import settings

    assert settings.host == "127.0.0.1"
