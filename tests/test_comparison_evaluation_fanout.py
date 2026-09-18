"""Comparison Evaluation Fan-out — MA6 Slice 3C.

``EvaluationExecutionService.analyze_comparison`` fans out one independent
EvaluationRun per eligible ComparisonCandidate under one ComparisonRun --
reusing ``create_run``/``create_agent_evaluator_run`` verbatim per
candidate, never a second inference pipeline and never a comparative/
ranking evaluator call across candidates. MA5 winner-selection-state
isolation is covered separately in tests/test_evaluation_ma5_isolation.py
(extended here is not needed -- this file focuses on the fan-out's own
eligibility/idempotency/shared-configuration contract).

Candidates are built directly against the ORM (the exact shape
``ComparisonService.launch_comparison`` produces), the same approach
tests/test_evaluation_ma5_isolation.py's ``_comparison_candidate_subject``
already established -- so these tests don't depend on running a full
comparison end-to-end (that orchestration is covered exhaustively in
tests/test_comparison_service.py already).
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, List

import pytest

from app.db.enums import (
    ComparisonRunStatus,
    EvaluationMethod,
    EvaluationRunStatus,
    TaskRunStatus,
    VersionStatus,
)
from app.errors import NotFoundError
from app.models.artifacts_eval import ComparisonCandidate, ComparisonRun
from app.models.evaluation_runs import EvaluationRun
from app.models.execution import JobQueue
from app.models.tasks import AgentRun, Task, TaskRun
from app.providers.base import InvokeRequest, InvokeResponse
from app.services.evaluation_definition_service import EvaluationDefinitionService
from app.services.evaluation_execution_service import EvaluationExecutionService
from app.services.execution_service import AgentExecutionService
from tests.conftest import (
    make_agent,
    make_agent_run,
    make_agent_version,
    make_artifact_with_content,
    make_evaluation_definition,
    make_model,
    make_project,
    make_provider,
    make_provider_model,
    make_task,
    make_task_run,
)

# -- fakes / setup helpers -------------------------------------------------------


@dataclass
class FakeAdapter:
    responses: List[Any] = field(default_factory=list)
    calls: List[InvokeRequest] = field(default_factory=list)

    def invoke(self, request: InvokeRequest) -> InvokeResponse:
        self.calls.append(request)
        return self.responses.pop(0)


def _factory_for(adapter: FakeAdapter):
    return lambda db, provider: adapter


def _published_version(db, project, criteria, *, name="Rubric"):
    definition = make_evaluation_definition(db, project=project, name=name)
    db.commit()
    version = EvaluationDefinitionService(db).create_version(
        evaluation_definition_id=definition.id, description=None, criteria=criteria
    )
    return EvaluationDefinitionService(db).publish_version(version.id)


def _criteria(*keys):
    return [{"key": k, "label": k.title()} for k in keys]


def _evaluator_version(db, project, name="Evaluator", *, wired=False):
    agent = make_agent(db, project=project, name=name)
    version = make_agent_version(db, agent=agent, status=VersionStatus.ACTIVE)
    if wired:
        model = make_model(db, canonical_model_id=f"model/{name}")
        pm = make_provider_model(
            db,
            model=model,
            provider=make_provider(db),
            cost_input_per_mtok=Decimal(0),
            cost_output_per_mtok=Decimal(0),
        )
        version.model_policy = {"mode": "manual", "manual_provider_model_id": pm.id}
    db.commit()
    return version


def _bookkeeping_comparison(db, project, status=ComparisonRunStatus.RUNNING) -> ComparisonRun:
    bookkeeping_task = make_task(db, project=project)
    bookkeeping_task_run = make_task_run(db, task=bookkeeping_task)
    comparison = ComparisonRun(task_run_id=bookkeeping_task_run.id, status=status)
    db.add(comparison)
    db.flush()
    return comparison


def _eligible_candidate(
    db, tmp_path, comparison, project, *, label, content="candidate output", agent_version=None
) -> ComparisonCandidate:
    """A candidate whose own Task Run already reached COMPLETED with a
    real resolvable output Artifact -- the only shape ``analyze_comparison``
    ever creates an EvaluationRun for."""
    agent_version = agent_version or make_agent_version(db, status=VersionStatus.ACTIVE)
    task = make_task(db, project=project)
    task_run = make_task_run(db, task=task, status=TaskRunStatus.COMPLETED)
    agent_run = make_agent_run(db, task_run=task_run, agent_version=agent_version)
    make_artifact_with_content(db, tmp_path, agent_run=agent_run, content=content)
    candidate = ComparisonCandidate(
        comparison_run_id=comparison.id,
        agent_version_id=agent_version.id,
        task_run_id=task_run.id,
        agent_run_id=agent_run.id,
        label=label,
    )
    db.add(candidate)
    db.commit()
    return candidate


def _not_launched_candidate(db, comparison, project, *, label) -> ComparisonCandidate:
    agent_version = make_agent_version(db, status=VersionStatus.ACTIVE)
    candidate = ComparisonCandidate(
        comparison_run_id=comparison.id, agent_version_id=agent_version.id, label=label
    )
    db.add(candidate)
    db.commit()
    return candidate


def _incomplete_candidate(
    db, comparison, project, *, label, task_run_status=TaskRunStatus.RUNNING
) -> ComparisonCandidate:
    agent_version = make_agent_version(db, status=VersionStatus.ACTIVE)
    task = make_task(db, project=project)
    task_run = make_task_run(db, task=task, status=task_run_status)
    agent_run = make_agent_run(db, task_run=task_run, agent_version=agent_version)
    candidate = ComparisonCandidate(
        comparison_run_id=comparison.id,
        agent_version_id=agent_version.id,
        task_run_id=task_run.id,
        agent_run_id=agent_run.id,
        label=label,
    )
    db.add(candidate)
    db.commit()
    return candidate


def _missing_artifact_candidate(db, comparison, project, *, label) -> ComparisonCandidate:
    """COMPLETED Task Run, but the candidate's Agent Run produced no
    Artifact row at all -- e.g. a run that completed with nothing to show."""
    agent_version = make_agent_version(db, status=VersionStatus.ACTIVE)
    task = make_task(db, project=project)
    task_run = make_task_run(db, task=task, status=TaskRunStatus.COMPLETED)
    agent_run = make_agent_run(db, task_run=task_run, agent_version=agent_version)
    candidate = ComparisonCandidate(
        comparison_run_id=comparison.id,
        agent_version_id=agent_version.id,
        task_run_id=task_run.id,
        agent_run_id=agent_run.id,
        label=label,
    )
    db.add(candidate)
    db.commit()
    return candidate


def _mismatched_artifact_candidate(db, tmp_path, comparison, project, *, label) -> ComparisonCandidate:
    """COMPLETED Task Run whose ``final_artifact_id`` (the BUILD_REVIEW
    resolution path ``_candidate_artifact`` prefers) points at an Artifact
    belonging to a *different* Agent Run than the candidate's own --
    create_run's exact-artifact protection rejects this at EvaluationRun
    creation time, a genuine per-candidate *failure* (not a skip, since a
    real Artifact *is* resolved) that must not abort sibling candidates."""
    agent_version = make_agent_version(db, status=VersionStatus.ACTIVE)
    task = make_task(db, project=project)
    task_run = make_task_run(db, task=task, status=TaskRunStatus.COMPLETED)
    agent_run = make_agent_run(db, task_run=task_run, agent_version=agent_version)

    other_task_run = make_task_run(db, task=make_task(db, project=project), status=TaskRunStatus.COMPLETED)
    other_agent_run = make_agent_run(db, task_run=other_task_run, agent_version=agent_version)
    mismatched_artifact = make_artifact_with_content(
        db, tmp_path, agent_run=other_agent_run, content="belongs to a different agent run"
    )
    task_run.final_artifact_id = mismatched_artifact.id
    db.commit()

    candidate = ComparisonCandidate(
        comparison_run_id=comparison.id,
        agent_version_id=agent_version.id,
        task_run_id=task_run.id,
        agent_run_id=agent_run.id,
        label=label,
    )
    db.add(candidate)
    db.commit()
    return candidate


def _outcomes_by_label(db, comparison, outcomes):
    candidates = {c.id: c.label for c in comparison.candidates}
    return {candidates[o.comparison_candidate_id]: o for o in outcomes}


# -- eligibility / fan-out shape --------------------------------------------------


def test_analyze_comparison_creates_independent_run_per_eligible_candidate(db, tmp_path):
    project = make_project(db)
    comparison = _bookkeeping_comparison(db, project)
    a = _eligible_candidate(db, tmp_path, comparison, project, label="A", content="output A")
    b = _eligible_candidate(db, tmp_path, comparison, project, label="B", content="output B")
    c = _eligible_candidate(db, tmp_path, comparison, project, label="C", content="output C")
    version = _published_version(db, project, _criteria("non_empty_output"))

    outcomes = EvaluationExecutionService(db).analyze_comparison(
        comparison_id=comparison.id, evaluation_definition_version_id=version.id
    )

    assert len(outcomes) == 3
    assert {o.status for o in outcomes} == {"created"}
    run_ids = {o.evaluation_run_id for o in outcomes}
    assert len(run_ids) == 3  # independent, never shared/merged

    by_candidate = {o.comparison_candidate_id: o for o in outcomes}
    assert by_candidate[a.id].subject_agent_run_id == a.agent_run_id
    assert by_candidate[b.id].subject_agent_run_id == b.agent_run_id
    assert by_candidate[c.id].subject_agent_run_id == c.agent_run_id

    for o in outcomes:
        run = db.get(EvaluationRun, o.evaluation_run_id)
        assert run.method == EvaluationMethod.DETERMINISTIC
        assert run.evaluation_definition_version_id == version.id


def test_analyze_comparison_shared_configuration_applied_to_every_candidate(db, tmp_path):
    project = make_project(db)
    comparison = _bookkeeping_comparison(db, project)
    _eligible_candidate(db, tmp_path, comparison, project, label="A")
    _eligible_candidate(db, tmp_path, comparison, project, label="B")
    evaluator_version = _evaluator_version(db, project)
    override = {"mode": "manual", "manual_provider_model_id": "some-provider-model"}
    version = _published_version(db, project, _criteria("a"))

    outcomes = EvaluationExecutionService(db).analyze_comparison(
        comparison_id=comparison.id,
        evaluation_definition_version_id=version.id,
        method=EvaluationMethod.AGENT_EVALUATOR,
        evaluator_agent_version_id=evaluator_version.id,
        evaluator_model_policy_override=override,
    )

    assert {o.status for o in outcomes} == {"created"}
    for o in outcomes:
        run = db.get(EvaluationRun, o.evaluation_run_id)
        assert run.evaluation_definition_version_id == version.id
        assert run.evaluator_agent_version_id == evaluator_version.id
        assert run.evaluator_model_policy_override_json == override


def test_analyze_comparison_supports_different_source_agents(db, tmp_path):
    """The same evaluator/definition can evaluate candidate outputs
    produced by completely different Agents/Roles -- Section: MA6 Slice
    3C requirement."""
    project = make_project(db)
    comparison = _bookkeeping_comparison(db, project)
    agent_a = make_agent(db, project=project, name="Backend Engineer")
    version_a = make_agent_version(db, agent=agent_a, status=VersionStatus.ACTIVE)
    agent_b = make_agent(db, project=project, name="Frontend Engineer")
    version_b = make_agent_version(db, agent=agent_b, status=VersionStatus.ACTIVE)

    a = _eligible_candidate(db, tmp_path, comparison, project, label="A", agent_version=version_a)
    b = _eligible_candidate(db, tmp_path, comparison, project, label="B", agent_version=version_b)
    definition_version = _published_version(db, project, _criteria("non_empty_output"))

    outcomes = EvaluationExecutionService(db).analyze_comparison(
        comparison_id=comparison.id, evaluation_definition_version_id=definition_version.id
    )

    assert {o.status for o in outcomes} == {"created"}
    by_candidate = {o.comparison_candidate_id: o for o in outcomes}
    run_a = db.get(EvaluationRun, by_candidate[a.id].evaluation_run_id)
    run_b = db.get(EvaluationRun, by_candidate[b.id].evaluation_run_id)
    agent_run_a = db.get(AgentRun, run_a.subject_agent_run_id)
    agent_run_b = db.get(AgentRun, run_b.subject_agent_run_id)
    assert agent_run_a.agent_version_id == version_a.id
    assert agent_run_b.agent_version_id == version_b.id


def test_analyze_comparison_skips_incomplete_and_failed_candidates_without_aborting_siblings(db, tmp_path):
    project = make_project(db)
    comparison = _bookkeeping_comparison(db, project)
    a = _eligible_candidate(db, tmp_path, comparison, project, label="A")
    b_running = _incomplete_candidate(db, comparison, project, label="B-running", task_run_status=TaskRunStatus.RUNNING)
    b_failed = _incomplete_candidate(db, comparison, project, label="B-failed", task_run_status=TaskRunStatus.FAILED)
    not_launched = _not_launched_candidate(db, comparison, project, label="D-not-launched")
    c = _eligible_candidate(db, tmp_path, comparison, project, label="C")
    version = _published_version(db, project, _criteria("non_empty_output"))

    outcomes = EvaluationExecutionService(db).analyze_comparison(
        comparison_id=comparison.id, evaluation_definition_version_id=version.id
    )
    by_id = {o.comparison_candidate_id: o for o in outcomes}

    assert by_id[a.id].status == "created"
    assert by_id[c.id].status == "created"
    assert by_id[b_running.id].status == "skipped"
    assert by_id[b_running.id].reason is not None
    assert by_id[b_failed.id].status == "skipped"
    assert by_id[not_launched.id].status == "skipped"
    assert by_id[not_launched.id].evaluation_run_id is None


def test_analyze_comparison_skips_candidate_with_no_resolvable_artifact(db, tmp_path):
    project = make_project(db)
    comparison = _bookkeeping_comparison(db, project)
    a = _eligible_candidate(db, tmp_path, comparison, project, label="A")
    b_no_artifact = _missing_artifact_candidate(db, comparison, project, label="B")
    version = _published_version(db, project, _criteria("non_empty_output"))

    outcomes = EvaluationExecutionService(db).analyze_comparison(
        comparison_id=comparison.id, evaluation_definition_version_id=version.id
    )
    by_id = {o.comparison_candidate_id: o for o in outcomes}

    assert by_id[a.id].status == "created"
    assert by_id[b_no_artifact.id].status == "skipped"
    assert by_id[b_no_artifact.id].evaluation_run_id is None


def test_analyze_comparison_one_candidate_failure_does_not_abort_siblings(db, tmp_path):
    project = make_project(db)
    comparison = _bookkeeping_comparison(db, project)
    a = _eligible_candidate(db, tmp_path, comparison, project, label="A")
    b_mismatch = _mismatched_artifact_candidate(db, tmp_path, comparison, project, label="B")
    c = _eligible_candidate(db, tmp_path, comparison, project, label="C")
    version = _published_version(db, project, _criteria("non_empty_output"))

    outcomes = EvaluationExecutionService(db).analyze_comparison(
        comparison_id=comparison.id, evaluation_definition_version_id=version.id
    )
    by_id = {o.comparison_candidate_id: o for o in outcomes}

    assert by_id[a.id].status == "created"
    assert by_id[c.id].status == "created"
    assert by_id[b_mismatch.id].status == "failed"
    assert by_id[b_mismatch.id].reason  # a safe, non-empty explanation
    assert by_id[b_mismatch.id].evaluation_run_id is None


# -- idempotency -------------------------------------------------------------------


def test_analyze_comparison_identical_retry_is_idempotent(db, tmp_path):
    project = make_project(db)
    comparison = _bookkeeping_comparison(db, project)
    _eligible_candidate(db, tmp_path, comparison, project, label="A")
    _eligible_candidate(db, tmp_path, comparison, project, label="B")
    version = _published_version(db, project, _criteria("non_empty_output"))

    first = EvaluationExecutionService(db).analyze_comparison(
        comparison_id=comparison.id, evaluation_definition_version_id=version.id
    )
    assert {o.status for o in first} == {"created"}
    first_run_ids = {o.comparison_candidate_id: o.evaluation_run_id for o in first}
    run_count_after_first = db.query(EvaluationRun).count()
    job_count_after_first = db.query(JobQueue).count()

    second = EvaluationExecutionService(db).analyze_comparison(
        comparison_id=comparison.id, evaluation_definition_version_id=version.id
    )
    assert {o.status for o in second} == {"reused"}
    second_run_ids = {o.comparison_candidate_id: o.evaluation_run_id for o in second}
    assert second_run_ids == first_run_ids

    # no duplicate EvaluationRuns, no duplicate queued evaluator/checker jobs
    assert db.query(EvaluationRun).count() == run_count_after_first
    assert db.query(JobQueue).count() == job_count_after_first


def test_analyze_comparison_identical_retry_agent_evaluator_no_duplicate_execution_tree(db, tmp_path):
    project = make_project(db)
    comparison = _bookkeeping_comparison(db, project)
    _eligible_candidate(db, tmp_path, comparison, project, label="A")
    evaluator_version = _evaluator_version(db, project)
    version = _published_version(db, project, _criteria("a"))

    svc = EvaluationExecutionService(db)
    first = svc.analyze_comparison(
        comparison_id=comparison.id,
        evaluation_definition_version_id=version.id,
        method=EvaluationMethod.AGENT_EVALUATOR,
        evaluator_agent_version_id=evaluator_version.id,
    )
    assert first[0].status == "created"
    run = db.get(EvaluationRun, first[0].evaluation_run_id)
    task_count = db.query(Task).count()
    task_run_count = db.query(TaskRun).count()
    agent_run_count = db.query(AgentRun).count()
    job_count = db.query(JobQueue).count()

    second = svc.analyze_comparison(
        comparison_id=comparison.id,
        evaluation_definition_version_id=version.id,
        method=EvaluationMethod.AGENT_EVALUATOR,
        evaluator_agent_version_id=evaluator_version.id,
    )
    assert second[0].status == "reused"
    assert second[0].evaluation_run_id == run.id

    # no duplicate evaluator Task/Task Run/Agent Run/queued job tree
    assert db.query(Task).count() == task_count
    assert db.query(TaskRun).count() == task_run_count
    assert db.query(AgentRun).count() == agent_run_count
    assert db.query(JobQueue).count() == job_count


def test_analyze_comparison_changed_model_override_creates_distinct_historical_evaluation(db, tmp_path):
    project = make_project(db)
    comparison = _bookkeeping_comparison(db, project)
    _eligible_candidate(db, tmp_path, comparison, project, label="A")
    evaluator_version = _evaluator_version(db, project)
    version = _published_version(db, project, _criteria("a"))
    svc = EvaluationExecutionService(db)

    first = svc.analyze_comparison(
        comparison_id=comparison.id,
        evaluation_definition_version_id=version.id,
        method=EvaluationMethod.AGENT_EVALUATOR,
        evaluator_agent_version_id=evaluator_version.id,
        evaluator_model_policy_override={"mode": "manual", "manual_provider_model_id": "pm-1"},
    )
    second = svc.analyze_comparison(
        comparison_id=comparison.id,
        evaluation_definition_version_id=version.id,
        method=EvaluationMethod.AGENT_EVALUATOR,
        evaluator_agent_version_id=evaluator_version.id,
        evaluator_model_policy_override={"mode": "manual", "manual_provider_model_id": "pm-2"},
    )
    assert first[0].status == "created"
    assert second[0].status == "created"
    assert first[0].evaluation_run_id != second[0].evaluation_run_id
    assert db.query(EvaluationRun).count() == 2


def test_analyze_comparison_changed_evaluator_agent_version_creates_distinct_historical_evaluation(db, tmp_path):
    project = make_project(db)
    comparison = _bookkeeping_comparison(db, project)
    _eligible_candidate(db, tmp_path, comparison, project, label="A")
    evaluator_v1 = _evaluator_version(db, project, name="Evaluator1")
    evaluator_v2 = _evaluator_version(db, project, name="Evaluator2")
    version = _published_version(db, project, _criteria("a"))
    svc = EvaluationExecutionService(db)

    first = svc.analyze_comparison(
        comparison_id=comparison.id,
        evaluation_definition_version_id=version.id,
        method=EvaluationMethod.AGENT_EVALUATOR,
        evaluator_agent_version_id=evaluator_v1.id,
    )
    second = svc.analyze_comparison(
        comparison_id=comparison.id,
        evaluation_definition_version_id=version.id,
        method=EvaluationMethod.AGENT_EVALUATOR,
        evaluator_agent_version_id=evaluator_v2.id,
    )
    assert first[0].status == "created"
    assert second[0].status == "created"
    assert first[0].evaluation_run_id != second[0].evaluation_run_id
    assert db.query(EvaluationRun).count() == 2


def test_analyze_comparison_changed_definition_version_creates_distinct_historical_evaluation(db, tmp_path):
    project = make_project(db)
    comparison = _bookkeeping_comparison(db, project)
    _eligible_candidate(db, tmp_path, comparison, project, label="A")
    version_1 = _published_version(db, project, _criteria("a"), name="Rubric 1")
    version_2 = _published_version(db, project, _criteria("a"), name="Rubric 2")
    svc = EvaluationExecutionService(db)

    first = svc.analyze_comparison(comparison_id=comparison.id, evaluation_definition_version_id=version_1.id)
    second = svc.analyze_comparison(comparison_id=comparison.id, evaluation_definition_version_id=version_2.id)
    assert first[0].status == "created"
    assert second[0].status == "created"
    assert first[0].evaluation_run_id != second[0].evaluation_run_id
    assert db.query(EvaluationRun).count() == 2


# -- cross-project isolation ---------------------------------------------------


def test_analyze_comparison_missing_comparison_raises_not_found(db):
    with pytest.raises(NotFoundError):
        EvaluationExecutionService(db).analyze_comparison(
            comparison_id="does-not-exist", evaluation_definition_version_id="also-missing"
        )


def test_analyze_comparison_cross_project_definition_version_rejected_per_candidate(db, tmp_path):
    project = make_project(db)
    other_project = make_project(db, name="Other")
    comparison = _bookkeeping_comparison(db, project)
    _eligible_candidate(db, tmp_path, comparison, project, label="A")
    foreign_version = _published_version(db, other_project, _criteria("non_empty_output"))

    outcomes = EvaluationExecutionService(db).analyze_comparison(
        comparison_id=comparison.id, evaluation_definition_version_id=foreign_version.id
    )

    assert outcomes[0].status == "failed"
    assert outcomes[0].evaluation_run_id is None
    assert db.query(EvaluationRun).count() == 0


def test_analyze_comparison_cross_project_evaluator_agent_version_rejected_per_candidate(db, tmp_path):
    project = make_project(db)
    other_project = make_project(db, name="Other Evaluator Project")
    comparison = _bookkeeping_comparison(db, project)
    _eligible_candidate(db, tmp_path, comparison, project, label="A")
    version = _published_version(db, project, _criteria("a"))
    foreign_evaluator_version = _evaluator_version(db, other_project)

    outcomes = EvaluationExecutionService(db).analyze_comparison(
        comparison_id=comparison.id,
        evaluation_definition_version_id=version.id,
        method=EvaluationMethod.AGENT_EVALUATOR,
        evaluator_agent_version_id=foreign_evaluator_version.id,
    )

    assert outcomes[0].status == "failed"
    assert outcomes[0].evaluation_run_id is None
    assert db.query(EvaluationRun).count() == 0


# -- no comparative inference: exactly one evaluator call per candidate --------


def test_analyze_comparison_exactly_one_evaluator_inference_per_candidate_never_a_fourth_comparative_call(
    db, tmp_path
):
    project = make_project(db)
    comparison = _bookkeeping_comparison(db, project)
    _eligible_candidate(db, tmp_path, comparison, project, label="A")
    _eligible_candidate(db, tmp_path, comparison, project, label="B")
    _eligible_candidate(db, tmp_path, comparison, project, label="C")
    evaluator_version = _evaluator_version(db, project, wired=True)
    version = _published_version(db, project, _criteria("correctness"))

    outcomes = EvaluationExecutionService(db).analyze_comparison(
        comparison_id=comparison.id,
        evaluation_definition_version_id=version.id,
        method=EvaluationMethod.AGENT_EVALUATOR,
        evaluator_agent_version_id=evaluator_version.id,
    )
    assert {o.status for o in outcomes} == {"created"}

    evaluator_agent_run_ids = set()
    evaluator_task_run_ids = set()
    for o in outcomes:
        run = db.get(EvaluationRun, o.evaluation_run_id)
        assert run.evaluator_agent_run_id is not None
        evaluator_agent_run_ids.add(run.evaluator_agent_run_id)
        evaluator_task_run_ids.add(run.evaluator_task_run_id)

    # three independent evaluator execution trees -- never merged/shared
    assert len(evaluator_agent_run_ids) == 3
    assert len(evaluator_task_run_ids) == 3

    total_calls = 0
    for run_id in evaluator_agent_run_ids:
        adapter = FakeAdapter(
            responses=[
                InvokeResponse(
                    text='{"criteria": [{"key": "correctness", "finding": "MET", "rationale": "ok"}]}',
                    tokens_in=10,
                    tokens_out=5,
                )
            ]
        )
        AgentExecutionService(db, adapter_factory=_factory_for(adapter)).execute(
            run_id, worker_id="test-worker"
        )
        assert len(adapter.calls) == 1  # exactly one inference for this candidate's rubric
        total_calls += len(adapter.calls)

    # exactly N inferences for N candidates -- never an (N+1)th combined/
    # comparative call across A+B+C
    assert total_calls == 3

    for run_id in {db.get(EvaluationRun, o.evaluation_run_id).id for o in outcomes}:
        run = db.get(EvaluationRun, run_id)
        db.refresh(run)
        assert run.status == EvaluationRunStatus.COMPLETED
