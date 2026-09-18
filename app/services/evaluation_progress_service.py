"""Evaluation Run progress notification — MA6 Slice 3B.

A second, independent consumer of the same seam
``app.services.comparison_progress_service`` established for MA5:
``notify_task_run_terminal`` is called at every place a Task Run already
reaches a terminal status (COMPLETED/FAILED/CANCELLED) --
``AgentExecutionService``'s own SINGLE_AGENT success tail and its
``_finalize_failed``/``_finalize_cancelled`` -- and is a no-op for the
overwhelming majority of Task Runs that aren't an evaluator's own
dedicated bookkeeping Task Run at all (one indexed lookup, no other side
effect).

This module never modifies ``comparison_progress_service.py`` and never
touches ``ComparisonRun``/``ComparisonCandidate`` state -- an evaluator's
dedicated Task Run is never a comparison candidate's, so the two seams
never observe the same row. It never runs a provider, never decides
anything about *how* an evaluator executes -- it only notices "this
evaluator Agent Run is now done" and hands off to
``EvaluationExecutionService.finalize_agent_evaluator_run`` to parse the
result and finalize the ``EvaluationRun``.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.enums import TaskRunStatus
from app.models.evaluation_runs import EvaluationRun
from app.models.tasks import TaskRun

_TERMINAL_TASK_RUN_STATUSES = {TaskRunStatus.COMPLETED, TaskRunStatus.FAILED, TaskRunStatus.CANCELLED}


def notify_task_run_terminal(db: Session, task_run: TaskRun) -> None:
    if task_run.status not in _TERMINAL_TASK_RUN_STATUSES:
        return  # defensive: only ever called right after a terminal write, but never assume

    run = db.execute(
        select(EvaluationRun).where(EvaluationRun.evaluator_task_run_id == task_run.id)
    ).scalar_one_or_none()
    if run is None:
        return  # the common case: not an evaluator's own bookkeeping Task Run at all

    # Imported here, not at module scope, to avoid a needless import-time
    # coupling for the overwhelming majority of Task Run completions that
    # never reach this far (same reasoning as app.worker's lazy service
    # imports).
    from app.services.evaluation_execution_service import EvaluationExecutionService

    EvaluationExecutionService(db).finalize_agent_evaluator_run(run.id, task_run_status=task_run.status)
