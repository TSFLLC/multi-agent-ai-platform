"""MA6 Slice 2 / MA5 boundary regression -- evaluation is independent of
comparison winner/canonical-selection state.

Explicit guard against the exact failure mode the MA6 checkpoint forbids:
running or completing an Evaluation Run against a comparison candidate's
Agent Run/Artifact must never read, set, or otherwise influence
``ComparisonCandidate.is_winner``/``rank`` or
``ComparisonRun.status``/``winner_agent_run_id``/``winner_artifact_id``/
``winner_artifact_hash``/``completed_at`` -- those columns remain
ComparisonService's exclusive surface (app.services.comparison_service),
and the human canonical selection (``select_canonical``) remains the only
path that ever writes them.
"""

from app.db.enums import ComparisonRunStatus, EvaluationFinding, EvaluationRunStatus, VersionStatus
from app.models.artifacts_eval import ComparisonCandidate, ComparisonRun
from app.services.evaluation_definition_service import EvaluationDefinitionService
from app.services.evaluation_execution_service import EvaluationExecutionService
from tests.conftest import (
    make_agent_run,
    make_agent_version,
    make_artifact_with_content,
    make_evaluation_definition,
    make_project,
    make_task,
    make_task_run,
)


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
