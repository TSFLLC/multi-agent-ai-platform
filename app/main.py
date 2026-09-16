"""FastAPI app factory — MA0 contract stubs + MA1 platform foundation.

Binds to ``127.0.0.1`` by default (app.config.settings.host); nothing in
this module or a ``uvicorn`` invocation exposes the app beyond the local
machine unless someone explicitly sets ``MAP_ALLOW_REMOTE_BIND=true`` (see
app.config.Settings, which fails configuration validation otherwise). No
cloud infrastructure, no remote database, no external queue — the entire
platform runs as a single local process against the local SQLite file
(Section 10.5, Owner's local-first instruction).
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routers import (
    agents,
    approvals,
    comparisons,
    evaluations,
    events,
    internal,
    models,
    projects,
    tasks,
    usage,
    workflows,
)
from app.config import settings
from app.db.lifecycle import get_current_revision, get_head_revision, is_schema_up_to_date
from app.db.session import engine
from app.errors import register_exception_handlers
from app.logging_config import configure_logging

logger = logging.getLogger("app.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    logger.info(
        "app_starting environment=%s host=%s port=%s", settings.environment, settings.host, settings.port
    )

    try:
        with engine.connect():
            pass
    except Exception:
        logger.exception("database_unreachable_at_startup database_path=%s", settings.database_path)
        raise

    if not is_schema_up_to_date(engine):
        current = get_current_revision(engine)
        head = get_head_revision()
        logger.warning(
            "schema_not_up_to_date current_revision=%s head_revision=%s "
            "-- run `alembic upgrade head` before relying on this instance",
            current,
            head,
        )
    else:
        logger.info("schema_up_to_date revision=%s", get_head_revision())

    yield

    logger.info("app_stopping")
    engine.dispose()
    logger.info("app_stopped")


app = FastAPI(
    title="Multi-Agent AI Platform / Agent Control Plane",
    version="0.1.0-ma1",
    description=(
        "Local-first Agent Control Plane. MA1: platform foundation (bootstrap, "
        "worker, Flight Recorder, idempotency, audit, SSE, error/logging "
        "contracts) — still no agent execution, no live model calls, no "
        "deployment."
    ),
    lifespan=lifespan,
)

register_exception_handlers(app)

for router in (
    projects.router,
    agents.router,
    models.router,
    tasks.router,
    workflows.router,
    comparisons.router,
    evaluations.router,
    approvals.router,
    usage.router,
    events.router,
    internal.router,
):
    app.include_router(router)


@app.get("/health", tags=["health"])
def health() -> dict:
    """Liveness only — does not touch the database. See /ready for that."""
    return {"status": "ok", "phase": "MA1"}


@app.get("/ready", tags=["health"])
def ready() -> dict:
    """Readiness: is the local SQLite database reachable and migrated to
    head? A local operator (or the worker) can poll this before assuming
    the platform is usable."""
    try:
        with engine.connect():
            db_reachable = True
    except Exception:
        logger.warning("ready_check_db_unreachable", exc_info=True)
        db_reachable = False

    schema_ready = db_reachable and is_schema_up_to_date(engine)
    return {
        "ready": db_reachable and schema_ready,
        "database_reachable": db_reachable,
        "schema_up_to_date": schema_ready,
        "current_revision": get_current_revision(engine) if db_reachable else None,
        "head_revision": get_head_revision(),
    }
