"""Shared FastAPI dependencies for MA0's contract-stub API layer.

MA0 freezes the OpenAPI *contract* (request/response shapes, resource
boundaries, status codes) without implementing the business logic behind
it — per the spec, even MA1 only ships Agent/Model services as "empty CRUD
shells" (Section 31), so MA0's handlers uniformly raise 501 rather than
touching the database. This keeps "an endpoint exists" from being mistaken
for "the endpoint works."
"""

from typing import NoReturn

from fastapi import HTTPException
from sqlalchemy.orm import sessionmaker

from app.db.session import SessionLocal, get_db

__all__ = ["get_db", "get_session_factory", "not_implemented"]


def not_implemented(detail: str = "Not implemented in MA0 — contract stub only.") -> NoReturn:
    raise HTTPException(status_code=501, detail=detail)


def get_session_factory() -> sessionmaker:
    """Overridable indirection for code (e.g. the SSE generator) that
    can't use the per-request ``Depends(get_db)`` session because it
    needs to open a fresh short-lived session on every poll iteration of
    a long-lived streaming response. Tests override this the same way
    they override ``get_db``, so the SSE stream reads from the same test
    database as the rest of a request."""
    return SessionLocal
