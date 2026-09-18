"""Comparison evaluation read surface — MA6 Slice 3D.

``list_comparison_evaluations``/``GET /comparisons/{id}/evaluations``
groups the existing, independent EvaluationRuns already created for a
comparison's candidates (one at a time via POST /agent-runs/{id}/
evaluations, or in bulk via POST /comparisons/{id}/evaluations, MA6 Slice
3C) by candidate -- read-only aggregation, never starts/reruns an
evaluation, never ranks/scores/picks a winner, never calls a model.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, List

from sqlalchemy import event, select

from app.db.enums import (
    ComparisonRunStatus,
    EvaluationMethod,
    TaskRunStatus,
    VersionStatus,
)
from app.models.artifacts_eval import ComparisonCandidate, ComparisonRun
from app.models.evaluation_runs import EvaluationRun
from app.models.execution import JobQueue
from app.providers.base import InvokeRequest, InvokeResponse
from app.services.evaluation_definition_service import EvaluationDefinitionService
from app.services.evaluation_execution_service import EvaluationExecutionService, list_comparison_evaluations
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

    def invoke(self, request: InvokeRequest) -> InvokeResponse:
        return self.responses.pop(0)


def _factory_for(adapter: FakeAdapter):
    return lambda db, provider: adapter


def _evaluator_version(db, project, name="Evaluator", *, wired=False):
    agent = make_agent(db, project=project, name=name)
    version = make_agent_version(db, agent=agent, status=VersionStatus.ACTIVE)
    if wired:
        model = make_model(db, canonical_model_id=f"model/{name}")
        pm = make_provider_model(
            db,
            model=model,
            provider=make_provider(db, name=f"Provider-{name}"),
            cost_input_per_mtok=Decimal(0),
            cost_output_per_mtok=Decimal(0),
        )
        version.model_policy = {"mode": "manual", "manual_provider_model_id": pm.id}
    db.commit()
    return version


def _published_version(db, project, criteria, *, name="Rubric"):
    definition = make_evaluation_definition(db, project=project, name=name)
    db.commit()
    version = EvaluationDefinitionService(db).create_version(
        evaluation_definition_id=definition.id, description=None, criteria=criteria
    )
    return EvaluationDefinitionService(db).publish_version(version.id)


def _criteria(*keys):
    return [{"key": k, "label": k.title()} for k in keys]


def _bookkeeping_comparison(db, project, status=ComparisonRunStatus.RUNNING) -> ComparisonRun:
    bookkeeping_task = make_task(db, project=project)
    bookkeeping_task_run = make_task_run(db, task=bookkeeping_task)
    comparison = ComparisonRun(task_run_id=bookkeeping_task_run.id, status=status)
    db.add(comparison)
    db.flush()
    return comparison


def _eligible_candidate(db, tmp_path, comparison, project, *, label, content="candidate output"):
    agent_version = make_agent_version(db, status=VersionStatus.ACTIVE)
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


class _QueryCounter:
    """Test-only SQL statement counter, scoped to one engine via SQLAlchemy
    events -- not application infrastructure/caching, just an assertion
    helper for the "avoid obvious N+1" requirement."""

    def __init__(self, engine):
        self.engine = engine
        self.count = 0

    def __enter__(self):
        event.listen(self.engine, "before_cursor_execute", self._before)
        return self

    def __exit__(self, *exc):
        event.remove(self.engine, "before_cursor_execute", self._before)

    def _before(self, conn, cursor, statement, parameters, context, executemany):
        self.count += 1


# -- grouping correctness -----------------------------------------------------------


def test_list_comparison_evaluations_groups_runs_under_correct_candidate(db, tmp_path):
    project = make_project(db)
    comparison = _bookkeeping_comparison(db, project)
    a = _eligible_candidate(db, tmp_path, comparison, project, label="A")
    b = _eligible_candidate(db, tmp_path, comparison, project, label="B")
    version = _published_version(db, project, _criteria("non_empty_output"))

    svc = EvaluationExecutionService(db)
    run_a = svc.create_run(
        agent_run_id=a.agent_run_id, subject_artifact_id=_artifact_for(db, a), evaluation_definition_version_id=version.id
    )
    run_b = svc.create_run(
        agent_run_id=b.agent_run_id, subject_artifact_id=_artifact_for(db, b), evaluation_definition_version_id=version.id
    )

    detail = list_comparison_evaluations(db, comparison.id)
    assert detail is not None
    by_candidate = {c.comparison_candidate_id: c for c in detail.candidates}

    assert [r.run.id for r in by_candidate[a.id].evaluation_runs] == [run_a.id]
    assert [r.run.id for r in by_candidate[b.id].evaluation_runs] == [run_b.id]
    assert by_candidate[a.id].subject_agent_run_id == a.agent_run_id
    assert by_candidate[b.id].subject_agent_run_id == b.agent_run_id


def _artifact_for(db, candidate):
    from app.models.artifacts_eval import Artifact

    return (
        db.execute(select(Artifact).where(Artifact.agent_run_id == candidate.agent_run_id))
        .scalars()
        .first()
        .id
    )


def test_list_comparison_evaluations_candidate_with_no_evaluations_yet_has_empty_list(db, tmp_path):
    project = make_project(db)
    comparison = _bookkeeping_comparison(db, project)
    _eligible_candidate(db, tmp_path, comparison, project, label="A")

    detail = list_comparison_evaluations(db, comparison.id)
    assert detail.candidates[0].evaluation_runs == []


def test_list_comparison_evaluations_missing_comparison_returns_none(db):
    assert list_comparison_evaluations(db, "does-not-exist") is None


# -- history remains visible, never collapsed ----------------------------------------


def test_list_comparison_evaluations_multiple_historical_evaluations_remain_visible(db, tmp_path):
    project = make_project(db)
    comparison = _bookkeeping_comparison(db, project)
    a = _eligible_candidate(db, tmp_path, comparison, project, label="A")
    version_1 = _published_version(db, project, _criteria("non_empty_output"), name="Rubric 1")
    version_2 = _published_version(db, project, _criteria("non_empty_output"), name="Rubric 2")
    artifact_id = _artifact_for(db, a)

    svc = EvaluationExecutionService(db)
    run_1 = svc.create_run(
        agent_run_id=a.agent_run_id, subject_artifact_id=artifact_id, evaluation_definition_version_id=version_1.id
    )
    run_2 = svc.create_run(
        agent_run_id=a.agent_run_id, subject_artifact_id=artifact_id, evaluation_definition_version_id=version_2.id
    )

    detail = list_comparison_evaluations(db, comparison.id)
    run_ids = {r.run.id for c in detail.candidates for r in c.evaluation_runs}
    assert run_ids == {run_1.id, run_2.id}  # both remain, never collapsed to "the latest"


def test_list_comparison_evaluations_different_evaluator_models_distinguishable(db, tmp_path):
    project = make_project(db)
    comparison = _bookkeeping_comparison(db, project)
    a = _eligible_candidate(db, tmp_path, comparison, project, label="A")
    artifact_id = _artifact_for(db, a)
    version = _published_version(db, project, _criteria("a"))
    evaluator_1 = _evaluator_version(db, project, name="Evaluator1", wired=True)
    evaluator_2 = _evaluator_version(db, project, name="Evaluator2", wired=True)

    svc = EvaluationExecutionService(db)
    run_1 = svc.create_agent_evaluator_run(
        agent_run_id=a.agent_run_id,
        subject_artifact_id=artifact_id,
        evaluation_definition_version_id=version.id,
        evaluator_agent_version_id=evaluator_1.id,
    )
    run_2 = svc.create_agent_evaluator_run(
        agent_run_id=a.agent_run_id,
        subject_artifact_id=artifact_id,
        evaluation_definition_version_id=version.id,
        evaluator_agent_version_id=evaluator_2.id,
    )
    # evaluator_model resolves off the evaluator's own executed Agent Run
    # (model_id/provider_id are only set once it actually runs) -- never
    # inferred from the AgentVersion/model_policy alone.
    for run in (run_1, run_2):
        adapter = FakeAdapter(
            responses=[InvokeResponse(text='{"criteria": [{"key": "a", "finding": "MET", "rationale": "ok"}]}', tokens_in=5, tokens_out=5)]
        )
        AgentExecutionService(db, adapter_factory=_factory_for(adapter)).execute(
            run.evaluator_agent_run_id, worker_id="test-worker"
        )

    detail = list_comparison_evaluations(db, comparison.id)
    runs_by_id = {r.run.id: r for c in detail.candidates for r in c.evaluation_runs}
    model_1 = runs_by_id[run_1.id].evaluator_model
    model_2 = runs_by_id[run_2.id].evaluator_model
    assert model_1 is not None and model_2 is not None
    assert model_1.canonical_model_id != model_2.canonical_model_id


def test_list_comparison_evaluations_deterministic_and_agent_evaluator_coexist(db, tmp_path):
    project = make_project(db)
    comparison = _bookkeeping_comparison(db, project)
    a = _eligible_candidate(db, tmp_path, comparison, project, label="A")
    artifact_id = _artifact_for(db, a)
    version = _published_version(db, project, _criteria("non_empty_output"))
    evaluator_version = _evaluator_version(db, project)

    svc = EvaluationExecutionService(db)
    deterministic_run = svc.create_run(
        agent_run_id=a.agent_run_id, subject_artifact_id=artifact_id, evaluation_definition_version_id=version.id
    )
    agent_evaluator_run = svc.create_agent_evaluator_run(
        agent_run_id=a.agent_run_id,
        subject_artifact_id=artifact_id,
        evaluation_definition_version_id=version.id,
        evaluator_agent_version_id=evaluator_version.id,
    )

    detail = list_comparison_evaluations(db, comparison.id)
    runs = detail.candidates[0].evaluation_runs
    methods_by_id = {r.run.id: r.run.method for r in runs}
    assert methods_by_id[deterministic_run.id] == EvaluationMethod.DETERMINISTIC
    assert methods_by_id[agent_evaluator_run.id] == EvaluationMethod.AGENT_EVALUATOR


# -- query-count / N+1 regression ---------------------------------------------------


def test_list_comparison_evaluations_query_count_does_not_scale_with_candidate_count(db, tmp_path, engine):
    project = make_project(db)
    version = _published_version(db, project, _criteria("non_empty_output"))
    svc = EvaluationExecutionService(db)

    def _comparison_with(n):
        comparison = _bookkeeping_comparison(db, project)
        for i in range(n):
            c = _eligible_candidate(db, tmp_path, comparison, project, label=f"C{i}", content=f"output {i}")
            svc.create_run(
                agent_run_id=c.agent_run_id,
                subject_artifact_id=_artifact_for(db, c),
                evaluation_definition_version_id=version.id,
            )
        return comparison

    small = _comparison_with(2)
    large = _comparison_with(8)

    with _QueryCounter(engine) as counter_small:
        list_comparison_evaluations(db, small.id)
    small_count = counter_small.count

    with _QueryCounter(engine) as counter_large:
        list_comparison_evaluations(db, large.id)
    large_count = counter_large.count

    # a genuine N+1 (one extra query per candidate/run) would make the
    # 8-candidate read cost roughly 4x the 2-candidate read's query count;
    # a bounded, batched read stays flat regardless of candidate count.
    assert large_count <= small_count + 2, (
        f"query count grew with candidate count ({small_count} -> {large_count}); "
        "looks like an N+1 pattern"
    )


# -- side-effect freedom / MA5 isolation --------------------------------------------


def test_list_comparison_evaluations_creates_no_jobs_or_runs(db, tmp_path):
    project = make_project(db)
    comparison = _bookkeeping_comparison(db, project)
    a = _eligible_candidate(db, tmp_path, comparison, project, label="A")
    version = _published_version(db, project, _criteria("non_empty_output"))
    EvaluationExecutionService(db).create_run(
        agent_run_id=a.agent_run_id, subject_artifact_id=_artifact_for(db, a), evaluation_definition_version_id=version.id
    )

    jobs_before = db.query(JobQueue).count()
    runs_before = db.query(EvaluationRun).count()

    for _ in range(3):
        list_comparison_evaluations(db, comparison.id)

    assert db.query(JobQueue).count() == jobs_before
    assert db.query(EvaluationRun).count() == runs_before


def test_list_comparison_evaluations_never_touches_ma5_winner_state(db, tmp_path):
    project = make_project(db)
    comparison = _bookkeeping_comparison(db, project)
    a = _eligible_candidate(db, tmp_path, comparison, project, label="A")
    version = _published_version(db, project, _criteria("non_empty_output"))
    EvaluationExecutionService(db).create_run(
        agent_run_id=a.agent_run_id, subject_artifact_id=_artifact_for(db, a), evaluation_definition_version_id=version.id
    )

    db.expire_all()
    comparison_before = db.get(ComparisonRun, comparison.id)
    candidate_before = db.get(ComparisonCandidate, a.id)
    snapshot_before = (
        comparison_before.status,
        comparison_before.winner_agent_run_id,
        comparison_before.winner_artifact_id,
        comparison_before.completed_at,
        candidate_before.is_winner,
        candidate_before.rank,
    )

    list_comparison_evaluations(db, comparison.id)

    db.expire_all()
    comparison_after = db.get(ComparisonRun, comparison.id)
    candidate_after = db.get(ComparisonCandidate, a.id)
    snapshot_after = (
        comparison_after.status,
        comparison_after.winner_agent_run_id,
        comparison_after.winner_artifact_id,
        comparison_after.completed_at,
        candidate_after.is_winner,
        candidate_after.rank,
    )
    assert snapshot_after == snapshot_before


def test_list_comparison_evaluations_response_has_no_score_rank_winner_field(db, tmp_path):
    project = make_project(db)
    comparison = _bookkeeping_comparison(db, project)
    a = _eligible_candidate(db, tmp_path, comparison, project, label="A")
    version = _published_version(db, project, _criteria("non_empty_output"))
    EvaluationExecutionService(db).create_run(
        agent_run_id=a.agent_run_id, subject_artifact_id=_artifact_for(db, a), evaluation_definition_version_id=version.id
    )

    from app.schemas.comparisons import ComparisonCandidateEvaluationsRead, ComparisonEvaluationsRead

    detail = list_comparison_evaluations(db, comparison.id)
    from app.api.routers.comparisons import _evaluation_run_read

    payload = ComparisonEvaluationsRead(
        comparison_id=detail.comparison_id,
        candidates=[
            ComparisonCandidateEvaluationsRead(
                comparison_candidate_id=c.comparison_candidate_id,
                subject_agent_run_id=c.subject_agent_run_id,
                evaluation_runs=[
                    _evaluation_run_read(r.run, r.criterion_results, r.evaluator_model)
                    for r in c.evaluation_runs
                ],
            )
            for c in detail.candidates
        ],
    )
    blob = payload.model_dump_json().lower()
    for term in ["score", "percentage", "percent", "rank", "winner", "grade", "recommend"]:
        assert term not in blob
