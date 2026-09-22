"""MA7.6B -- failed-Agent-node recovery with model replacement.

Mirrors the flagship UAT run cd25e9c3: the ``code`` Agent node's resolved
model fails (``provider_invalid_response``) while its parallel siblings
(``security``, ``test``) and their evaluations complete normally. This is the
bounded recovery slice: an operator retries exactly the failed node with a
different, currently ACTIVE provider model -- a run/attempt-level override
only, never a WorkflowVersion edit -- and, on success, the engine resumes
only what that failure blocked (``eval_code`` -> ``gate``). Nothing already
completed (``security``, ``test``, ``eval_security``, ``eval_test``) is
re-run, and the original failed WorkflowNodeRun/AgentRun is kept untouched.

Runs go through the REAL engine, queue and Worker with only the provider
faked (the MA7.5B/5C harness), then the new retry/attempts routes are
exercised over HTTP exactly as the Control Room uses them.
"""

from decimal import Decimal

from app.db.enums import ProjectRole, WorkflowNodeRunStatus, WorkflowRunStatus
from app.models.providers import Provider
from app.models.tasks import AgentRun
from app.models.workflow import WorkflowNode, WorkflowNodeRun, WorkflowRun
from app.providers.base import ProviderInvalidResponseError
from tests.conftest import make_model, make_provider_model
from tests.ma7_3b_support import resolve_via_api
from tests.ma7_4b_support import new_worker, running_worker, wait_until
from tests.test_ma7_5b_parallel_evaluations import EVALS, REVIEWERS, build_parallel, drain, go
from tests.test_ma7_6b_control_room_api import detail, foreign_project, gate


def node_run_row(session_factory, run_id, node_key, *, iteration=None):
    """Direct, iteration-precise lookup -- unlike ``ma7_3b_support.snapshot``'s
    ``node_runs`` dict (keyed by node_key over ALL iterations, so ambiguous
    once a node has more than one row), this always returns exactly the row
    asked for: the given ``iteration``, or the latest one."""
    db = session_factory()
    try:
        run = db.get(WorkflowRun, run_id)
        node = db.query(WorkflowNode).filter(
            WorkflowNode.workflow_version_id == run.workflow_version_id, WorkflowNode.node_key == node_key
        ).one()
        rows = (
            db.query(WorkflowNodeRun)
            .filter(WorkflowNodeRun.workflow_run_id == run_id, WorkflowNodeRun.workflow_node_id == node.id)
            .order_by(WorkflowNodeRun.iteration)
            .all()
        )
        return rows[-1] if iteration is None else next((r for r in rows if r.iteration == iteration), None)
    finally:
        db.close()


def fail_first_call(harness, role, message="provider_invalid_response: boom"):
    """The role's FIRST invocation fails; any later one (the retry, on a
    different provider model) succeeds normally."""
    seen = {"count": 0}

    def hook(_request):
        seen["count"] += 1
        if seen["count"] == 1:
            raise ProviderInvalidResponseError(message)

    harness.provider.hooks[role] = hook


def fail_after_siblings_complete(session_factory, harness, role, other_keys, message="provider_invalid_response: boom"):
    """The role's FIRST invocation blocks until every one of ``other_keys``
    has COMPLETED, then fails -- reproducing the reported incident exactly:
    the parallel siblings (and their evaluations) finished normally, and only
    THEN did this branch's resolved model return an error. A later
    invocation (the retry, on a different model) succeeds immediately."""
    seen = {"count": 0}

    def done():
        for key in other_keys:
            row = node_run_row(session_factory, harness.run.id, key)
            if row is None or row.status != WorkflowNodeRunStatus.COMPLETED:
                return False
        return True

    def hook(_request):
        seen["count"] += 1
        if seen["count"] == 1:
            assert wait_until(done, timeout=10), "siblings never completed"
            raise ProviderInvalidResponseError(message)

    harness.provider.hooks[role] = hook


def add_replacement_model(db, harness, role, label):
    """A second, real, currently-ACTIVE ProviderModel the fake provider also
    recognizes for ``role`` -- exactly what an operator's replacement pick
    is: a currently valid model, distinct from the one the failed attempt
    resolved to."""
    provider = db.query(Provider).first()
    canonical = f"fake/{label}-{role}-replacement"
    provider_model = make_provider_model(
        db,
        model=make_model(db, canonical_model_id=canonical),
        provider=provider,
        cost_input_per_mtok=Decimal(0),
        cost_output_per_mtok=Decimal(0),
    )
    db.commit()
    harness.provider.role_of[canonical] = role
    return provider_model


def failed_flagship(db, session_factory, monkeypatch, bootstrap, label, *, role="code"):
    """The MA7.5 flagship graph, driven to exactly the reported incident: the
    other two reviewers and their own evaluations complete normally (real
    concurrency, ``Worker(concurrency=3)``), and only then does ``role``'s
    resolved model fail."""
    harness = build_parallel(db, bootstrap.project, monkeypatch, label)
    other_reviewers = [r for r in REVIEWERS if r != role]
    other_keys = other_reviewers + [EVALS[r] for r in other_reviewers]
    fail_after_siblings_complete(session_factory, harness, role, other_keys)
    go(db, harness)

    def run_status():
        session = session_factory()
        try:
            return session.get(WorkflowRun, harness.run.id).status
        finally:
            session.close()

    with running_worker(session_factory, concurrency=3):
        assert wait_until(lambda: run_status() == WorkflowRunStatus.FAILED, timeout=15), "run never failed"
    # The queue may still hold a signalled-but-not-yet-processed job (a
    # cooperative cancellation of a node that was never actually dispatched,
    # for instance); a plain single-lane drain settles it exactly like any
    # other MA7.5 test does after its own concurrent phase.
    drain(new_worker(session_factory))
    return harness


# =============================================================================
# the recovery path itself
# =============================================================================


def test_retry_with_a_replacement_model_resumes_only_the_blocked_path(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch
):
    harness = failed_flagship(db, session_factory, monkeypatch, bootstrap, "r1")
    body = detail(client, auth_headers, harness.run.id)
    nodes = {n["node_key"]: n for n in body["nodes"]}

    assert body["status"] == "failed" and nodes["code"]["status"] == "failed"
    assert nodes["code"]["failure"]["category"] == "provider_invalid_response"
    assert {nodes[k]["status"] for k in ("security", "test", "eval_security", "eval_test")} == {"completed"}
    assert nodes["eval_code"]["status"] == "pending" and nodes["gate"]["status"] == "pending"

    original = node_run_row(session_factory, harness.run.id, "code")
    original_agent_run_id = original.agent_run_id
    assert original.iteration == 0 and original.status == WorkflowNodeRunStatus.FAILED

    replacement = add_replacement_model(db, harness, "code", "r1")

    response = client.post(
        f"/workflow-runs/{harness.run.id}/nodes/{original.id}/retry",
        headers=auth_headers,
        json={"replacement_provider_model_id": replacement.id},
    )
    assert response.status_code == 200, response.text
    retried = response.json()
    assert retried["iteration"] == 1 and retried["workflow_node_id"] == original.workflow_node_id
    assert retried["agent_run_id"] != original_agent_run_id

    # The run is active again the instant the retry is accepted (before the
    # worker has even picked the job up) -- recovery does not wait for the
    # retry to finish to stop being "failed".
    mid = detail(client, auth_headers, harness.run.id)
    assert mid["status"] in ("running", "node_waiting_for_approval")

    drain(new_worker(session_factory))

    final = detail(client, auth_headers, harness.run.id)
    final_nodes = {n["node_key"]: n for n in final["nodes"]}
    assert final["status"] == "node_waiting_for_approval"
    assert final_nodes["code"]["status"] == "completed"
    assert final_nodes["code"]["agent"]["model"]["canonical_model_id"] == "fake/r1-code-replacement"
    assert final_nodes["eval_code"]["status"] == "completed"
    assert final_nodes["gate"]["status"] == "waiting_for_approval"

    # Nothing already completed was re-run.
    for role in ("security", "test", "eval_security", "eval_test", "planner", "engineer"):
        assert harness.provider.count(role) == 1
    assert harness.provider.count("code") == 2  # the original failure + the retry

    # The original failed attempt is preserved as immutable history.
    original_after = node_run_row(session_factory, harness.run.id, "code", iteration=0)
    assert original_after.status == WorkflowNodeRunStatus.FAILED
    assert original_after.agent_run_id == original_agent_run_id
    original_agent_run = db.get(AgentRun, original_agent_run_id)
    assert original_agent_run.status.value == "failed"

    # Full attempt lineage via the Control Room's attempts endpoint.
    attempts = client.get(
        f"/workflow-runs/{harness.run.id}/nodes/{original.workflow_node_id}/attempts", headers=auth_headers
    ).json()
    assert [a["iteration"] for a in attempts] == [0, 1]
    assert attempts[0]["status"] == "failed" and attempts[0]["agent_run_id"] == original_agent_run_id
    assert attempts[1]["status"] == "completed" and attempts[1]["agent_run_id"] == retried["agent_run_id"]

    # The run finishes normally once a human approves -- exactly the existing
    # Human Approval contract, untouched by recovery.
    approval = gate(client, auth_headers, harness.run.id)
    resolved = resolve_via_api(client, auth_headers, approval, approve=True)
    assert resolved.status_code == 200
    completed = detail(client, auth_headers, harness.run.id)
    assert completed["status"] == "completed" and completed["ended_at"] is not None


def test_the_control_rooms_own_model_catalog_id_is_exactly_what_the_retry_endpoint_expects(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch
):
    """MA7.6B follow-up: the replacement-model picker returns rows shaped like
    ``GET /models`` (``ModelCatalogEntryRead``) -- this proves ``id`` on THAT
    exact JSON response (never ``model_id``, ``provider_model_id``, or a
    provider_model_snapshot id) is what the retry endpoint accepts, by
    fetching the real catalog over HTTP and feeding its own value straight
    into the real retry request -- the exact frontend/API contract, not the
    ORM row the test happened to construct."""
    harness = failed_flagship(db, session_factory, monkeypatch, bootstrap, "r8")
    original = node_run_row(session_factory, harness.run.id, "code")
    replacement = add_replacement_model(db, harness, "code", "r8")

    catalog = client.get("/models", headers=auth_headers).json()
    entry = next(e for e in catalog if e["id"] == replacement.id)
    assert entry["model_id"] != entry["id"] and entry["provider_model_id"] != entry["id"]  # distinct id spaces
    assert entry["model_status"] == "active"

    response = client.post(
        f"/workflow-runs/{harness.run.id}/nodes/{original.id}/retry",
        headers=auth_headers,
        json={"replacement_provider_model_id": entry["id"]},  # the catalog row's own "id", nothing else
    )
    assert response.status_code == 200, response.text
    assert response.json()["iteration"] == 1


# =============================================================================
# eligibility / rejection
# =============================================================================


def test_retry_rejects_an_inactive_replacement_model(client, db, session_factory, auth_headers, bootstrap, monkeypatch):
    """Distinct from a nonexistent id: a real ProviderModel row whose Model
    is DEPRECATED -- exactly what resolve_manual's own ACTIVE check refuses,
    and exactly the "became unavailable between loading the picker and
    retrying" case the Control Room's error message is for."""
    from app.db.enums import ModelStatus
    from app.models.providers import Model

    harness = failed_flagship(db, session_factory, monkeypatch, bootstrap, "r9")
    original = node_run_row(session_factory, harness.run.id, "code")
    replacement = add_replacement_model(db, harness, "code", "r9")
    db.get(Model, replacement.model_id).status = ModelStatus.DEPRECATED
    db.commit()

    response = client.post(
        f"/workflow-runs/{harness.run.id}/nodes/{original.id}/retry",
        headers=auth_headers,
        json={"replacement_provider_model_id": replacement.id},
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "retry_not_allowed"
    assert "not active" in body["error"]["message"]
    # Refused -- nothing was created, the original failure stands untouched.
    unchanged = node_run_row(session_factory, harness.run.id, "code")
    assert unchanged.status == WorkflowNodeRunStatus.FAILED and unchanged.iteration == 0
    assert detail(client, auth_headers, harness.run.id)["status"] == "failed"


def test_retry_rejects_a_completed_node(client, db, session_factory, auth_headers, bootstrap, monkeypatch):
    harness = failed_flagship(db, session_factory, monkeypatch, bootstrap, "r2")
    completed = node_run_row(session_factory, harness.run.id, "security")
    replacement = add_replacement_model(db, harness, "security", "r2")

    response = client.post(
        f"/workflow-runs/{harness.run.id}/nodes/{completed.id}/retry",
        headers=auth_headers,
        json={"replacement_provider_model_id": replacement.id},
    )
    assert response.status_code == 400 and response.json()["error"]["code"] == "retry_not_allowed"


def test_retry_rejects_a_nonexistent_replacement_model_clearly(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch
):
    """Distinct from an inactive-but-real model: an id that resolves to no
    ProviderModel row at all -- ``resolve_manual``'s own "does not exist"
    branch, reported clearly rather than as a bare 404."""
    harness = failed_flagship(db, session_factory, monkeypatch, bootstrap, "r3")
    original = node_run_row(session_factory, harness.run.id, "code")

    response = client.post(
        f"/workflow-runs/{harness.run.id}/nodes/{original.id}/retry",
        headers=auth_headers,
        json={"replacement_provider_model_id": "does-not-exist"},
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "retry_not_allowed"
    assert "does not exist" in body["error"]["message"]
    # Refused -- nothing was created, the original failure stands untouched.
    unchanged = node_run_row(session_factory, harness.run.id, "code")
    assert unchanged.status == WorkflowNodeRunStatus.FAILED and unchanged.iteration == 0
    assert detail(client, auth_headers, harness.run.id)["status"] == "failed"


def test_retry_rejects_a_second_attempt_at_an_already_superseded_node_run(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch
):
    harness = failed_flagship(db, session_factory, monkeypatch, bootstrap, "r4")
    original = node_run_row(session_factory, harness.run.id, "code")
    replacement = add_replacement_model(db, harness, "code", "r4")

    first = client.post(
        f"/workflow-runs/{harness.run.id}/nodes/{original.id}/retry",
        headers=auth_headers,
        json={"replacement_provider_model_id": replacement.id},
    )
    assert first.status_code == 200

    again = client.post(
        f"/workflow-runs/{harness.run.id}/nodes/{original.id}/retry",
        headers=auth_headers,
        json={"replacement_provider_model_id": replacement.id},
    )
    assert again.status_code == 400 and again.json()["error"]["code"] == "retry_not_allowed"


def test_retry_rejects_a_non_agent_node(client, db, session_factory, auth_headers, bootstrap, monkeypatch):
    """A human's rejection fails the gate (HUMAN_APPROVAL, not AGENT) --
    recovery is scoped to Agent nodes only."""
    harness = build_parallel(db, bootstrap.project, monkeypatch, "r5")
    go(db, harness)
    drain(new_worker(session_factory))
    approval = gate(client, auth_headers, harness.run.id)
    assert resolve_via_api(client, auth_headers, approval, approve=False).status_code == 200
    drain(new_worker(session_factory))
    assert detail(client, auth_headers, harness.run.id)["status"] == "failed"

    gate_run = node_run_row(session_factory, harness.run.id, "gate")
    response = client.post(
        f"/workflow-runs/{harness.run.id}/nodes/{gate_run.id}/retry",
        headers=auth_headers,
        json={"replacement_provider_model_id": "whatever"},
    )
    assert response.status_code == 400 and response.json()["error"]["code"] == "retry_not_allowed"


# =============================================================================
# authorization / isolation
# =============================================================================


def test_retry_requires_modify_and_is_denied_to_a_viewer(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch
):
    harness = failed_flagship(db, session_factory, monkeypatch, bootstrap, "r6")
    original = node_run_row(session_factory, harness.run.id, "code")
    replacement = add_replacement_model(db, harness, "code", "r6")
    bootstrap.membership.role = ProjectRole.VIEWER
    db.commit()

    response = client.post(
        f"/workflow-runs/{harness.run.id}/nodes/{original.id}/retry",
        headers=auth_headers,
        json={"replacement_provider_model_id": replacement.id},
    )
    assert response.status_code == 403
    unchanged = node_run_row(session_factory, harness.run.id, "code")
    assert unchanged.status == WorkflowNodeRunStatus.FAILED


def test_retry_is_refused_for_another_projects_run(client, db, session_factory, auth_headers, bootstrap, monkeypatch):
    other = foreign_project(db, bootstrap, "r7-hostile")
    harness = build_parallel(db, other, monkeypatch, "r7")
    fail_first_call(harness, "code")
    go(db, harness)
    drain(new_worker(session_factory))
    original = node_run_row(session_factory, harness.run.id, "code")
    replacement = add_replacement_model(db, harness, "code", "r7")

    response = client.post(
        f"/workflow-runs/{harness.run.id}/nodes/{original.id}/retry",
        headers=auth_headers,
        json={"replacement_provider_model_id": replacement.id},
    )
    assert response.status_code == 403
    unchanged = node_run_row(session_factory, harness.run.id, "code")
    assert unchanged.status == WorkflowNodeRunStatus.FAILED
