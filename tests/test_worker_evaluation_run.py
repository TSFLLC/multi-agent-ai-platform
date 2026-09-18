"""Worker dispatch of JobType.EVALUATION — MA6 Slice 2.

Exercises the worker's own responsibilities around deterministic
Evaluation Run execution (claim -> delegate to
EvaluationExecutionService.execute -> mark the job DONE/FAILED) without
depending on EvaluationExecutionService's internals -- those are covered
exhaustively in test_evaluation_execution_service.py. Here,
EvaluationExecutionService.execute is monkeypatched at the class level for
the boundary checks, mirroring test_worker_agent_run.py's shape for
JobType.AGENT_RUN. No lease-extension test exists here on purpose (see
Worker._run_evaluation_job's own docstring: a deterministic check has no
long blocking operation to size a lease around, unlike an Agent Run).
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

import app.services.evaluation_execution_service as evaluation_execution_service_module
from app.db.enums import EvaluationFinding, EvaluationRunStatus, JobQueueStatus, VersionStatus
from app.models.evaluation_runs import EvaluationRun
from app.models.execution import JobQueue
from app.repositories.job_queue_repository import JobQueueRepository
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

_repo = JobQueueRepository()


def _pending_run(db, tmp_path, content="real output") -> EvaluationRun:
    project = make_project(db)
    task = make_task(db, project=project)
    task_run = make_task_run(db, task=task)
    agent_version = make_agent_version(db, status=VersionStatus.ACTIVE)
    agent_run = make_agent_run(db, task_run=task_run, agent_version=agent_version)
    artifact = make_artifact_with_content(db, tmp_path, agent_run=agent_run, content=content)
    definition = make_evaluation_definition(db, project=project)
    db.commit()
    version = EvaluationDefinitionService(db).create_version(
        evaluation_definition_id=definition.id,
        description=None,
        criteria=[{"key": "non_empty_output", "label": "Non-empty output"}],
    )
    version = EvaluationDefinitionService(db).publish_version(version.id)
    return EvaluationExecutionService(db).create_run(
        agent_run_id=agent_run.id,
        subject_artifact_id=artifact.id,
        evaluation_definition_version_id=version.id,
    )


def _job_for(db, run: EvaluationRun) -> JobQueue:
    return db.execute(select(JobQueue).where(JobQueue.payload_ref == run.id)).scalar_one()


def test_worker_dispatches_evaluation_job_to_execution_service(db, fast_worker, monkeypatch, tmp_path):
    run = _pending_run(db, tmp_path)
    job = _job_for(db, run)

    calls = []

    def fake_execute(self, evaluation_run_id, *, worker_id):
        calls.append((evaluation_run_id, worker_id))

    monkeypatch.setattr(
        evaluation_execution_service_module.EvaluationExecutionService, "execute", fake_execute
    )

    processed = fast_worker.run_once()

    assert processed is True
    assert calls == [(run.id, fast_worker.worker_id)]
    db.expire_all()
    assert db.get(JobQueue, job.id).status == JobQueueStatus.DONE


def test_worker_marks_job_failed_when_execution_service_raises(db, fast_worker, monkeypatch, tmp_path):
    run = _pending_run(db, tmp_path)
    job = _job_for(db, run)

    def fake_execute(self, evaluation_run_id, *, worker_id):
        raise RuntimeError("catastrophic")

    monkeypatch.setattr(
        evaluation_execution_service_module.EvaluationExecutionService, "execute", fake_execute
    )

    fast_worker.run_once()

    db.expire_all()
    assert db.get(JobQueue, job.id).status == JobQueueStatus.FAILED


def test_worker_end_to_end_completes_evaluation_run(db, fast_worker, tmp_path):
    """No monkeypatching -- the real EvaluationExecutionService.execute
    runs via the worker's dispatch, mirroring an operator's actual
    manual-trigger flow end to end."""
    run = _pending_run(db, tmp_path)

    assert fast_worker.run_once() is True

    db.refresh(run)
    assert run.status == EvaluationRunStatus.COMPLETED
    assert run.criterion_results[0].finding == EvaluationFinding.MET


def test_dead_worker_on_evaluation_job_is_reclaimed_and_stale_completion_rejected(
    db, session_factory, tmp_path
):
    """Durability under interruption (Section 20), same guarantee as
    test_worker_agent_run.py's AGENT_RUN analog: worker-a claims an
    EVALUATION job then "dies" before completing it; worker-b must be able
    to reclaim it with a fresh fencing token, and worker-a's late
    completion attempt (stale fencing token) must be rejected."""
    run = _pending_run(db, tmp_path)
    job = _job_for(db, run)

    claim_db = session_factory()
    try:
        claimed = _repo.claim_one(claim_db, worker_id="worker-a", lease_seconds=30)
        assert claimed is not None
        zombie_fencing_token = claimed.fencing_token
        claimed.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        claim_db.commit()
    finally:
        claim_db.close()

    reclaim_db = session_factory()
    try:
        reclaimed = _repo.claim_one(reclaim_db, worker_id="worker-b", lease_seconds=30)
    finally:
        reclaim_db.close()

    assert reclaimed is not None
    assert reclaimed.id == job.id
    assert reclaimed.fencing_token == zombie_fencing_token + 1

    worker_b_completed = _repo.complete(
        db,
        job_id=job.id,
        worker_id="worker-b",
        fencing_token=reclaimed.fencing_token,
        status=JobQueueStatus.DONE,
    )
    assert worker_b_completed is True

    zombie_completed = _repo.complete(
        db,
        job_id=job.id,
        worker_id="worker-a",
        fencing_token=zombie_fencing_token,
        status=JobQueueStatus.FAILED,
    )
    assert zombie_completed is False

    db.expire_all()
    final = db.get(JobQueue, job.id)
    assert final.status == JobQueueStatus.DONE  # worker-b's outcome stands
