"""Task vs. Task Run lifecycle separation — Section 26.1/26.7 #20.

Task.status is template/lifecycle-only; all execution status lives on
Task Run. A Task may have multiple, including concurrently "running",
Task Runs.
"""

from app.db.enums import TaskRunStatus, TaskStatus
from app.models.tasks import TaskRun
from tests.conftest import make_task, make_task_run


def test_task_status_values_are_lifecycle_only(db):
    task = make_task(db, status=TaskStatus.DRAFT)
    assert task.status == TaskStatus.DRAFT
    task.status = TaskStatus.READY
    db.commit()
    db.refresh(task)
    assert task.status == TaskStatus.READY


def test_a_task_can_have_multiple_task_runs(db):
    task = make_task(db)
    run1 = make_task_run(db, task=task, status=TaskRunStatus.COMPLETED)
    run2 = make_task_run(db, task=task, status=TaskRunStatus.RUNNING)

    runs = db.query(TaskRun).filter_by(task_id=task.id).all()
    assert {r.id for r in runs} == {run1.id, run2.id}


def test_task_runs_can_be_concurrently_running(db):
    """Two Task Runs for the same Task both in RUNNING simultaneously is a
    valid state — Task.status never duplicates/blocks this (26.1 fix)."""
    task = make_task(db)
    run1 = make_task_run(db, task=task, status=TaskRunStatus.RUNNING)
    run2 = make_task_run(db, task=task, status=TaskRunStatus.RUNNING)
    assert run1.status == TaskRunStatus.RUNNING
    assert run2.status == TaskRunStatus.RUNNING
    assert run1.id != run2.id
