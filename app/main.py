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
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.routers import (
    agents,
    approvals,
    comparisons,
    evaluation_definitions,
    evaluation_runs,
    evaluations,
    events,
    internal,
    models,
    projects,
    tasks,
    usage,
    workflows,
)
from app.auth import ensure_local_auth_token
from app.backup import create_backup
from app.bootstrap import ensure_local_bootstrap
from app.config import settings
from app.db.lifecycle import get_current_revision, get_head_revision, is_schema_up_to_date
from app.db.session import SessionLocal, engine
from app.errors import register_exception_handlers
from app.logging_config import configure_logging
from app.starter_agents import ensure_starter_agents
from app.web import FRONTEND_DIR
from app.web import router as ui_router

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

        # Pre-MA3 checkpoint: a WAL-safe backup (VACUUM INTO) before this
        # startup's bootstrap/seeding writes anything new. Backup failure
        # is logged, never fatal to startup.
        if settings.backup_on_startup:
            try:
                backup_path = create_backup(engine)
                logger.info("startup_backup_created path=%s", backup_path)
            except Exception:
                logger.exception("startup_backup_failed")

        # Local bootstrap identity (MA1B) — idempotent; needs the migrated
        # schema to exist, so it only runs once the revision check above
        # passes. A stale/unmigrated DB simply skips bootstrap rather than
        # crashing startup — /ready already reports that state clearly.
        db = SessionLocal()
        try:
            identities = ensure_local_bootstrap(db)
            logger.info(
                "local_bootstrap_ready org_id=%s user_id=%s project_id=%s",
                identities.organization.id,
                identities.user.id,
                identities.project.id,
            )
            starter_agents = ensure_starter_agents(db, project_id=identities.project.id)
            logger.info("starter_agents_ready count=%s", len(starter_agents))
        finally:
            db.close()

        # MA7.3: idempotent recovery sweep over non-terminal Workflow Runs
        # (a durably waiting approval must survive a restart; a run stopped
        # mid-transition is healed). Only against an up-to-date schema, and a
        # failure here never blocks startup. The Worker runs the same sweep
        # at its own startup; both are safe together (CAS/unique guarded).
        db = SessionLocal()
        try:
            from app.services.workflow_execution_service import WorkflowExecutionService

            reconciled = WorkflowExecutionService(db).reconcile_active_runs()
            logger.info("workflow_reconciliation_at_startup runs=%s", reconciled)
        except Exception:
            logger.exception("workflow_reconciliation_failed_at_startup")
        finally:
            db.close()

    ensure_local_auth_token()

    yield

    logger.info("app_stopping")
    engine.dispose()
    logger.info("app_stopped")


def hosted_docs_urls(hosted_mode: bool):
    """MA7.7B: FastAPI's built-in /docs, /redoc and /openapi.json are
    unauthenticated by construction (there is no Depends() to attach a
    doc-serving route to) — fine on a loopback-only local install, but in
    hosted mode they would hand the platform's whole API surface
    (including request/response shapes for provider-secret and approval
    endpoints) to anyone with the URL. A plain function (not inlined into
    the FastAPI(...) call below) so this decision is unit-testable on its
    own, independent of the app singleton's own one-time construction."""
    if hosted_mode:
        return None, None, None
    return "/docs", "/redoc", "/openapi.json"


_docs_url, _redoc_url, _openapi_url = hosted_docs_urls(settings.hosted_mode)

app = FastAPI(
    title="Multi-Agent AI Platform / Agent Control Plane",
    version="0.1.0-ma5",
    docs_url=_docs_url,
    redoc_url=_redoc_url,
    openapi_url=_openapi_url,
    description=(
        "Local-first Agent Control Plane. MA5: Parallel Comparison — one "
        "Task executed independently by 2+ Agent/model candidates (same "
        "Agent/different models, different Agents/same or different "
        "models), each its own isolated Task Run + Agent Run on MA3's "
        "execution engine, with every result and its execution evidence "
        "preserved and NO automatic winner selection — a human always "
        "selects the exact, hash-bound canonical artifact. Built on MA4's "
        "Agent-to-Agent Review (optional per candidate), MA3's execution "
        "engine (queue/worker, provider invocation, budget governor, "
        "Flight Recorder, cancellation/recovery/fencing), and MA2's "
        "Agent + Model Registry — still no evaluation/judge engine, "
        "workflow/DAG engine, or tool execution."
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
    evaluation_definitions.router,
    evaluation_runs.router,
    approvals.router,
    usage.router,
    events.router,
    internal.router,
):
    app.include_router(router)

# Local Operator Console (MA5-UI) — a frontend/operator-console slice on
# top of the API above, never a second backend. Mounted after every real
# API router so "/assets/*" and "/" can never shadow a resource route.
app.mount("/assets", StaticFiles(directory=FRONTEND_DIR / "assets"), name="ui-assets")
app.include_router(ui_router)


@app.get("/health", tags=["health"])
def health() -> dict:
    """Liveness only — does not touch the database. See /ready for that."""
    return {"status": "ok", "phase": "MA4"}


@app.get("/ready", tags=["health"])
def ready() -> JSONResponse:
    """Readiness: is the local SQLite database reachable and migrated to
    head? A local operator (or the worker) can poll this before assuming
    the platform is usable.

    MA7.7B: returns HTTP 503 (not 200) when not ready — a hosted
    deployment's own healthcheck/orchestration reads the status code, not
    just the JSON body, to decide whether to route traffic or restart a
    still-migrating instance. The JSON body's shape is unchanged either
    way, so an existing caller reading the ``ready`` field keeps working."""
    try:
        with engine.connect():
            db_reachable = True
    except Exception:
        logger.warning("ready_check_db_unreachable", exc_info=True)
        db_reachable = False

    schema_ready = db_reachable and is_schema_up_to_date(engine)
    is_ready = db_reachable and schema_ready
    payload = {
        "ready": is_ready,
        "database_reachable": db_reachable,
        "schema_up_to_date": schema_ready,
        "current_revision": get_current_revision(engine) if db_reachable else None,
        "head_revision": get_head_revision(),
    }
    return JSONResponse(content=payload, status_code=200 if is_ready else 503)
