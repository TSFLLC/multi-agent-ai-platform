"""Flight Recorder monotonic ordering — Section 22, 24.4 #4, 25.3 resumability."""

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.observability import ExecutionEvent
from tests.conftest import make_task, make_task_run


def _event(task, task_run, seq, event_type="agent_run.started"):
    return ExecutionEvent(
        task_id=task.id,
        task_run_id=task_run.id,
        sequence_number=seq,
        event_type=event_type,
    )


def test_sequence_numbers_are_unique_per_task_run(db):
    task = make_task(db)
    run = make_task_run(db, task=task)
    db.add(_event(task, run, 1))
    db.commit()

    db.add(_event(task, run, 1, event_type="model_call.completed"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_same_sequence_number_allowed_across_different_task_runs(db):
    """The uniqueness is (task_run_id, sequence_number), not global — two
    independent runs each start their own sequence at 1."""
    task = make_task(db)
    run_a = make_task_run(db, task=task)
    run_b = make_task_run(db, task=task)
    db.add(_event(task, run_a, 1))
    db.add(_event(task, run_b, 1))
    db.commit()


def test_events_replay_in_sequence_order(db):
    task = make_task(db)
    run = make_task_run(db, task=task)
    # Insert out of order to prove ordering comes from sequence_number, not
    # insertion/created_at order.
    db.add(_event(task, run, 3, event_type="agent_run.completed"))
    db.add(_event(task, run, 1, event_type="agent_run.started"))
    db.add(_event(task, run, 2, event_type="model_call.completed"))
    db.commit()

    events = (
        db.query(ExecutionEvent).filter_by(task_run_id=run.id).order_by(ExecutionEvent.sequence_number).all()
    )
    assert [e.event_type for e in events] == [
        "agent_run.started",
        "model_call.completed",
        "agent_run.completed",
    ]


def test_last_event_id_resume_query(db):
    """SSE resume semantics (Section 25.3): a client reconnecting with
    last_event_id=2 should receive only events with a higher sequence
    number."""
    task = make_task(db)
    run = make_task_run(db, task=task)
    for i in range(1, 6):
        db.add(_event(task, run, i))
    db.commit()

    resumed = (
        db.query(ExecutionEvent)
        .filter(ExecutionEvent.task_run_id == run.id, ExecutionEvent.sequence_number > 2)
        .order_by(ExecutionEvent.sequence_number)
        .all()
    )
    assert [e.sequence_number for e in resumed] == [3, 4, 5]
