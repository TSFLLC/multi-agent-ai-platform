"""SQLite concurrency behavior — Section 10.5.2/33.1.

WAL mode + short transactions + busy_timeout should give "one writer at a
time, many concurrent readers, no reader blocked by a writer," with
transient writer/writer collisions retried by SQLite rather than
surfaced as errors — and where two writers genuinely conflict (same
idempotency key, same job claim), exactly one must win, never both.

Each thread opens its own Session against the shared file-based engine
(Session objects are not thread-safe to share; Engines are).
"""

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db.enums import BudgetReservationStatus, IdempotencyScope, IdempotencyStatus, JobQueueStatus, JobType
from app.models.execution import IdempotencyKey, JobQueue
from tests.conftest import make_budget, make_task, make_task_run


def test_concurrent_writers_to_different_rows_all_succeed(engine):
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    def insert_job(i: int):
        session = Session()
        try:
            session.add(
                JobQueue(
                    job_type=JobType.AGENT_RUN, payload_ref=f"agent_run:{i}", status=JobQueueStatus.PENDING
                )
            )
            session.commit()
            return True
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(insert_job, range(20)))

    assert all(results)

    verify = Session()
    assert verify.query(JobQueue).count() == 20
    verify.close()


def test_concurrent_idempotency_key_insert_exactly_one_winner(engine):
    """Two threads racing to record the same operation (e.g. a client
    retrying a POST with the same Idempotency-Key) must never both
    succeed."""
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    barrier = threading.Barrier(5)

    def try_insert():
        session = Session()
        barrier.wait()
        try:
            session.add(
                IdempotencyKey(
                    key="race-key",
                    scope=IdempotencyScope.API_REQUEST,
                    resource_type="task_run",
                    status=IdempotencyStatus.IN_PROGRESS,
                )
            )
            session.commit()
            return "ok"
        except IntegrityError:
            session.rollback()
            return "rejected"
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=5) as pool:
        outcomes = list(pool.map(lambda _: try_insert(), range(5)))

    assert outcomes.count("ok") == 1
    assert outcomes.count("rejected") == 4


def test_concurrent_job_claim_exactly_one_winner(engine):
    """Simulates the lease-claim protocol (Section 24.4 #12): an UPDATE
    guarded by ``WHERE status = 'pending'`` is the compare-and-swap that
    makes concurrent claims safe. Only one of N racing workers may
    transition the row to leased."""
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    setup = Session()
    job = JobQueue(
        job_type=JobType.AGENT_RUN, payload_ref="agent_run:contested", status=JobQueueStatus.PENDING
    )
    setup.add(job)
    setup.commit()
    job_id = job.id
    setup.close()

    barrier = threading.Barrier(6)

    def try_claim(worker_id: int):
        session = Session()
        barrier.wait()
        try:
            result = session.execute(
                text(
                    "UPDATE job_queue SET status='leased', lease_owner=:owner, "
                    "fencing_token=fencing_token+1 WHERE id=:id AND status='pending'"
                ),
                {"owner": f"worker-{worker_id}", "id": job_id},
            )
            session.commit()
            return result.rowcount
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=6) as pool:
        rowcounts = list(pool.map(try_claim, range(6)))

    assert sum(rowcounts) == 1

    verify = Session()
    final = verify.get(JobQueue, job_id)
    assert final.status == JobQueueStatus.LEASED
    assert final.fencing_token == 1
    verify.close()


def test_concurrent_budget_reservations_all_recorded_independently(engine):
    """Reservations are independent INSERTs, not a shared mutable counter
    — so N concurrent reservations against one budget never lose an
    update the way a naive read-increment-write counter would."""
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    setup = Session()
    budget = make_budget(setup, limit_amount="100.00")
    task_run = make_task_run(setup)
    budget_id, task_run_id = budget.id, task_run.id
    setup.commit()
    setup.close()

    from app.models.governance import BudgetReservation

    def reserve(i: int):
        session = Session()
        try:
            session.add(
                BudgetReservation(
                    budget_id=budget_id,
                    task_run_id=task_run_id,
                    reserved_amount=Decimal("1.00"),
                    status=BudgetReservationStatus.ACTIVE,
                )
            )
            session.commit()
            return True
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(reserve, range(10)))

    assert all(results)

    verify = Session()
    total = verify.query(BudgetReservation).filter_by(budget_id=budget_id).count()
    assert total == 10
    verify.close()


def test_flight_recorder_concurrent_writes_across_task_runs(engine):
    """Different task_runs can be written to concurrently with no
    contention on each other's sequence_number space."""
    from app.models.observability import ExecutionEvent

    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    setup = Session()
    task = make_task(setup)
    run_ids = [make_task_run(setup, task=task).id for _ in range(5)]
    task_id = task.id
    setup.commit()
    setup.close()

    def write_event(run_id: str):
        session = Session()
        try:
            session.add(
                ExecutionEvent(
                    task_id=task_id, task_run_id=run_id, sequence_number=1, event_type="agent_run.started"
                )
            )
            session.commit()
            return True
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(write_event, rid) for rid in run_ids]
        results = [f.result() for f in as_completed(futures)]

    assert all(results)
