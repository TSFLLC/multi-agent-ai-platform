"""MA7.5A -- the EVALUATION workflow node: Engineer -> Evaluation -> Human Approval.

A. Node contract: publish-time validation (structure, config allow-list,
   registry state, project isolation).
B. Start-time preflight: a deprecated/inactive bound version refuses the run
   BEFORE any work or cost; nothing is silently swapped for a newer version.
C. MA6 non-committing builder + public MA6 behavior unchanged.
D. PRODUCTION PATH: real WorkflowExecutionService, Worker, queue, MA6
   EvaluationExecutionService, AgentExecutionService and lifecycle -- only the
   provider is faked. Approve and reject; NOT_MET is evidence, not failure and
   never decides the approval.
E. Completion semantics: the node follows the EvaluationRun, not the evaluator
   AgentRun.
F. Failure: malformed output, provider failure, budget, missing/tampered
   subject, oversized context, deprecated-after-preflight, cancellation.
G. Exactly-once + recovery: duplicate dispatch, concurrent finalization,
   evaluator terminal before finalization, EvaluationRun terminal before node
   mapping, duplicate callbacks, claimed-without-job, restart sweeps.
H. Evaluator-input protections (bytes re-hash, size cap) and MA6 CAS finalize.

Every database is a disposable temp file; artifacts/logs go to tmp dirs.
"""

import ast
import inspect
import json
import textwrap
import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text

import app.services.evaluation_execution_service as evaluation_service_module
import app.services.execution_service as execution_service_module
from app.config import Settings, settings
from app.db.enums import (
    AgentRunRole,
    AgentRunStatus,
    ApprovalStatus,
    EvaluationFinding,
    EvaluationMethod,
    EvaluationRunStatus,
    JobQueueStatus,
    TaskRunStatus,
    VersionStatus,
    WorkflowNodeRunStatus,
    WorkflowNodeType,
    WorkflowRunStatus,
)
from app.errors import ArtifactHashMismatchError, ConflictError
from app.models.artifacts_eval import Artifact
from app.models.evaluation_runs import EvaluationCriterionResult, EvaluationRun
from app.models.execution import JobQueue
from app.models.governance import Approval
from app.models.identity import Project
from app.models.observability import ExecutionEvent
from app.models.providers import Provider
from app.models.tasks import AgentRun, AgentRunAttempt, Task, TaskRun
from app.models.workflow import WorkflowNodeRun, WorkflowRun
from app.providers.base import ProviderInvalidResponseError
from app.repositories.job_queue_repository import JobQueueRepository
from app.services.evaluation_execution_service import (
    EvaluationExecutionService,
    verify_artifact_bytes,
)
from app.services.execution_service import AgentExecutionService, _EvaluationContextTooLarge
from app.services.workflow_definition_service import WorkflowDefinitionService
from app.services.workflow_execution_service import WorkflowExecutionError, WorkflowExecutionService
from app.services.workflow_validation_service import DAGValidationError
from app.worker import Worker
from tests.conftest import (
    make_agent,
    make_agent_run,
    make_budget,
    make_model,
    make_provider_model,
    make_runnable_agent_version,
)
from tests.ma7_3b_support import AGENT, APPROVAL, TERMINAL, events, resolve_via_api, snapshot, start
from tests.ma7_4a_support import finish_agent
from tests.ma7_4b_support import (
    EVALUATION,
    RoleProvider,
    build_graph,
    evaluator_json,
    install_provider,
    make_evaluation_setup,
    new_worker,
    running_worker,
    wait_until,
)

CRITERIA = ("correctness", "security")


@pytest.fixture()
def artifacts_dir(monkeypatch, tmp_path):
    path = tmp_path / "artifacts"
    monkeypatch.setattr(settings, "artifacts_dir", path)
    return path


# -- helpers ----------------------------------------------------------------------------


def rows(session_factory, model, *criteria):
    session = session_factory()
    try:
        return session.query(model).filter(*criteria).all()
    finally:
        session.close()


def totals(state):
    return (state["agent_runs_total"], state["task_runs_total"], state["jobs_total"])


def delta(before, after):
    return tuple(a - b for a, b in zip(totals(after), totals(before)))


def counts(session_factory):
    """Every row family this feature must create exactly once (or not at all)."""
    return {
        "tasks": len(rows(session_factory, Task)),
        "task_runs": len(rows(session_factory, TaskRun)),
        "agent_runs": len(rows(session_factory, AgentRun)),
        "evaluation_runs": len(rows(session_factory, EvaluationRun)),
        "jobs": len(rows(session_factory, JobQueue)),
        "criterion_results": len(rows(session_factory, EvaluationCriterionResult)),
        "approvals": len(rows(session_factory, Approval)),
    }


def count_events(session_factory, task_run_id, event_type, node_run_id=None):
    return len(
        [e for e in events(session_factory, task_run_id) if e[0] == event_type and node_run_id in (None, e[3])]
    )


def in_threads(count, work, *, timeout=60):
    barrier = threading.Barrier(count, timeout=20)

    def wrapped(i):
        barrier.wait()
        return work(i)

    with ThreadPoolExecutor(max_workers=count) as pool:
        futures = [pool.submit(wrapped, i) for i in range(count)]
        return [future.result(timeout=timeout) for future in futures]


def drain(worker, limit=30):
    processed = 0
    while worker.run_once():
        processed += 1
        assert processed <= limit
    return processed


def evaluation_run_of(session_factory):
    (run,) = rows(session_factory, EvaluationRun)
    return run


def results_of(session_factory, evaluation_run_id):
    return {
        r.criterion_key: r.finding
        for r in rows(session_factory, EvaluationCriterionResult, EvaluationCriterionResult.evaluation_run_id == evaluation_run_id)
    }


def read_artifact_text(session_factory, artifact_id):
    session = session_factory()
    try:
        return Path(session.get(Artifact, artifact_id).storage_ref).read_text(encoding="utf-8")
    finally:
        session.close()


class Harness(SimpleNamespace):
    """Engineer -> Evaluation -> (Human Approval | post agent) -> TERMINAL, with
    real inference plumbing and a fake provider."""


def build_harness(
    db,
    project,
    monkeypatch,
    label,
    *,
    findings=None,
    evaluator_text=None,
    engineer_text="OUTPUT-OF-engineer",
    priced=False,
    downstream="gate",
    override_model=False,
):
    setup = make_evaluation_setup(db, project, label, CRITERIA, priced=priced)
    extra_role_of = {}
    if override_model:  # the workflow FREEZES a model-policy override that points at a DIFFERENT model
        canonical = f"fake/{label}-override"
        override_pm = make_provider_model(
            db,
            model=make_model(db, canonical_model_id=canonical),
            provider=db.query(Provider).first(),
            cost_input_per_mtok=Decimal(0),
            cost_output_per_mtok=Decimal(0),
        )
        setup.override = {"mode": "manual", "manual_provider_model_id": override_pm.id}
        extra_role_of[canonical] = "override-evaluator"
        db.commit()
    tail = [("gate", APPROVAL)] if downstream == "gate" else [("post", AGENT)]
    nodes = [("engineer", AGENT), ("evaluation", EVALUATION)] + tail + [("result", TERMINAL)]
    edges = [("engineer", "evaluation"), ("evaluation", tail[0][0]), (tail[0][0], "result")]
    graph = build_graph(db, project, nodes, edges, label=label, real=True, evaluations={"evaluation": setup})

    def text_for(role):
        if role in ("evaluator", "override-evaluator"):
            return evaluator_text if evaluator_text is not None else evaluator_json(CRITERIA, findings)
        if role == "engineer":
            return engineer_text
        return f"OUTPUT-OF-{role}"

    provider = install_provider(
        monkeypatch, RoleProvider({**graph.role_of, **setup.role_of, **extra_role_of}, text=text_for)
    )
    return Harness(graph=graph, built=graph.built, setup=setup, provider=provider, run=None)


def start_harness(db, harness):
    harness.run = start(db, harness.built)
    return harness.run


def engine_state(session_factory, harness):
    return snapshot(session_factory, harness.run.id)


# =============================================================================
# A. Node contract -- publish-time validation
# =============================================================================


def draft_workflow(db, project, label, *, config=None, parents=("engineer",), parent_kind=AGENT, node_kwargs=None):
    """engineer (+ optional extra parents) -> evaluation -> end, left UNPUBLISHED."""
    definitions = WorkflowDefinitionService(db)
    workflow = definitions.create_workflow(project.id, f"{label}-wf")
    version = definitions.get_latest_version(workflow.id)
    setup = make_evaluation_setup(db, project, label, CRITERIA)
    base = {
        "evaluation_definition_version_id": setup.version.id,
        "evaluator_agent_version_id": setup.agent_version.id,
    }
    cfg = base if config is None else config(base, setup) if callable(config) else config

    made = {}
    for key in parents:
        if parent_kind == AGENT:
            made[key] = definitions.add_node(
                workflow.id,
                version.version,
                key,
                WorkflowNodeType.AGENT,
                config={"agent_version_id": make_runnable_agent_version(db, make_agent(db, project, key)).id},
            )
        else:
            made[key] = definitions.add_node(
                workflow.id, version.version, key, WorkflowNodeType.HUMAN_APPROVAL, config={"approval_group": "g"}
            )
    evaluation = definitions.add_node(
        workflow.id, version.version, "evaluation", WorkflowNodeType.EVALUATION, config=cfg, **(node_kwargs or {})
    )
    end = definitions.add_node(workflow.id, version.version, "end", WorkflowNodeType.TERMINAL)
    for key in parents:
        definitions.add_edge(workflow.id, version.version, made[key].id, evaluation.id)
    definitions.add_edge(workflow.id, version.version, evaluation.id, end.id)
    return definitions, workflow, version, setup


def publish_issues(db, project, label, **kwargs):
    definitions, workflow, version, _setup = draft_workflow(db, project, label, **kwargs)
    with pytest.raises(DAGValidationError) as exc:
        definitions.publish_version(workflow.id, version.version)
    return exc.value.issues


def test_a_valid_evaluation_node_publishes_and_persists_its_type_and_frozen_config(db, bootstrap):
    definitions, workflow, version, setup = draft_workflow(db, bootstrap.project, "a1")
    published = definitions.publish_version(workflow.id, version.version)
    assert published.status == VersionStatus.ACTIVE
    node = next(n for n in published.nodes if n.node_key == "evaluation")
    assert node.node_type == WorkflowNodeType.EVALUATION
    assert node.config == {
        "evaluation_definition_version_id": setup.version.id,
        "evaluator_agent_version_id": setup.agent_version.id,
    }


def test_a_well_formed_manual_or_auto_model_policy_override_is_accepted(db, bootstrap):
    from app.db.enums import RouterFreePolicy

    for label, override in (
        ("a2m", {"mode": "manual", "manual_provider_model_id": make_provider_model(db).id}),
        ("a2a", {"mode": "auto", "auto_policy": next(iter(RouterFreePolicy)).value}),
    ):
        definitions, workflow, version, _ = draft_workflow(
            db, bootstrap.project, label, config=lambda base, s, o=override: {**base, "evaluator_model_policy_override": o}
        )
        assert definitions.publish_version(workflow.id, version.version).status == VersionStatus.ACTIVE


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ("text", "non-empty object"),
        ({}, "non-empty object"),
        ({"mode": "sideways"}, "mode must be"),
        ({"mode": "manual"}, "requires manual_provider_model_id"),
        ({"mode": "manual", "manual_provider_model_id": "no-such-model"}, "unknown provider model"),
        ({"mode": "auto", "auto_policy": "nonsense"}, "requires auto_policy"),
        ({"mode": "auto", "auto_policy": "x", "extra": 1}, "unsupported key"),
    ],
)
def test_a_malformed_model_policy_override_is_rejected(db, bootstrap, override, fragment):
    issues = publish_issues(
        db, bootstrap.project, "a3", config=lambda base, s: {**base, "evaluator_model_policy_override": override}
    )
    assert any(fragment in issue for issue in issues), issues


@pytest.mark.parametrize(
    "extra",
    [
        {"judge_agent_version_id": "x"},
        {"auto_approve": True},
        {"auto_reject": True},
        {"score": 1},
        {"threshold": 0.5},
        {"winner": "a"},
        {"rank": 1},
        {"approval_group": "g"},
        {"method": "deterministic"},
        {"decision_rule": "majority"},
    ],
)
def test_decision_judge_scoring_and_auto_approval_keys_are_rejected(db, bootstrap, extra):
    issues = publish_issues(db, bootstrap.project, "a4", config=lambda base, s: {**base, **extra})
    assert any("evidence only" in issue and next(iter(extra)) in issue for issue in issues), issues


def test_unknown_configuration_keys_fail_closed(db, bootstrap):
    issues = publish_issues(db, bootstrap.project, "a5", config=lambda base, s: {**base, "surprise": 1})
    assert any("unsupported configuration key" in issue and "surprise" in issue for issue in issues)


@pytest.mark.parametrize("missing", ["evaluation_definition_version_id", "evaluator_agent_version_id"])
def test_both_frozen_version_ids_are_required(db, bootstrap, missing):
    issues = publish_issues(
        db, bootstrap.project, "a6", config=lambda base, s: {k: v for k, v in base.items() if k != missing}
    )
    assert any(f"requires config.{missing}" in issue for issue in issues)
    issues = publish_issues(db, bootstrap.project, "a6b", config=lambda base, s: {**base, missing: 7})
    assert any(f"requires config.{missing}" in issue for issue in issues)


def test_unknown_registry_references_are_rejected(db, bootstrap):
    issues = publish_issues(
        db,
        bootstrap.project,
        "a7",
        config=lambda base, s: {
            "evaluation_definition_version_id": "no-such-version",
            "evaluator_agent_version_id": "no-such-agent-version",
        },
    )
    assert any("unknown evaluation definition version" in i for i in issues)
    assert any("unknown evaluator agent version" in i for i in issues)


def test_versions_from_another_project_are_rejected(db, bootstrap):
    other = Project(org_id=bootstrap.organization.id, name="a8-other")
    db.add(other)
    db.flush()
    foreign = make_evaluation_setup(db, other, "a8f", CRITERIA)
    issues = publish_issues(
        db,
        bootstrap.project,
        "a8",
        config=lambda base, s: {
            "evaluation_definition_version_id": foreign.version.id,
            "evaluator_agent_version_id": foreign.agent_version.id,
        },
    )
    assert any("evaluation definition version from a different project" in i for i in issues)
    assert any("evaluator agent version from a different project" in i for i in issues)


@pytest.mark.parametrize("status", [VersionStatus.DRAFT, VersionStatus.DEPRECATED, VersionStatus.RETIRED])
def test_inactive_definition_or_evaluator_versions_are_rejected(db, bootstrap, status):
    definitions, workflow, version, setup = draft_workflow(db, bootstrap.project, f"a9-{status.value}")
    setup.version.status = status
    setup.agent_version.status = status
    db.commit()
    with pytest.raises(DAGValidationError) as exc:
        definitions.publish_version(workflow.id, version.version)
    assert any("evaluation definition version" in i and "is not ACTIVE" in i for i in exc.value.issues)
    assert any("evaluator agent version" in i and "is not ACTIVE" in i for i in exc.value.issues)


def test_exactly_one_incoming_edge_is_required(db, bootstrap):
    issues = publish_issues(db, bootstrap.project, "a10", parents=("engineer", "reviewer"))
    assert any("exactly one incoming edge" in i and "found 2" in i for i in issues)


def test_an_evaluation_node_cannot_be_an_entry_node(db, bootstrap):
    issues = publish_issues(db, bootstrap.project, "a11", parents=())
    assert any("exactly one incoming edge" in i and "found 0" in i for i in issues)


def test_the_single_parent_must_be_an_agent_node(db, bootstrap):
    issues = publish_issues(db, bootstrap.project, "a12", parents=("gate",), parent_kind=APPROVAL)
    assert any("must be fed by an AGENT node" in i and "HUMAN_APPROVAL" in i for i in issues)


def test_node_level_iteration_and_timeout_settings_are_rejected(db, bootstrap):
    issues = publish_issues(db, bootstrap.project, "a13", node_kwargs={"timeout_seconds": 60})
    assert any("must not set max_iterations or timeout_seconds" in i for i in issues)


def test_judge_remains_unsupported_and_is_not_an_evaluation(db, session_factory, bootstrap):
    from tests.ma7_4a_support import single_unsupported_node

    version_id, task_run_id = single_unsupported_node(db, bootstrap.project, "a14", node_type=WorkflowNodeType.JUDGE)
    run = WorkflowExecutionService(db).start_workflow_run(version_id, task_run_id)
    state = snapshot(session_factory, run.id)
    assert state["run"] == WorkflowRunStatus.FAILED and state["nodes"]["j"] == WorkflowNodeRunStatus.FAILED
    assert counts(session_factory)["evaluation_runs"] == 0


# =============================================================================
# B. Start-time preflight
# =============================================================================


@pytest.mark.parametrize("what", ["definition_deprecated", "definition_retired", "agent_deprecated", "agent_draft"])
def test_a_version_deactivated_after_publish_refuses_the_run_before_any_work(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir, what
):
    harness = build_harness(db, bootstrap.project, monkeypatch, f"b1-{what}")
    if what.startswith("definition"):
        harness.setup.version.status = (
            VersionStatus.DEPRECATED if what == "definition_deprecated" else VersionStatus.RETIRED
        )
    else:
        harness.setup.agent_version.status = (
            VersionStatus.DEPRECATED if what == "agent_deprecated" else VersionStatus.DRAFT
        )
    db.commit()
    before = counts(session_factory)

    with pytest.raises(WorkflowExecutionError, match="no longer valid; refusing to start"):
        WorkflowExecutionService(db).start_workflow_run(harness.built.published.id, harness.built.task_run.id)

    assert counts(session_factory) == before  # no Planner/Engineer AgentRun, TaskRun, job, ...
    assert rows(session_factory, WorkflowRun) == []
    assert harness.provider.roles() == []  # and no provider call, so no cost


def test_the_preflight_never_substitutes_a_newer_version(db, session_factory, bootstrap, monkeypatch, artifacts_dir):
    harness = build_harness(db, bootstrap.project, monkeypatch, "b2")
    harness.setup.version.status = VersionStatus.DEPRECATED
    from app.models.evaluation_definitions import EvaluationCriterion, EvaluationDefinitionVersion

    newer = EvaluationDefinitionVersion(
        evaluation_definition_id=harness.setup.definition.id, version=2, status=VersionStatus.ACTIVE
    )
    db.add(newer)
    db.flush()
    db.add(EvaluationCriterion(evaluation_definition_version_id=newer.id, key="correctness", label="C", order_index=0))
    db.commit()

    with pytest.raises(WorkflowExecutionError, match=harness.setup.version.id):
        WorkflowExecutionService(db).start_workflow_run(harness.built.published.id, harness.built.task_run.id)

    node = next(n for n in harness.built.published.nodes if n.node_key == "evaluation")
    assert node.config["evaluation_definition_version_id"] == harness.setup.version.id  # still pinned to v1


def test_the_start_endpoint_reports_the_refusal_as_400(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch, artifacts_dir
):
    harness = build_harness(db, bootstrap.project, monkeypatch, "b3")
    harness.setup.version.status = VersionStatus.DEPRECATED
    db.commit()
    response = client.post(
        f"/workflows/{harness.built.workflow.id}/versions/{harness.built.published.version}/runs",
        headers=auth_headers,
        json={"task_run_id": harness.built.task_run.id},
    )
    assert response.status_code == 400 and "refusing to start" in response.text


def test_a_version_deactivated_between_preflight_and_dispatch_fails_the_node_closed(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir, tmp_path
):
    """The preflight passed and the Engineer ran; THEN the definition version was
    deprecated. The evaluation node fails closed at dispatch -- nothing is
    built, and no newer version is substituted."""
    harness = build_harness(db, bootstrap.project, monkeypatch, "b4")
    run = start_harness(db, harness)
    agent = db.get(AgentRun, snapshot(session_factory, run.id)["node_runs"]["engineer"].agent_run_id)
    finish_agent(db, agent, AgentRunStatus.COMPLETED, text="ENG", tmp_path=tmp_path)  # callback not run yet
    harness.setup.version.status = VersionStatus.DEPRECATED
    db.commit()
    before = counts(session_factory)

    WorkflowExecutionService(db).on_agent_run_complete(agent.id)

    final = snapshot(session_factory, run.id)
    assert final["nodes"]["evaluation"] == WorkflowNodeRunStatus.FAILED
    assert final["run"] == WorkflowRunStatus.FAILED
    assert counts(session_factory) == before  # nothing was built
    assert harness.provider.roles() == []


# =============================================================================
# C. MA6 non-committing builder
# =============================================================================


def subject_chain(db, session_factory, bootstrap, tmp_path, label):
    """A COMPLETED subject Agent Run with one hashed artifact on disk (MA6-style)."""
    from tests.conftest import make_artifact_with_content, make_task, make_task_run

    task = make_task(db, bootstrap.project)
    task_run = make_task_run(db, task, status=TaskRunStatus.COMPLETED)
    agent_run = make_agent_run(db, task_run=task_run, status=AgentRunStatus.COMPLETED)
    artifact = make_artifact_with_content(db, tmp_path, agent_run=agent_run, content=f"subject text {label}")
    setup = make_evaluation_setup(db, bootstrap.project, label, CRITERIA)
    db.commit()
    return SimpleNamespace(agent_run=agent_run, artifact=artifact, setup=setup, task_run=task_run, task=task)


def build_kwargs(chain, **extra):
    return dict(
        agent_run_id=chain.agent_run.id,
        subject_artifact_id=chain.artifact.id,
        evaluation_definition_version_id=chain.setup.version.id,
        evaluator_agent_version_id=chain.setup.agent_version.id,
        **extra,
    )


def test_the_builder_commits_nothing_enqueues_nothing_and_a_rollback_leaves_no_row(
    db, session_factory, bootstrap, tmp_path
):
    chain = subject_chain(db, session_factory, bootstrap, tmp_path, "c1")
    before = counts(session_factory)

    built = EvaluationExecutionService(db).build_agent_evaluator_run(**build_kwargs(chain))
    assert built.run.id and built.evaluator_agent_run.id  # flushed: ids exist inside the transaction
    assert counts(session_factory) == before  # ...but nothing is visible to anyone else
    db.rollback()

    assert counts(session_factory) == before  # a lost race / a refusal leaves NOTHING behind


def test_the_builder_constructs_the_exact_ma6_rows_and_the_caller_owns_the_commit(
    db, session_factory, bootstrap, tmp_path
):
    chain = subject_chain(db, session_factory, bootstrap, tmp_path, "c2")
    before = counts(session_factory)
    override = {"mode": "auto", "auto_policy": "x"}

    built = EvaluationExecutionService(db).build_agent_evaluator_run(
        **build_kwargs(
            chain,
            evaluator_model_policy_override=override,
            initial_task_run_status=TaskRunStatus.QUEUED,
            task_run_config_snapshot={"node_key": "n"},
            task_run_workflow_version_id=None,
            input_context_extra={"workflow_node_run_id": "wnr-1"},
        )
    )
    db.commit()

    after = counts(session_factory)
    assert {k: after[k] - before[k] for k in before} == {
        "tasks": 1,
        "task_runs": 1,
        "agent_runs": 1,
        "evaluation_runs": 1,
        "jobs": 0,  # the builder never enqueues
        "criterion_results": 0,
        "approvals": 0,
    }
    run, agent_run = built.run, built.evaluator_agent_run
    assert run.method == EvaluationMethod.AGENT_EVALUATOR and run.status == EvaluationRunStatus.RUNNING
    assert run.subject_agent_run_id == chain.agent_run.id and run.subject_artifact_id == chain.artifact.id
    assert run.subject_artifact_content_hash == chain.artifact.content_hash
    assert run.evaluator_agent_run_id == agent_run.id and run.evaluator_task_run_id == built.evaluator_task_run.id
    assert run.evaluator_agent_version_id == chain.setup.agent_version.id
    assert run.evaluator_model_policy_override_json == override
    assert agent_run.role == AgentRunRole.EVALUATOR and agent_run.model_policy_override_json == override
    assert built.evaluator_task_run.status == TaskRunStatus.QUEUED
    assert built.evaluator_task_run.config_snapshot == {"node_key": "n"}
    assert built.evaluator_task.title.startswith("Evaluation")
    assert agent_run.input_context_json["kind"] == "evaluation_request"
    assert agent_run.input_context_json["workflow_node_run_id"] == "wnr-1"  # lineage rides along, ignored by the prompt


def test_the_builder_raises_the_same_errors_as_before_and_leaves_nothing(db, session_factory, bootstrap, tmp_path):
    chain = subject_chain(db, session_factory, bootstrap, tmp_path, "c3")
    chain.setup.version.status = VersionStatus.DEPRECATED
    db.commit()
    before = counts(session_factory)
    with pytest.raises(ConflictError, match="not published"):
        EvaluationExecutionService(db).build_agent_evaluator_run(**build_kwargs(chain))
    db.rollback()
    assert counts(session_factory) == before


def test_the_public_ma6_method_still_commits_queues_and_enqueues_exactly_as_before(
    db, session_factory, bootstrap, tmp_path
):
    chain = subject_chain(db, session_factory, bootstrap, tmp_path, "c4")
    before = counts(session_factory)

    run = EvaluationExecutionService(db).create_agent_evaluator_run(**build_kwargs(chain, requested_by_user_id=None))

    after = counts(session_factory)
    assert after["jobs"] - before["jobs"] == 1 and after["evaluation_runs"] - before["evaluation_runs"] == 1
    session = session_factory()
    try:
        assert session.get(TaskRun, run.evaluator_task_run_id).status == TaskRunStatus.QUEUED
        job = session.query(JobQueue).filter(JobQueue.payload_ref == run.evaluator_agent_run_id).one()
        assert job.job_type.value == "agent_run"
    finally:
        session.close()


# =============================================================================
# D. PRODUCTION PATH -- Engineer -> Evaluation -> Human Approval -> TERMINAL
# =============================================================================


def run_to_waiting_gate(db, session_factory, harness):
    start_harness(db, harness)
    solo = new_worker(session_factory)
    assert solo.run_once() is True  # Engineer
    assert solo.run_once() is True  # Evaluator
    assert solo.run_once() is False
    return solo


def test_production_path_engineer_evaluation_approval_terminal_approve(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch, artifacts_dir
):
    harness = build_harness(db, bootstrap.project, monkeypatch, "d1", findings={"security": "NOT_MET"})
    run = start_harness(db, harness)
    parent = harness.built.task_run.id
    solo = new_worker(session_factory)

    # -- Engineer executes exactly once and produces one artifact
    assert solo.run_once() is True
    mid = snapshot(session_factory, run.id)
    assert harness.provider.roles() == ["engineer"]
    assert mid["nodes"]["engineer"] == WorkflowNodeRunStatus.COMPLETED
    engineer_run_id = mid["node_runs"]["engineer"].agent_run_id
    engineer_artifacts = rows(session_factory, Artifact, Artifact.agent_run_id == engineer_run_id)
    assert len(engineer_artifacts) == 1
    engineer_artifact = engineer_artifacts[0]

    # -- Evaluation dispatched exactly once: one evaluator Task/TaskRun/AgentRun and one EvaluationRun,
    #    the node RUNNING (NOT completed until the EvaluationRun completes)
    assert mid["nodes"]["evaluation"] == WorkflowNodeRunStatus.RUNNING
    assert mid["nodes"]["gate"] == WorkflowNodeRunStatus.PENDING and mid["approvals"] == []
    evaluation = evaluation_run_of(session_factory)
    assert evaluation.status == EvaluationRunStatus.RUNNING
    evaluator_tasks = rows(session_factory, Task, Task.title.like("Evaluation%"))
    evaluator_agent_runs = rows(session_factory, AgentRun, AgentRun.role == AgentRunRole.EVALUATOR)
    assert len(evaluator_tasks) == 1 and len(evaluator_agent_runs) == 1
    assert mid["node_runs"]["evaluation"].agent_run_id == evaluation.evaluator_agent_run_id == evaluator_agent_runs[0].id
    now = counts(session_factory)  # a fresh DB: exactly what this one workflow created
    assert (now["tasks"], now["task_runs"], now["agent_runs"], now["evaluation_runs"], now["jobs"]) == (2, 3, 2, 1, 2)

    # -- lineage / provenance
    assert evaluation.method == EvaluationMethod.AGENT_EVALUATOR
    assert evaluation.subject_agent_run_id == engineer_run_id
    assert evaluation.subject_artifact_id == engineer_artifact.id
    assert evaluation.subject_artifact_content_hash == engineer_artifact.content_hash
    assert evaluation.evaluation_definition_version_id == harness.setup.version.id
    assert evaluation.evaluator_agent_version_id == harness.setup.agent_version.id
    assert evaluation.evaluator_model_policy_override_json is None
    assert evaluation.requested_by_user_id is None

    # -- the evaluator executes; the node completes only after the EvaluationRun did
    assert solo.run_once() is True
    state = snapshot(session_factory, run.id)
    evaluation = evaluation_run_of(session_factory)
    assert evaluation.status == EvaluationRunStatus.COMPLETED
    assert state["nodes"]["evaluation"] == WorkflowNodeRunStatus.COMPLETED
    assert harness.provider.roles() == ["engineer", "evaluator"]  # each exactly once
    assert results_of(session_factory, evaluation.id) == {
        "correctness": EvaluationFinding.MET,
        "security": EvaluationFinding.NOT_MET,
    }
    prompt = harness.provider.prompt_of("evaluator")  # the evaluator got the frozen rubric and the exact subject
    assert "correctness" in prompt and "security" in prompt and "OUTPUT-OF-engineer" in prompt
    assert engineer_artifact.content_hash in prompt

    # -- the node exposes the evaluator's raw output artifact downstream
    evaluator_artifacts = rows(session_factory, Artifact, Artifact.agent_run_id == evaluation.evaluator_agent_run_id)
    assert len(evaluator_artifacts) == 1
    output = state["node_runs"]["evaluation"].output_snapshot_ref
    assert output == evaluator_artifacts[0].id
    assert json.loads(read_artifact_text(session_factory, output))["criteria"][1]["finding"] == "NOT_MET"

    # -- the Human Approval appears afterwards, binds THAT artifact/hash, and a NOT_MET does not decide it
    assert state["nodes"]["gate"] == WorkflowNodeRunStatus.WAITING_FOR_APPROVAL
    assert state["run"] == WorkflowRunStatus.NODE_WAITING_FOR_APPROVAL
    (approval,) = state["approvals"]
    assert approval.status == ApprovalStatus.PENDING and approval.resolved_by is None
    assert approval.bound_artifact_id == output
    evidence = client.get(f"/approvals/{approval.id}/evidence", headers=auth_headers).json()
    assert [(e["node_key"], e["artifact_id"], e["content_hash"]) for e in evidence["evidence"]] == [
        ("evaluation", output, evaluator_artifacts[0].content_hash)
    ]
    assert evidence["fingerprint_matches"] is True
    for _ in range(3):  # time, sweeps and further worker polls do not "resolve" it
        assert solo.run_once() is False
        new_worker(session_factory).reconcile_workflows()
    still = snapshot(session_factory, run.id)
    assert still["approvals"][0].status == ApprovalStatus.PENDING and still["run"] == WorkflowRunStatus.NODE_WAITING_FOR_APPROVAL

    # -- read model: the node run exposes its EvaluationRun; nothing else does
    nodes = {n["workflow_node_id"]: n for n in client.get(f"/workflow-runs/{run.id}/nodes", headers=auth_headers).json()}
    by_key = {key: nodes[harness.built.nodes[key].id] for key in harness.built.nodes}
    assert by_key["evaluation"]["evaluation_run_id"] == evaluation.id
    assert all(by_key[k]["evaluation_run_id"] is None for k in ("engineer", "gate", "result"))
    detail = client.get(f"/evaluation-runs/{evaluation.id}", headers=auth_headers).json()
    assert detail["evaluator_agent_version_id"] == harness.setup.agent_version.id
    assert detail["evaluation_definition"]["evaluation_definition_version_id"] == harness.setup.version.id
    assert detail["status"] == "completed" and detail["subject_artifact_id"] == engineer_artifact.id

    # -- the human approves; TERMINAL completes; the WorkflowRun completes
    assert resolve_via_api(client, auth_headers, approval).status_code == 200
    final = snapshot(session_factory, run.id)
    assert final["run"] == WorkflowRunStatus.COMPLETED and final["run_ended_at"] is not None
    assert set(final["nodes"].values()) == {WorkflowNodeRunStatus.COMPLETED}
    assert final["approvals"][0].status == ApprovalStatus.APPROVED

    # -- restart/reconciliation creates nothing
    before = counts(session_factory)
    events_before = [e[0] for e in events(session_factory, parent)]
    for _ in range(3):
        new_worker(session_factory).reconcile_workflows()
    in_threads(4, lambda _i: new_worker(session_factory).reconcile_workflows())
    assert counts(session_factory) == before
    assert [e[0] for e in events(session_factory, parent)] == events_before
    assert count_events(session_factory, parent, "workflow.completed") == 1


def test_production_path_human_reject_fails_the_run_and_preserves_the_evaluation(
    client, db, session_factory, auth_headers, bootstrap, monkeypatch, artifacts_dir
):
    harness = build_harness(
        db, bootstrap.project, monkeypatch, "d2", findings={"correctness": "NOT_MET", "security": "NOT_MET"}
    )
    solo = run_to_waiting_gate(db, session_factory, harness)
    approval = snapshot(session_factory, harness.run.id)["approvals"][0]
    evaluation_before = evaluation_run_of(session_factory)
    before = counts(session_factory)

    assert resolve_via_api(client, auth_headers, approval, approve=False).status_code == 200

    final = snapshot(session_factory, harness.run.id)
    assert final["run"] == WorkflowRunStatus.FAILED and final["run_ended_at"] is not None
    assert final["nodes"]["gate"] == WorkflowNodeRunStatus.FAILED
    assert final["nodes"]["result"] == WorkflowNodeRunStatus.PENDING
    assert final["nodes"]["evaluation"] == WorkflowNodeRunStatus.COMPLETED  # the evidence is preserved
    assert evaluation_run_of(session_factory).status == EvaluationRunStatus.COMPLETED
    assert results_of(session_factory, evaluation_before.id) == {
        "correctness": EvaluationFinding.NOT_MET,
        "security": EvaluationFinding.NOT_MET,
    }
    assert counts(session_factory) == before and solo.run_once() is False  # the rejection executed nothing


def test_all_findings_not_met_is_still_a_completed_node_and_a_pending_gate(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir
):
    harness = build_harness(
        db, bootstrap.project, monkeypatch, "d3", findings={"correctness": "NOT_MET", "security": "NOT_MET"}
    )
    run_to_waiting_gate(db, session_factory, harness)
    state = engine_state(session_factory, harness)
    assert state["nodes"]["evaluation"] == WorkflowNodeRunStatus.COMPLETED
    assert state["nodes"]["gate"] == WorkflowNodeRunStatus.WAITING_FOR_APPROVAL
    assert state["approvals"][0].status == ApprovalStatus.PENDING


def test_a_frozen_model_policy_override_is_what_the_evaluator_runs_on(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir
):
    harness = build_harness(db, bootstrap.project, monkeypatch, "d3o", override_model=True)
    run_to_waiting_gate(db, session_factory, harness)
    evaluation = evaluation_run_of(session_factory)
    assert harness.provider.roles() == ["engineer", "override-evaluator"]  # the override, not the agent version's own model
    assert evaluation.evaluator_model_policy_override_json == harness.setup.override
    evaluator_agent = db.get(AgentRun, evaluation.evaluator_agent_run_id)
    db.refresh(evaluator_agent)
    assert evaluator_agent.model_policy_override_json == harness.setup.override


def test_a_node_after_the_evaluation_receives_the_evaluator_output_as_upstream(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir
):
    harness = build_harness(db, bootstrap.project, monkeypatch, "d4", downstream="post")
    start_harness(db, harness)
    drain(new_worker(session_factory))

    state = engine_state(session_factory, harness)
    assert state["run"] == WorkflowRunStatus.COMPLETED
    evaluator_artifact = state["node_runs"]["evaluation"].output_snapshot_ref
    context = db.get(AgentRun, state["node_runs"]["post"].agent_run_id).input_context_json
    assert context["upstream_artifact_ids"] == [evaluator_artifact]
    assert [u["node_key"] for u in context["upstream"]] == ["evaluation"]
    assert '"criteria"' in harness.provider.prompt_of("post")  # the findings JSON reached the next agent


def test_the_whole_workflow_also_runs_under_worker_concurrency(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir
):
    harness = build_harness(db, bootstrap.project, monkeypatch, "d5", findings={"security": "PARTIAL"})
    start_harness(db, harness)
    with running_worker(session_factory, 2):
        assert wait_until(lambda: engine_state(session_factory, harness)["run"] == WorkflowRunStatus.NODE_WAITING_FOR_APPROVAL)
        assert wait_until(lambda: {j.status for j in rows(session_factory, JobQueue)} == {JobQueueStatus.DONE})
    assert sorted(harness.provider.roles()) == ["engineer", "evaluator"]
    assert results_of(session_factory, evaluation_run_of(session_factory).id)["security"] == EvaluationFinding.PARTIAL
    assert len(rows(session_factory, Approval)) == 1


def test_there_is_no_code_path_from_an_evaluation_to_an_approval_decision():
    """Structural guard: the evaluation modules and the engine's evaluation
    methods never touch ApprovalService or approval state."""
    def identifiers(source):
        tree = ast.parse(textwrap.dedent(source))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        names |= {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
        names |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        return names

    checked = [identifiers(inspect.getsource(evaluation_service_module))] + [
        identifiers(inspect.getsource(method))
        for method in (WorkflowExecutionService._dispatch_evaluation, WorkflowExecutionService._evaluation_outcome)
    ]
    for names in checked:
        assert not [n for n in names if "approval" in n.lower()], names  # docstrings may say it; code may not touch it


# =============================================================================
# E/F. Completion semantics and failures
# =============================================================================


def failing_state(db, session_factory, harness):
    start_harness(db, harness)
    drain(new_worker(session_factory))
    return engine_state(session_factory, harness)


@pytest.mark.parametrize("fenced", [False, True])
def test_a_fenced_valid_response_is_accepted_like_any_other_valid_response(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir, fenced
):
    harness = build_harness(
        db, bootstrap.project, monkeypatch, f"e0-{fenced}", evaluator_text=evaluator_json(CRITERIA, fence=fenced)
    )
    run_to_waiting_gate(db, session_factory, harness)
    assert evaluation_run_of(session_factory).status == EvaluationRunStatus.COMPLETED


@pytest.mark.parametrize(
    ("text_value", "fragment"),
    [
        ("this is not json", "not valid JSON"),
        (json.dumps({"criteria": [{"key": "correctness", "finding": "MET", "rationale": "r"}]}), "criteri"),
        (
            json.dumps(
                {"criteria": [{"key": k, "finding": "AMAZING", "rationale": "r"} for k in CRITERIA]}
            ),
            "invalid finding",
        ),
        (
            json.dumps({"criteria": [{"key": "made_up", "finding": "MET", "rationale": "r"}]}),
            "unknown criterion key",
        ),
    ],
)
def test_a_completed_evaluator_with_a_rejected_response_fails_the_node_not_completes_it(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir, text_value, fragment
):
    """THE completion rule: evaluator AgentRun COMPLETED + EvaluationRun FAILED
    (parser rejected it) => the workflow node is FAILED."""
    harness = build_harness(db, bootstrap.project, monkeypatch, "e1", evaluator_text=text_value)
    state = failing_state(db, session_factory, harness)

    evaluation = evaluation_run_of(session_factory)
    evaluator_agent = db.get(AgentRun, evaluation.evaluator_agent_run_id)
    db.refresh(evaluator_agent)
    assert evaluator_agent.status == AgentRunStatus.COMPLETED  # the inference itself succeeded ...
    assert evaluation.status == EvaluationRunStatus.FAILED and fragment in (evaluation.failure_reason or "")
    assert results_of(session_factory, evaluation.id) == {}  # nothing partially trusted
    assert state["nodes"]["evaluation"] == WorkflowNodeRunStatus.FAILED  # ... but the node follows the EvaluationRun
    assert state["run"] == WorkflowRunStatus.FAILED and state["run_ended_at"] is not None
    assert state["nodes"]["gate"] == WorkflowNodeRunStatus.PENDING and state["approvals"] == []  # never reached
    assert count_events(session_factory, harness.built.task_run.id, "workflow.failed") == 1


def test_a_provider_failure_fails_the_evaluation_and_the_node(db, session_factory, bootstrap, monkeypatch, artifacts_dir):
    harness = build_harness(db, bootstrap.project, monkeypatch, "e2")
    harness.provider.hooks["evaluator"] = lambda _r: (_ for _ in ()).throw(ProviderInvalidResponseError("boom"))
    state = failing_state(db, session_factory, harness)
    evaluation = evaluation_run_of(session_factory)
    assert evaluation.status == EvaluationRunStatus.FAILED and "provider_invalid_response" in evaluation.failure_reason
    assert state["nodes"]["evaluation"] == WorkflowNodeRunStatus.FAILED and state["run"] == WorkflowRunStatus.FAILED
    assert results_of(session_factory, evaluation.id) == {}


def test_budget_exhaustion_fails_the_evaluation_before_the_provider_is_called(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir
):
    harness = build_harness(db, bootstrap.project, monkeypatch, "e3", priced=True)
    budget = make_budget(db, bootstrap.project, limit_amount="0.01")
    harness.built.task_run.budget_id = budget.id  # nodes share the workflow's budget
    db.commit()
    state = failing_state(db, session_factory, harness)

    evaluation = evaluation_run_of(session_factory)
    assert harness.provider.roles() == ["engineer"]  # the free Engineer ran; the priced evaluator never reached the model
    assert evaluation.status == EvaluationRunStatus.FAILED and "budget_exceeded" in evaluation.failure_reason
    assert state["nodes"]["evaluation"] == WorkflowNodeRunStatus.FAILED and state["run"] == WorkflowRunStatus.FAILED
    evaluator_task_run = next(iter(rows(session_factory, TaskRun, TaskRun.id == evaluation.evaluator_task_run_id)))
    assert evaluator_task_run.budget_id == budget.id  # accounted against the workflow's budget


def stub_engineer_workflow(db, session_factory, bootstrap, label):
    """Engineer (stub agent, driven by hand) -> Evaluation -> gate -> end."""
    setup = make_evaluation_setup(db, bootstrap.project, label, CRITERIA)
    graph = build_graph(
        db,
        bootstrap.project,
        [("engineer", AGENT), ("evaluation", EVALUATION), ("gate", APPROVAL), ("result", TERMINAL)],
        [("engineer", "evaluation"), ("evaluation", "gate"), ("gate", "result")],
        label=label,
        evaluations={"evaluation": setup},
    )
    run = start(db, graph.built)
    agent = db.get(AgentRun, snapshot(session_factory, run.id)["node_runs"]["engineer"].agent_run_id)
    return graph, run, agent, setup


@pytest.mark.parametrize("damage", ["delete_file", "edit_file", "alter_hash", "delete_row"])
def test_a_missing_or_tampered_subject_fails_the_node_at_dispatch_and_builds_nothing(
    db, session_factory, bootstrap, tmp_path, damage
):
    graph, run, agent, _setup = stub_engineer_workflow(db, session_factory, bootstrap, f"f1-{damage}")
    finish_agent(db, agent, AgentRunStatus.COMPLETED, text="ENGINEER OUTPUT", tmp_path=tmp_path)  # callback not yet run
    artifact = db.query(Artifact).filter(Artifact.agent_run_id == agent.id).one()
    if damage == "delete_file":
        Path(artifact.storage_ref).unlink()
    elif damage == "edit_file":
        Path(artifact.storage_ref).write_text("ALTERED AFTER THE FACT", encoding="utf-8")
    elif damage == "alter_hash":
        db.execute(text("UPDATE artifacts SET content_hash = :h WHERE id = :id"), {"h": "e" * 64, "id": artifact.id})
        db.commit()
    elif damage == "delete_row":
        db.execute(text("DELETE FROM artifacts WHERE id = :id"), {"id": artifact.id})
        db.commit()
    before = counts(session_factory)

    WorkflowExecutionService(db).on_agent_run_complete(agent.id)

    state = snapshot(session_factory, run.id)
    assert state["nodes"]["evaluation"] == WorkflowNodeRunStatus.FAILED
    assert state["run"] == WorkflowRunStatus.FAILED and state["nodes"]["gate"] == WorkflowNodeRunStatus.PENDING
    assert counts(session_factory) == before  # no evaluator Task/TaskRun/AgentRun, no EvaluationRun, no job
    failed = [e for e in events(session_factory, graph.built.task_run.id) if e[0] == "workflow.node.failed"]
    assert len(failed) == 1


def test_a_subject_altered_between_dispatch_and_evaluator_execution_fails_before_inference(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir
):
    harness = build_harness(db, bootstrap.project, monkeypatch, "f2")
    start_harness(db, harness)
    solo = new_worker(session_factory)
    assert solo.run_once() is True  # Engineer; the evaluator job is now queued
    subject = rows(session_factory, Artifact, Artifact.agent_run_id == evaluation_run_of(session_factory).subject_agent_run_id)[0]
    Path(subject.storage_ref).write_text("ALTERED while the evaluator job waited", encoding="utf-8")

    assert solo.run_once() is True  # the evaluator job runs against altered bytes

    evaluation = evaluation_run_of(session_factory)
    state = engine_state(session_factory, harness)
    assert harness.provider.roles() == ["engineer"]  # NO provider call for the evaluator
    assert evaluation.status == EvaluationRunStatus.FAILED and "evaluation_context_error" in evaluation.failure_reason
    assert "no longer matches" in evaluation.failure_reason
    assert state["nodes"]["evaluation"] == WorkflowNodeRunStatus.FAILED and results_of(session_factory, evaluation.id) == {}


def test_an_oversized_subject_fails_before_inference_with_an_actionable_content_free_error(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir
):
    monkeypatch.setattr(settings, "evaluation_context_max_chars", 4000)
    harness = build_harness(db, bootstrap.project, monkeypatch, "f3", engineer_text="SECRETCONTENT " * 500)
    state = failing_state(db, session_factory, harness)

    evaluation = evaluation_run_of(session_factory)
    assert harness.provider.roles() == ["engineer"]
    assert evaluation.status == EvaluationRunStatus.FAILED
    assert "evaluation_context_too_large" in evaluation.failure_reason
    assert "exceeds the limit of 4000 characters" in evaluation.failure_reason
    assert "platform cap" in evaluation.failure_reason and "never truncated" in evaluation.failure_reason
    assert "SECRETCONTENT" not in evaluation.failure_reason  # no artifact content in the error
    assert state["nodes"]["evaluation"] == WorkflowNodeRunStatus.FAILED
    attempt = rows(session_factory, AgentRunAttempt, AgentRunAttempt.agent_run_id == evaluation.evaluator_agent_run_id)
    assert len(attempt) == 1 and attempt[0].error["category"] == "evaluation_context_too_large"  # non-retryable


def test_within_the_cap_the_subject_reaches_the_evaluator_complete_and_untruncated(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir
):
    monkeypatch.setattr(settings, "evaluation_context_max_chars", 100_000)
    harness = build_harness(db, bootstrap.project, monkeypatch, "f4", engineer_text="X" * 20_000)
    run_to_waiting_gate(db, session_factory, harness)
    assert harness.provider.prompt_of("evaluator").count("X") == 20_000


def _fake_resolved(window):
    return SimpleNamespace(model=SimpleNamespace(context_window=window))


def test_the_evaluator_cap_rules_platform_cap_authoritative_window_only_lowers_it(db):
    service = AgentExecutionService(db)

    def check(size, resolved, cap=10_000):
        service._enforce_context_limit(
            size,
            1,
            resolved,
            cap=cap,
            cap_name="platform cap",
            error_cls=_EvaluationContextTooLarge,
            what="The evaluator input for this run",
        )

    check(10_000, None)  # exactly at the cap
    check(9_999, _fake_resolved(None))  # unknown window: nothing invented
    with pytest.raises(_EvaluationContextTooLarge, match="exceeds the limit of 10000"):
        check(10_001, _fake_resolved(None))
    with pytest.raises(_EvaluationContextTooLarge, match=r"model context window \(1000 tokens\)"):
        check(2_500, _fake_resolved(1000))  # 1000 tokens * 2 chars = 2000 < cap
    check(1_999, _fake_resolved(1000))
    with pytest.raises(_EvaluationContextTooLarge, match="platform cap"):
        check(10_001, _fake_resolved(10_000_000))  # a huge window never RAISES the cap


def test_the_evaluation_cap_setting_is_bounded():
    assert Settings().evaluation_context_max_chars == 200_000
    for bad in (0, 999, 10_000_001):
        with pytest.raises(ValueError):
            Settings(evaluation_context_max_chars=bad)


# -- cancellation --------------------------------------------------------------------------


def test_cancelling_while_the_evaluator_job_is_queued_cancels_the_evaluation_and_the_node(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir
):
    harness = build_harness(db, bootstrap.project, monkeypatch, "g1")
    start_harness(db, harness)
    solo = new_worker(session_factory)
    assert solo.run_once() is True  # Engineer; evaluator job queued
    WorkflowExecutionService(db).cancel_workflow_run(harness.run.id, cancelled_by_user_id=bootstrap.user.id)
    assert engine_state(session_factory, harness)["run"] == WorkflowRunStatus.CANCELLING

    assert solo.run_once() is True  # the queued evaluator stops at its first checkpoint

    state = engine_state(session_factory, harness)
    assert harness.provider.roles() == ["engineer"]
    assert evaluation_run_of(session_factory).status == EvaluationRunStatus.CANCELLED
    assert state["nodes"]["evaluation"] == WorkflowNodeRunStatus.CANCELLED
    assert state["run"] == WorkflowRunStatus.CANCELLED and state["run_ended_at"] is not None
    assert state["nodes"]["engineer"] == WorkflowNodeRunStatus.COMPLETED  # completed work preserved
    assert results_of(session_factory, evaluation_run_of(session_factory).id) == {}


def test_cancelling_an_in_flight_evaluation_discards_it_even_though_the_call_returns(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir
):
    harness = build_harness(db, bootstrap.project, monkeypatch, "g2")
    in_flight, release = threading.Event(), threading.Event()

    def evaluator(_request):
        in_flight.set()
        assert release.wait(30)

    harness.provider.hooks["evaluator"] = evaluator
    start_harness(db, harness)
    with running_worker(session_factory, 1):
        assert wait_until(in_flight.is_set)
        WorkflowExecutionService(db).cancel_workflow_run(harness.run.id, cancelled_by_user_id=bootstrap.user.id)
        release.set()
        assert wait_until(lambda: engine_state(session_factory, harness)["run"] == WorkflowRunStatus.CANCELLED)

    state = engine_state(session_factory, harness)
    evaluation = evaluation_run_of(session_factory)
    assert evaluation.status == EvaluationRunStatus.CANCELLED and state["nodes"]["evaluation"] == WorkflowNodeRunStatus.CANCELLED
    assert results_of(session_factory, evaluation.id) == {}  # a cancelled evaluation never publishes findings


# =============================================================================
# G. Exactly-once + recovery
# =============================================================================


def ready_evaluation_undispatched(db, session_factory, bootstrap, tmp_path, monkeypatch, label):
    """Engineer COMPLETED (real artifact), the evaluation node READY but not dispatched."""
    graph, run, agent, setup = stub_engineer_workflow(db, session_factory, bootstrap, label)
    finish_agent(db, agent, AgentRunStatus.COMPLETED, text="ENGINEER OUTPUT", tmp_path=tmp_path)
    real = WorkflowExecutionService._dispatch_node_for_execution
    monkeypatch.setattr(WorkflowExecutionService, "_dispatch_node_for_execution", lambda self, nr: None)
    WorkflowExecutionService(db).on_agent_run_complete(agent.id)
    monkeypatch.setattr(WorkflowExecutionService, "_dispatch_node_for_execution", real)
    assert snapshot(session_factory, run.id)["nodes"]["evaluation"] == WorkflowNodeRunStatus.PENDING
    return graph, run, agent, setup


def test_duplicate_dispatch_creates_exactly_one_of_everything_and_losers_leave_nothing(
    db, session_factory, bootstrap, tmp_path, monkeypatch
):
    for round_number in range(4):
        _graph, run, _agent, _setup = ready_evaluation_undispatched(
            db, session_factory, bootstrap, tmp_path, monkeypatch, f"g3-{round_number}"
        )
        node_run_id = snapshot(session_factory, run.id)["node_runs"]["evaluation"].id
        before = counts(session_factory)

        def dispatch(_i, node_run_id=node_run_id):
            session = session_factory()
            try:
                WorkflowExecutionService(session)._dispatch_node_for_execution(session.get(WorkflowNodeRun, node_run_id))
            finally:
                session.close()

        in_threads(6, dispatch)

        after = counts(session_factory)
        assert {k: after[k] - before[k] for k in before} == {
            "tasks": 1,
            "task_runs": 1,
            "agent_runs": 1,
            "evaluation_runs": 1,
            "jobs": 1,
            "criterion_results": 0,
            "approvals": 0,
        }, round_number
        state = snapshot(session_factory, run.id)
        assert state["nodes"]["evaluation"] == WorkflowNodeRunStatus.RUNNING
        evaluation = [e for e in rows(session_factory, EvaluationRun) if e.evaluator_agent_run_id == state["node_runs"]["evaluation"].agent_run_id]
        assert len(evaluation) == 1


def test_an_artifact_that_no_longer_belongs_to_the_upstream_agent_run_is_refused_by_the_engine(
    db, session_factory, bootstrap, tmp_path, monkeypatch
):
    graph, run, _agent, _setup = ready_evaluation_undispatched(
        db, session_factory, bootstrap, tmp_path, monkeypatch, "g3o"
    )
    engineer_ref = snapshot(session_factory, run.id)["node_runs"]["engineer"].output_snapshot_ref
    other = make_agent_run(db)  # some OTHER agent run
    db.commit()
    db.execute(text("UPDATE artifacts SET agent_run_id = :a WHERE id = :id"), {"a": other.id, "id": engineer_ref})
    db.commit()
    before = counts(session_factory)

    WorkflowExecutionService(db)._schedule_ready_nodes(run.id)

    state = snapshot(session_factory, run.id)
    assert state["nodes"]["evaluation"] == WorkflowNodeRunStatus.FAILED and state["run"] == WorkflowRunStatus.FAILED
    assert counts(session_factory) == before  # nothing built
    session = session_factory()
    try:
        error = (
            session.query(ExecutionEvent)
            .filter(
                ExecutionEvent.task_run_id == graph.built.task_run.id,
                ExecutionEvent.event_type == "workflow.node.failed",
            )
            .one()
            .error
        )
    finally:
        session.close()
    # the ENGINE's own ownership check refused it (not merely MA6's builder, one step later)
    assert "does not belong to the upstream Agent Run" in json.dumps(error)


def test_a_dispatch_into_a_cancelled_run_builds_nothing(db, session_factory, bootstrap, tmp_path, monkeypatch):
    _graph, run, _agent, _setup = ready_evaluation_undispatched(db, session_factory, bootstrap, tmp_path, monkeypatch, "g4")
    node_run_id = snapshot(session_factory, run.id)["node_runs"]["evaluation"].id
    db.execute(text("UPDATE workflow_runs SET status='cancelling' WHERE id=:id"), {"id": run.id})
    db.commit()
    before = counts(session_factory)
    monkeypatch.setattr(WorkflowExecutionService, "_accepts_dispatch", lambda self, run_id: True)  # bypass the pre-check
    session = session_factory()
    try:
        WorkflowExecutionService(session)._dispatch_node_for_execution(session.get(WorkflowNodeRun, node_run_id))
    finally:
        session.close()
    assert counts(session_factory) == before  # the claim refused; every built row was rolled back
    assert snapshot(session_factory, run.id)["nodes"]["evaluation"] == WorkflowNodeRunStatus.PENDING


def evaluator_finished_but_unfinalized(db, session_factory, bootstrap, monkeypatch, label, *, evaluator_text=None):
    """Real path up to the evaluator's own completion -- with BOTH the MA6
    terminal notification and the worker's workflow callback suppressed: the
    evaluator AgentRun is COMPLETED, the EvaluationRun still RUNNING and the
    node still RUNNING (a crash right after the TaskRun's terminal commit)."""
    harness = build_harness(db, bootstrap.project, monkeypatch, label, evaluator_text=evaluator_text)
    start_harness(db, harness)
    solo = new_worker(session_factory)
    assert solo.run_once() is True  # Engineer (callback + dispatch work normally)
    real_notify, real_callback = execution_service_module.notify_evaluation_run_terminal, Worker._reconcile_workflow_node
    monkeypatch.setattr(execution_service_module, "notify_evaluation_run_terminal", lambda db_, task_run: None)
    monkeypatch.setattr(Worker, "_reconcile_workflow_node", lambda self, db_, agent_run_id: None)
    assert solo.run_once() is True  # the evaluator executes
    monkeypatch.setattr(execution_service_module, "notify_evaluation_run_terminal", real_notify)
    monkeypatch.setattr(Worker, "_reconcile_workflow_node", real_callback)
    evaluation = evaluation_run_of(session_factory)
    evaluator_agent = db.get(AgentRun, evaluation.evaluator_agent_run_id)
    db.refresh(evaluator_agent)
    assert evaluator_agent.status == AgentRunStatus.COMPLETED
    assert evaluation.status == EvaluationRunStatus.RUNNING
    assert engine_state(session_factory, harness)["nodes"]["evaluation"] == WorkflowNodeRunStatus.RUNNING
    return harness


def test_terminal_evaluator_before_finalization_is_healed_by_reconciliation_exactly_once(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir
):
    harness = evaluator_finished_but_unfinalized(db, session_factory, bootstrap, monkeypatch, "g5")
    before = counts(session_factory)

    for _ in range(3):
        new_worker(session_factory).reconcile_workflows()
    in_threads(5, lambda _i: new_worker(session_factory).reconcile_workflows())

    evaluation = evaluation_run_of(session_factory)
    state = engine_state(session_factory, harness)
    assert evaluation.status == EvaluationRunStatus.COMPLETED
    assert len(results_of(session_factory, evaluation.id)) == 2  # one set of criterion results, never two
    assert state["nodes"]["evaluation"] == WorkflowNodeRunStatus.COMPLETED
    assert state["nodes"]["gate"] == WorkflowNodeRunStatus.WAITING_FOR_APPROVAL and len(state["approvals"]) == 1
    after = counts(session_factory)
    assert {k: after[k] - before[k] for k in before} == {
        "tasks": 0, "task_runs": 0, "agent_runs": 0, "evaluation_runs": 0, "jobs": 0, "criterion_results": 2, "approvals": 1,
    }
    parent = harness.built.task_run.id
    assert count_events(session_factory, parent, "workflow.node.completed", state["node_runs"]["evaluation"].id) == 1


def test_terminal_evaluator_with_a_malformed_response_is_healed_to_failed(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir
):
    harness = evaluator_finished_but_unfinalized(
        db, session_factory, bootstrap, monkeypatch, "g6", evaluator_text="not json at all"
    )
    for _ in range(2):
        new_worker(session_factory).reconcile_workflows()
    evaluation = evaluation_run_of(session_factory)
    state = engine_state(session_factory, harness)
    assert evaluation.status == EvaluationRunStatus.FAILED and results_of(session_factory, evaluation.id) == {}
    assert state["nodes"]["evaluation"] == WorkflowNodeRunStatus.FAILED and state["run"] == WorkflowRunStatus.FAILED


def test_evaluation_run_terminal_before_the_node_was_mapped_is_mapped_exactly_once(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir
):
    harness = build_harness(db, bootstrap.project, monkeypatch, "g7")
    start_harness(db, harness)
    solo = new_worker(session_factory)
    assert solo.run_once() is True
    real_callback = Worker._reconcile_workflow_node
    monkeypatch.setattr(Worker, "_reconcile_workflow_node", lambda self, db_, agent_run_id: None)  # the worker dies here
    assert solo.run_once() is True
    monkeypatch.setattr(Worker, "_reconcile_workflow_node", real_callback)
    assert evaluation_run_of(session_factory).status == EvaluationRunStatus.COMPLETED  # MA6 finalized it
    assert engine_state(session_factory, harness)["nodes"]["evaluation"] == WorkflowNodeRunStatus.RUNNING  # ...not mapped

    in_threads(5, lambda _i: new_worker(session_factory).reconcile_workflows())
    new_worker(session_factory).reconcile_workflows()

    state = engine_state(session_factory, harness)
    assert state["nodes"]["evaluation"] == WorkflowNodeRunStatus.COMPLETED
    assert state["nodes"]["gate"] == WorkflowNodeRunStatus.WAITING_FOR_APPROVAL and len(state["approvals"]) == 1
    node_run_id = state["node_runs"]["evaluation"].id
    assert count_events(session_factory, harness.built.task_run.id, "workflow.node.completed", node_run_id) == 1
    assert len(results_of(session_factory, evaluation_run_of(session_factory).id)) == 2


@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
def test_an_evaluation_run_terminal_status_maps_to_the_matching_node_status(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir, outcome
):
    """C. COMPLETED -> COMPLETED, FAILED -> FAILED, CANCELLED -> CANCELLED --
    driven directly at the EvaluationRun, whatever the evaluator AgentRun says."""
    harness = evaluator_finished_but_unfinalized(db, session_factory, bootstrap, monkeypatch, f"g8-{outcome}")
    evaluation = evaluation_run_of(session_factory)
    status = {
        "completed": EvaluationRunStatus.COMPLETED,
        "failed": EvaluationRunStatus.FAILED,
        "cancelled": EvaluationRunStatus.CANCELLED,
    }[outcome]
    db.execute(
        text("UPDATE evaluation_runs SET status = :s, ended_at = CURRENT_TIMESTAMP WHERE id = :id"),
        {"s": status.value, "id": evaluation.id},
    )
    db.commit()

    for _ in range(2):
        new_worker(session_factory).reconcile_workflows()

    node = engine_state(session_factory, harness)["nodes"]["evaluation"]
    assert node == {
        "completed": WorkflowNodeRunStatus.COMPLETED,
        "failed": WorkflowNodeRunStatus.FAILED,
        "cancelled": WorkflowNodeRunStatus.CANCELLED,
    }[outcome]


def test_duplicate_completion_callbacks_change_nothing(db, session_factory, bootstrap, monkeypatch, artifacts_dir):
    harness = build_harness(db, bootstrap.project, monkeypatch, "g9")
    run_to_waiting_gate(db, session_factory, harness)
    state = engine_state(session_factory, harness)
    agent_id = state["node_runs"]["evaluation"].agent_run_id
    before, types_before = counts(session_factory), [e[0] for e in events(session_factory, harness.built.task_run.id)]

    for _ in range(5):
        WorkflowExecutionService(db).on_agent_run_complete(agent_id)
    def replay(_i):
        session = session_factory()
        try:
            WorkflowExecutionService(session).on_agent_run_complete(agent_id)
        finally:
            session.close()

    in_threads(4, replay)

    assert counts(session_factory) == before
    assert [e[0] for e in events(session_factory, harness.built.task_run.id)] == types_before
    assert engine_state(session_factory, harness)["nodes"]["evaluation"] == WorkflowNodeRunStatus.COMPLETED


def test_a_claimed_evaluation_whose_job_was_never_enqueued_is_repaired_once(
    db, session_factory, bootstrap, tmp_path, monkeypatch
):
    _graph, run, _agent, _setup = ready_evaluation_undispatched(db, session_factory, bootstrap, tmp_path, monkeypatch, "g10")
    real_enqueue = JobQueueRepository.enqueue
    monkeypatch.setattr(
        JobQueueRepository, "enqueue", lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("died before enqueue"))
    )
    with pytest.raises(RuntimeError):
        WorkflowExecutionService(db)._schedule_ready_nodes(run.id)
    monkeypatch.setattr(JobQueueRepository, "enqueue", real_enqueue)
    stranded = snapshot(session_factory, run.id)
    assert stranded["nodes"]["evaluation"] == WorkflowNodeRunStatus.RUNNING  # claimed, all rows durable ...
    jobs_before = counts(session_factory)["jobs"]  # ... but no job for the evaluator

    for _ in range(3):
        new_worker(session_factory).reconcile_workflows()
    in_threads(4, lambda _i: new_worker(session_factory).reconcile_workflows())

    now = counts(session_factory)
    assert now["jobs"] == jobs_before + 1  # exactly one job, however often it runs
    assert now["evaluation_runs"] == 1 and now["agent_runs"] == 2


def test_concurrent_finalization_writes_one_result_set_and_a_stale_failure_cannot_overwrite_it(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir
):
    harness = evaluator_finished_but_unfinalized(db, session_factory, bootstrap, monkeypatch, "g11")
    evaluation = evaluation_run_of(session_factory)

    def finalize(i):
        session = session_factory()
        try:
            status = TaskRunStatus.COMPLETED if i % 2 == 0 else TaskRunStatus.FAILED  # conflicting stale claims
            EvaluationExecutionService(session).finalize_agent_evaluator_run(evaluation.id, task_run_status=status)
        finally:
            session.close()

    in_threads(8, finalize)

    final = evaluation_run_of(session_factory)
    results = results_of(session_factory, final.id)
    # exactly ONE finalizer won: either a clean COMPLETED with its full result set, or a clean FAILED with none --
    # never a mixture, never duplicates, never an exception escaping
    assert (final.status, len(results)) in {(EvaluationRunStatus.COMPLETED, 2), (EvaluationRunStatus.FAILED, 0)}
    assert final.ended_at is not None

    # and once decided, a stale contrary finalizer changes nothing
    winner = final.status
    session = session_factory()
    try:
        EvaluationExecutionService(session).finalize_agent_evaluator_run(
            final.id,
            task_run_status=TaskRunStatus.FAILED if winner == EvaluationRunStatus.COMPLETED else TaskRunStatus.COMPLETED,
        )
        assert EvaluationExecutionService(session)._cas_terminal(final.id, EvaluationRunStatus.FAILED) is False
        session.rollback()
    finally:
        session.close()
    again = evaluation_run_of(session_factory)
    assert again.status == winner and results_of(session_factory, again.id) == results
    assert harness is not None


def test_a_completed_evaluation_is_never_overwritten_by_a_late_failed_finalizer(
    db, session_factory, bootstrap, monkeypatch, artifacts_dir
):
    harness = build_harness(db, bootstrap.project, monkeypatch, "g12")
    run_to_waiting_gate(db, session_factory, harness)
    evaluation = evaluation_run_of(session_factory)
    assert evaluation.status == EvaluationRunStatus.COMPLETED
    before = results_of(session_factory, evaluation.id)

    for _ in range(3):
        EvaluationExecutionService(db).finalize_agent_evaluator_run(evaluation.id, task_run_status=TaskRunStatus.FAILED)
        EvaluationExecutionService(db).finalize_agent_evaluator_run(evaluation.id, task_run_status=TaskRunStatus.COMPLETED)

    after = evaluation_run_of(session_factory)
    assert after.status == EvaluationRunStatus.COMPLETED and after.failure_reason is None
    assert results_of(session_factory, after.id) == before


# =============================================================================
# H. MA6 integrity hardening (applies to every evaluator run, workflow or not)
# =============================================================================


def test_verify_artifact_bytes_rechecks_the_file_not_just_the_recorded_hash(db, tmp_path):
    from tests.conftest import make_artifact_with_content

    artifact = make_artifact_with_content(db, tmp_path, content="original")
    assert verify_artifact_bytes(artifact, artifact.content_hash) == b"original"
    Path(artifact.storage_ref).write_text("altered", encoding="utf-8")
    with pytest.raises(ArtifactHashMismatchError, match="no longer matches"):
        verify_artifact_bytes(artifact, artifact.content_hash)
    Path(artifact.storage_ref).unlink()
    with pytest.raises(ConflictError, match="could not be read"):
        verify_artifact_bytes(artifact, artifact.content_hash)


def test_a_plain_ma6_evaluation_of_a_tampered_subject_fails_before_inference(
    db, session_factory, bootstrap, tmp_path, monkeypatch, artifacts_dir
):
    chain = subject_chain(db, session_factory, bootstrap, tmp_path, "h2")
    provider = install_provider(monkeypatch, RoleProvider(chain.setup.role_of))
    run = EvaluationExecutionService(db).create_agent_evaluator_run(**build_kwargs(chain))
    Path(chain.artifact.storage_ref).write_text("tampered", encoding="utf-8")

    assert new_worker(session_factory).run_once() is True

    final = next(iter(rows(session_factory, EvaluationRun, EvaluationRun.id == run.id)))
    assert provider.roles() == []
    assert final.status == EvaluationRunStatus.FAILED and "evaluation_context_error" in final.failure_reason


def test_ma6_finalization_rechecks_the_subject_bytes_before_trusting_a_result(
    db, session_factory, bootstrap, tmp_path, artifacts_dir
):
    chain = subject_chain(db, session_factory, bootstrap, tmp_path, "h3")
    run = EvaluationExecutionService(db).create_agent_evaluator_run(**build_kwargs(chain))
    evaluator_agent = db.get(AgentRun, run.evaluator_agent_run_id)
    finish_agent(
        db, evaluator_agent, AgentRunStatus.COMPLETED, text=evaluator_json(CRITERIA), tmp_path=tmp_path
    )  # a valid evaluator answer...
    Path(chain.artifact.storage_ref).write_text("altered after the evaluator answered", encoding="utf-8")

    EvaluationExecutionService(db).finalize_agent_evaluator_run(run.id, task_run_status=TaskRunStatus.COMPLETED)

    final = next(iter(rows(session_factory, EvaluationRun, EvaluationRun.id == run.id)))
    assert final.status == EvaluationRunStatus.FAILED and "no longer matches" in final.failure_reason
    assert results_of(session_factory, final.id) == {}  # tampered evidence never becomes a trusted result
