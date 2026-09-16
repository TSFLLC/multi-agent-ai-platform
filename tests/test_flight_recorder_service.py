"""Flight Recorder foundation, made real — Section H.

Builds on MA0's schema-level ordering tests (test_flight_recorder_ordering.py)
by exercising the real FlightRecorderService/ExecutionEventRepository
atomic sequence-number assignment, including under concurrent writers.
"""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from sqlalchemy.orm import sessionmaker

from app.services.flight_recorder import FlightRecorderService
from tests.conftest import make_task, make_task_run


def test_record_assigns_sequential_numbers(db):
    task = make_task(db)
    run = make_task_run(db, task=task)
    recorder = FlightRecorderService(db)

    e1 = recorder.record(task_id=task.id, task_run_id=run.id, event_type="agent_run.started")
    e2 = recorder.record(task_id=task.id, task_run_id=run.id, event_type="model_call.completed")
    e3 = recorder.record(task_id=task.id, task_run_id=run.id, event_type="agent_run.completed")

    assert [e1.sequence_number, e2.sequence_number, e3.sequence_number] == [1, 2, 3]


def test_list_for_task_run_returns_in_order(db):
    task = make_task(db)
    run = make_task_run(db, task=task)
    recorder = FlightRecorderService(db)
    for event_type in ("a", "b", "c"):
        recorder.record(task_id=task.id, task_run_id=run.id, event_type=event_type)

    events = recorder.list_for_task_run(task_run_id=run.id)
    assert [e.event_type for e in events] == ["a", "b", "c"]


def test_resume_after_sequence_returns_only_newer_events(db):
    task = make_task(db)
    run = make_task_run(db, task=task)
    recorder = FlightRecorderService(db)
    for event_type in ("a", "b", "c", "d"):
        recorder.record(task_id=task.id, task_run_id=run.id, event_type=event_type)

    resumed = recorder.list_for_task_run(task_run_id=run.id, after_sequence=2)
    assert [e.event_type for e in resumed] == ["c", "d"]


def test_latest_sequence_reflects_current_state(db):
    task = make_task(db)
    run = make_task_run(db, task=task)
    recorder = FlightRecorderService(db)
    assert recorder.latest_sequence(task_run_id=run.id) == 0
    recorder.record(task_id=task.id, task_run_id=run.id, event_type="a")
    assert recorder.latest_sequence(task_run_id=run.id) == 1


def test_decision_summary_never_required_to_carry_raw_reasoning(db):
    """Section 22.5: only structured fields/decision_summary text — this
    is a contract discipline check, not something the schema can enforce
    mechanically, so it's exercised as a usage example."""
    task = make_task(db)
    run = make_task_run(db, task=task)
    recorder = FlightRecorderService(db)
    event = recorder.record(
        task_id=task.id,
        task_run_id=run.id,
        event_type="agent_run.completed",
        decision_summary="Selected DeepSeek over Claude: 40% cheaper, met min_context requirement.",
    )
    assert "40%" in event.decision_summary


def test_concurrent_record_calls_never_collide_on_sequence_number(engine, db):
    task = make_task(db)
    run = make_task_run(db, task=task)
    db.commit()
    task_id, run_id = task.id, run.id

    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    barrier = Barrier(8)

    def record_one(i: int):
        session = Session()
        barrier.wait()
        try:
            recorder = FlightRecorderService(session)
            event = recorder.record(task_id=task_id, task_run_id=run_id, event_type=f"evt-{i}")
            return event.sequence_number
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        sequence_numbers = list(pool.map(record_one, range(8)))

    assert sorted(sequence_numbers) == list(range(1, 9))
