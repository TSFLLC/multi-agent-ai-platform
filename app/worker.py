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

    python -m app.worker
"""

import logging
import os
import platform
import signal
import socket
import threading
import time
import uuid
from types import FrameType
from typing import Optional

from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.db.enums import JobQueueStatus, JobType
from app.db.session import SessionLocal as _default_session_factory
from app.models.execution import JobQueue
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
    ):
        self.worker_id = worker_id or new_worker_id()
        self._session_factory = session_factory or _default_session_factory
        self.poll_interval_seconds = poll_interval_seconds or settings.worker_poll_interval_seconds
        self.lease_seconds = lease_seconds or settings.worker_lease_seconds
        self.heartbeat_interval_seconds = (
            heartbeat_interval_seconds or settings.worker_heartbeat_interval_seconds
        )
        self.internal_test_work_seconds = internal_test_work_seconds
        self.internal_test_iterations = internal_test_iterations

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

    def run_forever(self) -> None:
        self._install_signal_handlers()
        logger.info("worker_started worker_id=%s", self.worker_id)
        try:
            while not self._shutdown.is_set():
                processed = self.run_once()
                if not processed and not self._shutdown.is_set():
                    self._shutdown.wait(self.poll_interval_seconds)
        finally:
            logger.info("worker_stopped worker_id=%s", self.worker_id)

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

        # Real dispatch (Agent Runs, Workflow nodes) is MA3+. A job of a
        # type this worker doesn't know how to run yet is released back
        # to pending rather than silently dropped or marked done.
        logger.warning(
            "job_type_not_yet_supported worker_id=%s job_id=%s job_type=%s",
            self.worker_id,
            job.id,
            job.job_type.value,
        )
        self._release(job)

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


def main() -> None:
    from app.logging_config import configure_logging

    configure_logging()
    Worker().run_forever()


if __name__ == "__main__":
    main()
