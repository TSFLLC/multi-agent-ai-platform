"""Local Python worker runtime foundation — Section F.

Runs entirely on the local machine as a separate process from the FastAPI
app, polling the SQLite ``job_queue`` table (no external broker — Section
10.5.3). Conceptually::

    FastAPI --> SQLite job_queue <-- this worker

MA1 does not dispatch real Agent Runs — the only job type this worker
knows how to process is ``JobType.INTERNAL_TEST``, a harmless, side-
effect-free job that exists solely to prove claim/heartbeat/fencing/
complete/graceful-shutdown end-to-end (Owner-sanctioned scaffolding, see
app.api.routers.internal). Real dispatch (Agent Runs, Workflow nodes) is
MA3+.

Run standalone with::

    python -m app.worker                    # one job at a time (default)
    python -m app.worker --concurrency 3    # up to 3 jobs at once (MAP_WORKER_CONCURRENCY)

MA7.4c concurrency: ``concurrency=N`` runs N *lanes* in this one process, each a
thread with its own worker id and its own short-lived DB sessions, all
claiming from the same SQLite ``job_queue`` with the same leases, heartbeats
and fencing tokens (a lane is exactly the historical single worker loop), and
all stopping on the one shared shutdown event. ``concurrency=1`` (the default)
is the unchanged single loop -- no thread is started.
"""

import argparse
import logging
import os
import platform
import signal
import socket
import threading
import time
import uuid
from types import FrameType
from typing import Optional, Sequence

from sqlalchemy.orm import Session, sessionmaker

from app.config import MAX_WORKER_CONCURRENCY, MIN_WORKER_CONCURRENCY, settings
from app.db.enums import JobQueueStatus, JobType
from app.db.session import SessionLocal as _default_session_factory
from app.models.execution import JobQueue
from app.models.tasks import AgentRun
from app.repositories.job_queue_repository import JobQueueRepository

logger = logging.getLogger("app.worker")


def new_worker_id() -> str:
    host = socket.gethostname() or platform.node() or "unknown-host"
    return f"{settings.worker_id_prefix}:{host}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class Worker:
    def __init__(
        self,
        *,
        worker_id: Optional[str] = None,
        poll_interval_seconds: Optional[float] = None,
        lease_seconds: Optional[int] = None,
        heartbeat_interval_seconds: Optional[float] = None,
        internal_test_work_seconds: float = 0.3,
        internal_test_iterations: int = 3,
        session_factory: Optional[sessionmaker] = None,
        reconcile_interval_seconds: Optional[float] = None,
        concurrency: Optional[int] = None,
    ):
        self.worker_id = worker_id or new_worker_id()
        # Explicit None check: it is the only "use the setting" signal.
        self.concurrency = settings.worker_concurrency if concurrency is None else concurrency
        if not MIN_WORKER_CONCURRENCY <= self.concurrency <= MAX_WORKER_CONCURRENCY:
            raise ValueError(
                f"concurrency must be between {MIN_WORKER_CONCURRENCY} and {MAX_WORKER_CONCURRENCY}, "
                f"got {self.concurrency}"
            )
        self._session_factory = session_factory or _default_session_factory
        self.poll_interval_seconds = poll_interval_seconds or settings.worker_poll_interval_seconds
        self.lease_seconds = lease_seconds or settings.worker_lease_seconds
        self.heartbeat_interval_seconds = (
            heartbeat_interval_seconds or settings.worker_heartbeat_interval_seconds
        )
        self.internal_test_work_seconds = internal_test_work_seconds
        self.internal_test_iterations = internal_test_iterations
        # MA7.4a: how often an IDLE worker re-runs the workflow reconciliation
        # sweep (0 = never; the startup sweep is unconditional). Explicit None
        # check: 0 is a meaningful value here.
        self.reconcile_interval_seconds = (
            settings.worker_reconcile_interval_seconds
            if reconcile_interval_seconds is None
            else reconcile_interval_seconds
        )
        self._last_reconcile = time.monotonic()

        self._repo = JobQueueRepository()
        self._shutdown = threading.Event()

    # -- lifecycle -----------------------------------------------------

    def request_shutdown(self) -> None:
        self._shutdown.set()

    def _install_signal_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            # signal.signal() only works from the main thread (CPython
            # restriction). The real CLI entrypoint always runs in the
            # main thread; a worker driven from a background thread (e.g.
            # embedded in a test, or a future in-process supervisor) must
            # rely on request_shutdown() instead of OS signals.
            logger.info(
                "signal_handlers_skipped_not_main_thread worker_id=%s -- use request_shutdown() instead",
                self.worker_id,
            )
            return

        def _handler(signum: int, frame: Optional[FrameType]) -> None:
            logger.info("worker_shutdown_requested worker_id=%s signal=%s", self.worker_id, signum)
            self.request_shutdown()

        signal.signal(signal.SIGINT, _handler)
        try:
            signal.signal(signal.SIGTERM, _handler)
        except (ValueError, AttributeError):
            # SIGTERM isn't meaningfully overridable on some platforms/threads.
            pass

    def reconcile_workflows(self, *, periodic: bool = False) -> int:
        """MA7.3: idempotent recovery sweep over every non-terminal
        WorkflowRun -- heals a run a previous process left mid-transition
        (e.g. an approved gate whose next node was never dispatched) and
        confirms a durably-waiting run is still correctly waiting. Safe
        alongside another process doing the same (FastAPI runs it at its own
        startup): every change it makes is a compare-and-swap or
        unique-constraint-guarded insert. Never raises -- a failing sweep
        must not stop the worker from claiming jobs. Returns the number of
        runs reconciled (0 on failure). ``periodic`` sweeps (MA7.4a) log at
        DEBUG so an idle worker stays quiet."""
        from app.services.workflow_execution_service import WorkflowExecutionService

        db = self._session_factory()
        try:
            reconciled = WorkflowExecutionService(db).reconcile_active_runs()
            logger.log(
                logging.DEBUG if periodic else logging.INFO,
                "worker_workflow_reconciliation worker_id=%s runs=%s periodic=%s",
                self.worker_id,
                reconciled,
                periodic,
            )
            return reconciled
        except Exception:
            logger.exception("worker_workflow_reconciliation_failed worker_id=%s", self.worker_id)
            return 0
        finally:
            db.close()

    def run_forever(self) -> None:
        self._install_signal_handlers()
        logger.info("worker_started worker_id=%s concurrency=%s", self.worker_id, self.concurrency)
        self.reconcile_workflows()
        self._last_reconcile = time.monotonic()
        try:
            if self.concurrency == 1:
                self._lane_loop()
            else:
                self._run_lanes()
        finally:
            logger.info("worker_stopped worker_id=%s", self.worker_id)

    def _lane_loop(self) -> None:
        """One claim/process loop -- the whole of the historical worker."""
        while not self._shutdown.is_set():
            processed = self.run_once()
            if not processed and not self._shutdown.is_set():
                # Idle: the one place a periodic recovery sweep may run.
                # It is throttled to reconcile_interval_seconds, so the
                # loop never spins on it -- the wait below still paces it.
                self._reconcile_if_due()
                self._shutdown.wait(self.poll_interval_seconds)

    def _run_lanes(self) -> None:
        """MA7.4c: ``concurrency`` lanes, each an independent ``Worker`` (own
        worker id => own lease owner / attempt records, own per-call DB
        sessions) sharing this worker's shutdown event. Only lane 0 runs the
        idle reconciliation sweep, so it is not repeated N times an interval.
        If a lane dies of an unexpected error the whole worker stops -- loudly
        and re-raised here, like the single loop would -- rather than silently
        continuing with fewer lanes."""
        failures = []

        def run_lane(lane: "Worker") -> None:
            try:
                lane._lane_loop()
            except BaseException as exc:  # re-raised below, after every lane has stopped
                logger.exception("worker_lane_failed worker_id=%s", lane.worker_id)
                failures.append(exc)
                self.request_shutdown()

        lanes = []
        for index in range(self.concurrency):
            lane = Worker(
                worker_id=f"{self.worker_id}:lane{index}",
                poll_interval_seconds=self.poll_interval_seconds,
                lease_seconds=self.lease_seconds,
                heartbeat_interval_seconds=self.heartbeat_interval_seconds,
                internal_test_work_seconds=self.internal_test_work_seconds,
                internal_test_iterations=self.internal_test_iterations,
                session_factory=self._session_factory,
                reconcile_interval_seconds=self.reconcile_interval_seconds if index == 0 else 0,
                concurrency=1,
            )
            lane._shutdown = self._shutdown
            lanes.append(lane)
        threads = [
            threading.Thread(target=run_lane, args=(lane,), name=f"worker-lane-{i}", daemon=True)
            for i, lane in enumerate(lanes)
        ]
        for thread in threads:
            thread.start()
        try:
            # Short joins keep the main thread responsive to SIGINT/SIGTERM.
            while any(thread.is_alive() for thread in threads):
                for thread in threads:
                    thread.join(0.2)
        finally:
            self.request_shutdown()
            for thread in threads:
                thread.join()
        if failures:
            raise failures[0]

    def _reconcile_if_due(self) -> Optional[int]:
        """Runs the workflow reconciliation sweep if this worker is idle and
        at least ``reconcile_interval_seconds`` have passed since the last
        one (startup included). Returns the sweep result, or ``None`` when
        disabled or not yet due. The clock is advanced *before* the sweep, so
        a failing sweep is retried on the next interval, never in a tight
        loop. Uses the existing sweep -- no second scheduler."""
        if self.reconcile_interval_seconds <= 0:
            return None
        now = time.monotonic()
        if now - self._last_reconcile < self.reconcile_interval_seconds:
            return None
        self._last_reconcile = now
        return self.reconcile_workflows(periodic=True)

    # -- one claim/process cycle ----------------------------------------

    def run_once(self) -> bool:
        """Claims and processes at most one job. Returns True if a job was
        claimed (whether or not it completed successfully) — callers/tests
        use this for deterministic single-step execution."""
        db = self._session_factory()
        try:
            job = self._repo.claim_one(db, worker_id=self.worker_id, lease_seconds=self.lease_seconds)
        finally:
            db.close()

        if job is None:
            return False

        logger.info(
            "job_claimed worker_id=%s job_id=%s job_type=%s fencing_token=%s",
            self.worker_id,
            job.id,
            job.job_type.value,
            job.fencing_token,
        )
        self._process(job)
        return True

    def _process(self, job: JobQueue) -> None:
        if job.job_type == JobType.INTERNAL_TEST:
            self._run_internal_test_job(job)
            return

        if job.job_type == JobType.AGENT_RUN:
            self._run_agent_run_job(job)
            return

        if job.job_type == JobType.EVALUATION:
            self._run_evaluation_job(job)
            return

        # Real dispatch of remaining job types (Workflow nodes) is post-MA3.
        # A job of a type this worker doesn't know how to run yet is
        # released back to pending rather than silently dropped or marked
        # done.
        logger.warning(
            "job_type_not_yet_supported worker_id=%s job_id=%s job_type=%s",
            self.worker_id,
            job.id,
            job.job_type.value,
        )
        self._release(job)

    def _run_agent_run_job(self, job: JobQueue) -> None:
        """MA3: real Agent execution, dispatched to AgentExecutionService.

        ``job.payload_ref`` is the Agent Run id (Section 24.4 #12 — one
        durable job maps to one Agent Run). Before doing any real work, the
        lease is extended to cover the Agent Run's own timeout — a single
        blocking provider call cannot be safely interrupted mid-flight to
        heartbeat, so instead of a background heartbeat thread (unnecessary
        complexity for MA3), the lease is sized up front to outlive the
        call it's about to make.
        """
        # Imported here, not at module scope, so a worker that never
        # touches an AGENT_RUN job never needs the execution service (and
        # its provider/budget dependencies) importable.
        from app.services.execution_service import AgentExecutionService

        agent_run_id = job.payload_ref
        fencing_token = job.fencing_token

        db = self._session_factory()
        try:
            agent_run = db.get(AgentRun, agent_run_id)
            if agent_run is None:
                logger.error(
                    "agent_run_job_missing_agent_run worker_id=%s job_id=%s agent_run_id=%s",
                    self.worker_id,
                    job.id,
                    agent_run_id,
                )
                self._release(job, fencing_token=fencing_token)
                return
            required_lease_seconds = agent_run.timeout_seconds + 60
        finally:
            db.close()

        db = self._session_factory()
        try:
            still_ours = self._repo.heartbeat(
                db,
                job_id=job.id,
                worker_id=self.worker_id,
                fencing_token=fencing_token,
                lease_seconds=required_lease_seconds,
            )
        finally:
            db.close()

        if not still_ours:
            logger.warning(
                "worker_lost_lease_before_execution worker_id=%s job_id=%s agent_run_id=%s",
                self.worker_id,
                job.id,
                agent_run_id,
            )
            return

        db = self._session_factory()
        try:
            service = AgentExecutionService(db)
            try:
                service.execute(agent_run_id, worker_id=self.worker_id)
                job_status = JobQueueStatus.DONE
            except Exception:
                # AgentExecutionService already finalizes the Agent
                # Run / Task Run to a terminal FAILED state on any error it
                # can categorize (and even on ones it can't, via its own
                # last-resort handler) — this except is only a safety net
                # so a truly catastrophic failure (e.g. this session
                # itself is broken) still frees the job row instead of
                # leaving it LEASED until the lease expires.
                logger.exception(
                    "agent_run_job_execution_error worker_id=%s job_id=%s agent_run_id=%s",
                    self.worker_id,
                    job.id,
                    agent_run_id,
                )
                job_status = JobQueueStatus.FAILED

            try:
                self._reconcile_workflow_node(db, agent_run_id)
            except Exception:
                # Reconciliation is a downstream concern of this Agent
                # Run's own completion, already recorded above -- a bug
                # here must not leave the AGENT_RUN job LEASED, nor affect
                # the (already-decided) job_status for non-workflow runs.
                logger.exception(
                    "workflow_reconciliation_error worker_id=%s job_id=%s agent_run_id=%s",
                    self.worker_id,
                    job.id,
                    agent_run_id,
                )
        finally:
            db.close()

        db = self._session_factory()
        try:
            completed = self._repo.complete(
                db,
                job_id=job.id,
                worker_id=self.worker_id,
                fencing_token=fencing_token,
                status=job_status,
            )
        finally:
            db.close()

        if completed:
            logger.info(
                "agent_run_job_finished worker_id=%s job_id=%s agent_run_id=%s status=%s",
                self.worker_id,
                job.id,
                agent_run_id,
                job_status.value,
            )
        else:
            logger.warning(
                "agent_run_job_complete_rejected_stale_fencing worker_id=%s job_id=%s agent_run_id=%s",
                self.worker_id,
                job.id,
                agent_run_id,
            )

    def _reconcile_workflow_node(self, db: Session, agent_run_id: str) -> None:
        """MA7.2: if ``agent_run_id`` was dispatched by the Workflow Engine
        (i.e. some ``WorkflowNodeRun.agent_run_id`` durably references it —
        set at dispatch time in
        ``WorkflowExecutionService._dispatch_node_for_execution``), advance
        the owning WorkflowRun now that the Agent Run has reached a
        terminal status: mark the WorkflowNodeRun terminal, freeze its
        output artifact reference, and schedule the next ready node (or
        finalize the WorkflowRun if none remain).

        A no-op for every non-workflow Agent Run (MA3 single-agent, MA4
        review, MA5 comparison, MA6 evaluator) --
        ``WorkflowExecutionService.on_agent_run_complete`` itself resolves
        ownership via that same ``agent_run_id`` lookup and returns
        immediately when no WorkflowNodeRun references it, so this needs no
        workflow-specific branching here. Also idempotent against
        worker retry/replay -- a WorkflowNodeRun already in a terminal
        status short-circuits the same way.
        """
        from app.services.workflow_execution_service import WorkflowExecutionService

        WorkflowExecutionService(db).on_agent_run_complete(agent_run_id)

    def _run_evaluation_job(self, job: JobQueue) -> None:
        """MA6 Slice 2: deterministic Evaluation Run execution, dispatched to
        EvaluationExecutionService. ``job.payload_ref`` is the Evaluation
        Run id.

        Unlike ``_run_agent_run_job``, the lease is never extended up
        front: a deterministic check reads one artifact and returns almost
        immediately -- no provider call, no sandbox, nothing that could
        plausibly run anywhere near the default lease window -- so there is
        no long blocking operation to size the lease around.
        """
        # Imported here, not at module scope, so a worker that never
        # touches an EVALUATION job never needs the evaluation execution
        # service importable (same reasoning as _run_agent_run_job's
        # AgentExecutionService import).
        from app.services.evaluation_execution_service import EvaluationExecutionService

        evaluation_run_id = job.payload_ref
        fencing_token = job.fencing_token

        db = self._session_factory()
        try:
            service = EvaluationExecutionService(db)
            try:
                service.execute(evaluation_run_id, worker_id=self.worker_id)
                job_status = JobQueueStatus.DONE
            except Exception:
                # EvaluationExecutionService already finalizes the
                # Evaluation Run to a terminal FAILED state on any error it
                # can categorize -- this except is only a last-resort
                # safety net so a truly catastrophic failure still frees
                # the job row instead of leaving it LEASED until the lease
                # expires.
                logger.exception(
                    "evaluation_run_job_execution_error worker_id=%s job_id=%s evaluation_run_id=%s",
                    self.worker_id,
                    job.id,
                    evaluation_run_id,
                )
                job_status = JobQueueStatus.FAILED
        finally:
            db.close()

        db = self._session_factory()
        try:
            completed = self._repo.complete(
                db,
                job_id=job.id,
                worker_id=self.worker_id,
                fencing_token=fencing_token,
                status=job_status,
            )
        finally:
            db.close()

        if completed:
            logger.info(
                "evaluation_run_job_finished worker_id=%s job_id=%s evaluation_run_id=%s status=%s",
                self.worker_id,
                job.id,
                evaluation_run_id,
                job_status.value,
            )
        else:
            logger.warning(
                "evaluation_run_job_complete_rejected_stale_fencing worker_id=%s job_id=%s evaluation_run_id=%s",
                self.worker_id,
                job.id,
                evaluation_run_id,
            )

    def _run_internal_test_job(self, job: JobQueue) -> None:
        fencing_token = job.fencing_token
        for i in range(self.internal_test_iterations):
            if self._shutdown.is_set():
                logger.info(
                    "job_cancelled_on_shutdown worker_id=%s job_id=%s at_iteration=%s",
                    self.worker_id,
                    job.id,
                    i,
                )
                self._release(job, fencing_token=fencing_token)
                return

            time.sleep(self.internal_test_work_seconds)

            db = self._session_factory()
            try:
                still_ours = self._repo.heartbeat(
                    db,
                    job_id=job.id,
                    worker_id=self.worker_id,
                    fencing_token=fencing_token,
                    lease_seconds=self.lease_seconds,
                )
            finally:
                db.close()

            if not still_ours:
                # Our lease expired and another worker reclaimed this job
                # with a fresh fencing_token — we are now a zombie and
                # must stop; any further write we attempted would be
                # rejected anyway (Section 24.4 #12).
                logger.warning(
                    "worker_lost_lease worker_id=%s job_id=%s fencing_token=%s",
                    self.worker_id,
                    job.id,
                    fencing_token,
                )
                return

        db = self._session_factory()
        try:
            completed = self._repo.complete(
                db,
                job_id=job.id,
                worker_id=self.worker_id,
                fencing_token=fencing_token,
                status=JobQueueStatus.DONE,
            )
        finally:
            db.close()

        if completed:
            logger.info("job_completed worker_id=%s job_id=%s", self.worker_id, job.id)
        else:
            logger.warning(
                "job_complete_rejected_stale_fencing worker_id=%s job_id=%s", self.worker_id, job.id
            )

    def _release(self, job: JobQueue, *, fencing_token: Optional[int] = None) -> None:
        """Best-effort: mark a job we can no longer/won't process back as
        pending so another worker (or a future run) can retry it, rather
        than leaving it stuck LEASED under a lease that will simply expire
        anyway. Failing to release is not fatal — the lease's own
        expiration is the real safety net."""
        db = self._session_factory()
        try:
            self._repo.complete(
                db,
                job_id=job.id,
                worker_id=self.worker_id,
                fencing_token=fencing_token if fencing_token is not None else job.fencing_token,
                status=JobQueueStatus.PENDING,
            )
        finally:
            db.close()


def _concurrency_arg(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not an integer") from None
    if not MIN_WORKER_CONCURRENCY <= parsed <= MAX_WORKER_CONCURRENCY:
        raise argparse.ArgumentTypeError(
            f"must be between {MIN_WORKER_CONCURRENCY} and {MAX_WORKER_CONCURRENCY}, got {parsed}"
        )
    return parsed


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m app.worker", description="Local job-queue worker.")
    parser.add_argument(
        "--concurrency",
        type=_concurrency_arg,
        default=None,
        metavar="N",
        help=(
            f"jobs to run at once, {MIN_WORKER_CONCURRENCY}-{MAX_WORKER_CONCURRENCY} "
            "(default: MAP_WORKER_CONCURRENCY, else 1)"
        ),
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> None:
    from app.logging_config import configure_logging

    args = parse_args(argv)
    configure_logging()
    Worker(concurrency=args.concurrency).run_forever()


if __name__ == "__main__":
    main()
