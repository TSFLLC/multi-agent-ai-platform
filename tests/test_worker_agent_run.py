"""Worker dispatch of JobType.AGENT_RUN — MA3.

Exercises the worker's own responsibilities around real Agent execution
(claim -> extend lease to cover the Agent Run's timeout -> delegate to
AgentExecutionService -> mark the job DONE/FAILED) without depending on
AgentExecutionService's internals — those are covered exhaustively in
test_execution_service.py. Here, AgentExecutionService.execute is
monkeypatched at the class level so these tests stay a pure boundary
check on app.worker.
"""

from datetime import datetime, timedelta, timezone

import app.services.execution_service as execution_service_module
from app.db.enums import JobQueueStatus, JobType
from app.models.execution import JobQueue
from app.repositories.job_queue_repository import JobQueueRepository
from tests.conftest import make_agent_run

_repo = JobQueueRepository()


def test_worker_dispatches_agent_run_job_to_execution_service(db, fast_worker, monkeypatch):
    agent_run = make_agent_run(db)
    db.commit()
    job = _repo.enqueue(db, job_type=JobType.AGENT_RUN, payload_ref=agent_run.id)

    calls = []

    def fake_execute(self, agent_run_id, *, worker_id):
        calls.append((agent_run_id, worker_id))

    monkeypatch.setattr(execution_service_module.AgentExecutionService, "execute", fake_execute)

    processed = fast_worker.run_once()

    assert processed is True
    assert calls == [(agent_run.id, fast_worker.worker_id)]
    db.expire_all()
    assert db.get(JobQueue, job.id).status == JobQueueStatus.DONE


def test_worker_marks_job_failed_when_execution_service_raises(db, fast_worker, monkeypatch):
    agent_run = make_agent_run(db)
    db.commit()
    job = _repo.enqueue(db, job_type=JobType.AGENT_RUN, payload_ref=agent_run.id)

    def fake_execute(self, agent_run_id, *, worker_id):
        raise RuntimeError("catastrophic")

    monkeypatch.setattr(execution_service_module.AgentExecutionService, "execute", fake_execute)

    fast_worker.run_once()

    db.expire_all()
    assert db.get(JobQueue, job.id).status == JobQueueStatus.FAILED


def test_worker_extends_lease_to_cover_agent_run_timeout(db, fast_worker, session_factory, monkeypatch):
    """fast_worker's own lease_seconds is deliberately tiny (2s) so tests
    stay fast — but a real Agent Run's timeout is typically much longer
    (default 1800s). The worker must re-extend the lease immediately after
    claiming, before doing any real work, or a legitimately slow provider
    call would lose its lease mid-flight."""
    agent_run = make_agent_run(db)
    agent_run.timeout_seconds = 5000
    db.commit()
    job = _repo.enqueue(db, job_type=JobType.AGENT_RUN, payload_ref=agent_run.id)

    observed = {}

    def fake_execute(self, agent_run_id, *, worker_id):
        check_db = session_factory()
        try:
            observed["lease_expires_at"] = check_db.get(JobQueue, job.id).lease_expires_at
        finally:
            check_db.close()

    monkeypatch.setattr(execution_service_module.AgentExecutionService, "execute", fake_execute)

    fast_worker.run_once()

    lease_expires_at = observed["lease_expires_at"].replace(tzinfo=timezone.utc)
    assert lease_expires_at - datetime.now(timezone.utc) > timedelta(seconds=4900)


def test_dead_worker_lease_is_reclaimed_and_stale_completion_rejected(db, session_factory):
    """Durability under interruption (Section 20): worker-a claims an
    AGENT_RUN job then "dies" (its lease is forced into the past without
    it ever completing the job). worker-b must be able to reclaim it with
    a fresh fencing token and finish it; worker-a's late completion
    attempt, using its now-stale fencing token, must be rejected rather
    than corrupting the outcome worker-b already recorded."""
    agent_run = make_agent_run(db)
    db.commit()
    job = _repo.enqueue(db, job_type=JobType.AGENT_RUN, payload_ref=agent_run.id)

    claim_db = session_factory()
    try:
        claimed = _repo.claim_one(claim_db, worker_id="worker-a", lease_seconds=30)
        assert claimed is not None
        zombie_fencing_token = claimed.fencing_token
        # Simulate worker-a crashing before it ever extends the lease or
        # calls complete() — force the lease into the past so it looks
        # abandoned to any other worker's claim_one().
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

    # worker-a's late write, using the fencing token it was originally
    # issued, must not be honored now that worker-b owns the job.
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
