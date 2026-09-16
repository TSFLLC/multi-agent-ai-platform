"""FastAPI app factory — MA0 contract-stub API.

Binds to ``127.0.0.1`` by default (app.config.settings.host); nothing in
this module or ``uvicorn`` invocation exposes the app beyond the local
machine unless someone explicitly overrides ``MAP_HOST``. No cloud
infrastructure, no remote database, no external queue — the entire
platform runs as a single local process against the local SQLite file
(Section 10.5, Owner's local-first instruction).
"""

from fastapi import FastAPI

from app.api.routers import (
    agents,
    approvals,
    comparisons,
    evaluations,
    events,
    models,
    projects,
    tasks,
    usage,
    workflows,
)

app = FastAPI(
    title="Multi-Agent AI Platform / Agent Control Plane",
    version="0.1.0-ma0",
    description=(
        "Local-first Agent Control Plane. MA0: architecture & contracts only — "
        "no agent execution, no live model calls, no deployment."
    ),
)

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
):
    app.include_router(router)


@app.get("/health", tags=["health"])
def health() -> dict:
    return {"status": "ok", "phase": "MA0"}
