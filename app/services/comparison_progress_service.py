"""Comparison progress notification — MA5.

The one, tiny seam between MA3/MA4's execution engine and MA5 comparisons:
``notify_task_run_terminal`` is called at every place a Task Run already
reaches a terminal status (COMPLETED/FAILED/CANCELLED) —
AgentExecutionService's own SINGLE_AGENT success tail, its
``_finalize_failed``/``_finalize_cancelled``, and
ReviewOrchestrationService's three equivalents — and is a no-op for the
overwhelming majority of Task Runs that aren't a comparison candidate at
all (one indexed lookup, no other side effect). It never runs a provider,
never decides anything about *how* a candidate executes — it only notices
"this candidate is now done" and updates comparison-level bookkeeping:
Flight Recorder narration + (once every candidate is terminal) the
comparison's own READY_FOR_SELECTION-or-FAILED transition.

The platform never picks a winner here — an all-candidates-succeeded
comparison simply becomes ready for the human (Section 10: "no automatic
winner"); only an all-candidates-failed comparison gets a stored terminal
status (FAILED) of its own, because there is nothing left to select.
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.enums import ComparisonRunStatus, TaskRunStatus
from app.models.artifacts_eval import ComparisonCandidate, ComparisonRun
from app.models.tasks import TaskRun
from app.services.flight_recorder import FlightRecorderService

_TERMINAL_TASK_RUN_STATUSES = {TaskRunStatus.COMPLETED, TaskRunStatus.FAILED, TaskRunStatus.CANCELLED}

_CANDIDATE_EVENT_BY_STATUS = {
    TaskRunStatus.COMPLETED: "candidate.completed",
    TaskRunStatus.FAILED: "candidate.failed",
    TaskRunStatus.CANCELLED: "candidate.cancelled",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def notify_task_run_terminal(db: Session, task_run: TaskRun) -> None:
    if task_run.status not in _TERMINAL_TASK_RUN_STATUSES:
        return  # defensive: only ever called right after a terminal write, but never assume

    candidate = db.execute(
        select(ComparisonCandidate).where(ComparisonCandidate.task_run_id == task_run.id)
    ).scalar_one_or_none()
    if candidate is None:
        return  # the common case: not a comparison candidate at all

    comparison = db.get(ComparisonRun, candidate.comparison_run_id)
    if comparison is None:
        return

    recorder = FlightRecorderService(db)
    anchor_task_run = db.get(TaskRun, comparison.task_run_id)
    if anchor_task_run is None:
        return

    recorder.record(
        task_id=anchor_task_run.task_id,
        task_run_id=comparison.task_run_id,
        event_type=_CANDIDATE_EVENT_BY_STATUS[task_run.status],
        decision_summary=(
            f"candidate {candidate.label!r} ({candidate.id}) -> {task_run.status.value} "
            f"(task_run {task_run.id})"
        ),
    )

    _maybe_transition_comparison(db, comparison, anchor_task_run, recorder)


def _maybe_transition_comparison(
    db: Session, comparison: ComparisonRun, anchor_task_run: TaskRun, recorder: FlightRecorderService
) -> None:
    candidates = list(
        db.execute(select(ComparisonCandidate).where(ComparisonCandidate.comparison_run_id == comparison.id))
        .scalars()
        .all()
    )
    if not candidates:
        return

    candidate_task_runs = []
    for c in candidates:
        if c.task_run_id is None:
            return  # a candidate hasn't even been launched yet -- not all-terminal
        tr = db.get(TaskRun, c.task_run_id)
        if tr is None or tr.status not in _TERMINAL_TASK_RUN_STATUSES:
            return  # at least one candidate still has work outstanding
        candidate_task_runs.append(tr)

    # Every candidate's own Task Run is now terminal.
    recorder.record(
        task_id=anchor_task_run.task_id,
        task_run_id=comparison.task_run_id,
        event_type="comparison.candidates_terminal",
        decision_summary=f"all {len(candidates)} candidates reached a terminal state",
    )

    if comparison.cancellation_requested_at is not None:
        # An explicit cancel was requested (Section 16) -- reach an honest
        # terminal state rather than quietly offering survivors for
        # selection as if nothing happened. Already-completed candidates'
        # artifacts are untouched/still inspectable; the comparison itself
        # is done.
        comparison.status = ComparisonRunStatus.CANCELLED
        comparison.completed_at = _utcnow()
        db.commit()
        recorder.record(
            task_id=anchor_task_run.task_id,
            task_run_id=comparison.task_run_id,
            event_type="comparison.cancelled",
            decision_summary="comparison cancelled; all candidates reached a terminal state",
        )
        return

    any_succeeded = any(tr.status == TaskRunStatus.COMPLETED for tr in candidate_task_runs)
    if any_succeeded:
        # The platform never auto-selects a winner (Section 10) -- the
        # comparison stays RUNNING (there is no ComparisonRunStatus value
        # for "awaiting the human," and none is needed: this is a
        # derived/computed fact, see
        # app.services.comparison_service.compute_comparison_phase), only
        # narrated here so an operator/UI watching the event stream knows
        # it's time to look.
        recorder.record(
            task_id=anchor_task_run.task_id,
            task_run_id=comparison.task_run_id,
            event_type="comparison.ready_for_selection",
            decision_summary="all candidates terminal; at least one succeeded; awaiting human selection",
        )
        return

    # All candidates failed/cancelled -- nothing left to select from.
    comparison.status = ComparisonRunStatus.FAILED
    comparison.completed_at = _utcnow()
    db.commit()
    recorder.record(
        task_id=anchor_task_run.task_id,
        task_run_id=comparison.task_run_id,
        event_type="comparison.failed",
        decision_summary="all candidates failed or were cancelled; nothing to select",
    )


def find_candidate_for_task_run(db: Session, task_run_id: str) -> Optional[ComparisonCandidate]:
    return db.execute(
        select(ComparisonCandidate).where(ComparisonCandidate.task_run_id == task_run_id)
    ).scalar_one_or_none()
