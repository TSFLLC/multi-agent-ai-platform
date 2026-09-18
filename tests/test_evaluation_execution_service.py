"""EvaluationExecutionService — MA6 Slice 2 core scenarios.

Covers: Evaluation Run creation (and its validations), multiple immutable
historical runs for the same subject, exact Artifact/hash binding (both at
creation and at execution time), the deterministic "non_empty_output"
checker, unsupported-criterion handling (NOT_APPLICABLE, never a
fabricated MET/NOT_MET), project isolation, and the Flight Recorder event
lifecycle. No score/percentage/rank assertion exists anywhere in this
file on purpose (MA6 non-negotiable invariant).
"""

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.db.enums import EvaluationFinding, EvaluationMethod, EvaluationRunStatus, VersionStatus
from app.errors import ConflictError, NotFoundError
from app.models.observability import ExecutionEvent
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

# -- setup helpers -------------------------------------------------------------


def _published_version(db, project, criteria):
    definition = make_evaluation_definition(db, project=project)
    db.commit()
    version = EvaluationDefinitionService(db).create_version(
        evaluation_definition_id=definition.id, description=None, criteria=criteria
    )
    return EvaluationDefinitionService(db).publish_version(version.id)


def _subject(db, tmp_path, *, project=None, content="hello world") -> SimpleNamespace:
    project = project or make_project(db)
    task = make_task(db, project=project)
    task_run = make_task_run(db, task=task)
    agent_version = make_agent_version(db, status=VersionStatus.ACTIVE)
    agent_run = make_agent_run(db, task_run=task_run, agent_version=agent_version)
    artifact = make_artifact_with_content(db, tmp_path, agent_run=agent_run, content=content)
    db.commit()
    return SimpleNamespace(
        project=project, task=task, task_run=task_run, agent_run=agent_run, artifact=artifact
    )


def _events(db, task_run_id):
    stmt = (
        select(ExecutionEvent.event_type)
        .where(ExecutionEvent.task_run_id == task_run_id)
        .order_by(ExecutionEvent.sequence_number)
    )
    return [row[0] for row in db.execute(stmt).all()]


# -- create_run: validation ----------------------------------------------------


def test_create_run_persists_bound_provenance(db, tmp_path):
    s = _subject(db, tmp_path)
    version = _published_version(db, s.project, [{"key": "non_empty_output", "label": "Non-empty output"}])
    svc = EvaluationExecutionService(db)

    run = svc.create_run(
        agent_run_id=s.agent_run.id,
        subject_artifact_id=s.artifact.id,
        evaluation_definition_version_id=version.id,
        requested_by_user_id=None,
    )

    assert run.subject_agent_run_id == s.agent_run.id
    assert run.subject_artifact_id == s.artifact.id
    assert run.subject_artifact_content_hash == s.artifact.content_hash
    assert run.evaluation_definition_version_id == version.id
    assert run.method == EvaluationMethod.DETERMINISTIC
    assert run.status == EvaluationRunStatus.PENDING


def test_create_run_rejects_artifact_from_a_different_agent_run(db, tmp_path):
    s = _subject(db, tmp_path)
    other = _subject(db, tmp_path, project=s.project)
    version = _published_version(db, s.project, [{"key": "non_empty_output", "label": "X"}])
    svc = EvaluationExecutionService(db)

    with pytest.raises(ConflictError):
        svc.create_run(
            agent_run_id=s.agent_run.id,
            subject_artifact_id=other.artifact.id,
            evaluation_definition_version_id=version.id,
        )


def test_create_run_rejects_artifact_with_no_content_hash(db, tmp_path):
    from app.db.enums import ArtifactType
    from app.models.artifacts_eval import Artifact

    s = _subject(db, tmp_path)
    unhashed = Artifact(
        agent_run_id=s.agent_run.id, type=ArtifactType.FILE, storage_ref="x.txt", content_hash=None
    )
    db.add(unhashed)
    db.commit()
    version = _published_version(db, s.project, [{"key": "non_empty_output", "label": "X"}])
    svc = EvaluationExecutionService(db)

    with pytest.raises(ConflictError):
        svc.create_run(
            agent_run_id=s.agent_run.id,
            subject_artifact_id=unhashed.id,
            evaluation_definition_version_id=version.id,
        )


def test_create_run_rejects_unpublished_definition_version(db, tmp_path):
    s = _subject(db, tmp_path)
    definition = make_evaluation_definition(db, project=s.project)
    db.commit()
    draft_version = EvaluationDefinitionService(db).create_version(
        evaluation_definition_id=definition.id, description=None, criteria=[{"key": "a", "label": "A"}]
    )
    svc = EvaluationExecutionService(db)

    with pytest.raises(ConflictError):
        svc.create_run(
            agent_run_id=s.agent_run.id,
            subject_artifact_id=s.artifact.id,
            evaluation_definition_version_id=draft_version.id,
        )


def test_create_run_rejects_definition_version_from_a_different_project(db, tmp_path):
    s = _subject(db, tmp_path)
    other_project = make_project(db, name="Other")
    version = _published_version(db, other_project, [{"key": "non_empty_output", "label": "X"}])
    svc = EvaluationExecutionService(db)

    with pytest.raises(ConflictError):
        svc.create_run(
            agent_run_id=s.agent_run.id,
            subject_artifact_id=s.artifact.id,
            evaluation_definition_version_id=version.id,
        )


def test_create_run_rejects_missing_agent_run(db, tmp_path):
    s = _subject(db, tmp_path)
    version = _published_version(db, s.project, [{"key": "non_empty_output", "label": "X"}])
    svc = EvaluationExecutionService(db)

    with pytest.raises(NotFoundError):
        svc.create_run(
            agent_run_id="does-not-exist",
            subject_artifact_id=s.artifact.id,
            evaluation_definition_version_id=version.id,
        )


def test_multiple_evaluation_runs_allowed_for_same_subject(db, tmp_path):
    s = _subject(db, tmp_path)
    version = _published_version(db, s.project, [{"key": "non_empty_output", "label": "X"}])
    svc = EvaluationExecutionService(db)

    run1 = svc.create_run(
        agent_run_id=s.agent_run.id,
        subject_artifact_id=s.artifact.id,
        evaluation_definition_version_id=version.id,
    )
    run2 = svc.create_run(
        agent_run_id=s.agent_run.id,
        subject_artifact_id=s.artifact.id,
        evaluation_definition_version_id=version.id,
    )

    assert run1.id != run2.id
    runs = svc.list_runs_for_agent_run(agent_run_id=s.agent_run.id)
    assert {r.id for r in runs} == {run1.id, run2.id}


# -- execute(): deterministic checking -----------------------------------------


def test_execute_non_empty_output_criterion_met(db, tmp_path):
    s = _subject(db, tmp_path, content="real output")
    version = _published_version(db, s.project, [{"key": "non_empty_output", "label": "Non-empty output"}])
    svc = EvaluationExecutionService(db)
    run = svc.create_run(
        agent_run_id=s.agent_run.id,
        subject_artifact_id=s.artifact.id,
        evaluation_definition_version_id=version.id,
    )

    svc.execute(run.id, worker_id="test-worker")
    db.refresh(run)

    assert run.status == EvaluationRunStatus.COMPLETED
    assert run.started_at is not None and run.ended_at is not None
    results = list(run.criterion_results)
    assert len(results) == 1
    assert results[0].criterion_key == "non_empty_output"
    assert results[0].finding == EvaluationFinding.MET
    assert results[0].evidence_refs == [s.artifact.id]


def test_execute_non_empty_output_criterion_not_met_for_blank_artifact(db, tmp_path):
    s = _subject(db, tmp_path, content="   \n  ")
    version = _published_version(db, s.project, [{"key": "non_empty_output", "label": "Non-empty output"}])
    svc = EvaluationExecutionService(db)
    run = svc.create_run(
        agent_run_id=s.agent_run.id,
        subject_artifact_id=s.artifact.id,
        evaluation_definition_version_id=version.id,
    )

    svc.execute(run.id, worker_id="test-worker")
    db.refresh(run)

    assert run.status == EvaluationRunStatus.COMPLETED
    assert run.criterion_results[0].finding == EvaluationFinding.NOT_MET


def test_execute_unsupported_criterion_is_not_applicable_never_fabricated(db, tmp_path):
    s = _subject(db, tmp_path, content="real output")
    version = _published_version(
        db,
        s.project,
        [
            {"key": "non_empty_output", "label": "Non-empty output"},
            {"key": "architecture_quality", "label": "Architecture Quality"},
        ],
    )
    svc = EvaluationExecutionService(db)
    run = svc.create_run(
        agent_run_id=s.agent_run.id,
        subject_artifact_id=s.artifact.id,
        evaluation_definition_version_id=version.id,
    )

    svc.execute(run.id, worker_id="test-worker")
    db.refresh(run)

    by_key = {r.criterion_key: r for r in run.criterion_results}
    assert by_key["non_empty_output"].finding == EvaluationFinding.MET
    assert by_key["architecture_quality"].finding == EvaluationFinding.NOT_APPLICABLE
    assert "Slice 3" in by_key["architecture_quality"].rationale
    assert by_key["architecture_quality"].evidence_refs is None


def test_execute_preserves_ordering_matching_criterion_order(db, tmp_path):
    s = _subject(db, tmp_path, content="x")
    version = _published_version(
        db,
        s.project,
        [
            {"key": "architecture_quality", "label": "Z"},
            {"key": "non_empty_output", "label": "A"},
        ],
    )
    svc = EvaluationExecutionService(db)
    run = svc.create_run(
        agent_run_id=s.agent_run.id,
        subject_artifact_id=s.artifact.id,
        evaluation_definition_version_id=version.id,
    )
    svc.execute(run.id, worker_id="test-worker")
    db.refresh(run)

    assert [r.criterion_key for r in run.criterion_results] == ["architecture_quality", "non_empty_output"]
    assert [r.order_index for r in run.criterion_results] == [0, 1]


# -- execute(): exact-artifact protection ---------------------------------------


def test_execute_fails_safe_when_artifact_reassigned_to_different_agent_run(db, tmp_path):
    s = _subject(db, tmp_path)
    version = _published_version(db, s.project, [{"key": "non_empty_output", "label": "X"}])
    svc = EvaluationExecutionService(db)
    run = svc.create_run(
        agent_run_id=s.agent_run.id,
        subject_artifact_id=s.artifact.id,
        evaluation_definition_version_id=version.id,
    )

    # Simulate the artifact no longer belonging to the subject Agent Run --
    # nothing in the app currently does this, but the safeguard must hold
    # regardless (defense in depth, same principle as MA5's hash binding).
    other_agent_run = make_agent_run(db, task_run=s.task_run)
    s.artifact.agent_run_id = other_agent_run.id
    db.commit()

    svc.execute(run.id, worker_id="test-worker")
    db.refresh(run)

    assert run.status == EvaluationRunStatus.FAILED
    assert "does not belong to" in run.failure_reason or "no longer belongs" in run.failure_reason
    assert list(run.criterion_results) == []


def test_execute_fails_safe_on_stale_content_hash(db, tmp_path):
    s = _subject(db, tmp_path, content="original")
    version = _published_version(db, s.project, [{"key": "non_empty_output", "label": "X"}])
    svc = EvaluationExecutionService(db)
    run = svc.create_run(
        agent_run_id=s.agent_run.id,
        subject_artifact_id=s.artifact.id,
        evaluation_definition_version_id=version.id,
    )

    # Simulate the artifact's content changing underneath the bound hash --
    # Artifact rows are write-once in this codebase today, but the runtime
    # re-check must still refuse to evaluate silently if it ever happened.
    s.artifact.content_hash = "0" * 64
    db.commit()

    svc.execute(run.id, worker_id="test-worker")
    db.refresh(run)

    assert run.status == EvaluationRunStatus.FAILED
    assert "changed" in run.failure_reason
    assert list(run.criterion_results) == []


def test_execute_is_a_noop_when_run_is_not_pending(db, tmp_path):
    s = _subject(db, tmp_path)
    version = _published_version(db, s.project, [{"key": "non_empty_output", "label": "X"}])
    svc = EvaluationExecutionService(db)
    run = svc.create_run(
        agent_run_id=s.agent_run.id,
        subject_artifact_id=s.artifact.id,
        evaluation_definition_version_id=version.id,
    )
    svc.execute(run.id, worker_id="test-worker")
    db.refresh(run)
    assert run.status == EvaluationRunStatus.COMPLETED
    result_count_before = len(run.criterion_results)

    svc.execute(run.id, worker_id="test-worker")  # already COMPLETED -- must not re-run
    db.refresh(run)

    assert run.status == EvaluationRunStatus.COMPLETED
    assert len(run.criterion_results) == result_count_before


def test_execute_missing_run_is_handled_gracefully(db):
    EvaluationExecutionService(db).execute("does-not-exist", worker_id="test-worker")  # must not raise


# -- immutability: historical results untouched by a later run -----------------


def test_historical_criterion_results_untouched_by_a_later_run(db, tmp_path):
    s = _subject(db, tmp_path, content="v1 content")
    version = _published_version(db, s.project, [{"key": "non_empty_output", "label": "X"}])
    svc = EvaluationExecutionService(db)

    run1 = svc.create_run(
        agent_run_id=s.agent_run.id,
        subject_artifact_id=s.artifact.id,
        evaluation_definition_version_id=version.id,
    )
    svc.execute(run1.id, worker_id="test-worker")
    db.refresh(run1)
    run1_result_id = run1.criterion_results[0].id
    run1_finding = run1.criterion_results[0].finding

    run2 = svc.create_run(
        agent_run_id=s.agent_run.id,
        subject_artifact_id=s.artifact.id,
        evaluation_definition_version_id=version.id,
    )
    svc.execute(run2.id, worker_id="test-worker")

    db.refresh(run1)
    assert run1.criterion_results[0].id == run1_result_id
    assert run1.criterion_results[0].finding == run1_finding
    assert run1.id != run2.id


# -- Flight Recorder evidence ----------------------------------------------------


def test_flight_recorder_event_lifecycle(db, tmp_path):
    s = _subject(db, tmp_path)
    version = _published_version(db, s.project, [{"key": "non_empty_output", "label": "X"}])
    svc = EvaluationExecutionService(db)

    run = svc.create_run(
        agent_run_id=s.agent_run.id,
        subject_artifact_id=s.artifact.id,
        evaluation_definition_version_id=version.id,
    )
    svc.execute(run.id, worker_id="test-worker")

    events = _events(db, s.task_run.id)
    assert events == ["evaluation_run.requested", "evaluation_run.started", "evaluation_run.completed"]


def test_flight_recorder_records_failure_event(db, tmp_path):
    s = _subject(db, tmp_path)
    version = _published_version(db, s.project, [{"key": "non_empty_output", "label": "X"}])
    svc = EvaluationExecutionService(db)
    run = svc.create_run(
        agent_run_id=s.agent_run.id,
        subject_artifact_id=s.artifact.id,
        evaluation_definition_version_id=version.id,
    )
    s.artifact.content_hash = "0" * 64
    db.commit()

    svc.execute(run.id, worker_id="test-worker")

    events = _events(db, s.task_run.id)
    assert events == ["evaluation_run.requested", "evaluation_run.started", "evaluation_run.failed"]
