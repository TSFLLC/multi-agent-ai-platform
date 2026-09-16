"""Minimum local SSE infrastructure — Section J.

SQLite remains authoritative; there is no pub/sub layer (no Redis). The
stream is a simple poll loop over ``execution_events``, which is cheap and
correct at local, single-machine scale — exactly the "no sophisticated
realtime system" instruction. Resume semantics follow Section 25.3:
reconnecting with ``last_event_id`` (here, the Flight Recorder's
``sequence_number``) replays only events after that point.
"""

import asyncio
import json
from typing import AsyncIterator, Optional

from sqlalchemy.orm import sessionmaker

from app.db.session import SessionLocal as _default_session_factory
from app.services.flight_recorder import FlightRecorderService


def _format_sse(sequence_number: int, event_type: str, data: dict) -> str:
    payload = json.dumps(data, default=str)
    return f"id: {sequence_number}\nevent: {event_type}\ndata: {payload}\n\n"


async def stream_task_run_events(
    task_run_id: str,
    *,
    last_event_id: Optional[int] = None,
    poll_interval_seconds: float = 0.5,
    max_empty_polls: Optional[int] = None,
    session_factory: Optional[sessionmaker] = None,
) -> AsyncIterator[str]:
    """Yields SSE-framed Flight Recorder events for ``task_run_id``,
    starting strictly after ``last_event_id``.

    ``max_empty_polls`` is test-only: the real endpoint passes ``None``
    (stream forever, as SSE requires); tests pass a small integer so the
    generator terminates deterministically instead of running forever.

    ``session_factory`` defaults to the app's real SessionLocal; the
    endpoint passes the overridable ``get_session_factory`` dependency so
    tests can point this at a temp database the same way they do for
    ``get_db``.
    """
    factory = session_factory or _default_session_factory
    cursor = last_event_id or 0
    empty_polls = 0

    while True:
        db = factory()
        try:
            recorder = FlightRecorderService(db)
            new_events = recorder.list_for_task_run(task_run_id=task_run_id, after_sequence=cursor)
        finally:
            db.close()

        if not new_events:
            empty_polls += 1
            if max_empty_polls is not None and empty_polls >= max_empty_polls:
                return
            await asyncio.sleep(poll_interval_seconds)
            continue

        empty_polls = 0
        for event in new_events:
            cursor = event.sequence_number
            yield _format_sse(
                event.sequence_number,
                event.event_type,
                {
                    "id": event.id,
                    "occurred_at": event.occurred_at,
                    "sequence_number": event.sequence_number,
                    "event_type": event.event_type,
                    "decision_summary": event.decision_summary,
                },
            )
