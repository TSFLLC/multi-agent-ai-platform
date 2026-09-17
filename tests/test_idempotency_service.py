"""Idempotency foundation — Section G.

Duplicate requests must never create duplicate side effects — proven via
the real IdempotencyService + the /internal/test-jobs endpoint (which
enqueues exactly one job_queue row per distinct Idempotency-Key).
"""

import uuid

from app.db.enums import IdempotencyScope, JobType
from app.models.execution import JobQueue
from app.repositories.job_queue_repository import JobQueueRepository
from app.services.idempotency_service import BeginOutcome, IdempotencyService


def test_first_begin_starts_and_is_reusable_for_completion(db):
    svc = IdempotencyService(db)
    key = str(uuid.uuid4())

    begin = svc.begin(key, scope=IdempotencyScope.API_REQUEST, resource_type="internal_test_job")
    assert begin.outcome == BeginOutcome.STARTED

    svc.complete(key, result_ref={"job_id": "abc"})

    second = svc.begin(key, scope=IdempotencyScope.API_REQUEST, resource_type="internal_test_job")
    assert second.outcome == BeginOutcome.ALREADY_COMPLETED
    assert second.key_row.result_ref == {"job_id": "abc"}


def test_concurrent_in_progress_key_raises_conflict(db):
    import pytest

    from app.errors import IdempotencyConflictError

    svc = IdempotencyService(db)
    key = str(uuid.uuid4())
    svc.begin(key, scope=IdempotencyScope.API_REQUEST, resource_type="internal_test_job")

    with pytest.raises(IdempotencyConflictError):
        svc.begin(key, scope=IdempotencyScope.API_REQUEST, resource_type="internal_test_job")


def test_failed_key_is_safely_reclaimable(db):
    svc = IdempotencyService(db)
    key = str(uuid.uuid4())
    svc.begin(key, scope=IdempotencyScope.API_REQUEST, resource_type="internal_test_job")
    svc.fail(key)

    retried = svc.begin(key, scope=IdempotencyScope.API_REQUEST, resource_type="internal_test_job")
    assert retried.outcome == BeginOutcome.STARTED


def test_internal_test_job_endpoint_requires_auth(client):
    """MA2 pre-MA3 checkpoint: no longer an unauthenticated mutation endpoint."""
    resp = client.post("/internal/test-jobs")
    assert resp.status_code == 401


def test_internal_test_job_endpoint_is_idempotent(client, db, auth_headers, bootstrap):
    key = str(uuid.uuid4())
    headers = {**auth_headers, "Idempotency-Key": key}

    first = client.post("/internal/test-jobs", headers=headers)
    assert first.status_code == 201
    job_id_1 = first.json()["id"]

    second = client.post("/internal/test-jobs", headers=headers)
    assert second.status_code == 201
    job_id_2 = second.json()["id"]

    assert job_id_1 == job_id_2
    assert db.query(JobQueue).count() == 1


def test_internal_test_job_endpoint_without_key_creates_distinct_jobs(client, db, auth_headers, bootstrap):
    first = client.post("/internal/test-jobs", headers=auth_headers)
    second = client.post("/internal/test-jobs", headers=auth_headers)

    assert first.json()["id"] != second.json()["id"]
    assert db.query(JobQueue).count() == 2


def test_internal_test_job_get_returns_current_status(client, db, auth_headers, bootstrap):
    repo = JobQueueRepository()
    job = repo.enqueue(db, job_type=JobType.INTERNAL_TEST, payload_ref="direct-1")

    resp = client.get(f"/internal/test-jobs/{job.id}", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"


def test_internal_test_job_get_missing_returns_404(client, auth_headers, bootstrap):
    resp = client.get("/internal/test-jobs/00000000-0000-0000-0000-000000000000", headers=auth_headers)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
