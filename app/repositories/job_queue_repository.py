"""JobQueueRepository — the lease/heartbeat/fencing protocol, made real.

Section 10.5.4 requires domain services to talk to a repository interface,
never hand-written SQL scattered through business logic. This is that
repository for ``job_queue`` (Section 24.4 #12).

Every method here is one short transaction — callers (the worker loop)
never hold a Session open across the actual job processing work, per
Section 10.5.2's "never hold a transaction open across ... long-running
I/O" discipline.

The claim protocol is the two-step, compare-and-swap pattern already
proven safe under concurrency in MA0's ``tests/test_concurrency.py``:
1. SELECT a candidate row id (pending, or leased with an expired lease).
2. UPDATE ... WHERE id = :id AND (still pending / still expired) — only
   one concurrent caller's UPDATE affects a row; everyone else's rowcount
   is 0, so at most one worker ever wins a given job.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional, cast

from sqlalchemy import select, text
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.db.enums import JobQueueStatus, JobType
from app.models.execution import JobQueue


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobQueueRepository:
    def enqueue(self, db: Session, *, job_type: JobType, payload_ref: str) -> JobQueue:
        job = JobQueue(job_type=job_type, payload_ref=payload_ref, status=JobQueueStatus.PENDING)
        db.add(job)
        db.commit()
        db.refresh(job)
        return job

    def get(self, db: Session, job_id: str) -> Optional[JobQueue]:
        return db.get(JobQueue, job_id)

    def claim_one(self, db: Session, *, worker_id: str, lease_seconds: int) -> Optional[JobQueue]:
        now = utcnow()
        candidate_id = db.execute(
            select(JobQueue.id)
            .where(
                (JobQueue.status == JobQueueStatus.PENDING)
                | ((JobQueue.status == JobQueueStatus.LEASED) & (JobQueue.lease_expires_at < now))
            )
            .order_by(JobQueue.created_at)
            .limit(1)
        ).scalar_one_or_none()

        if candidate_id is None:
            return None

        update_stmt = text(
            """
            UPDATE job_queue
            SET status = :leased,
                lease_owner = :worker_id,
                lease_expires_at = :lease_expires_at,
                heartbeat_at = :now,
                fencing_token = fencing_token + 1,
                attempt_count = attempt_count + 1
            WHERE id = :id
              AND (status = :pending OR (status = :leased AND lease_expires_at < :now))
            """
        )
        result = cast(
            CursorResult,
            db.execute(
                update_stmt,
                {
                    "leased": JobQueueStatus.LEASED.value,
                    "pending": JobQueueStatus.PENDING.value,
                    "worker_id": worker_id,
                    "lease_expires_at": now + timedelta(seconds=lease_seconds),
                    "now": now,
                    "id": candidate_id,
                },
            ),
        )
        db.commit()

        if result.rowcount == 0:
            # Another worker won the race between our SELECT and UPDATE.
            return None

        # The UPDATE above was raw SQL, bypassing the ORM's identity map —
        # if this Session already holds a (now stale) JobQueue instance
        # for this id (e.g. a caller reusing one Session across several
        # repository calls, as tests do), a plain db.get() would silently
        # return the pre-claim attribute values. populate_existing forces
        # a fresh SELECT regardless of identity-map state.
        return db.get(JobQueue, candidate_id, populate_existing=True)

    def heartbeat(
        self, db: Session, *, job_id: str, worker_id: str, fencing_token: int, lease_seconds: int
    ) -> bool:
        """Returns False if this worker's lease was already reclaimed by
        someone else (stale fencing_token) — the caller must stop
        processing and treat itself as a zombie."""
        now = utcnow()
        update_stmt = text(
            """
            UPDATE job_queue
            SET heartbeat_at = :now, lease_expires_at = :lease_expires_at
            WHERE id = :id AND lease_owner = :worker_id AND fencing_token = :fencing_token
            """
        )
        result = cast(
            CursorResult,
            db.execute(
                update_stmt,
                {
                    "now": now,
                    "lease_expires_at": now + timedelta(seconds=lease_seconds),
                    "id": job_id,
                    "worker_id": worker_id,
                    "fencing_token": fencing_token,
                },
            ),
        )
        db.commit()
        return result.rowcount == 1

    def complete(
        self,
        db: Session,
        *,
        job_id: str,
        worker_id: str,
        fencing_token: int,
        status: JobQueueStatus,
    ) -> bool:
        """Returns False if the fencing token is stale — a zombie worker's
        late completion write must never be honored (Section 24.4 #12)."""
        update_stmt = text(
            "UPDATE job_queue SET status = :status "
            "WHERE id = :id AND lease_owner = :worker_id AND fencing_token = :fencing_token"
        )
        result = cast(
            CursorResult,
            db.execute(
                update_stmt,
                {
                    "status": status.value,
                    "id": job_id,
                    "worker_id": worker_id,
                    "fencing_token": fencing_token,
                },
            ),
        )
        db.commit()
        return result.rowcount == 1
