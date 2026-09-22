"""Hosted deployment entrypoint — MA7.7B.

Railway's single-service start command for the approved MA7.7A/B
topology: one service, one persistent volume, the FastAPI Web process and
the ``app.worker`` Worker process running as sibling subprocesses of this
one entrypoint (Option C from the MA7.7A investigation — no separate Web
and Worker services, since Railway volumes cannot be shared between
services).

Local development never runs this file. ``uvicorn app.main:app --reload``
and ``python -m app.worker`` continue to be started separately, by hand,
on the local machine, exactly as documented today — this module exists
only for the hosted container's ``CMD``/start command
(``python -m scripts.hosted_entrypoint``).

Startup order (MA7.7A REQUIRED item: "migration-before-worker ordering"):

1. Back up the existing database file, if one exists (``app.backup``,
   ``VACUUM INTO`` — the same mechanism ``app.main``'s local startup
   already uses), so a bad migration is always recoverable. Unlike that
   local startup backup (non-fatal by design, since it runs against a
   schema already confirmed current), a backup failure *here* is fatal:
   we are about to mutate schema, so proceeding without a safety net is
   never acceptable.
2. Run ``alembic upgrade head`` and re-confirm the schema actually landed
   at head (``app.db.lifecycle.upgrade_to_head``) — fatal, non-zero exit,
   on any failure. This must complete before step 3: the Worker has no
   migration-awareness of its own and assumes an already-current schema.
3. Start the Worker and the Web process as sibling subprocesses.
4. Supervise both, fail-together: this process's own SIGTERM/SIGINT
   (Railway's deployment-teardown signal) is forwarded to both children;
   if either child exits for *any* reason before that, the other is torn
   down too and this process exits non-zero. A Web process silently
   serving traffic with no Worker draining the queue behind it (or vice
   versa) is exactly the "Web and Worker see different state" risk
   MA7.7A flagged — this entrypoint never leaves the deployment "half
   up."
"""

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from typing import List, Sequence

from app.config import settings

logger = logging.getLogger("scripts.hosted_entrypoint")

# How long a child is given to exit on its own after being asked to
# (SIGTERM) before this entrypoint gives up and force-kills it. Bounded
# well under Railway's own deployment-teardown grace period so this
# entrypoint is never the reason a shutdown overruns that window.
DEFAULT_GRACE_SECONDS = 20.0


def _resolve_port() -> int:
    """Railway injects ``PORT`` dynamically; it takes priority over the
    static ``settings.port`` default when present, since the platform — not
    this application's own configuration — owns which port traffic
    actually arrives on."""
    raw = os.environ.get("PORT")
    if not raw:
        return settings.port
    try:
        return int(raw)
    except ValueError:
        logger.warning("invalid_port_env_value value=%r -- falling back to settings.port", raw)
        return settings.port


def build_worker_command() -> List[str]:
    return [sys.executable, "-m", "app.worker", "--concurrency", str(settings.worker_concurrency)]


def build_web_command() -> List[str]:
    return [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        settings.host,
        "--port",
        str(_resolve_port()),
    ]


def migrate_and_check(engine=None) -> str:
    """Backs up the existing database (if any), then migrates to head and
    confirms it landed there. Returns the resulting current revision.

    Takes an explicit ``engine`` (defaulting to the real
    ``app.db.session.engine`` singleton) for the same reason
    ``app.db.lifecycle.upgrade_to_head`` does: a caller that already
    resolved which database it means (a test pointed at a temp file, most
    concretely) must not have that silently swapped out for the real
    process-wide engine underneath it."""
    from app.backup import create_backup
    from app.db.lifecycle import upgrade_to_head

    if engine is None:
        from app.db.session import engine as default_engine

        engine = default_engine

    if settings.database_path.exists():
        try:
            backup_path = create_backup(engine)
        except Exception:
            logger.exception("hosted_pre_migration_backup_failed -- refusing to migrate without a safety net")
            raise
        logger.info("hosted_pre_migration_backup_created path=%s", backup_path)
    else:
        logger.info("hosted_pre_migration_backup_skipped reason=no_existing_database_file")

    revision = upgrade_to_head(engine)
    logger.info("hosted_migration_complete revision=%s", revision)
    return revision


def _install_forwarding_signal_handlers(stop: threading.Event) -> None:
    def _handler(signum: int, frame) -> None:
        logger.info("hosted_entrypoint_signal_received signal=%s", signum)
        stop.set()

    signal.signal(signal.SIGINT, _handler)
    try:
        signal.signal(signal.SIGTERM, _handler)
    except (ValueError, AttributeError):
        # Same defensive pattern as app.worker._install_signal_handlers:
        # not every platform/thread context supports overriding SIGTERM
        # (notably relevant only for local/Windows testing of this file —
        # the real hosted container is always Linux).
        pass


def _terminate_all(procs: Sequence[subprocess.Popen], *, grace_seconds: float) -> None:
    for proc in procs:
        if proc.poll() is None:
            proc.terminate()
    deadline = time.monotonic() + grace_seconds
    for proc in procs:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            logger.warning("hosted_process_force_killed pid=%s -- did not exit within grace period", proc.pid)
            proc.kill()
            proc.wait()


def supervise(commands: Sequence[Sequence[str]], *, grace_seconds: float = DEFAULT_GRACE_SECONDS) -> int:
    """Runs every command in ``commands`` as a sibling subprocess and
    waits, fail-together: the moment any one exits — for any reason,
    including a clean 0 — every other still-running process is sent
    SIGTERM, given ``grace_seconds`` to exit on its own, then killed if it
    hasn't. A sibling exiting early (even with code 0) is itself treated
    as a failure: the Worker and the Web process are both meant to run
    forever, so either one stopping while the other keeps running is an
    unhealthy deployment state, not a normal shutdown.

    A clean shutdown (this process's own SIGTERM/SIGINT, forwarded to
    every child the same way) returns 0.

    Returns the first-observed non-zero exit code, 0 on a clean signal-
    initiated shutdown, or 1 if a sibling exited with code 0 on its own.
    """
    if not commands:
        return 0

    procs = [subprocess.Popen(list(cmd)) for cmd in commands]
    for proc, cmd in zip(procs, commands):
        logger.info("hosted_process_started pid=%s command=%s", proc.pid, cmd)

    stop = threading.Event()
    _install_forwarding_signal_handlers(stop)

    exit_code = 0
    try:
        while not stop.is_set():
            exited = next((p for p in procs if p.poll() is not None), None)
            if exited is None:
                stop.wait(0.5)
                continue
            if exited.returncode != 0:
                logger.error(
                    "hosted_process_exited_nonzero pid=%s returncode=%s -- stopping the rest (fail-together)",
                    exited.pid,
                    exited.returncode,
                )
                exit_code = exited.returncode
            else:
                logger.warning(
                    "hosted_process_exited_early pid=%s -- stopping the rest (fail-together); "
                    "this process is meant to run forever, so this counts as a failure",
                    exited.pid,
                )
                exit_code = 1
            break
    finally:
        _terminate_all(procs, grace_seconds=grace_seconds)

    return exit_code


def main() -> int:
    from app.logging_config import configure_logging

    configure_logging()
    logger.info("hosted_entrypoint_starting hosted_mode=%s", settings.hosted_mode)
    if not settings.hosted_mode:
        logger.warning(
            "hosted_entrypoint_run_without_hosted_mode -- MAP_HOSTED_MODE is not true; "
            "this is almost certainly a misconfiguration for a Railway deployment."
        )

    try:
        migrate_and_check()
    except Exception:
        logger.exception("hosted_entrypoint_migration_failed -- not starting Worker/Web")
        return 1

    commands = [build_worker_command(), build_web_command()]
    logger.info("hosted_entrypoint_starting_processes commands=%s", commands)
    exit_code = supervise(commands)
    logger.info("hosted_entrypoint_exiting exit_code=%s", exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
