"""Internal/ops scaffolding — proves the job_queue worker foundation (F)
and the idempotency foundation (G) end-to-end without real Agent
execution (MA3). Enqueues a ``JobType.INTERNAL_TEST`` job that the local
worker (``app/worker.py``) claims, heartbeats, and completes doing
nothing but a short, deterministic sleep.

Not part of the frozen MA0 product API contract (Section 25.2/25.5) —
this router exists only for MA1's own proof-of-life and may be removed
once real Task/Agent Run endpoints (MA3) make it unnecessary.
"""

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Header
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.db.enums import IdempotencyScope, JobType
from app.errors import NotFoundError
from app.repositories.job_queue_repository import JobQueueRepository
from app.schemas.internal import InternalTestJobRead
from app.services.idempotency_service import BeginOutcome, IdempotencyService

router = APIRouter(prefix="/internal", tags=["internal"])
_job_repo = JobQueueRepository()


@router.post("/test-jobs", response_model=InternalTestJobRead, status_code=201)
def create_test_job(
    db: Session = Depends(get_db),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    key = idempotency_key or f"auto:{uuid.uuid4()}"
    idempotency = IdempotencyService(db)
    begin = idempotency.begin(key, scope=IdempotencyScope.API_REQUEST, resource_type="internal_test_job")

    if begin.outcome == BeginOutcome.ALREADY_COMPLETED:
        result_ref = begin.key_row.result_ref or {}
        job_id = result_ref.get("job_id")
        if job_id is None:
            raise NotFoundError(f"Idempotency key {key!r} completed with no recorded job_id.")
        job = _job_repo.get(db, job_id)
        if job is None:
            raise NotFoundError(f"Job {job_id} referenced by idempotency key no longer exists.")
        return job

    try:
        payload_ref = f"internal-test:{uuid.uuid4()}"
        job = _job_repo.enqueue(db, job_type=JobType.INTERNAL_TEST, payload_ref=payload_ref)
        idempotency.complete(key, result_ref={"job_id": job.id})
        return job
    except Exception:
        idempotency.fail(key)
        raise


@router.get("/test-jobs/{job_id}", response_model=InternalTestJobRead)
def get_test_job(job_id: str, db: Session = Depends(get_db)):
    job = _job_repo.get(db, job_id)
    if job is None:
        raise NotFoundError(f"Job {job_id} not found.")
    return job
