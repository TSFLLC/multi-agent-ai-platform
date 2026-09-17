"""SSE/event-stream foundation — Section J.

Tests the async generator directly (app.sse.stream_task_run_events)
rather than through a live HTTP connection, since a real SSE stream is
open-ended by design — ``max_empty_polls`` makes it terminate
deterministically for tests. Resume semantics mirror Section 25.3's
``last_event_id`` contract against execution_events.sequence_number.
"""

import asyncio

import app.sse as sse_module
from app.services.flight_recorder import FlightRecorderService
from tests.conftest import make_task, make_task_run


def _run(coro_factory):
    return asyncio.new_event_loop().run_until_complete(coro_factory())


def test_stream_yields_existing_events_in_order(db, session_factory):
    task = make_task(db)
    run = make_task_run(db, task=task)
    recorder = FlightRecorderService(db)
    for event_type in ("a", "b", "c"):
        recorder.record(task_id=task.id, task_run_id=run.id, event_type=event_type)

    async def collect():
        chunks = []
        async for chunk in sse_module.stream_task_run_events(
            run.id, poll_interval_seconds=0.01, max_empty_polls=1, session_factory=session_factory
        ):
            chunks.append(chunk)
        return chunks

    chunks = _run(collect)
    assert len(chunks) == 3
    assert "event: a" in chunks[0]
    assert "id: 1" in chunks[0]
    assert "event: c" in chunks[2]


def test_stream_resumes_after_last_event_id(db, session_factory):
    task = make_task(db)
    run = make_task_run(db, task=task)
    recorder = FlightRecorderService(db)
    for event_type in ("a", "b", "c", "d"):
        recorder.record(task_id=task.id, task_run_id=run.id, event_type=event_type)

    async def collect():
        chunks = []
        async for chunk in sse_module.stream_task_run_events(
            run.id,
            last_event_id=2,
            poll_interval_seconds=0.01,
            max_empty_polls=1,
            session_factory=session_factory,
        ):
            chunks.append(chunk)
        return chunks

    chunks = _run(collect)
    assert len(chunks) == 2
    assert "event: c" in chunks[0]
    assert "event: d" in chunks[1]


def test_stream_terminates_when_no_events_and_max_empty_polls_set(db, session_factory):
    task = make_task(db)
    run = make_task_run(db, task=task)

    async def collect():
        chunks = []
        async for chunk in sse_module.stream_task_run_events(
            run.id, poll_interval_seconds=0.01, max_empty_polls=2, session_factory=session_factory
        ):
            chunks.append(chunk)
        return chunks

    chunks = _run(collect)
    assert chunks == []


def test_stream_resumes_mid_review_cycle(db, session_factory):
    """SSE resume needs no MA4-specific change: a review cycle's events
    (review.queued, review.decision_*, repair.queued, ...) are ordinary
    execution_events rows like any MA3 event, ordered the same way."""
    task = make_task(db)
    run = make_task_run(db, task=task)
    recorder = FlightRecorderService(db)
    review_cycle_events = [
        "agent_run.completed",
        "review.queued",
        "agent_run.completed",
        "review.decision_repair_required",
        "repair.queued",
    ]
    for event_type in review_cycle_events:
        recorder.record(task_id=task.id, task_run_id=run.id, event_type=event_type)

    async def collect():
        chunks = []
        async for chunk in sse_module.stream_task_run_events(
            run.id,
            last_event_id=2,
            poll_interval_seconds=0.01,
            max_empty_polls=1,
            session_factory=session_factory,
        ):
            chunks.append(chunk)
        return chunks

    chunks = _run(collect)
    assert len(chunks) == 3  # everything strictly after sequence_number=2
    assert "event: review.decision_repair_required" in chunks[1]
    assert "event: repair.queued" in chunks[2]


def test_stream_endpoint_is_wired_and_returns_event_stream_media_type(db, session_factory, bootstrap):
    """Calls the route function directly rather than over a live HTTP
    connection: the real endpoint's generator polls forever by design
    (``max_empty_polls=None``), and driving a genuinely open-ended SSE
    stream through TestClient risks the test hanging on teardown. This
    checks the response is correctly shaped without consuming the body."""
    from app.api.routers.tasks import stream_task_run_events

    task = make_task(db, project=bootstrap.project)
    run = make_task_run(db, task=task)
    db.commit()

    response = stream_task_run_events(
        task.id, run.id, db=db, user=bootstrap.user, session_factory=session_factory
    )
    assert response.media_type == "text/event-stream"
    assert response.body_iterator is not None
