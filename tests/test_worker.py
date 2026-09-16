"""Local worker foundation — Section F.

Startup/shutdown, claiming, lease acquisition, heartbeat, fencing,
lease expiration/recovery, graceful shutdown, cancellation observation,
concurrent worker claim behavior — against the real job_queue lease
protocol (Section 24.4 #12), using the harmless INTERNAL_TEST job type.
"""

import threading
import time
from datetime import datetime, timedelta, timezone

from app.db.enums import JobQueueStatus, JobType
from app.models.execution import JobQueue
from app.repositories.job_queue_repository import JobQueueRepository
from app.worker import Worker, new_worker_id

_repo = JobQueueRepository()


def test_worker_id_is_stable_and_identifying():
    w = Worker()
    assert w.worker_id
    assert w.worker_id == w.worker_id  # stable across access
    assert new_worker_id() != new_worker_id()  # unique per call


def test_run_once_returns_false_when_queue_empty(fast_worker):
    assert fast_worker.run_once() is False


def test_run_once_claims_and_completes_internal_test_job(db, fast_worker):
    job = _repo.enqueue(db, job_type=JobType.INTERNAL_TEST, payload_ref="t1")

    processed = fast_worker.run_once()

    assert processed is True
    db.expire_all()
    completed = db.get(JobQueue, job.id)
    assert completed.status == JobQueueStatus.DONE
    assert completed.lease_owner == fast_worker.worker_id
    assert completed.fencing_token == 1


def test_run_once_leaves_pending_job_untouched_if_no_job_exists(db, fast_worker):
    assert fast_worker.run_once() is False
    assert db.query(JobQueue).count() == 0


def test_worker_releases_unsupported_job_type_back_to_pending(db, fast_worker):
    job = _repo.enqueue(db, job_type=JobType.AGENT_RUN, payload_ref="unsupported-1")

    fast_worker.run_once()

    db.expire_all()
    released = db.get(JobQueue, job.id)
    # Real Agent Run dispatch is MA3+; the worker claims it (proving the
    # generic claim path works for any job_type) then releases it back to
    # pending rather than silently dropping or falsely completing it.
    assert released.status == JobQueueStatus.PENDING


def test_heartbeat_extends_lease(db, session_factory, fast_worker):
    _repo.enqueue(db, job_type=JobType.INTERNAL_TEST, payload_ref="hb-1")
    claimed = _repo.claim_one(db, worker_id=fast_worker.worker_id, lease_seconds=fast_worker.lease_seconds)
    first_expiry = claimed.lease_expires_at

    time.sleep(0.05)
    hb_db = session_factory()
    try:
        ok = _repo.heartbeat(
            hb_db,
            job_id=claimed.id,
            worker_id=fast_worker.worker_id,
            fencing_token=claimed.fencing_token,
            lease_seconds=fast_worker.lease_seconds,
        )
    finally:
        hb_db.close()

    assert ok is True
    db.expire_all()
    refreshed = db.get(JobQueue, claimed.id)
    assert refreshed.lease_expires_at > first_expiry


def test_heartbeat_fails_with_stale_fencing_token(db, fast_worker):
    _repo.enqueue(db, job_type=JobType.INTERNAL_TEST, payload_ref="hb-stale-1")
    claimed = _repo.claim_one(db, worker_id=fast_worker.worker_id, lease_seconds=fast_worker.lease_seconds)
    stale_token = claimed.fencing_token

    ok = _repo.heartbeat(
        db,
        job_id=claimed.id,
        worker_id=fast_worker.worker_id,
        fencing_token=stale_token + 999,  # never actually issued
        lease_seconds=fast_worker.lease_seconds,
    )
    assert ok is False


def test_expired_lease_is_reclaimed_with_higher_fencing_token(db, session_factory):
    job = _repo.enqueue(db, job_type=JobType.INTERNAL_TEST, payload_ref="reclaim-1")

    claim_db = session_factory()
    try:
        first = _repo.claim_one(claim_db, worker_id="worker-a", lease_seconds=30)
        assert first.fencing_token == 1
        # Simulate worker-a crashing: force its lease into the past.
        first.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        claim_db.commit()
    finally:
        claim_db.close()

    reclaim_db = session_factory()
    try:
        second = _repo.claim_one(reclaim_db, worker_id="worker-b", lease_seconds=30)
    finally:
        reclaim_db.close()

    assert second is not None
    assert second.id == job.id
    assert second.lease_owner == "worker-b"
    assert second.fencing_token == 2  # strictly higher than worker-a's token


def test_completion_with_stale_fencing_token_is_rejected(db):
    job = _repo.enqueue(db, job_type=JobType.INTERNAL_TEST, payload_ref="complete-stale-1")
    claimed = _repo.claim_one(db, worker_id="zombie", lease_seconds=30)
    zombie_token = claimed.fencing_token  # capture before it can be refreshed away

    # Simulate a second worker reclaiming after zombie's lease expired.
    claimed.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    reclaimed = _repo.claim_one(db, worker_id="worker-b", lease_seconds=30)
    assert reclaimed.fencing_token == zombie_token + 1

    # The original zombie worker's late completion write, using its stale
    # token, must be rejected — Section 24.4 #12's core guarantee.
    zombie_completed = _repo.complete(
        db,
        job_id=job.id,
        worker_id="zombie",
        fencing_token=zombie_token,
        status=JobQueueStatus.DONE,
    )
    assert zombie_completed is False

    db.expire_all()
    current = db.get(JobQueue, job.id)
    assert current.status == JobQueueStatus.LEASED
    assert current.lease_owner == "worker-b"


def test_concurrent_workers_claim_exactly_one_winner(engine, db):
    from sqlalchemy.orm import sessionmaker

    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    _repo.enqueue(db, job_type=JobType.INTERNAL_TEST, payload_ref="race-1")
    barrier = threading.Barrier(6)

    def try_claim(worker_id: int):
        session = Session()
        barrier.wait()
        try:
            claimed = _repo.claim_one(session, worker_id=f"worker-{worker_id}", lease_seconds=30)
            return claimed is not None
        finally:
            session.close()

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(try_claim, range(6)))

    assert results.count(True) == 1


def test_graceful_shutdown_stops_the_run_loop(session_factory):
    # Bound to the real test DB (not the default app engine) so the
    # background loop's run_once() polls successfully instead of raising
    # on a missing table — this test is about shutdown behavior, not
    # error handling.
    w = Worker(session_factory=session_factory, poll_interval_seconds=0.01)
    thread = threading.Thread(target=w.run_forever, daemon=True)
    thread.start()
    time.sleep(0.05)
    w.request_shutdown()
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_cancellation_observed_between_heartbeat_checkpoints(db, session_factory):
    """A worker mid-job that receives a shutdown request must stop at the
    next checkpoint (Section 26.7's cooperative-cancellation pattern) and
    release the job rather than finish it or crash."""
    worker = Worker(
        session_factory=session_factory,
        lease_seconds=30,
        internal_test_work_seconds=0.02,
        internal_test_iterations=10,  # long enough to interrupt mid-way
    )
    job = _repo.enqueue(db, job_type=JobType.INTERNAL_TEST, payload_ref="cancel-1")

    def run_and_cancel():
        # Cancel shortly after the job would have been claimed.
        time.sleep(0.03)
        worker.request_shutdown()

    canceller = threading.Thread(target=run_and_cancel)
    canceller.start()
    worker.run_once()
    canceller.join()

    db.expire_all()
    final = db.get(JobQueue, job.id)
    assert final.status == JobQueueStatus.PENDING
