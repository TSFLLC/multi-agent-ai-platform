"""MA6 Slice 2/3B / MA5 boundary regression -- evaluation is independent of
comparison winner/canonical-selection state.

Explicit guard against the exact failure mode the MA6 checkpoint forbids:
running or completing an Evaluation Run (deterministic OR agent-evaluator)
against a comparison candidate's Agent Run/Artifact must never read, set,
or otherwise influence ``ComparisonCandidate.is_winner``/``rank`` or
``ComparisonRun.status``/``winner_agent_run_id``/``winner_artifact_id``/
``winner_artifact_hash``/``completed_at`` -- those columns remain
ComparisonService's exclusive surface (app.services.comparison_service),
and the human canonical selection (``select_canonical``) remains the only
path that ever writes them.
"""

import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, List

from app.db.enums import ComparisonRunStatus, EvaluationFinding, EvaluationRunStatus, VersionStatus
from app.models.artifacts_eval import ComparisonCandidate, ComparisonRun
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


@dataclass
class _FakeAdapter:
    responses: List[Any] = field(default_factory=list)
    calls: List[InvokeRequest] = field(default_factory=list)

    def invoke(self, request: InvokeRequest) -> InvokeResponse:
        self.calls.append(request)
        return self.responses.pop(0)


def _published_evaluator_agent_version(db, project):
    agent = make_agent(db, project=project, name="Evaluator")
    version = make_agent_version(db, agent=agent, status=VersionStatus.ACTIVE)
    model = make_model(db, canonical_model_id="model/evaluator")
    pm = make_provider_model(
        db,
        model=model,
        provider=make_provider(db),
        cost_input_per_mtok=Decimal(0),
        cost_output_per_mtok=Decimal(0),
    )
    version.model_policy = {"mode": "manual", "manual_provider_model_id": pm.id}
    return version


def _comparison_candidate_subject(db, tmp_path, project):
    """Minimal ComparisonRun + ComparisonCandidate whose agent_run_id/
    artifact already point at a real (unselected) candidate -- exactly the
    shape ComparisonService.launch_comparison produces, built directly
    here so this test doesn't depend on running a full comparison
    (FakeAdapter/AgentExecutionService orchestration is exercised
    exhaustively in test_comparison_service.py already)."""
    bookkeeping_task = make_task(db, project=project)
    bookkeeping_task_run = make_task_run(db, task=bookkeeping_task)

    candidate_task = make_task(db, project=project)
    candidate_task_run = make_task_run(db, task=candidate_task)
    agent_version = make_agent_version(db, status=VersionStatus.ACTIVE)
    candidate_agent_run = make_agent_run(db, task_run=candidate_task_run, agent_version=agent_version)
    candidate_artifact = make_artifact_with_content(db, tmp_path, agent_run=candidate_agent_run)

    comparison = ComparisonRun(task_run_id=bookkeeping_task_run.id, status=ComparisonRunStatus.RUNNING)
    db.add(comparison)
    db.flush()
    candidate = ComparisonCandidate(
        comparison_run_id=comparison.id,
        agent_version_id=agent_version.id,
        task_run_id=candidate_task_run.id,
        agent_run_id=candidate_agent_run.id,
        label="Candidate A",
    )
    db.add(candidate)
    db.commit()
    return comparison, candidate, candidate_agent_run, candidate_artifact


def _snapshot(db, comparison_id, candidate_id):
    db.expire_all()
    comparison = db.get(ComparisonRun, comparison_id)
    candidate = db.get(ComparisonCandidate, candidate_id)
    return {
        "comparison_status": comparison.status,
        "winner_agent_run_id": comparison.winner_agent_run_id,
        "winner_artifact_id": comparison.winner_artifact_id,
        "winner_artifact_hash": comparison.winner_artifact_hash,
        "completed_at": comparison.completed_at,
        "candidate_is_winner": candidate.is_winner,
        "candidate_rank": candidate.rank,
    }


def test_running_and_completing_an_evaluation_run_never_touches_comparison_winner_state(db, tmp_path):
    project = make_project(db)
    comparison, candidate, candidate_agent_run, candidate_artifact = _comparison_candidate_subject(
        db, tmp_path, project
    )
    before = _snapshot(db, comparison.id, candidate.id)
    assert before["candidate_is_winner"] is False
    assert before["winner_agent_run_id"] is None

    definition = make_evaluation_definition(db, project=project)
    db.commit()
    version = EvaluationDefinitionService(db).create_version(
        evaluation_definition_id=definition.id,
        description=None,
        criteria=[{"key": "non_empty_output", "label": "Non-empty output"}],
    )
    version = EvaluationDefinitionService(db).publish_version(version.id)

    svc = EvaluationExecutionService(db)
    run = svc.create_run(
        agent_run_id=candidate_agent_run.id,
        subject_artifact_id=candidate_artifact.id,
        evaluation_definition_version_id=version.id,
    )
    after_create = _snapshot(db, comparison.id, candidate.id)
    assert after_create == before

    svc.execute(run.id, worker_id="test-worker")
    db.refresh(run)
    assert run.status == EvaluationRunStatus.COMPLETED
    assert run.criterion_results[0].finding == EvaluationFinding.MET

    after_execute = _snapshot(db, comparison.id, candidate.id)
    assert after_execute == before
    assert after_execute["comparison_status"] == ComparisonRunStatus.RUNNING  # unchanged, still not selected


def test_running_and_completing_an_agent_evaluator_run_never_touches_comparison_winner_state(db, tmp_path):
    """Same guarantee as the deterministic test above, for MA6 Slice 3B's
    AGENT_EVALUATOR method -- a real evaluator Agent Run (dispatched
    through the unmodified MA3 execution engine) against a comparison
    candidate's Agent Run/Artifact must never touch that candidate's own
    Comparison state either."""
    project = make_project(db)
    comparison, candidate, candidate_agent_run, candidate_artifact = _comparison_candidate_subject(
        db, tmp_path, project
    )
    before = _snapshot(db, comparison.id, candidate.id)

    evaluator_version = _published_evaluator_agent_version(db, project)
    definition = make_evaluation_definition(db, project=project)
    db.commit()
    version = EvaluationDefinitionService(db).create_version(
        evaluation_definition_id=definition.id,
        description=None,
        criteria=[{"key": "correctness", "label": "Correctness"}],
    )
    version = EvaluationDefinitionService(db).publish_version(version.id)

    svc = EvaluationExecutionService(db)
    run = svc.create_agent_evaluator_run(
        agent_run_id=candidate_agent_run.id,
        subject_artifact_id=candidate_artifact.id,
        evaluation_definition_version_id=version.id,
        evaluator_agent_version_id=evaluator_version.id,
    )
    after_create = _snapshot(db, comparison.id, candidate.id)
    assert after_create == before

    response_text = json.dumps(
        {"criteria": [{"key": "correctness", "finding": "MET", "rationale": "matches spec"}]}
    )
    adapter = _FakeAdapter(responses=[InvokeResponse(text=response_text, tokens_in=10, tokens_out=5)])
    AgentExecutionService(db, adapter_factory=lambda db, provider: adapter).execute(
        run.evaluator_agent_run_id, worker_id="test-worker"
    )

    db.refresh(run)
    assert run.status == EvaluationRunStatus.COMPLETED
    assert run.criterion_results[0].finding == EvaluationFinding.MET

    after_execute = _snapshot(db, comparison.id, candidate.id)
    assert after_execute == before
    assert after_execute["comparison_status"] == ComparisonRunStatus.RUNNING  # unchanged, still not selected

    # the candidate's own Agent Run/Task Run remain completely untouched
    db.refresh(candidate_agent_run)
    assert candidate_agent_run.status.value == "created"  # never executed, never touched by evaluation
