"""MA7.3b -- a workflow-dispatched Agent Run receives its upstream artifacts'
CONTENT, not only their ids.

Pre-MA7.3b, ``input_context_json`` for a workflow node held only
``upstream_artifact_ids`` (no ``kind``), and ``AgentExecutionService.
_build_extra_context`` returned ``None`` for it -- so a downstream agent (and
therefore an approved Planner output) never actually reached the Engineer's
prompt. These tests pin the correction: the real execution service, a fake
provider adapter, real artifact files with real sha256, and no change for any
non-workflow Agent Run. Disposable temp DBs and temp artifact dirs only.
"""

import hashlib

import pytest
from sqlalchemy import select

from app.config import settings
from app.db.enums import AgentRunStatus, ArtifactType, TaskRunStatus, VersionStatus
from app.models.artifacts_eval import Artifact
from app.models.observability import ExecutionEvent
from app.models.tasks import AgentRunAttempt
from app.prompt_builder import build_workflow_upstream_extra_context
from app.providers.base import InvokeResponse
from app.services.execution_service import AgentExecutionService, _MissingWorkflowContext
from tests.conftest import (
    make_agent,
    make_agent_run,
    make_agent_version,
    make_model,
    make_project,
    make_provider,
    make_provider_model,
    make_task,
    make_task_run,
)
from tests.test_execution_service import FakeAdapter, _factory_for, _manual


def _artifact(db, tmp_path, name, text, *, hash_override=None, write=True):
    producer = make_agent_run(db)
    path = tmp_path / name
    data = text.encode("utf-8")
    if write:
        path.write_bytes(data)
    artifact = Artifact(
        agent_run_id=producer.id,
        type=ArtifactType.REPORT,
        storage_ref=str(path),
        content_hash=hash_override if hash_override is not None else hashlib.sha256(data).hexdigest(),
    )
    db.add(artifact)
    db.flush()
    return artifact


def _downstream_run(db, upstream_ids, *, kind="workflow_upstream", with_context=True):
    project = make_project(db)
    task = make_task(db, project=project)
    task_run = make_task_run(db, task=task)
    model = make_model(db, canonical_model_id="free/model")
    provider = make_provider(db)
    from decimal import Decimal

    pm = make_provider_model(
        db, model=model, provider=provider, cost_input_per_mtok=Decimal(0), cost_output_per_mtok=Decimal(0)
    )
    agent = make_agent(db, project=project)
    agent_version = make_agent_version(db, agent=agent, status=VersionStatus.ACTIVE)
    agent_version.model_policy = _manual(pm)
    agent_run = make_agent_run(db, task_run=task_run, agent_version=agent_version)
    if with_context:
        agent_run.input_context_json = {
            "kind": kind,
            "upstream_artifact_ids": upstream_ids,
            "upstream_node_run_ids": ["nr-1"],
        }
    db.commit()
    return agent_run, task_run


# --- pure rendering -----------------------------------------------------------


def test_prompt_block_carries_id_hash_and_full_text_for_every_upstream_artifact():
    block = build_workflow_upstream_extra_context(
        [("art-1", "h1", "first text"), ("art-2", "h2", "second text")]
    )
    for expected in ("art-1", "h1", "first text", "art-2", "h2", "second text", "UPSTREAM WORKFLOW OUTPUT"):
        assert expected in block
    assert block.index("first text") < block.index("second text")


# --- _build_extra_context ------------------------------------------------------


def test_downstream_agent_receives_the_upstream_artifact_content(db, tmp_path):
    first = _artifact(db, tmp_path, "a.md", "PLAN: build the thing")
    second = _artifact(db, tmp_path, "b.md", "NOTES: watch the edge cases")
    agent_run, _ = _downstream_run(db, [first.id, second.id])

    context = AgentExecutionService(db)._build_extra_context(agent_run)

    assert "PLAN: build the thing" in context and "NOTES: watch the edge cases" in context
    assert first.id in context and first.content_hash in context
    assert context.index("PLAN: build the thing") < context.index("NOTES: watch the edge cases")


@pytest.mark.parametrize(
    "case",
    ["no_context", "empty_ids", "unrelated_kind"],
)
def test_runs_without_workflow_upstream_context_are_unchanged(db, tmp_path, case):
    """Non-workflow / non-lineage runs get no extra context, exactly as before."""
    if case == "no_context":
        agent_run, _ = _downstream_run(db, [], with_context=False)
    elif case == "empty_ids":
        agent_run, _ = _downstream_run(db, [])
    else:
        agent_run, _ = _downstream_run(db, ["ignored"], kind="something_else")
    assert AgentExecutionService(db)._build_extra_context(agent_run) is None


def test_missing_artifact_row_is_a_categorized_context_error(db, tmp_path):
    agent_run, _ = _downstream_run(db, ["no-such-artifact"])
    with pytest.raises(_MissingWorkflowContext, match="no longer exists"):
        AgentExecutionService(db)._build_extra_context(agent_run)


def test_missing_artifact_file_is_a_categorized_context_error(db, tmp_path):
    artifact = _artifact(db, tmp_path, "gone.md", "text", write=False)
    agent_run, _ = _downstream_run(db, [artifact.id])
    with pytest.raises(_MissingWorkflowContext, match="could not be read"):
        AgentExecutionService(db)._build_extra_context(agent_run)


def test_artifact_content_that_no_longer_matches_its_hash_is_refused(db, tmp_path):
    """What the human approved is what the agent receives -- or nothing."""
    artifact = _artifact(db, tmp_path, "a.md", "approved text")
    (tmp_path / "a.md").write_text("tampered after approval", encoding="utf-8")
    agent_run, _ = _downstream_run(db, [artifact.id])
    with pytest.raises(_MissingWorkflowContext, match="sha256"):
        AgentExecutionService(db)._build_extra_context(agent_run)


def test_undecodable_artifact_is_a_categorized_context_error(db, tmp_path):
    path = tmp_path / "bin.md"
    path.write_bytes(b"\xff\xfe\x00bad")
    artifact = Artifact(
        agent_run_id=make_agent_run(db).id,
        type=ArtifactType.REPORT,
        storage_ref=str(path),
        content_hash=None,
    )
    db.add(artifact)
    db.flush()
    agent_run, _ = _downstream_run(db, [artifact.id])
    with pytest.raises(_MissingWorkflowContext, match="could not be read"):
        AgentExecutionService(db)._build_extra_context(agent_run)


def test_artifact_without_a_recorded_hash_is_still_rendered(db, tmp_path):
    (tmp_path / "nohash.md").write_text("legacy artifact", encoding="utf-8")
    artifact = Artifact(
        agent_run_id=make_agent_run(db).id,
        type=ArtifactType.REPORT,
        storage_ref=str(tmp_path / "nohash.md"),
        content_hash=None,
    )
    db.add(artifact)
    db.flush()
    agent_run, _ = _downstream_run(db, [artifact.id])
    assert "legacy artifact" in AgentExecutionService(db)._build_extra_context(agent_run)


# --- through the real execute() -------------------------------------------------


@pytest.fixture()
def artifacts_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "artifacts_dir", tmp_path / "out")
    return tmp_path


def test_execute_sends_the_upstream_content_to_the_provider(db, artifacts_dir):
    upstream = _artifact(db, artifacts_dir, "plan.md", "PLAN: APPROVED-CONTENT-MARKER")
    agent_run, _task_run = _downstream_run(db, [upstream.id])
    adapter = FakeAdapter(responses=[InvokeResponse(text="done", tokens_in=5, tokens_out=2, latency_ms=1)])

    AgentExecutionService(db, adapter_factory=_factory_for(adapter)).execute(agent_run.id, worker_id="w")

    db.refresh(agent_run)
    assert agent_run.status == AgentRunStatus.COMPLETED
    (request,) = adapter.calls
    assert "PLAN: APPROVED-CONTENT-MARKER" in request.user_prompt
    assert upstream.id in request.user_prompt


def test_execute_fails_before_any_provider_call_when_upstream_content_is_altered(db, artifacts_dir):
    upstream = _artifact(db, artifacts_dir, "plan.md", "the approved plan")
    (artifacts_dir / "plan.md").write_text("a different plan", encoding="utf-8")
    agent_run, task_run = _downstream_run(db, [upstream.id])
    adapter = FakeAdapter(
        responses=[InvokeResponse(text="must never be requested", tokens_in=1, tokens_out=1)]
    )

    AgentExecutionService(db, adapter_factory=_factory_for(adapter)).execute(agent_run.id, worker_id="w")

    db.refresh(agent_run)
    db.refresh(task_run)
    assert adapter.calls == []  # no provider call, no spend
    assert agent_run.status == AgentRunStatus.FAILED and task_run.status == TaskRunStatus.FAILED
    failure = db.execute(
        select(ExecutionEvent).where(
            ExecutionEvent.agent_run_id == agent_run.id, ExecutionEvent.event_type == "agent_run.failed"
        )
    ).scalar_one()
    assert failure.error["category"] == "workflow_context_error"
    attempt = db.execute(
        select(AgentRunAttempt).where(AgentRunAttempt.agent_run_id == agent_run.id)
    ).scalar_one()
    assert attempt.error["category"] == "workflow_context_error"


def test_a_plain_single_agent_run_still_sends_only_the_task_prompt(db, artifacts_dir):
    agent_run, _ = _downstream_run(db, [], with_context=False)
    adapter = FakeAdapter(responses=[InvokeResponse(text="ok", tokens_in=1, tokens_out=1, latency_ms=1)])
    AgentExecutionService(db, adapter_factory=_factory_for(adapter)).execute(agent_run.id, worker_id="w")

    db.refresh(agent_run)
    assert agent_run.status == AgentRunStatus.COMPLETED
    assert "UPSTREAM WORKFLOW OUTPUT" not in adapter.calls[0].user_prompt
