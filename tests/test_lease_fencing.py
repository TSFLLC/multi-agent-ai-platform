"""Lease / heartbeat / fencing-token contract — Section 24.4 #12, 26.6.

The actual claim/renew/complete repository logic is a later-phase (MA3)
concern; MA0 only guarantees the schema those operations depend on:
lease_owner/lease_expires_at/heartbeat_at/fencing_token columns exist on
job_queue, agent_runs, and workflow_node_runs, fencing_token is a plain
comparable integer, and (job_type, payload_ref) is unique so the same
logical unit of work is never enqueued twice.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from app.db.enums import JobQueueStatus, JobType
from app.models.execution import JobQueue


def test_job_queue_payload_uniqueness(db):
    db.add(JobQueue(job_type=JobType.AGENT_RUN, payload_ref="agent_run:1", status=JobQueueStatus.PENDING))
    db.commit()

    db.add(JobQueue(job_type=JobType.AGENT_RUN, payload_ref="agent_run:1", status=JobQueueStatus.PENDING))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_claim_renew_reclaim_fencing_sequence(db):
    now = datetime.now(timezone.utc)
    job = JobQueue(job_type=JobType.AGENT_RUN, payload_ref="agent_run:2", status=JobQueueStatus.PENDING)
    db.add(job)
    db.commit()

    # Worker A claims: short transaction, increments fencing_token.
    job.status = JobQueueStatus.LEASED
    job.lease_owner = "worker-a"
    job.lease_expires_at = now + timedelta(seconds=30)
    job.fencing_token = 1
    db.commit()
    worker_a_token = job.fencing_token

    # Simulate the lease expiring (worker A crashed/hung) without a
    # completion write.
    job.lease_expires_at = now - timedelta(seconds=1)
    db.commit()

    # Worker B re-claims with a fresh, higher fencing_token.
    job.lease_owner = "worker-b"
    job.lease_expires_at = now + timedelta(seconds=30)
    job.fencing_token = worker_a_token + 1
    db.commit()

    # A late write from the zombie worker A, using its stale token, is
    # detectably invalid — the repository layer (MA3) rejects any write
    # where the caller's token != the row's current fencing_token. MA0
    # guarantees the comparison is possible; it does not yet perform it.
    db.refresh(job)
    assert job.fencing_token != worker_a_token
    assert job.lease_owner == "worker-b"


def test_agent_runs_and_workflow_node_runs_carry_the_same_lease_columns(engine):
    from sqlalchemy import inspect

    inspector = inspect(engine)
    for table in ("job_queue", "agent_runs", "workflow_node_runs"):
        cols = {c["name"] for c in inspector.get_columns(table)}
        assert {"lease_owner", "lease_expires_at", "heartbeat_at", "fencing_token"} <= cols
